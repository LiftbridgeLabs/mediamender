import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.maintenance import lease
from src.storage import atomic_write_json
from src.branding import PRODUCT_SLUG


logger = logging.getLogger(f"{PRODUCT_SLUG}.library_refresh")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


class LibraryRefreshManager:
    """Persist Plex library refresh requests and post-request safety holds."""

    def __init__(self, data_dir: str = "data"):
        self.path = Path(data_dir) / "library-refresh.json"
        self._lock = threading.RLock()
        self._running: dict = {}
        self._holds: dict[str, datetime] = {}

    def _read(self) -> dict:
        try:
            import json
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, value: dict) -> None:
        atomic_write_json(str(self.path), value)

    @staticmethod
    def key(instance_name: str, library_name: str) -> str:
        return f"{instance_name}::{library_name}"

    def status(self) -> dict:
        with self._lock:
            saved = self._read()
            return {
                "records": saved.get("records", {}),
                "history": saved.get("history", [])[:100],
                "running": dict(self._running),
            }

    def trash_hold_reason(self, instance_name: str,
                          library_name: str) -> str | None:
        key = self.key(instance_name, library_name)
        with self._lock:
            until = self._holds.get(key)
            if until is None:
                record = self._read().get("records", {}).get(key, {})
                raw_until = record.get("trash_hold_until")
                if not raw_until:
                    return None
                try:
                    until = datetime.fromisoformat(
                        str(raw_until).replace("Z", "+00:00")
                    )
                except ValueError:
                    return None
        remaining = int((until - _now()).total_seconds())
        if remaining <= 0:
            return None
        minutes = max(1, (remaining + 59) // 60)
        return f"Plex library refresh hold is active ({minutes}m remaining)"

    def run(self, instance, library, plex, source: str = "manual") -> dict:
        key = self.key(instance.name, library.name)
        with self._lock:
            if key in self._running:
                return {"ok": False, "error": "This library refresh is already running"}
            self._running[key] = {
                "instance": instance.name,
                "library": library.name,
                "source": source,
                "started_at": _iso(),
            }
        result = {"ok": False, "error": "Refresh request did not run"}
        try:
            section_id = library.section_id or plex.find_section_id(library.name)
            if not section_id:
                result = {"ok": False, "error": "Plex library section was not found"}
            else:
                with lease(instance.name, operation="library_refresh") as (acquired, reason):
                    if not acquired:
                        result = {"ok": False, "error": f"Plex maintenance busy — {reason}"}
                    else:
                        result = plex.refresh_section(str(section_id))
        except Exception as exc:
            logger.error("[%s / %s] Library refresh failed (%s)",
                         instance.name, library.name, type(exc).__name__)
            result = {"ok": False, "error": type(exc).__name__}

        completed_at = _now()
        record = {
            "instance": instance.name,
            "library": library.name,
            "source": source,
            "requested_at": _iso(completed_at),
            "status": "accepted" if result.get("ok") else "failed",
            "http": result.get("http"),
            "error": result.get("error", ""),
        }
        if result.get("ok"):
            guard_minutes = max(1, int(library.refresh_guard_minutes))
            hold_until = completed_at + timedelta(minutes=guard_minutes)
            record["trash_hold_until"] = _iso(hold_until)
            with self._lock:
                self._holds[key] = hold_until
            logger.info("[%s / %s] Plex accepted library refresh request; "
                        "Empty Trash held for %s minutes",
                        instance.name, library.name, guard_minutes)
        else:
            logger.warning("[%s / %s] Plex library refresh request failed (%s)",
                           instance.name, library.name,
                           result.get("http") or result.get("error", "unknown"))
        try:
            with self._lock:
                saved = self._read()
                records = saved.setdefault("records", {})
                history = saved.setdefault("history", [])
                records[key] = record
                history.insert(0, record)
                saved["history"] = history[:100]
                self._write(saved)
        finally:
            with self._lock:
                self._running.pop(key, None)
        return {**result, **record}
