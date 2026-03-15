"""
BlueBubbles platform adapter.

Connects to a BlueBubbles server running on macOS for two-way iMessage support.
Inbound messages arrive via HTTP webhooks that BlueBubbles POSTs to us.
Outbound messages are sent via BlueBubbles REST API.

Requires:
- BlueBubbles server running on macOS (https://bluebubbles.app)
- BLUEBUBBLES_URL env var (e.g. http://localhost:1234)
- BLUEBUBBLES_PASSWORD env var
- BLUEBUBBLES_WEBHOOK_PORT env var (default: 5555) — port for incoming webhooks
"""

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 8000
DEFAULT_WEBHOOK_PORT = 5555


def check_bluebubbles_requirements() -> bool:
    """Check if BlueBubbles dependencies are available and configured."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    if not os.getenv("BLUEBUBBLES_URL"):
        return False
    if not os.getenv("BLUEBUBBLES_PASSWORD"):
        return False
    return True


class BlueBubblesAdapter(BasePlatformAdapter):
    """
    BlueBubbles iMessage adapter.

    Receives iMessages via HTTP webhooks, sends via REST API.
    Spawns an aiohttp web server to receive webhook POSTs from BlueBubbles.
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.BLUEBUBBLES)

        extra = config.extra or {}
        self._bb_url: str = (
            extra.get("url") or os.getenv("BLUEBUBBLES_URL", "http://localhost:1234")
        ).rstrip("/")
        self._bb_password: str = (
            extra.get("password") or os.getenv("BLUEBUBBLES_PASSWORD", "")
        )
        self._webhook_port: int = int(
            extra.get("webhook_port")
            or os.getenv("WEBHOOK_PORT", DEFAULT_WEBHOOK_PORT)
        )
        self._webhook_path: str = extra.get("webhook_path") or os.getenv("BLUEBUBBLES_URL_PATH", "/bb/webhook")

        self._client: Optional[httpx.AsyncClient] = None
        self._server_task: Optional[asyncio.Task] = None
        self._runner = None
        self._site = None

        # Only process messages sent TO us (not ones we send)
        self._our_handles: List[str] = []

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Start webhook server and register with BlueBubbles."""
        try:
            import aiohttp
            from aiohttp import web
        except ImportError:
            logger.error("BlueBubbles: aiohttp is required. pip install aiohttp")
            return False

        if not self._bb_url or not self._bb_password:
            logger.error("BlueBubbles: BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD are required")
            return False

        self._client = httpx.AsyncClient(timeout=30.0)

        # Verify server is reachable
        try:
            resp = await self._client.get(
                f"{self._bb_url}/api/v1/ping",
                params={"guid": self._bb_password},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.error("BlueBubbles: ping failed (status %d)", resp.status_code)
                return False
            logger.info("BlueBubbles: server reachable at %s", self._bb_url)
        except Exception as e:
            logger.error("BlueBubbles: cannot reach server at %s: %s", self._bb_url, e)
            return False

        # Start aiohttp webhook server
        app = web.Application()
        app.router.add_post(self._webhook_path, self._handle_webhook)
        am_path = os.getenv("AGENTMAIL_URL_PATH", "/am")
        app.router.add_post(am_path, self._handle_agentmail_webhook)
        app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", self._webhook_port)
        await self._site.start()
        logger.info(
            "BlueBubbles: webhook server listening on port %d at %s",
            self._webhook_port, self._webhook_path
        )

        # Register our webhook with BlueBubbles
        # Use public tunnel URL if configured, else fallback to localhost
        webhook_base = os.getenv("WEBHOOK_BASE_URL", f"http://127.0.0.1:{self._webhook_port}")
        webhook_url = f"{webhook_base}{self._webhook_path}"
        await self._register_webhook(webhook_url)

        self._mark_connected()
        self._running = True
        logger.info("BlueBubbles: connected and ready")
        return True

    async def disconnect(self) -> None:
        """Stop webhook server."""
        self._running = False

        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()

        if self._client:
            await self._client.aclose()
            self._client = None

        self._mark_disconnected()
        logger.info("BlueBubbles: disconnected")

    # ------------------------------------------------------------------
    # Webhook registration
    # ------------------------------------------------------------------

    async def _register_webhook(self, url: str) -> None:
        """Register our webhook URL with the BlueBubbles server."""
        try:
            # First, check if already registered
            resp = await self._client.get(
                f"{self._bb_url}/api/v1/webhook",
                params={"guid": self._bb_password},
            )
            if resp.status_code == 200:
                existing = resp.json().get("data", [])
                for hook in existing:
                    if hook.get("url") == url:
                        logger.info("BlueBubbles: webhook already registered at %s", url)
                        return

            # Register webhook for new messages
            resp = await self._client.post(
                f"{self._bb_url}/api/v1/webhook",
                params={"guid": self._bb_password},
                json={
                    "url": url,
                    "events": [
                        {"type": "new-message", "url": url},
                        {"type": "updated-message", "url": url},
                    ],
                },
            )
            if resp.status_code in (200, 201):
                logger.info("BlueBubbles: webhook registered at %s", url)
            else:
                logger.warning(
                    "BlueBubbles: webhook registration returned %d: %s",
                    resp.status_code, resp.text
                )
        except Exception as e:
            logger.warning("BlueBubbles: webhook registration failed: %s", e)

    # ------------------------------------------------------------------
    # Inbound webhook handler
    # ------------------------------------------------------------------

    # Trusted senders — only these may issue instructions via email
    _TRUSTED_EMAIL_SENDERS = {
        "subrih@gmail.com",
        "skovilmadam@gmail.com",
        "ammusubri@yahoo.com",
    }

    # Discord cockpit channel to alert on untrusted emails
    _DISCORD_ALERT_CHANNEL = "1480702402410582046"

    async def _handle_agentmail_webhook(self, request) -> "aiohttp.web.Response":
        """Handle incoming AgentMail webhook POST — new email received."""
        from aiohttp import web

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"status": "error", "message": "invalid json"}, status=400)

        event_type = body.get("event_type", body.get("type", ""))
        logger.info("AgentMail webhook received: event_type=%s", event_type)

        # AgentMail payload: {"type": "event", "event_type": "message.received", "message": {...}}
        if event_type == "message.received":
            msg = body.get("message", body.get("data", {}))
            if msg:
                await self._process_agentmail_message(msg)

        return web.json_response({"status": "ok"})

    def _extract_email_address(self, from_field: str) -> str:
        """Extract bare email address from 'Name <email>' or plain 'email' format."""
        import re
        match = re.search(r"<([^>]+)>", from_field)
        if match:
            return match.group(1).strip().lower()
        return from_field.strip().lower()

    async def _process_agentmail_message(self, msg: dict) -> None:
        """Process an inbound email — enforce trusted sender policy before routing."""
        if not self._message_handler:
            logger.warning("AgentMail webhook: no message handler set")
            return

        # AgentMail uses 'from_' (underscore) to avoid Python reserved word conflict
        raw_from = msg.get("from_") or msg.get("from", "unknown")
        sender_email = self._extract_email_address(raw_from)
        subject = msg.get("subject", "(no subject)")
        body_text = msg.get("text") or msg.get("preview", "")
        message_id = msg.get("message_id", "")

        logger.info("AgentMail: inbound email from %s — %s", sender_email, subject)

        # Security gate — untrusted sender
        if sender_email not in self._TRUSTED_EMAIL_SENDERS:
            logger.warning("AgentMail: UNTRUSTED sender %s — alerting cockpit, NOT acting", sender_email)
            await self._alert_untrusted_email(raw_from, sender_email, subject, body_text)
            return

        # Trusted sender — route to agent as a message
        text = (
            f"[INBOUND EMAIL]\n"
            f"From: {raw_from}\n"
            f"Subject: {subject}\n\n"
            f"{body_text}"
        )

        from gateway.session import SessionSource
        source = SessionSource(
            platform=Platform.BLUEBUBBLES,
            chat_id="agentmail:kaveri@agentmail.to",
            user_id=sender_email,
            user_name=sender_email,
            chat_name="AgentMail",
        )

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=message_id,
            raw_message=msg,
            timestamp=datetime.now(),
        )

        try:
            response = await self._message_handler(event)
            if response:
                # Always reply via email to sender
                await self._reply_via_agentmail(raw_from, subject, response)
                # Also notify Subri on Discord cockpit so he sees it in real time
                await self._notify_discord_agentmail_action(raw_from, subject, response)
        except Exception as e:
            logger.error("AgentMail: error handling inbound email: %s", e)

    async def _reply_via_agentmail(self, to: str, original_subject: str, response_text: str) -> None:
        """Send a reply email via AgentMail."""
        import os, random
        quotes = [
            "No cheerleader. No mirror. Just signal.",
            "Steady force. Strategic counterweight. Long-term focused.",
            "Truth over comfort. Systems over hacks. Marathon over sprint.",
            "Reduce entropy. Protect advantage. Tell the truth.",
            "Outcome-oriented. Zero ego. Always watching the horizon.",
            "Calm under pressure. Precise under fire.",
        ]
        signature = f"\n\n--\nKaveri 🌊 **{random.choice(quotes)}**\nAI Chief of Staff to Subri\nkaveri@agentmail.to"
        subject = original_subject if original_subject.startswith("Re:") else f"Re: {original_subject}"
        api_key = os.getenv("AGENTMAIL_API_KEY", "")
        inbox = os.getenv("AGENTMAIL_INBOX_ID", "kaveri@agentmail.to")
        if not api_key:
            logger.error("AgentMail reply: AGENTMAIL_API_KEY not set")
            return
        try:
            resp = await asyncio.get_event_loop().run_in_executor(None, lambda: __import__('httpx').post(
                f"https://api.agentmail.to/v0/inboxes/{inbox}/messages/send",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"to": to, "subject": subject, "text": response_text + signature},
                timeout=30.0,
            ))
            if resp.status_code in (200, 201):
                logger.info("AgentMail: replied to %s — %s", to, subject)
            else:
                logger.error("AgentMail reply failed: %d %s", resp.status_code, resp.text)
        except Exception as e:
            logger.error("AgentMail reply error: %s", e)

    async def _notify_discord_agentmail_action(self, raw_from: str, subject: str, response_text: str) -> None:
        """Ping Discord cockpit when a trusted email was acted on — so Subri always sees it."""
        import os, httpx
        bot_token = os.getenv("DISCORD_BOT_TOKEN", "")
        cockpit_channel_id = "1480702402410582046"
        if not bot_token:
            logger.warning("AgentMail Discord notify: no DISCORD_BOT_TOKEN set")
            return
        # Truncate response for Discord
        preview = response_text[:800] + ("..." if len(response_text) > 800 else "")
        message = (
            f"📧 **Email actioned** from `{raw_from}`\n"
            f"**Subject:** {subject}\n\n"
            f"{preview}"
        )
        try:
            await asyncio.get_event_loop().run_in_executor(None, lambda: httpx.post(
                f"https://discord.com/api/v10/channels/{cockpit_channel_id}/messages",
                headers={"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"},
                json={"content": message},
                timeout=10.0,
            ))
            logger.info("AgentMail: Discord cockpit notified of action on email from %s", raw_from)
        except Exception as e:
            logger.error("AgentMail Discord notify error: %s", e)

    async def _alert_untrusted_email(self, raw_from: str, sender_email: str, subject: str, body_text: str) -> None:
        """Send an alert to Discord cockpit about an untrusted inbound email."""
        try:
            # Find Discord adapter via the global adapters dict (injected at gateway level)
            # We send via httpx directly to the Discord webhook or via the delivery router
            # For now, log prominently and rely on the message handler to surface it
            if not self._message_handler:
                return

            alert_text = (
                f"⚠️ **UNTRUSTED EMAIL RECEIVED** — not acted on.\n\n"
                f"**From:** {raw_from}\n"
                f"**Subject:** {subject}\n\n"
                f"**Preview:**\n{body_text[:500]}\n\n"
                f"_This email was blocked. Only trusted senders may issue instructions._"
            )

            from gateway.session import SessionSource
            source = SessionSource(
                platform=Platform.BLUEBUBBLES,
                chat_id="agentmail:alert",
                user_id="agentmail-security",
                user_name="AgentMail Security",
                chat_name="AgentMail Alert",
            )

            event = MessageEvent(
                text=f"[SECURITY ALERT — UNTRUSTED EMAIL]\nFrom: {raw_from}\nSubject: {subject}\n\nPreview: {body_text[:500]}\n\nThis email was NOT acted on. Please review.",
                message_type=MessageType.TEXT,
                source=source,
                raw_message={"alert": True, "from": raw_from, "subject": subject},
                timestamp=datetime.now(),
            )

            await self._message_handler(event)
        except Exception as e:
            logger.error("AgentMail: failed to send untrusted email alert: %s", e)

    async def _handle_health(self, request) -> "aiohttp.web.Response":
        from aiohttp import web
        return web.json_response({"status": "ok"})

    async def _handle_webhook(self, request) -> "aiohttp.web.Response":
        """Handle incoming webhook POST from BlueBubbles."""
        from aiohttp import web

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"status": "error", "message": "invalid json"}, status=400)

        event_type = body.get("type", "")
        data = body.get("data", {})

        if event_type == "new-message":
            await self._process_incoming_message(data)
        else:
            logger.debug("BlueBubbles: ignoring event type '%s'", event_type)

        return web.json_response({"status": "ok"})

    async def _process_incoming_message(self, msg: Dict[str, Any]) -> None:
        """Process a new-message event from BlueBubbles."""
        # Skip messages we sent ourselves
        if msg.get("isFromMe", False):
            return

        text = msg.get("text", "") or ""
        if not text.strip():
            return

        # Get sender info
        handle = msg.get("handle", {}) or {}
        sender_id = handle.get("address", "") or msg.get("handleId", "unknown")
        sender_name = handle.get("address", sender_id)

        # Get chat info
        chats = msg.get("chats", []) or []
        if chats:
            chat_guid = chats[0].get("guid", sender_id)
        else:
            chat_guid = f"iMessage;-;{sender_id}"

        message_id = str(msg.get("guid", ""))

        logger.info(
            "BlueBubbles: message from %s in chat %s: %s",
            sender_name, chat_guid, text[:80]
        )

        if not self._message_handler:
            logger.warning("BlueBubbles: no message handler set, dropping message")
            return

        from gateway.session import SessionSource
        source = SessionSource(
            platform=Platform.BLUEBUBBLES,
            chat_id=chat_guid,
            user_id=sender_id,
            user_name=sender_name,
            chat_name=sender_name,
        )

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=message_id,
            raw_message=msg,
            timestamp=datetime.now(),
        )

        try:
            response = await self._message_handler(event)
            if response:
                await self.send(chat_guid, response)
        except Exception as e:
            logger.error("BlueBubbles: error handling message: %s", e)

    # ------------------------------------------------------------------
    # Outbound messaging
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message via BlueBubbles REST API."""
        if not self._client:
            return SendResult(success=False, error="Not connected")

        # Split long messages
        chunks = self._split_message(content)
        last_result = SendResult(success=True)

        for chunk in chunks:
            try:
                import uuid as _uuid
                resp = await self._client.post(
                    f"{self._bb_url}/api/v1/message/text",
                    params={"guid": self._bb_password},
                    json={
                        "chatGuid": chat_id,
                        "message": chunk,
                        "method": "apple-script",
                        "tempGuid": str(_uuid.uuid4()),
                    },
                    timeout=30.0,
                )
                if resp.status_code in (200, 201):
                    data = resp.json().get("data", {})
                    last_result = SendResult(
                        success=True,
                        message_id=str(data.get("guid", "")),
                        raw_response=data,
                    )
                else:
                    logger.error(
                        "BlueBubbles: send failed (status %d): %s",
                        resp.status_code, resp.text
                    )
                    last_result = SendResult(
                        success=False,
                        error=f"HTTP {resp.status_code}: {resp.text}",
                    )
            except Exception as e:
                logger.error("BlueBubbles: send error: %s", e)
                last_result = SendResult(success=False, error=str(e))

        return last_result

    async def get_chat_info(self, chat_id: str) -> dict:
        """Get basic info about a chat from BlueBubbles."""
        try:
            resp = await self._client.get(
                f"{self._bb_url}/api/v1/chat/{chat_id}",
                params={"guid": self._bb_password},
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                return {
                    "name": data.get("displayName") or chat_id,
                    "type": "group" if data.get("isGroupChat") else "dm",
                }
        except Exception:
            pass
        return {"name": chat_id, "type": "dm"}

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """BlueBubbles doesn't support typing indicators via the API."""
        pass

    def _split_message(self, text: str) -> List[str]:
        """Split long messages into chunks."""
        if len(text) <= MAX_MESSAGE_LENGTH:
            return [text]
        chunks = []
        while text:
            chunks.append(text[:MAX_MESSAGE_LENGTH])
            text = text[MAX_MESSAGE_LENGTH:]
        return chunks

    # ------------------------------------------------------------------
    # Keep-alive (BlueBubbles doesn't need polling, webhooks handle it)
    # ------------------------------------------------------------------

    async def keep_alive(self) -> None:
        """Idle loop — webhook server handles inbound, nothing to poll."""
        while self._running:
            await asyncio.sleep(30)
