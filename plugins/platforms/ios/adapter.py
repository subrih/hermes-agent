"""iOS app platform adapter (Hermes plugin).

Bridges the existing Kaveri iOS app (``com.kaveri.cockpit``) to the
Hermes agent using the app's *existing* Firebase transport — so the app
needs no rewrite. It reproduces the contract the old ``sangamam-v3``
relay implemented (``firebase_watcher.py`` + ``firebase_writer.py``):

Inbound
    Firestore ``on_snapshot`` listener on
    ``users/{uid}/channels/{channel}/messages``. Docs with
    ``role == "user"`` and ``intent == "interact"`` (created after
    startup) are dispatched to the agent as fresh turns.

Outbound
    The agent's reply is written back to the same subcollection as a
    ``role == "assistant"``, ``intent == "interact"`` doc with a
    server timestamp. The app's realtime Firestore listener renders it.

Live status (optional, best-effort)
    ``typing`` is flipped on at turn start and cleared at turn end in the
    Firebase RTDB at ``channel_state/{channel}`` — the iOS "ripple" box.

Identity
    Firestore is gated behind a service account, and the app authenticates
    to write via a Firebase custom token (``x-kaveri-secret`` → backend).
    The doc's ``user`` field / the channel owner ``uid`` is the identity.
    The gateway allowlist (``IOS_ALLOWED_USERS``) is checked against the
    channel ``uid``.

This adapter ships under ``plugins/platforms/ios/`` and is discovered by
the Hermes plugin loader at startup — no edits to core files. The blocking
firebase-admin ``on_snapshot`` listener runs on a background thread and
bridges to the gateway event loop via ``run_coroutine_threadsafe``.

Configuration (env wins over config.yaml ``extra``)::

    IOS_FIREBASE_CREDENTIALS   Path to service-account JSON (project kaveri-chat)
    IOS_FIREBASE_RTDB_URL      Realtime DB URL for typing/thoughts ripple
    IOS_UID                    Firebase uid to watch (default: subri)
    IOS_CHANNELS               Comma-separated channels (default: cockpit)
    IOS_HOME_CHANNEL           Default channel for cron delivery (default: cockpit)
    IOS_ALLOWED_USERS          Allowed uids (default: IOS_UID)
    IOS_ALLOW_ALL_USERS        Allow any uid (dev only)
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

try:
    import firebase_admin
    from firebase_admin import credentials
    from firebase_admin import db as rtdb
    from firebase_admin import firestore as _fs_sync
    from google.cloud.firestore_v1.base_query import FieldFilter
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False
    firebase_admin = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

DEFAULT_UID = "subri"
DEFAULT_CHANNEL = "cockpit"
DEFAULT_CREDS = os.path.expanduser("~/.secrets/firebase-kaveri-admin.json")


def _md_hardbreaks(text: str) -> str:
    """Make single newlines render as line breaks in the iOS app's Markdown.

    The app renders message text as Markdown, where a single ``\\n`` is a soft
    break (collapses to a space) — so the gateway's multi-line tool-progress
    ('💻 …\\n💻 …') shows on one line. Upgrading every lone ``\\n`` to a
    paragraph break (``\\n\\n``) puts each line on its own row.

    Safe for the progress case (each line is a self-contained '<emoji> tool: …'
    bullet). To avoid mangling code blocks in real answers, lines inside a
    ``` fenced block are left untouched, and existing blank lines are kept as-is.
    """
    if not text or "\n" not in text:
        return text
    out = []
    in_fence = False
    lines = text.split("\n")
    for i, line in enumerate(lines):
        out.append(line)
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if i == len(lines) - 1:
            continue
        nxt = lines[i + 1]
        # Only insert a blank line when this is a single newline join (next
        # line is non-empty) and we're not inside a code fence. Existing blank
        # lines (paragraph breaks) already render correctly — don't double them.
        if not in_fence and line.strip() and nxt.strip():
            out.append("")
    return "\n".join(out)

# Shared Firebase app — initialized once per process regardless of how many
# adapters/channels exist. firebase_admin raises if initialize_app is called
# twice with the default name, so we guard on the existing app.
_FIREBASE_APP_LOCK = False


def _ensure_firebase_app(creds_path: str, rtdb_url: str) -> None:
    """Initialize the default firebase_admin app once (idempotent)."""
    global _FIREBASE_APP_LOCK
    if _FIREBASE_APP_LOCK:
        return
    try:
        firebase_admin.get_app()
        _FIREBASE_APP_LOCK = True
        return
    except ValueError:
        pass  # not initialized yet
    cred = credentials.Certificate(creds_path)
    options = {}
    if rtdb_url:
        options["databaseURL"] = rtdb_url
    firebase_admin.initialize_app(cred, options or None)
    _FIREBASE_APP_LOCK = True
    logger.info("[ios] firebase_admin initialized (creds=%s rtdb=%s)", creds_path, bool(rtdb_url))


def _bool_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")


def check_requirements() -> bool:
    """Adapter is installable iff firebase-admin is present and creds configured."""
    if not FIREBASE_AVAILABLE:
        return False
    creds = os.getenv("IOS_FIREBASE_CREDENTIALS", DEFAULT_CREDS)
    return bool(creds) and os.path.exists(os.path.expanduser(creds))


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    creds = extra.get("credentials") or os.getenv("IOS_FIREBASE_CREDENTIALS", DEFAULT_CREDS)
    return bool(creds) and os.path.exists(os.path.expanduser(creds))


def is_connected(config) -> bool:
    return check_requirements()


class IOSAdapter(BasePlatformAdapter):
    """iOS app adapter over the Firebase (Firestore + RTDB) transport."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("ios"))
        extra = config.extra or {}

        self._creds_path: str = os.path.expanduser(
            extra.get("credentials")
            or os.getenv("IOS_FIREBASE_CREDENTIALS", DEFAULT_CREDS)
        )
        self._rtdb_url: str = (
            extra.get("rtdb_url") or os.getenv("IOS_FIREBASE_RTDB_URL", "")
        ).strip()
        self._uid: str = (
            extra.get("uid") or os.getenv("IOS_UID", DEFAULT_UID)
        ).strip() or DEFAULT_UID

        channels_raw = (
            extra.get("channels") or os.getenv("IOS_CHANNELS", DEFAULT_CHANNEL)
        )
        self._channels: List[str] = [
            c.strip() for c in str(channels_raw).split(",") if c.strip()
        ] or [DEFAULT_CHANNEL]

        # Firestore client + per-channel watch handles (background-thread Watches).
        self._db = None
        self._watches: Dict[str, Any] = {}
        # Our own writes — skip echoing them back as inbound turns.
        self._our_msg_ids: set[str] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- Connection lifecycle ----------------------------------------------

    async def connect(self) -> bool:
        if not FIREBASE_AVAILABLE:
            logger.warning("[ios] firebase-admin not installed. Run: uv pip install firebase-admin")
            return False
        if not os.path.exists(self._creds_path):
            logger.warning("[ios] Firebase credentials not found at %s", self._creds_path)
            return False

        try:
            _ensure_firebase_app(self._creds_path, self._rtdb_url)
            self._db = _fs_sync.client()
            self._loop = asyncio.get_running_loop()
            for channel in self._channels:
                self._start_channel_watch(channel)
            self._mark_connected()
            logger.info(
                "[ios] Connected — watching uid=%s channels=%s",
                self._uid, ",".join(self._channels),
            )
            return True
        except Exception as e:
            logger.error("[ios] Failed to connect: %s", e)
            return False

    def _start_channel_watch(self, channel: str) -> None:
        """Register a Firestore on_snapshot listener for one channel.

        Mirrors sangamam-v3 firebase_watcher.start_channel_watcher: only docs
        created after startup are dispatched, and only role=user/intent=interact.
        """
        if channel in self._watches:
            logger.warning("[ios] duplicate watch for channel=%s — skipping", channel)
            return

        startup_ts = datetime.now(timezone.utc)
        query = (
            self._db.collection("users").document(self._uid)
            .collection("channels").document(channel)
            .collection("messages")
            .where(filter=FieldFilter("ts", ">", startup_ts))
        )

        def on_snapshot(col_snapshot, changes, read_time):
            for change in changes:
                if change.type.name != "ADDED":
                    continue
                data = change.document.to_dict()
                if not data:
                    continue
                msg_id = data.get("message_id") or change.document.id
                if msg_id in self._our_msg_ids:
                    continue
                role = data.get("role")
                intent = data.get("intent")
                # Dispatch two kinds of inbound as agent turns (mirrors the old
                # sangamam watcher):
                #   1. live user messages: role=user, intent=interact
                #   2. notifications/escalations: intent=notify (any role; e.g. a
                #      sibling profile's escalate writes role=system/intent=notify).
                #      These are hidden in the iOS UI but still dispatched.
                is_user_turn = role == "user" and intent == "interact"
                is_notify = intent == "notify"
                if not (is_user_turn or is_notify):
                    continue
                # Flip typing on immediately (background thread) for snappy ripple.
                self._rtdb_typing(channel, True)
                self._dispatch_inbound(channel, msg_id, data)

        self._watches[channel] = query.on_snapshot(on_snapshot)
        logger.info("[ios] watch started: users/%s/channels/%s/messages", self._uid, channel)

    def _dispatch_inbound(self, channel: str, msg_id: str, data: dict) -> None:
        """Bridge a Firestore doc (background thread) into the gateway loop."""
        text = data.get("text") or ""
        user = data.get("user") or self._uid
        ts_val = data.get("ts")
        try:
            timestamp = ts_val if isinstance(ts_val, datetime) else datetime.now(timezone.utc)
        except Exception:
            timestamp = datetime.now(timezone.utc)

        source = self.build_source(
            chat_id=channel,
            chat_name=channel,
            chat_type="dm",
            user_id=user,
            user_name=user,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=msg_id,
            raw_message=data,
            timestamp=timestamp,
        )
        logger.info("[ios] in channel=%s user=%s len=%d", channel, user, len(text))
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self.handle_message(event), self._loop)

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        for channel, watch in list(self._watches.items()):
            try:
                watch.unsubscribe()
            except Exception as e:
                logger.debug("[ios] unsubscribe %s failed: %s", channel, e)
        self._watches.clear()
        logger.info("[ios] Disconnected")

    # -- Outbound ----------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Write the agent's reply as a role=assistant doc to Firestore."""
        if self._db is None:
            return SendResult(success=False, error="Firestore client not initialized")

        channel = chat_id or (self._channels[0] if self._channels else DEFAULT_CHANNEL)
        msg_id = str(uuid4())
        # Track before write so the watcher dedups our own echo.
        self._our_msg_ids.add(msg_id)
        if len(self._our_msg_ids) > 2000:
            for m in list(self._our_msg_ids)[:200]:
                self._our_msg_ids.discard(m)

        doc: dict = {
            "message_id": msg_id,
            "role": "assistant",
            "intent": "interact",
            "text": content,
            "ts": _fs_sync.SERVER_TIMESTAMP,
            "device_id": None,
            "device_name": None,
            "trace_id": (metadata or {}).get("trace_id") or uuid4().hex,
        }

        try:
            await asyncio.to_thread(
                lambda: self._db.collection("users").document(self._uid)
                .collection("channels").document(channel)
                .collection("messages").document(msg_id)
                .set(doc)
            )
            # NOTE: do NOT clear the ripple here — send() fires for interim
            # narration mid-turn too. Turn-end clearing is handled by
            # on_processing_complete (mirrors Discord's reaction swap).
            logger.info("[ios] out channel=%s assistant %d chars", channel, len(content))
            return SendResult(success=True, message_id=msg_id)
        except Exception as e:
            logger.error("[ios] send error: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Flip RTDB typing on for the ripple box."""
        await asyncio.to_thread(self._rtdb_typing, chat_id, True)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edit a previously-sent Firestore message doc in place.

        Overriding this is what flips the gateway's capability gate
        (gateway/run.py:16513) so the gateway routes its built-in
        tool-progress to this adapter — EXACTLY like Discord. The gateway
        builds the full progress string in-process (e.g. '💻 terminal: "date"',
        with real registry emojis) and delivers it here as ``content``; we
        merge-update the message doc so the app sees one progress bubble that
        updates in place. The emoji is part of ``content`` — no rebuilding.

        Progress text is multiple tool lines joined by single ``\\n``. The iOS
        app renders message text as Markdown, where a single newline is a soft
        break (collapses to a space). ``_md_hardbreaks`` upgrades them to real
        line breaks so each tool shows on its own line.
        """
        if self._db is None:
            return SendResult(success=False, error="Firestore client not initialized")
        channel = chat_id or (self._channels[0] if self._channels else DEFAULT_CHANNEL)
        try:
            await asyncio.to_thread(
                lambda: self._db.collection("users").document(self._uid)
                .collection("channels").document(channel)
                .collection("messages").document(message_id)
                .set({
                    "text": _md_hardbreaks(content),
                    "edited": True,
                    "ts_edited": _fs_sync.SERVER_TIMESTAMP,
                }, merge=True)
            )
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            logger.warning("[ios] edit_message failed: %s", e)
            return SendResult(success=False, error=str(e))

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        """Delete a Firestore message doc — used by the gateway's optional
        cleanup_progress to remove the transient progress bubble after the
        final answer lands. Mirrors Discord's delete_message."""
        if self._db is None:
            return False
        channel = chat_id or (self._channels[0] if self._channels else DEFAULT_CHANNEL)
        try:
            await asyncio.to_thread(
                lambda: self._db.collection("users").document(self._uid)
                .collection("channels").document(channel)
                .collection("messages").document(message_id)
                .delete()
            )
            return True
        except Exception as e:
            logger.debug("[ios] delete_message failed: %s", e)
            return False

    async def on_processing_start(self, event) -> None:
        """Turn start — set the RTDB typing ripple on (iOS analog of
        Discord's 👀 reaction)."""
        try:
            chat_id = event.source.chat_id
            await asyncio.to_thread(self._rtdb_typing, chat_id, True)
        except Exception as e:
            logger.debug("[ios] on_processing_start failed: %s", e)

    async def on_processing_complete(self, event, outcome) -> None:
        """Turn end — clear the RTDB ripple (iOS analog of Discord's
        reaction swap to ✅/⚠️)."""
        try:
            chat_id = event.source.chat_id
            await asyncio.to_thread(self._rtdb_clear, chat_id)
        except Exception as e:
            logger.debug("[ios] on_processing_complete failed: %s", e)

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Render a native approval bubble in the iOS app via Firebase RTDB.

        Mirrors Discord's ``ExecApprovalView`` (buttons → resolve_gateway_approval)
        but uses the iOS app's existing approval contract so the app needs NO
        changes:
          - WRITE  ``channel_state/{channel}/pending_approval`` =
                   ``{request_id, description, agent_id}``  (app shows Allow/Decline)
          - LISTEN ``approval_decisions/{request_id}`` for the app's write
                   ``{agent_id, decision: "approve"|"decline", state_key}``
          - MAP    approve→"once", decline→"deny" and call
                   ``resolve_gateway_approval(session_key, choice)`` to unblock.

        Returning ``success=False`` (or raising) makes the gateway fall back to
        the plain-text ``/approve`` prompt — so text approval still works if RTDB
        is unavailable.

        ``agent_id`` carries the gateway ``session_key`` so the decision round-trip
        is self-correlating (the old PTY-socket meaning of agent_id is unused here).
        """
        if not self._rtdb_url:
            return SendResult(success=False, error="RTDB not configured")
        channel = chat_id or (self._channels[0] if self._channels else DEFAULT_CHANNEL)
        request_id = uuid4().hex
        # Put the command into the description the bubble shows (app renders
        # `description`; include the command so the user sees what they're approving).
        cmd_preview = command if len(command) <= 400 else command[:397] + "…"
        bubble_desc = f"{description}\n{cmd_preview}" if description else cmd_preview

        try:
            await asyncio.to_thread(
                lambda: rtdb.reference(f"channel_state/{channel}").update({
                    "pending_approval": {
                        "request_id": request_id,
                        "description": bubble_desc,
                        "agent_id": session_key,  # carries session_key for the round-trip
                    },
                    "last_hook": "PermissionRequest",
                    "updated_at": int(time.time() * 1000),
                })
            )
        except Exception as e:
            logger.warning("[ios] send_exec_approval write failed: %s", e)
            return SendResult(success=False, error=str(e))

        # Listen for the app's decision on approval_decisions/{request_id}.
        # firebase_admin fires the callback on a background thread; resolve_gateway_approval
        # is sync-safe so we can call it directly from there.
        reg_holder: dict = {}

        def _on_decision(event):
            data = event.data
            if not isinstance(data, dict):
                return  # initial None / partial — wait for the real write
            decision = str(data.get("decision", "")).lower()
            if decision not in ("approve", "decline", "deny", "allow"):
                return
            choice = "once" if decision in ("approve", "allow") else "deny"
            try:
                from tools.approval import resolve_gateway_approval
                n = resolve_gateway_approval(session_key, choice)
                logger.info(
                    "[ios] approval resolved request=%s decision=%s→%s (n=%d)",
                    request_id, decision, choice, n,
                )
            except Exception as exc:
                logger.error("[ios] resolve_gateway_approval failed: %s", exc)
            finally:
                # Clear the bubble + decision so it doesn't re-fire, and close listener.
                try:
                    rtdb.reference(f"channel_state/{channel}/pending_approval").delete()
                except Exception:
                    pass
                try:
                    rtdb.reference(f"approval_decisions/{request_id}").delete()
                except Exception:
                    pass
                reg = reg_holder.get("reg")
                if reg is not None:
                    try:
                        reg.close()
                    except Exception:
                        pass

        try:
            reg_holder["reg"] = await asyncio.to_thread(
                lambda: rtdb.reference(f"approval_decisions/{request_id}").listen(_on_decision)
            )
        except Exception as e:
            logger.warning("[ios] send_exec_approval listen failed: %s", e)
            return SendResult(success=False, error=str(e))

        logger.info("[ios] approval bubble shown channel=%s request=%s", channel, request_id)
        return SendResult(success=True, message_id=request_id)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}

    # -- RTDB ripple helpers (best-effort; no-op without rtdb_url) ----------

    def _rtdb_typing(self, channel: str, typing: bool) -> None:
        if not self._rtdb_url:
            return
        try:
            rtdb.reference(f"channel_state/{channel}").update({
                "typing": typing,
                "updated_at": int(time.time() * 1000),
            })
        except Exception as e:
            logger.debug("[ios] rtdb typing failed: %s", e)

    def _rtdb_clear(self, channel: str) -> None:
        if not self._rtdb_url:
            return
        try:
            rtdb.reference(f"channel_state/{channel}").update({
                "typing": False,
                "thoughts": None,
                "current_hook": None,
                "updated_at": int(time.time() * 1000),
            })
        except Exception as e:
            logger.debug("[ios] rtdb clear failed: %s", e)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def _env_enablement() -> dict | None:
    """Seed PlatformConfig.extra from env so env-only setups auto-enable."""
    creds = os.getenv("IOS_FIREBASE_CREDENTIALS", DEFAULT_CREDS)
    if not creds or not os.path.exists(os.path.expanduser(creds)):
        return None
    seed: dict = {"credentials": os.path.expanduser(creds)}
    rtdb_url = os.getenv("IOS_FIREBASE_RTDB_URL", "").strip()
    if rtdb_url:
        seed["rtdb_url"] = rtdb_url
    uid = os.getenv("IOS_UID", "").strip()
    if uid:
        seed["uid"] = uid
    channels = os.getenv("IOS_CHANNELS", "").strip()
    if channels:
        seed["channels"] = channels
    home = os.getenv("IOS_HOME_CHANNEL", "").strip() or (uid and DEFAULT_CHANNEL) or DEFAULT_CHANNEL
    seed["home_channel"] = {"chat_id": home, "name": home}
    return seed


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process Firestore write for cron / send_message_tool fallbacks."""
    if not FIREBASE_AVAILABLE:
        return {"error": "ios standalone send: firebase-admin not installed"}
    extra = getattr(pconfig, "extra", {}) or {}
    creds = os.path.expanduser(
        extra.get("credentials") or os.getenv("IOS_FIREBASE_CREDENTIALS", DEFAULT_CREDS)
    )
    rtdb_url = extra.get("rtdb_url") or os.getenv("IOS_FIREBASE_RTDB_URL", "")
    uid = extra.get("uid") or os.getenv("IOS_UID", DEFAULT_UID)
    channel = chat_id or os.getenv("IOS_HOME_CHANNEL", DEFAULT_CHANNEL)
    if not os.path.exists(creds):
        return {"error": f"ios standalone send: credentials not found at {creds}"}
    try:
        _ensure_firebase_app(creds, rtdb_url)
        db = _fs_sync.client()
        msg_id = str(uuid4())
        doc = {
            "message_id": msg_id,
            "role": "assistant",
            "intent": "interact",
            "text": message,
            "ts": _fs_sync.SERVER_TIMESTAMP,
            "trace_id": uuid4().hex,
        }
        await asyncio.to_thread(
            lambda: db.collection("users").document(uid)
            .collection("channels").document(channel)
            .collection("messages").document(msg_id).set(doc)
        )
        return {"success": True, "platform": "ios", "chat_id": channel, "message_id": msg_id}
    except Exception as e:
        return {"error": f"ios standalone send failed: {e}"}


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="ios",
        label="iOS App",
        adapter_factory=lambda cfg: IOSAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["IOS_FIREBASE_CREDENTIALS"],
        install_hint="uv pip install firebase-admin",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="IOS_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="IOS_ALLOWED_USERS",
        allow_all_env="IOS_ALLOW_ALL_USERS",
        emoji="📱",
        platform_hint=(
            "You are chatting with the user through the Kaveri iOS app. "
            "Responses render as chat bubbles — use concise, mobile-friendly "
            "plain text. Markdown is lightly supported."
        ),
    )
