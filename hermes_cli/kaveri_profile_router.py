"""[kaveri fork] Per-profile dashboard router.

The public dashboard (cockpit, behind the Cloudflare tunnel) is the single
ingress for the Hermes app. Each non-default profile (finance, logistics) runs
its OWN dashboard process on a loopback port, with its own HERMES_HOME → its own
SOUL, model, memory, and MCP tools. This module lets the cockpit dashboard act as
a thin router: a chat WebSocket carrying ``?profile=finance`` is proxied to the
finance dashboard instead of being handled in-process (which would run every
profile as cockpit, with no profile MCP tools — the bug this fixes).

Design goals:
- All routing logic lives HERE (new file) so the upstream edit in web_server.py
  is a 3-line hook → minimal rebase surface.
- Cockpit / default is NEVER routed (returns None) → the app's main path is
  untouched; if this module fails, only finance/logistics routing is affected.
- Loopback dashboards require a session token; we inject the shared token from
  the keychain (KAVERI_PROFILE_DASH_TOKEN) on the upstream connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess

_log = logging.getLogger(__name__)

# profile name → loopback port of its dashboard. Add new profiles here.
_PROFILE_PORTS: dict[str, int] = {
    "finance": 18791,
    "logistics": 18792,
}

_KEYCHAIN_GET = "/Users/kaveri/kaveri/scripts/keychain-get"
_TOKEN_NAME = "KAVERI_PROFILE_DASH_TOKEN"

_token_cache: str | None = None


def ensure_mcp_discovered() -> None:
    """Run MCP tool discovery inside this dashboard process for ITS HERMES_HOME.

    The ``dashboard`` command skips ``_prepare_agent_startup`` (main.py), so a
    dashboard process never discovers MCP servers — every chat turn it hosts gets
    built-in tools only, no profile MCP (e.g. finance's get_portfolio_summary).
    Calling this at dashboard startup populates the in-process MCP registry from
    the current profile's config, so agents built here get their MCP tools.
    Idempotent + fail-open; run off-thread so it never blocks startup.
    """
    import threading

    def _go() -> None:
        try:
            from tools.mcp_tool import discover_mcp_tools

            names = discover_mcp_tools()
            _log.info("dashboard MCP discovery: %d tool(s)", len(names or []))
        except Exception:
            _log.exception("dashboard MCP discovery failed")

    threading.Thread(target=_go, name="kaveri-mcp-discovery", daemon=True).start()


def _normalize(profile: str | None) -> str:
    name = (profile or "").strip().lower()
    return "default" if name in ("", "default", "cockpit") else name


def _current_profile() -> str:
    """This dashboard process's OWN profile, derived from its launch HERMES_HOME
    (``…/profiles/<name>`` → ``<name>``; the root → ``default``). Used so only the
    ingress (default/cockpit) routes — a per-profile dashboard must handle its own
    profile locally and never proxy (which would recurse into itself)."""
    try:
        from hermes_constants import get_hermes_home

        parts = str(get_hermes_home()).rstrip("/").split("/")
        if len(parts) >= 2 and parts[-2] == "profiles":
            return parts[-1].lower()
    except Exception:
        pass
    return "default"


def target_port(profile: str | None) -> int | None:
    """Loopback port to route this profile to, or None when it must be handled
    locally: this process is itself a per-profile dashboard (only the default
    ingress routes), the request is default/cockpit, or the profile is unknown."""
    if _current_profile() != "default":
        return None
    return _PROFILE_PORTS.get(_normalize(profile))


def target_profile_name(profile: str | None) -> str:
    """Canonical profile name routed traffic should carry upstream."""
    return _normalize(profile)


def _shared_token() -> str:
    global _token_cache
    if _token_cache is None:
        _token_cache = subprocess.run(
            ["/bin/bash", _KEYCHAIN_GET, _TOKEN_NAME],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    return _token_cache


# JSON-RPC methods whose params carry the owning profile. The first such frame on
# a connection decides where the whole connection is routed.
_ROUTING_METHODS = frozenset(
    {"session.create", "session.resume", "session.activate", "prompt.submit", "prompt.background"}
)


def _profile_from_frame(text: str) -> tuple[bool, str | None]:
    """(is_decision_frame, profile). A decision frame is a routing method; its
    params.profile (possibly absent → default) determines routing."""
    try:
        msg = json.loads(text)
    except (ValueError, TypeError):
        return False, None
    if not isinstance(msg, dict) or msg.get("method") not in _ROUTING_METHODS:
        return False, None
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    return True, (params or {}).get("profile")


async def route_ws(ws, local_handler) -> None:
    """Decide where a chat WS belongs by PEEKING its first frames.

    The app carries the profile in the message params (session.create/prompt.submit),
    not the WS URL — so we read the first routing frame, and if it names a
    non-default profile, proxy the whole connection to that profile's loopback
    dashboard (its own HERMES_HOME → its own MCP/SOUL/model). Otherwise we replay
    the buffered frames into the local handler (cockpit, in-process) unchanged.

    The client does not gate its first request on gateway.ready, so peeking before
    proxying is safe. Connection-level (not per-message): the first routing frame
    pins the connection — fine because the app opens a fresh socket per profile
    context.
    """
    await ws.accept()

    buffered: list[str] = []
    port: int | None = None
    profile_name = "default"
    # Decide on the FIRST frame only — peeking further risks stalling a connection
    # whose opening frame is a non-routing call (e.g. session.list) that needs a
    # reply. A routing frame (session.create/prompt.submit/…) carries the profile;
    # anything else routes local (cockpit). The app opens a fresh socket per profile
    # context, so its first frame is the profile-bearing one.
    try:
        frame = await ws.receive_text()
        buffered.append(frame)
        is_decision, prof = _profile_from_frame(frame)
        if is_decision:
            port = target_port(prof)
            profile_name = target_profile_name(prof)
    except Exception as exc:  # noqa: BLE001 — client vanished mid-handshake
        _log.debug("profile router: peek ended early: %s", exc)

    _log.info("[kaveri router] decided profile=%r port=%r (%d peeked)", profile_name, port, len(buffered))

    if port is None:
        # Local/cockpit: replay buffered frames into the in-process handler.
        _replay_into(ws, buffered)
        await local_handler(ws)
        return

    # Proxy the whole connection to the per-profile dashboard.
    import websockets

    token = _shared_token()
    upstream_url = f"ws://127.0.0.1:{port}/api/ws?profile={profile_name}&token={token}"
    try:
        async with websockets.connect(
            upstream_url, open_timeout=15, max_size=None, ping_interval=None
        ) as upstream:
            for frame in buffered:
                await upstream.send(frame)
            await _pump(ws, upstream, profile_name)
    except Exception as exc:  # noqa: BLE001 — best-effort proxy
        _log.warning("profile router: %s proxy failed: %s", profile_name, exc)
        try:
            await ws.close(code=1011)
        except Exception:
            pass


def _replay_into(ws, buffered: list[str]) -> None:
    """Make the already-accepted ws transparently re-yield the buffered frames to
    the local handler. Monkeypatch (not a wrapper subclass) so isinstance checks in
    handle_ws/WSTransport still see a real WebSocket."""
    pending = list(buffered)
    real_receive = ws.receive_text

    async def _accept_noop(*_a, **_k):  # handle_ws calls accept(); we already did
        return None

    async def _receive_replay():
        if pending:
            return pending.pop(0)
        return await real_receive()

    ws.accept = _accept_noop  # type: ignore[method-assign]
    ws.receive_text = _receive_replay  # type: ignore[method-assign]


async def _pump(ws, upstream, profile: str) -> None:
    from starlette.websockets import WebSocketDisconnect

    async def client_to_upstream() -> None:
        try:
            while True:
                msg = await ws.receive_text()
                await upstream.send(msg)
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            _log.debug("profile router %s c→u ended: %s", profile, exc)

    async def upstream_to_client() -> None:
        try:
            async for msg in upstream:
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", "replace")
                await ws.send_text(msg)
        except Exception as exc:  # noqa: BLE001
            _log.debug("profile router %s u→c ended: %s", profile, exc)

    t1 = asyncio.create_task(client_to_upstream())
    t2 = asyncio.create_task(upstream_to_client())
    _done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    try:
        await ws.close()
    except Exception:
        pass
