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


async def proxy_ws(ws, profile: str, port: int) -> None:
    """Accept the client WebSocket and pump frames to/from the per-profile
    dashboard's /api/ws. Best-effort: closes both sides on any error."""
    import websockets

    token = _shared_token()
    upstream_url = f"ws://127.0.0.1:{port}/api/ws?profile={profile}&token={token}"

    await ws.accept()
    try:
        async with websockets.connect(
            upstream_url, open_timeout=15, max_size=None, ping_interval=None
        ) as upstream:
            await _pump(ws, upstream, profile)
    except Exception as exc:  # noqa: BLE001 — best-effort proxy
        _log.warning("profile router: %s proxy failed: %s", profile, exc)
        try:
            await ws.close(code=1011)
        except Exception:
            pass


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
    done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    try:
        await ws.close()
    except Exception:
        pass
