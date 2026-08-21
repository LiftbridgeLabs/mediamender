import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from typing import Callable, Optional

from src.maintenance import lease, set_recovery_check
from src.storage import atomic_write_json
from src.branding import PRODUCT_NAME, PRODUCT_SLUG


logger = logging.getLogger(f"{PRODUCT_SLUG}.timestamp_repair")


@dataclass
class AffectedPart:
    library_section_id: str
    metadata_item_id: int
    media_item_id: int
    media_part_id: int
    file_path: str
    stored_timestamp: int
    folder: str
    item_title: str = ""
    parent_title: str = ""
    grandparent_title: str = ""
    filesystem_timestamp: Optional[float] = None
    filesystem_timestamp_iso: str = ""
    path_state: str = "repairable_timestamp"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inside(path: str, prefix: str) -> bool:
    """Lexically contain a library-facing entry without following its symlink."""
    try:
        return os.path.commonpath((os.path.abspath(path), os.path.abspath(prefix))) == os.path.abspath(prefix)
    except (OSError, ValueError):
        return False


def temporary_name(path: str) -> str:
    path_type = PureWindowsPath if "\\" in path else PurePosixPath
    candidate = path_type(path)
    if candidate.suffix and candidate.name != candidate.suffix:
        return str(candidate.with_name(f"{candidate.stem}.plexfix{candidate.suffix}"))
    return str(candidate.with_name(f"{candidate.name}.plexfix"))


def _rename_symlink(source: str, destination: str) -> None:
    os.replace(source, destination)


class TimestampRepairManager:
    def __init__(self, data_dir: str, sleep: Callable[[float], None] = time.sleep,
                 register_recovery_check: bool = True):
        self.root = Path(data_dir) / "timestamp-repair"
        self.active_path = self.root / "active.json"
        self.audit_path = self.root / "audit.json"
        self.history_path = self.root / "history.json"
        self._sleep = sleep
        self._guard = threading.RLock()
        self._cancel = threading.Event()
        self._status = {"running": False, "state": "idle", "last_heartbeat": None}
        if register_recovery_check:
            set_recovery_check(self.has_active_transaction)

    def _read_json(self, path: Path, fallback):
        try:
            import json
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return fallback

    def _write_active(self, transaction: dict) -> None:
        transaction["updated_at"] = _now()
        transaction["last_heartbeat"] = transaction["updated_at"]
        atomic_write_json(str(self.active_path), transaction)
        with self._guard:
            self._status.update({
                "running": True,
                "state": transaction["state"],
                "transaction": transaction,
                "last_heartbeat": transaction["last_heartbeat"],
            })

    def _archive(self, transaction: dict) -> None:
        history = self._read_json(self.history_path, [])
        history.insert(0, transaction)
        atomic_write_json(str(self.history_path), history[:100])
        try:
            self.active_path.unlink()
        except FileNotFoundError:
            pass
        with self._guard:
            self._status.update({"running": False, "state": transaction["state"], "transaction": None})

    def active_transaction(self) -> Optional[dict]:
        active = self._read_json(self.active_path, None)
        if isinstance(active, dict):
            return active
        if self.active_path.exists():
            return {
                "state": "recovery_required", "instance": "unknown",
                "library": "unknown", "folder": str(self.active_path),
                "error": "The active transaction manifest is unreadable or corrupt",
                "manifest_corrupt": True, "renames": [],
            }
        return None

    def has_active_transaction(self, instance_name: str,
                               operation: str = "maintenance") -> bool:
        active = self.active_transaction()
        if not active:
            return False
        if operation != "empty_trash":
            return True
        owner = str(active.get("instance", "")).strip()
        return not owner or owner == "unknown" or owner == instance_name

    @staticmethod
    def _connect(database_path: str) -> sqlite3.Connection:
        uri = Path(database_path).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def detect(self, repair_config, section_id: Optional[str] = None,
               deduplicate: bool = True) -> list[AffectedPart]:
        if not repair_config.enabled:
            raise ValueError("Timestamp repair is disabled")
        if not repair_config.database_path:
            raise ValueError("A read-only Plex database path is required")
        if not repair_config.allowed_prefixes:
            raise ValueError(f"At least one folder {PRODUCT_NAME} may repair is required")
        query = """
            SELECT md.library_section_id, md.id, mi.id, mp.id, mp.file,
                   mp.updated_at, COALESCE(md.title, ''),
                   COALESCE(parent.title, ''), COALESCE(grandparent.title, '')
              FROM media_parts mp
              JOIN media_items mi ON mi.id = mp.media_item_id
              JOIN metadata_items md ON md.id = mi.metadata_item_id
         LEFT JOIN metadata_items parent ON parent.id = md.parent_id
         LEFT JOIN metadata_items grandparent ON grandparent.id = parent.parent_id
             WHERE mp.updated_at < 0
        """
        params = []
        if section_id is not None:
            query += " AND md.library_section_id = ?"
            params.append(int(section_id))
        query += " ORDER BY md.library_section_id, mp.file"
        with closing(self._connect(repair_config.database_path)) as connection:
            rows = connection.execute(query, params).fetchall()
        affected = []
        for row in rows:
            path = str(row[4])
            if not any(_inside(path, prefix) for prefix in repair_config.allowed_prefixes):
                continue
            filesystem_timestamp = None
            filesystem_timestamp_iso = ""
            if not os.path.lexists(path):
                path_state = "missing_path"
            elif not os.path.islink(path):
                path_state = "regular_file"
            elif not os.path.exists(path):
                path_state = "broken_symlink"
            else:
                try:
                    filesystem_timestamp = os.stat(path).st_mtime
                    filesystem_timestamp_iso = datetime.fromtimestamp(
                        filesystem_timestamp, timezone.utc,
                    ).isoformat()
                    path_state = "repairable_timestamp"
                except OSError:
                    path_state = "broken_symlink"
            affected.append(AffectedPart(
                library_section_id=str(row[0]), metadata_item_id=row[1],
                media_item_id=row[2], media_part_id=row[3], file_path=path,
                stored_timestamp=row[5], folder=os.path.dirname(path),
                item_title=row[6], parent_title=row[7], grandparent_title=row[8],
                filesystem_timestamp=filesystem_timestamp,
                filesystem_timestamp_iso=filesystem_timestamp_iso,
                path_state=path_state,
            ))
        if not deduplicate:
            return affected
        return list({part.file_path: part for part in affected}.values())

    def _file_states(self, repair_config, section_id: str,
                     files: set[str]) -> dict[str, list[int]]:
        if not files:
            return {}
        placeholders = ",".join("?" for _ in files)
        with closing(self._connect(repair_config.database_path)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(media_parts)")
            }
            active = " AND mp.deleted_at IS NULL" if "deleted_at" in columns else ""
            rows = connection.execute(
                f"""SELECT mp.file, mp.updated_at
                      FROM media_parts mp
                      JOIN media_items mi ON mi.id = mp.media_item_id
                      JOIN metadata_items md ON md.id = mi.metadata_item_id
                     WHERE md.library_section_id = ?
                       AND mp.file IN ({placeholders}){active}""",
                [int(section_id), *sorted(files)],
            ).fetchall()
        states = {path: [] for path in files}
        for path, timestamp in rows:
            states.setdefault(path, []).append(int(timestamp or 0))
        return states

    def audit(self, instance, repair_config, plex=None) -> dict:
        rows = self.detect(repair_config, deduplicate=False)
        all_parts = list({part.file_path: part for part in rows}.values())
        parts = [
            part for part in all_parts
            if part.path_state == "repairable_timestamp"
        ]
        libraries = {}
        for library in instance.libraries:
            section_id = library.section_id
            if not section_id and plex is not None:
                section_id = plex.find_section_id(library.name)
            if section_id:
                libraries[str(section_id)] = library.name
        groups = {}
        for part in parts:
            key = (part.library_section_id, part.folder)
            group = groups.setdefault(key, {
                "instance": instance.name,
                "library_section_id": part.library_section_id,
                "library": libraries.get(part.library_section_id, f"Section {part.library_section_id}"),
                "folder": part.folder,
                "title": part.grandparent_title or part.parent_title or part.item_title or Path(part.folder).name,
                "subtitle": part.parent_title if part.grandparent_title else "",
                "files": [],
            })
            group["files"].append(asdict(part))
        folders = sorted(groups.values(), key=lambda item: (len(item["files"]), item["library"], item["folder"]))
        result = {
            "audited_at": _now(), "instance": instance.name,
            "database_path": repair_config.database_path,
            "allowed_prefixes": list(repair_config.allowed_prefixes),
            "negative_rows": len(rows), "distinct_files": len(parts),
            "database_distinct_files": len(all_parts),
            "affected_folders": len(folders), "folders": folders,
            "path_state_counts": {
                state: sum(part.path_state == state for part in all_parts)
                for state in (
                    "repairable_timestamp", "missing_path",
                    "broken_symlink", "regular_file",
                )
            },
            "path_issues": [
                asdict(part) for part in all_parts
                if part.path_state != "repairable_timestamp"
            ],
            "libraries": [
                {"library_section_id": section_id, "library": name}
                for section_id, name in libraries.items()
            ],
        }
        self.save_audit(result)
        return result

    def save_audit(self, result: dict) -> None:
        """Persist a controller-local copy of a local or worker audit."""
        audits = self._read_json(self.audit_path, {})
        if not isinstance(audits, dict):
            audits = {}
        audits[str(result.get("instance", ""))] = result
        atomic_write_json(str(self.audit_path), audits)

    def merge_history(self, entries: list[dict]) -> None:
        """Import worker transaction summaries without duplicating IDs."""
        history = self._read_json(self.history_path, [])
        combined = [*entries, *history]
        seen = set()
        unique = []
        for item in combined:
            key = item.get("transaction_id") or (
                item.get("instance"), item.get("folder"), item.get("completed_at")
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        atomic_write_json(str(self.history_path), unique[:100])

    def status(self) -> dict:
        with self._guard:
            runtime = dict(self._status)
        history = self._read_json(self.history_path, [])
        audits = self._read_json(self.audit_path, {})
        for audit in audits.values() if isinstance(audits, dict) else []:
            database_path = str(audit.get("database_path", ""))
            allowed_prefixes = list(audit.get("allowed_prefixes", []))
            if not database_path or not allowed_prefixes:
                continue
            try:
                live_parts = list({
                    part.file_path: part
                    for part in self.detect(SimpleNamespace(
                        enabled=True,
                        database_path=database_path,
                        allowed_prefixes=allowed_prefixes,
                    ), deduplicate=False)
                }.values())
                live_count = len(live_parts)
                audit["live_database_distinct_files"] = live_count
                audit["database_count_changed"] = (
                    live_count != int(audit.get("database_distinct_files", live_count))
                )
            except (OSError, sqlite3.Error, ValueError):
                pass
        repair_totals = {}
        for item in history:
            if item.get("state") != "completed":
                continue
            instance = str(item.get("instance", ""))
            repair_totals[instance] = repair_totals.get(instance, 0) + len(
                item.get("renames", [])
            )
        return {
            **runtime,
            "active_transaction": self.active_transaction(),
            "audits": audits,
            "history": history[:20],
            "repair_totals": repair_totals,
        }

    def audited_folder(self, instance_name: str, section_id: str, folder: str) -> bool:
        audit = self._read_json(self.audit_path, {}).get(instance_name, {})
        return any(
            item.get("library_section_id") == str(section_id)
            and item.get("folder") == folder
            for item in audit.get("folders", [])
        )

    def audited_files(self, instance_name: str, section_id: str,
                      folder: str) -> set[str]:
        audit = self._read_json(self.audit_path, {}).get(instance_name, {})
        match = next((
            item for item in audit.get("folders", [])
            if item.get("library_section_id") == str(section_id)
            and item.get("folder") == folder
        ), None)
        return {
            str(item.get("file_path", "")) for item in (match or {}).get("files", [])
            if item.get("file_path")
        }

    def complete_audited_folder(self, instance_name: str, section_id: str,
                                folder: str) -> None:
        """Remove one verified folder while preserving the remaining review."""
        audits = self._read_json(self.audit_path, {})
        audit = audits.get(instance_name, {}) if isinstance(audits, dict) else {}
        folders = list(audit.get("folders", []))
        removed = next((
            item for item in folders
            if item.get("library_section_id") == str(section_id)
            and item.get("folder") == folder
        ), None)
        if not removed:
            return
        removed_files = len(removed.get("files", []))
        audit["folders"] = [item for item in folders if item is not removed]
        audit["affected_folders"] = len(audit["folders"])
        audit["distinct_files"] = sum(
            len(item.get("files", [])) for item in audit["folders"]
        )
        audit["database_distinct_files"] = max(
            0, int(audit.get("database_distinct_files", 0)) - removed_files,
        )
        states = audit.get("path_state_counts", {})
        states["repairable_timestamp"] = audit["distinct_files"]
        audit["database_count_changed"] = False
        audit["live_database_distinct_files"] = audit["database_distinct_files"]
        atomic_write_json(str(self.audit_path), audits)

    def _validate_file(self, path: str, expected_prefixes: list[str]) -> dict:
        if not any(_inside(path, prefix) for prefix in expected_prefixes):
            raise ValueError(f"Path is outside the configured repair prefixes: {path}")
        if ".plexfix" in Path(path).name:
            raise ValueError(f"Refusing an already temporary filename: {path}")
        if not os.path.islink(path):
            raise ValueError(f"Affected path is not a symlink: {path}")
        target = os.readlink(path)
        resolved_target = os.path.realpath(path)
        if not os.path.exists(path) or not os.access(resolved_target, os.R_OK):
            raise ValueError(f"Symlink target is unavailable or unreadable: {path}")
        mtime = os.stat(resolved_target).st_mtime
        if mtime <= 0:
            raise ValueError(f"Symlink target does not have a positive modification time: {path}")
        temporary = temporary_name(path)
        if os.path.lexists(temporary):
            raise ValueError(f"Temporary filename already exists: {temporary}")
        return {"original": path, "temporary": temporary, "target": target, "resolved_target": resolved_target, "mtime": mtime}

    def _restore(self, transaction: dict) -> bool:
        transaction["state"] = "restoring"
        self._write_active(transaction)
        ambiguous = []
        for rename in transaction["renames"]:
            original_exists = os.path.lexists(rename["original"])
            temporary_exists = os.path.lexists(rename["temporary"])
            if not original_exists and temporary_exists:
                _rename_symlink(rename["temporary"], rename["original"])
            elif original_exists and not temporary_exists:
                pass
            else:
                ambiguous.append(rename["original"])
        if ambiguous:
            transaction["state"] = "recovery_required"
            transaction["error"] = "Ambiguous restore state: " + ", ".join(ambiguous)
            self._write_active(transaction)
            return False
        for rename in transaction["renames"]:
            if not os.path.islink(rename["original"]):
                ambiguous.append(rename["original"])
            elif os.readlink(rename["original"]) != rename["target"]:
                ambiguous.append(rename["original"])
        if ambiguous:
            transaction["state"] = "recovery_required"
            transaction["error"] = "Restored symlink target mismatch: " + ", ".join(ambiguous)
            self._write_active(transaction)
            return False
        transaction["state"] = "restored"
        self._write_active(transaction)
        return True

    def recover(self, instance_name: Optional[str] = None) -> dict:
        transaction = self.active_transaction()
        if not transaction:
            return {"ok": True, "message": "No recovery is required"}
        if instance_name and transaction.get("instance") != instance_name:
            return {"ok": False, "error": "The active transaction belongs to another Plex instance"}
        if transaction.get("manifest_corrupt"):
            return {"ok": False, "error": transaction["error"]}
        with lease(
            transaction["instance"],
            allow_recovery=True,
            operation="timestamp_recovery",
        ) as (acquired, reason):
            if not acquired:
                return {"ok": False, "error": reason}
            if not self._restore(transaction):
                return {"ok": False, "error": transaction.get("error", "Recovery requires operator review")}
            transaction["state"] = "recovered"
            transaction["completed_at"] = _now()
            self._archive(transaction)
            return {"ok": True, "message": "Temporary names were restored"}

    def cancel(self) -> None:
        self._cancel.set()

    def _failure(self, error: str) -> dict:
        with self._guard:
            self._status.update({
                "running": False, "state": "failed", "error": error,
                "last_heartbeat": _now(),
            })
        return {"ok": False, "error": error}

    def _heartbeat(self, transaction: dict, state: str, started: float) -> None:
        transaction["state"] = state
        transaction["scan_elapsed_seconds"] = int(time.monotonic() - started)
        self._write_active(transaction)
        logger.info("[%s / %s] %s; folder=%s elapsed=%ss transaction=%s",
                    transaction["instance"], transaction["library"], state,
                    transaction["folder"], transaction["scan_elapsed_seconds"],
                    transaction["transaction_id"])

    def _wait_for(self, transaction: dict, repair_config, predicate: Callable[[], bool], state: str) -> bool:
        started = time.monotonic()
        next_heartbeat = 0.0
        while time.monotonic() - started <= repair_config.scan_timeout_seconds:
            if predicate():
                return True
            if self._cancel.is_set():
                raise RuntimeError("Repair cancelled safely")
            elapsed = time.monotonic() - started
            if elapsed >= next_heartbeat:
                self._heartbeat(transaction, state, started)
                next_heartbeat = elapsed + repair_config.heartbeat_seconds
            self._sleep(repair_config.poll_interval_seconds)
        return False

    def run_folder(self, instance, library, repair_config, plex, folder: str,
                   section_id: Optional[str] = None,
                   preflight: Optional[Callable[[], dict]] = None,
                   expected_files: Optional[set[str]] = None,
                   batch_position: str = "1/1") -> dict:
        self._cancel.clear()
        resolved_section = str(section_id or library.section_id or "")
        if not resolved_section:
            return self._failure("Plex library section is unavailable")
        with lease(instance.name, operation="timestamp_repair") as (acquired, reason):
            if not acquired:
                logger.warning(
                    "[%s / %s] Timestamp repair blocked: %s",
                    instance.name, library.name, reason,
                )
                return self._failure(reason)
            transaction = None
            manifest_persisted = False
            try:
                if preflight:
                    failed = {
                        name: result for name, result in preflight().items()
                        if not result.get("pass")
                    }
                    if failed:
                        raise RuntimeError(
                            "Safety checks failed: " + ", ".join(failed)
                        )
                current = [
                    part for part in self.detect(repair_config, resolved_section)
                    if part.folder == folder
                    and part.path_state == "repairable_timestamp"
                ]
                if not current:
                    return self._failure("This folder no longer has negative timestamps")
                current_files = {part.file_path for part in current}
                if expected_files is not None and current_files != expected_files:
                    return self._failure("Affected files changed after the audit; run a fresh audit and review again")
                if len(current) > repair_config.max_files_per_folder:
                    return self._failure(f"Folder has {len(current)} affected files; configured limit is {repair_config.max_files_per_folder}")
                renames = [self._validate_file(part.file_path, repair_config.allowed_prefixes) for part in current]
                transaction = {
                    "transaction_id": str(uuid.uuid4()), "instance": instance.name,
                    "library": library.name, "library_section_id": resolved_section,
                    "folder": folder, "state": "prepared", "created_at": _now(),
                    "batch_position": batch_position, "renames": renames,
                    "timestamp_changes": [
                        {
                            "file_path": part.file_path,
                            "before": int(part.stored_timestamp),
                            "after": None,
                        }
                        for part in current
                    ],
                }
                self._write_active(transaction)
                manifest_persisted = True
                for rename in renames:
                    _rename_symlink(rename["original"], rename["temporary"])
                transaction["state"] = "renamed"
                self._write_active(transaction)
                transaction["state"] = "first_plex_scan"
                self._write_active(transaction)
                scan = plex.scan_path(resolved_section, folder)
                if not scan["ok"]:
                    raise RuntimeError(f"First folder scan failed with HTTP {scan.get('http')}")
                originals = {rename["original"] for rename in renames}
                first_done = self._wait_for(
                    transaction, repair_config,
                    lambda: all(
                        not timestamps
                        for timestamps in self._file_states(
                            repair_config, resolved_section, originals,
                        ).values()
                    ),
                    "waiting_for_first_scan",
                )
                if not first_done:
                    raise RuntimeError("First folder scan timed out")
                if not self._restore(transaction):
                    raise RuntimeError(transaction.get("error", "Automatic restore was incomplete"))
                transaction["state"] = "second_plex_scan"
                self._write_active(transaction)
                scan = plex.scan_path(resolved_section, folder)
                if not scan["ok"]:
                    raise RuntimeError(f"Second folder scan failed with HTTP {scan.get('http')}")
                verified = self._wait_for(
                    transaction, repair_config,
                    lambda: all(
                        timestamps and min(timestamps) > 0
                        for timestamps in self._file_states(
                            repair_config, resolved_section, originals,
                        ).values()
                    ),
                    "verifying",
                )
                if not verified:
                    raise RuntimeError("Folder verification timed out")
                verified_states = self._file_states(
                    repair_config, resolved_section, originals,
                )
                for change in transaction["timestamp_changes"]:
                    timestamps = verified_states.get(change["file_path"], [])
                    change["after"] = min(timestamps) if timestamps else None
                transaction["state"] = "completed"
                transaction["completed_at"] = _now()
                self._archive(transaction)
                return {"ok": True, "transaction_id": transaction["transaction_id"], "files": len(renames)}
            except Exception as exc:
                if transaction and manifest_persisted:
                    restored = self._restore(transaction)
                    if restored and transaction.get("state") == "restored":
                        # Reconcile Plex after cancellation, timeout, or any failure
                        # that occurred while the original filename was absent.
                        plex.scan_path(resolved_section, transaction["folder"])
                    transaction["error"] = str(exc)
                    if transaction["state"] != "recovery_required":
                        transaction["state"] = "failed"
                        self._archive(transaction)
                logger.error("[%s / %s] Timestamp repair failed (%s)", instance.name, library.name, type(exc).__name__)
                return self._failure(str(exc))
