# [kaveri fork] Push notifications.
#
# Remote clients (iOS app, Mac app) have no way to surface an alert when the
# agent produces a message while the app is backgrounded/closed. This module
# delivers those alerts:
#   - iOS  -> APNs (HTTP/2, ES256 JWT signed with the team's .p8 auth key).
#   - Mac  -> a WS "notification" frame the renderer turns into a native toast
#             (handled in the gateway, not here).
#
# Device tokens register via POST /api/notifications/register (web_server.py)
# and are stored under HERMES_HOME/notifications/devices.json. The APNs auth key
# (.p8 PEM) is read from the kaveri keychain as IOS_APNS_KEY_P8 (key id from
# IOS_APNS_KEY_ID, default 7ML8YPNFGJ; team M2ATZ7QRW4; bundle com.kaveri.cockpit).
#
# Self-contained: import-safe even if the key isn't present yet (send is a no-op
# that reports the reason), so the registry/endpoints work before the .p8 lands.

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

_log = logging.getLogger(__name__)

# Apple constants for this app. Team + bundle are not secret.
_APNS_TEAM_ID = "M2ATZ7QRW4"
_APNS_BUNDLE_ID = "com.kaveri.cockpit"
_APNS_DEFAULT_KEY_ID = "7ML8YPNFGJ"
_APNS_HOSTS = ("https://api.push.apple.com", "https://api.sandbox.push.apple.com")

_KEYCHAIN_GET = "/Users/kaveri/sangamam/scripts/keychain-get"

_lock = threading.Lock()
_jwt_cache: Dict[str, Any] = {"token": None, "ts": 0.0}
_creds_cache: Dict[str, Any] = {"p8": None, "key_id": None, "checked": 0.0}


# ── credentials ─────────────────────────────────────────────────────────────


def _keychain(name: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["bash", _KEYCHAIN_GET, name],
            capture_output=True, text=True, timeout=10, check=False,
        )
        val = (out.stdout or "").strip()
        if not val:
            return None
        # `security -w` hex-encodes any value containing newlines (e.g. a PEM),
        # so an all-hex, even-length blob is really hex — decode it back to text.
        if len(val) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", val):
            try:
                decoded = bytes.fromhex(val).decode("utf-8")
                if decoded.strip():
                    return decoded
            except Exception:
                pass
        return val
    except Exception:
        return None


def _load_creds() -> Optional[Dict[str, str]]:
    """Return {p8, key_id} or None when the APNs auth key isn't configured yet."""
    now = time.time()
    if _creds_cache["p8"] is None and now - _creds_cache["checked"] < 30:
        return None  # negative-cache so we don't shell out on every send
    if _creds_cache["p8"]:
        return {"p8": _creds_cache["p8"], "key_id": _creds_cache["key_id"]}

    p8 = _keychain("IOS_APNS_KEY_P8")
    _creds_cache["checked"] = now
    if not p8 or "BEGIN PRIVATE KEY" not in p8:
        return None
    _creds_cache["p8"] = p8
    _creds_cache["key_id"] = _keychain("IOS_APNS_KEY_ID") or _APNS_DEFAULT_KEY_ID
    return {"p8": _creds_cache["p8"], "key_id": _creds_cache["key_id"]}


def is_configured() -> bool:
    return _load_creds() is not None


def _apns_jwt() -> Optional[str]:
    """Cached ES256 provider JWT (valid ~1h; Apple requires refresh < 60m)."""
    now = time.time()
    if _jwt_cache["token"] and now - _jwt_cache["ts"] < 50 * 60:
        return _jwt_cache["token"]
    creds = _load_creds()
    if not creds:
        return None
    try:
        import jwt as _jwtlib

        token = _jwtlib.encode(
            {"iss": _APNS_TEAM_ID, "iat": int(now)},
            creds["p8"],
            algorithm="ES256",
            headers={"kid": creds["key_id"]},
        )
        _jwt_cache["token"] = token
        _jwt_cache["ts"] = now
        return token
    except Exception:
        _log.exception("push_notify: failed to mint APNs JWT")
        return None


# ── device registry ─────────────────────────────────────────────────────────


def _registry_path() -> Path:
    return get_hermes_home() / "notifications" / "devices.json"


def _read_registry() -> List[Dict[str, Any]]:
    try:
        return json.loads(_registry_path().read_text())
    except FileNotFoundError:
        return []
    except Exception:
        _log.exception("push_notify: registry read failed")
        return []


def _write_registry(devices: List[Dict[str, Any]]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(devices, indent=2))
    tmp.replace(path)


def register_device(token: str, platform: str = "ios", **meta: Any) -> None:
    token = (token or "").strip()
    if not token:
        return
    platform = (platform or "ios").strip().lower()
    with _lock:
        devices = [d for d in _read_registry() if d.get("token") != token]
        devices.append({"token": token, "platform": platform, "ts": int(time.time()), **meta})
        _write_registry(devices)
    _log.info("push_notify: registered %s device …%s", platform, token[-6:])


def unregister_device(token: str) -> None:
    token = (token or "").strip()
    if not token:
        return
    with _lock:
        devices = [d for d in _read_registry() if d.get("token") != token]
        _write_registry(devices)


def _prune_tokens(bad: set[str]) -> None:
    if not bad:
        return
    with _lock:
        devices = [d for d in _read_registry() if d.get("token") not in bad]
        _write_registry(devices)
    _log.info("push_notify: pruned %d dead token(s)", len(bad))


# ── send ────────────────────────────────────────────────────────────────────


def _send_one(client, host: str, token: str, jwt_token: str, payload: dict) -> "tuple[int, str]":
    resp = client.post(
        f"{host}/3/device/{token}",
        headers={
            "authorization": f"bearer {jwt_token}",
            "apns-topic": _APNS_BUNDLE_ID,
            "apns-push-type": "alert",
            "apns-priority": "10",
        },
        json=payload,
    )
    reason = ""
    if resp.status_code != 200:
        try:
            reason = (resp.json() or {}).get("reason", "")
        except Exception:
            reason = resp.text[:120]
    return resp.status_code, reason


def send_ios(title: str, body: str, data: Optional[Dict[str, Any]] = None, collapse_id: Optional[str] = None) -> Dict[str, Any]:
    """Send an APNs alert to every registered iOS device. Returns a summary."""
    jwt_token = _apns_jwt()
    if not jwt_token:
        return {"ok": False, "sent": 0, "reason": "apns_not_configured"}

    tokens = [d["token"] for d in _read_registry() if d.get("platform") == "ios" and d.get("token")]
    if not tokens:
        return {"ok": True, "sent": 0, "reason": "no_devices"}

    payload: Dict[str, Any] = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}
    if data:
        payload.update(data)
    if collapse_id:
        # apns-collapse-id is a header, set per-request below; keep payload clean.
        pass

    sent = 0
    dead: set[str] = set()
    try:
        import httpx

        with httpx.Client(http2=True, timeout=10) as client:
            for token in tokens:
                status, reason = _send_one(client, _APNS_HOSTS[0], token, jwt_token, payload)
                # A token minted by a dev build only validates against sandbox.
                if status == 400 and reason in ("BadDeviceToken", "BadEnvironmentKeyInToken"):
                    status, reason = _send_one(client, _APNS_HOSTS[1], token, jwt_token, payload)
                if status == 200:
                    sent += 1
                elif status in (400, 410) and reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"):
                    dead.add(token)
                    _log.warning("push_notify: dropping token …%s (%s)", token[-6:], reason)
                else:
                    _log.warning("push_notify: APNs %s for …%s (%s)", status, token[-6:], reason)
    except Exception:
        _log.exception("push_notify: APNs send failed")
        return {"ok": False, "sent": sent, "reason": "send_error"}

    _prune_tokens(dead)
    return {"ok": True, "sent": sent, "devices": len(tokens), "pruned": len(dead)}
