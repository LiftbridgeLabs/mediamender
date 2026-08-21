import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlencode

import requests

from src.plex_client import PlexClient
from src.storage import atomic_write_json
from src.version import __version__
from src.branding import PRODUCT_NAME, PRODUCT_SLUG


PLEX_PRODUCT = PRODUCT_NAME
PLEX_VERSION = __version__
PLEX_PLATFORM = "Web"
_PIN_TTL_SECONDS = 15 * 60
_sessions: Dict[str, dict] = {}
_lock = threading.Lock()


class PlexAuthError(RuntimeError):
    """A user-safe Plex authorization failure with no request URL or PIN code."""


def _raise_service_error(action: str, exc: requests.RequestException) -> None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        raise PlexAuthError(
            f"Plex authorization service returned HTTP {status} while {action}"
        ) from None
    raise PlexAuthError(
        f"Could not reach Plex authorization service while {action}"
    ) from None


def _retry_after_seconds(response) -> int:
    try:
        value = int(response.headers.get("Retry-After", "10"))
    except (TypeError, ValueError):
        value = 10
    return max(3, min(value, 60))


def _client_id_path() -> Path:
    return Path(os.environ.get("PLEX_CLIENT_ID_FILE", "data/plex-client.json"))


def get_client_identifier() -> str:
    configured = os.environ.get("PLEX_CLIENT_IDENTIFIER", "").strip()
    if configured:
        return configured
    path = _client_id_path()
    try:
        value = __import__("json").loads(path.read_text(encoding="utf-8"))
        client_id = str(value.get("client_identifier", "")).strip()
        if client_id:
            return client_id
    except (OSError, ValueError, TypeError):
        pass
    client_id = uuid.uuid4().hex
    atomic_write_json(str(path), {"client_identifier": client_id})
    return client_id


def _headers(token: str = "") -> dict:
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": PLEX_PRODUCT,
        "X-Plex-Version": PLEX_VERSION,
        "X-Plex-Platform": PLEX_PLATFORM,
        "X-Plex-Client-Identifier": get_client_identifier(),
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


def _prune() -> None:
    cutoff = time.time() - _PIN_TTL_SECONDS
    with _lock:
        for state in [key for key, value in _sessions.items() if value["created_at"] < cutoff]:
            _sessions.pop(state, None)


def start_auth() -> dict:
    _prune()
    try:
        response = requests.post(
            "https://plex.tv/api/v2/pins",
            headers=_headers(),
            data={"strong": "true"},
            timeout=15,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        _raise_service_error("starting sign-in", exc)
    pin = response.json()
    state = secrets.token_urlsafe(24)
    with _lock:
        _sessions[state] = {
            "created_at": time.time(),
            "pin_id": int(pin["id"]),
            "code": str(pin["code"]),
        }
    query = urlencode({
        "clientID": get_client_identifier(),
        "code": pin["code"],
        "context[device][product]": PLEX_PRODUCT,
    })
    return {
        "state": state,
        "auth_url": f"https://app.plex.tv/auth#?{query}",
        "expires_in": _PIN_TTL_SECONDS,
    }


def _connections(device: dict) -> List[dict]:
    connections = device.get("connections", device.get("Connection", [])) or []
    if isinstance(connections, dict):
        connections = [connections]
    normalized = []
    def as_bool(value) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes"}
        return bool(value)

    for connection in connections:
        uri = str(connection.get("uri", "")).rstrip("/")
        if not uri.startswith(("http://", "https://")):
            continue
        normalized.append({
            "uri": uri,
            "local": as_bool(connection.get("local", False)),
            "relay": as_bool(connection.get("relay", False)),
            "protocol": connection.get("protocol", ""),
        })
    return sorted(
        normalized,
        key=lambda c: (
            c["relay"],
            not c["local"],
            c["protocol"] != "https",
        ),
    )


def _resources(account_token: str) -> List[dict]:
    try:
        response = requests.get(
            "https://plex.tv/api/v2/resources",
            headers=_headers(account_token),
            params={"includeHttps": 1, "includeRelay": 1, "includeIPv6": 1},
            timeout=20,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        _raise_service_error("discovering servers", exc)
    payload = response.json()
    devices = payload if isinstance(payload, list) else payload.get("MediaContainer", {}).get("Device", [])
    servers = []
    for device in devices or []:
        provides = str(device.get("provides", ""))
        if device.get("product") != "Plex Media Server" and "server" not in provides.split(","):
            continue
        token = device.get("accessToken") or account_token
        connections = _connections(device)
        servers.append({
            "id": str(device.get("clientIdentifier", "")),
            "name": str(device.get("name", "Plex")),
            "owned": str(device.get("owned", "")).lower() in {"1", "true"},
            "token": token,
            "connections": connections,
        })
    return servers


def _discover_server(server: dict) -> dict:
    errors = []
    for connection in server["connections"]:
        plex = PlexClient(connection["uri"], server["token"])
        reachable = plex.check_reachable()
        if not reachable["pass"]:
            errors.append(reachable["detail"])
            continue
        try:
            libraries = plex.get_sections()
            return {
                "id": server["id"],
                "name": server["name"],
                "owned": server["owned"],
                "url": connection["uri"],
                "token": server["token"],
                "libraries": libraries,
            }
        except Exception as exc:
            errors.append(str(exc))
    return {
        "id": server["id"],
        "name": server["name"],
        "owned": server["owned"],
        "url": server["connections"][0]["uri"] if server["connections"] else "",
        "token": server["token"],
        "libraries": [],
        "error": "; ".join(errors[-3:]) or "No usable server connection advertised by Plex",
    }


def poll_auth(state: str) -> dict:
    _prune()
    with _lock:
        pending = dict(_sessions.get(state, {}))
    if not pending:
        return {"ok": False, "error": "Authorization request expired or was not found"}
    try:
        response = requests.get(
            f"https://plex.tv/api/v2/pins/{pending['pin_id']}",
            headers=_headers(),
            params={"code": pending["code"]},
            timeout=15,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        if getattr(exc.response, "status_code", None) == 429:
            return {
                "ok": True,
                "pending": True,
                "retry_after": _retry_after_seconds(exc.response),
            }
        _raise_service_error("checking sign-in", exc)
    except requests.RequestException as exc:
        _raise_service_error("checking sign-in", exc)
    account_token = response.json().get("authToken")
    if not account_token:
        return {"ok": True, "pending": True}

    with _lock:
        cached = _sessions.get(state, {}).get("servers")
    if cached is None:
        servers = [_discover_server(server) for server in _resources(account_token)]
        with _lock:
            if state in _sessions:
                _sessions[state]["servers"] = servers
    else:
        servers = cached
    return {"ok": True, "pending": False, "servers": servers}


def cancel_auth(state: str) -> bool:
    """Forget a pending browser authorization attempt."""
    with _lock:
        return _sessions.pop(state, None) is not None
