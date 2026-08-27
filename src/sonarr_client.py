"""Sonarr API client and non-secret webhook connection status storage."""

from __future__ import annotations

import copy
import ipaddress
import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

from src.storage import atomic_write_json


WEBHOOK_NAME = "mediaMender - Mark-it-Watched"
WEBHOOK_PATH = "/api/webhooks/sonarr"


class SonarrError(RuntimeError):
    """A safe-to-display Sonarr connection or API error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_http_url(value: str, label: str) -> tuple[bool, str]:
    try:
        parsed = urlparse(value)
    except Exception:
        return False, f"Invalid {label} URL"
    if parsed.scheme not in {"http", "https"}:
        return False, f"{label} URL must use http or https"
    if not parsed.hostname:
        return False, f"{label} URL must include a hostname"
    if parsed.username or parsed.password:
        return False, f"Credentials must not be embedded in the {label} URL"
    if parsed.query or parsed.fragment:
        return False, f"{label} URL cannot include a query or fragment"
    host = parsed.hostname.lower()
    if host in {"169.254.169.254", "metadata.google.internal", "fd00:ec2::254"}:
        return False, f"{label} URL targets a cloud metadata address"
    try:
        address = ipaddress.ip_address(host)
        if (address.is_link_local or address.is_multicast or
                address.is_unspecified or address.is_reserved):
            return False, f"{label} URL targets a non-routable or reserved address"
    except ValueError:
        pass
    return True, ""


def normalize_sonarr_url(value: str) -> str:
    """Validate a Sonarr base URL while retaining an optional URL base path."""
    value = str(value or "").strip().rstrip("/")
    ok, reason = _validate_http_url(value, "Sonarr")
    if not ok:
        raise ValueError(reason)
    return value


def normalize_callback_url(value: str) -> str:
    """Validate a mediaMender URL and append the fixed webhook path if omitted."""
    value = str(value or "").strip().rstrip("/")
    ok, reason = _validate_http_url(value, "Callback")
    if not ok:
        raise ValueError(reason)
    parsed = urlparse(value)
    path = parsed.path.rstrip("/")
    if not path:
        path = WEBHOOK_PATH
    elif path != WEBHOOK_PATH:
        raise ValueError(f"Callback URL path must be {WEBHOOK_PATH}")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


class SonarrClient:
    """Provision mediaMender's webhook using Sonarr's advertised schema."""

    def __init__(self, url: str, api_key: str, *, session=None, timeout: int = 15):
        self.url = normalize_sonarr_url(url)
        if not str(api_key or "").strip():
            raise ValueError("Sonarr API key is required")
        self.timeout = timeout
        self._api_key = str(api_key).strip()
        self.session = session or requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "X-Api-Key": self._api_key,
        })

    @staticmethod
    def _error_detail(response) -> str:
        """Extract Sonarr's useful validation message without echoing values."""
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return ""
        entries = payload if isinstance(payload, list) else [payload]
        messages = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            message = entry.get("errorMessage") or entry.get("message") or entry.get("detail")
            if message:
                cleaned = " ".join(str(message).split())
                if cleaned and cleaned not in messages:
                    messages.append(cleaned[:300])
        return "; ".join(messages[:3])

    def _request(self, method: str, path: str, payload=None, *, redactions=()):
        try:
            response = self.session.request(
                method,
                f"{self.url}/api/v3{path}",
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SonarrError("Could not reach Sonarr") from exc
        if not response.ok:
            detail = self._error_detail(response)
            for sensitive in (self._api_key, *redactions):
                if sensitive:
                    detail = detail.replace(str(sensitive), "[redacted]")
            raise SonarrError(
                f"Sonarr returned HTTP {response.status_code} for {method} {path}"
                + (f": {detail}" if detail else "")
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise SonarrError(f"Sonarr returned invalid JSON for {method} {path}") from exc

    def system_status(self) -> dict:
        result = self._request("GET", "/system/status")
        if not isinstance(result, dict):
            raise SonarrError("Sonarr returned an invalid system status")
        return result

    def _webhook_schema(self) -> dict:
        schemas = self._request("GET", "/notification/schema")
        if not isinstance(schemas, list):
            raise SonarrError("Sonarr returned an invalid notification schema")
        for schema in schemas:
            if isinstance(schema, dict) and str(schema.get("implementation", "")).lower() == "webhook":
                return copy.deepcopy(schema)
        raise SonarrError("This Sonarr version did not advertise Webhook connections")

    @staticmethod
    def _set_field(payload: dict, name: str, value) -> None:
        for field in payload.get("fields", []):
            if isinstance(field, dict) and str(field.get("name", "")).lower() == name.lower():
                field["value"] = value
                return
        raise SonarrError(f"Sonarr's Webhook schema is missing the {name} field")

    def _payload(self, schema: dict, callback_url: str, secret: str,
                 connection_id: str = "") -> dict:
        payload = copy.deepcopy(schema)
        payload.pop("id", None)
        payload.pop("presets", None)
        payload["name"] = WEBHOOK_NAME
        payload["tags"] = []
        # Sonarr's Download event is emitted after each episode file import. The
        # newer Import Complete event is aggregate and would duplicate work.
        for name in (
            "onGrab", "onRename", "onSeriesAdd", "onSeriesDelete",
            "onEpisodeFileDelete", "onEpisodeFileDeleteForUpgrade",
            "onHealthIssue", "onHealthRestored", "onApplicationUpdate",
            "onManualInteractionRequired", "onImportComplete",
        ):
            if name in payload:
                payload[name] = False
        payload["onDownload"] = True
        if "onUpgrade" in payload:
            payload["onUpgrade"] = True
        self._set_field(payload, "url", callback_url)
        # Keep Sonarr's advertised Webhook method default, which its schema
        # initializes to POST, instead of assuming an enum value.
        headers = [
            {"key": "X-Sonarr-Webhook-Secret", "value": secret},
        ]
        if connection_id:
            headers.append({
                "key": "X-MediaMender-Connection-ID", "value": connection_id,
            })
        self._set_field(payload, "headers", headers)
        return payload

    def _find_managed_notification(self) -> dict | None:
        notifications = self._request("GET", "/notification")
        if not isinstance(notifications, list):
            raise SonarrError("Sonarr returned an invalid notification list")
        return next((
            item for item in notifications
            if isinstance(item, dict)
            and item.get("name") == WEBHOOK_NAME
            and str(item.get("implementation", "")).lower() == "webhook"
        ), None)

    def provision_webhook(self, callback_url: str, secret: str,
                          *, status: dict | None = None,
                          connection_id: str = "") -> dict:
        callback_url = normalize_callback_url(callback_url)
        status = status or self.system_status()
        payload = self._payload(
            self._webhook_schema(), callback_url, secret, connection_id,
        )

        # Look for the existing connection before testing, not after. Sonarr
        # validates the test payload as if it were being saved, and its Name
        # uniqueness rule only excludes the record carrying the same Id — so
        # testing an unidentified payload named the same as a connection that
        # already exists fails with "Should be unique". That made the very
        # first connect succeed and every later one fail permanently, because
        # the test raised before this lookup could supply the Id.
        existing = self._find_managed_notification()
        if existing and isinstance(existing.get("id"), int):
            payload["id"] = existing["id"]

        # Test before changing Sonarr's saved connections. This invokes the same
        # callback and secret header that the saved connection will use.
        self._request(
            "POST", "/notification/test", payload, redactions=(secret,),
        )
        if "id" in payload:
            saved = self._request(
                "PUT", f"/notification/{payload['id']}", payload,
            )
            action = "updated"
        else:
            saved = self._request("POST", "/notification", payload)
            action = "created"
        saved = saved if isinstance(saved, dict) else {}
        return {
            "action": action,
            "notification_id": saved.get("id", payload.get("id")),
            "sonarr_version": str(status.get("version", "")),
            "sonarr_instance": str(status.get("instanceName", "Sonarr")),
            "callback_url": callback_url,
        }

    def remove_webhook(self) -> int:
        """Delete mediaMender-managed webhook connections from this Sonarr."""
        notifications = self._request("GET", "/notification")
        if not isinstance(notifications, list):
            raise SonarrError("Sonarr returned an invalid notification list")
        matches = [
            item for item in notifications
            if isinstance(item, dict)
            and item.get("name") == WEBHOOK_NAME
            and str(item.get("implementation", "")).lower() == "webhook"
            and isinstance(item.get("id"), int)
        ]
        for item in matches:
            self._request("DELETE", f"/notification/{item['id']}")
        return len(matches)


class SonarrConnectionStore:
    """Persist useful connection status without retaining Sonarr credentials."""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "sonarr-webhook.json"
        self._lock = threading.RLock()

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return {}
            connections = value.get("connections")
            if isinstance(connections, dict):
                return connections
            # Migrate the original single-connection status file in memory.
            if value.get("sonarr_url"):
                return {str(value["sonarr_url"]): value}
            return {}
        except (OSError, ValueError):
            return {}

    def _save(self, connections: dict) -> None:
        atomic_write_json(str(self.path), {"connections": connections})

    def status(self) -> dict:
        with self._lock:
            records = [
                {key: value for key, value in record.items()
                 if key != "connection_id"}
                for record in self._load().values() if isinstance(record, dict)
            ]
        records.sort(
            key=lambda item: str(item.get("last_success") or item.get("last_attempt") or ""),
            reverse=True,
        )
        return {"connections": records}

    def get(self, sonarr_url: str) -> dict | None:
        with self._lock:
            value = self._load().get(str(sonarr_url))
            return dict(value) if isinstance(value, dict) else None

    def remove(self, sonarr_url: str) -> bool:
        with self._lock:
            connections = self._load()
            removed = connections.pop(str(sonarr_url), None) is not None
            if removed:
                self._save(connections)
            return removed

    def prepare(self, sonarr_url: str, owner: str) -> dict:
        with self._lock:
            connections = self._load()
            value = dict(connections.get(sonarr_url, {}))
            value.update({
                "status": "configuring",
                "sonarr_url": sonarr_url,
                "owner": owner,
                "connection_id": value.get("connection_id") or secrets.token_urlsafe(18),
                "last_attempt": _utc_now(),
            })
            connections[sonarr_url] = value
            self._save(connections)
            return dict(value)

    def owner_for(self, connection_id: str) -> str:
        if not connection_id:
            return ""
        with self._lock:
            for value in self._load().values():
                if value.get("connection_id") == connection_id:
                    return str(value.get("owner", ""))
        return ""

    def success(self, sonarr_url: str, result: dict, *, owner: str = "",
                connection_id: str = "") -> dict:
        with self._lock:
            connections = self._load()
            value = dict(connections.get(sonarr_url, {}))
            value.update({
                "status": "connected",
                "sonarr_url": sonarr_url,
                "callback_url": result.get("callback_url", ""),
                "notification_id": result.get("notification_id"),
                "sonarr_version": result.get("sonarr_version", ""),
                "sonarr_instance": result.get("sonarr_instance", "Sonarr"),
                "action": result.get("action", "created"),
                "owner": owner or value.get("owner", ""),
                "connection_id": connection_id or value.get("connection_id", ""),
                "last_success": _utc_now(),
            })
            connections[sonarr_url] = value
            self._save(connections)
            return dict(value)

    def failure(self, sonarr_url: str, callback_url: str, error: str) -> dict:
        with self._lock:
            connections = self._load()
            value = dict(connections.get(sonarr_url, {}))
            value.update({
                "status": "failed",
                "sonarr_url": sonarr_url,
                "callback_url": callback_url,
                "error": str(error)[:300],
                "last_attempt": _utc_now(),
            })
            connections[sonarr_url] = value
            self._save(connections)
            return dict(value)
