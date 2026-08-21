import json
import logging
import os
import threading
import time
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional

from src.config import AppConfig, LibraryConfig, PathConfig, PlexInstanceConfig
from src.plex_client import PlexClient, trash_item_key
from src.checks import check_mountpoint, check_debrid_mount, check_file_threshold, count_files
from src.providers import check_provider
from src import notifications
from src.storage import atomic_write_json
from src.maintenance import lease
from src.branding import PRODUCT_SLUG

logger = logging.getLogger(PRODUCT_SLUG)

MAX_HISTORY      = 100
_history: List[Dict]          = []
_library_refresh_guard = None


def set_library_refresh_guard(check) -> None:
    global _library_refresh_guard
    _library_refresh_guard = check
_instance_status: Dict        = {}   # instance_name -> {library_name -> status}
_last_global_checks: Dict     = {}   # instance_name -> {check_name -> result}
_scheduling_enabled: bool     = True
_lock = threading.Lock()
_run_locks: Dict[str, threading.Lock] = {}

_STATE_FILE = os.environ.get("STATE_FILE", "data/state.json")
TRASH_SNAPSHOT_RETRY_DELAY = 1
TRASH_SNAPSHOT_MAX_RETRIES = 2


def _load_state():
    global _scheduling_enabled
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE) as f:
                state = json.load(f)
                _scheduling_enabled = state.get("scheduling_enabled", True)
    except Exception:
        pass


def _save_state():
    try:
        atomic_write_json(_STATE_FILE, {"scheduling_enabled": _scheduling_enabled})
    except Exception:
        pass


_load_state()


# ── State accessors ───────────────────────────────────────────────────────────

def get_history() -> List[Dict]:
    with _lock:
        return list(_history)

def get_instance_status() -> Dict:
    with _lock:
        return dict(_instance_status)

def get_last_global_checks() -> Dict:
    with _lock:
        return dict(_last_global_checks)

def get_scheduling_enabled() -> bool:
    with _lock:
        return _scheduling_enabled

def set_scheduling_enabled(enabled: bool):
    global _scheduling_enabled
    with _lock:
        _scheduling_enabled = enabled
    _save_state()
    logger.info(f"Scheduling {'enabled' if enabled else 'paused'}")


def prune_runtime_state(valid_libraries: set[tuple[str, str]]) -> None:
    """Drop dashboard state for instances/libraries removed by live reload."""
    with _lock:
        for instance_name in list(_instance_status):
            libraries = _instance_status[instance_name]
            for library_name in list(libraries):
                if (instance_name, library_name) not in valid_libraries:
                    libraries.pop(library_name, None)
            if not libraries:
                _instance_status.pop(instance_name, None)
        for instance_name in list(_last_global_checks):
            if not any(name == instance_name for name, _ in valid_libraries):
                _last_global_checks.pop(instance_name, None)


# ── History recording ─────────────────────────────────────────────────────────

def _record(instance_name: str, library_name: str, status: str,
            checks: Dict, message: str, removed_items: List[Dict] = None,
            removed_count: int = None):
    items = removed_items or []
    count = removed_count if removed_count is not None else len(items)
    record = {
        "timestamp":     datetime.now().isoformat(),
        "instance":      instance_name,
        "library":       library_name,
        "status":        status,
        "checks":        checks,
        "message":       message,
        "removed_items": items,
        "removed_count": count,
    }
    with _lock:
        _history.insert(0, record)
        if len(_history) > MAX_HISTORY:
            _history.pop()
        if instance_name not in _instance_status:
            _instance_status[instance_name] = {}
        _instance_status[instance_name][library_name] = {
            "last_run":      record["timestamp"],
            "last_status":   status,
            "last_message":  message,
            "removed_count": count,
        }
    return record


# ── Per-path checks ───────────────────────────────────────────────────────────

def _run_path_checks(path_cfg: PathConfig, plex_count: Optional[int],
                     config: AppConfig, skip_threshold: bool = False) -> Dict:
    """
    Run all checks appropriate for a single path based on its type.
    skip_threshold=True skips the individual file count check (used for mixed
    libraries where combined count is checked separately).
    """
    results = {}
    label = path_cfg.path.split("/")[-1] or path_cfg.path

    # 1. Mountpoint — always
    results[f"Mount ({label})"] = check_mountpoint(path_cfg.path)

    # 2. Debrid mount health — debrid and usenet only
    if path_cfg.type in ("debrid", "usenet"):
        results[f"Debrid mount ({label})"] = check_debrid_mount(path_cfg.path)

    # 3. File threshold — skipped for mixed (handled at library level)
    if not skip_threshold:
        results[f"Files ({label})"] = check_file_threshold(
            path_cfg.path, path_cfg.min_threshold, plex_count
        )

    # 4. Provider API checks — optional
    for pc in path_cfg.provider_checks:
        check_name = f"{pc.type.capitalize()} API ({label})"
        results[check_name] = check_provider(pc.type, pc.api_key, config=config)

    return results


def _run_mixed_threshold(library: LibraryConfig,
                         plex_count: Optional[int]) -> Dict:
    """
    For mixed libraries: sum files across ALL paths and compare combined
    total to Plex count. Uses the lowest min_threshold across all paths.
    """
    total_disk = sum(count_files(p.path) for p in library.paths)
    threshold  = min((p.min_threshold for p in library.paths), default=0.90)

    if plex_count is None:
        return {
            "pass": False,
            "detail": "Plex item count unavailable — refusing to empty trash",
        }

    if plex_count > 0:
        ratio = total_disk / plex_count
        if ratio < threshold:
            return {
                "pass":   False,
                "detail": (f"Combined ratio {ratio*100:.1f}% below threshold "
                           f"{threshold*100:.0f}% "
                           f"({total_disk} total on disk / {plex_count} in Plex)")
            }
        return {
            "pass":   True,
            "detail": (f"Combined OK: {ratio*100:.1f}% "
                       f"({total_disk} total on disk / {plex_count} in Plex)")
        }

    if total_disk == 0:
        return {"pass": False, "detail": "No files found across any path"}
    return {"pass": True, "detail": f"{total_disk} total files on disk"}


# ── Plex instance global checks ───────────────────────────────────────────────

def run_instance_checks(instance: PlexInstanceConfig,
                        plex: PlexClient) -> Dict:
    """Run Plex reachability check for an instance. Store result."""
    checks = {
        f"Plex ({instance.name})": plex.check_reachable(),
    }
    with _lock:
        _last_global_checks[instance.name] = checks
    return checks


def refresh_protection_status(instance: PlexInstanceConfig,
                              library: LibraryConfig,
                              config: AppConfig,
                              plex: PlexClient,
                              plex_checks: Optional[Dict] = None) -> Dict:
    """Populate dashboard health using checks that cannot empty Plex trash."""
    section_id = library.section_id or plex.find_section_id(library.name)
    if not section_id:
        checks = dict(plex_checks or run_instance_checks(instance, plex))
        checks["Plex library"] = {
            "pass": False,
            "detail": f"Could not find Plex section for '{library.name}'",
        }
    else:
        checks, _ = _collect_library_checks(
            instance, library, config, plex,
            plex_checks=plex_checks, section_id=section_id,
        )
    failed = _failed_checks(checks)
    status = "preflight_fail" if failed else "preflight_pass"
    message = (
        "Read-only safety checks failed: " + ", ".join(failed)
        if failed else "Read-only safety checks passed; no trash was emptied"
    )
    checked_at = datetime.now().isoformat()
    with _lock:
        _instance_status.setdefault(instance.name, {})[library.name] = {
            "last_checked": checked_at,
            "last_status": status,
            "last_message": message,
            "removed_count": None,
            "status_source": "preflight",
            "checks": checks,
        }
    logger.info("[%s / %s] %s", instance.name, library.name, message)
    return checks


# ── Library runner ────────────────────────────────────────────────────────────

def _breakdown(items: list) -> str:
    counts: dict = {}
    for item in items:
        t = item.get("type", "item")
        counts[t] = counts.get(t, 0) + 1
    order = ["episode", "season", "show", "movie"]
    parts = [f"{counts[k]} {k}{'s' if counts[k] != 1 else ''}" for k in order if k in counts]
    parts += [f"{v} {k}{'s' if v != 1 else ''}" for k, v in counts.items() if k not in order]
    return ", ".join(parts) if parts else f"{len(items)} item(s)"


def _items_removed(before: List[Dict], after: List[Dict]) -> List[Dict]:
    remaining = Counter(trash_item_key(item) for item in after)
    removed = []
    for item in before:
        key = trash_item_key(item)
        if remaining[key]:
            remaining[key] -= 1
        else:
            removed.append(item)
    return removed


def _headline_count(items: List[Dict]) -> int:
    episode_count = sum(1 for i in items if i.get("type") == "episode")
    return episode_count if episode_count > 0 else len(items)


def _trash_snapshots_match(before: List[Dict], after: List[Dict]) -> bool:
    return (
        Counter(trash_item_key(item) for item in before)
        == Counter(trash_item_key(item) for item in after)
    )


def _snapshot_difference(before: List[Dict], after: List[Dict]) -> tuple[List[Dict], List[Dict]]:
    """Return items that appeared and disappeared, preserving item details."""
    return _items_removed(after, before), _items_removed(before, after)


def _item_summary(items: List[Dict]) -> str:
    if not items:
        return "none"
    labels = []
    for item in items[:10]:
        label = item.get("title") or "Unknown"
        if item.get("year"):
            label += f" ({item['year']})"
        if item.get("rating_key"):
            label += f" [ratingKey {item['rating_key']}]"
        labels.append(label)
    if len(items) > 10:
        labels.append(f"...and {len(items) - 10} more")
    return ", ".join(labels)


def _deletion_limit_check(config: AppConfig, items: List[Dict],
                          plex_count: Optional[int]) -> Dict:
    count = _headline_count(items)
    max_items = max(0, int(config.max_trash_items))
    max_percent = max(0.0, float(config.max_trash_percent))
    if max_items and count > max_items:
        return {
            "pass": False,
            "detail": f"{count} items exceeds the configured limit of {max_items}",
        }
    if max_percent and plex_count and plex_count > 0:
        percent = count / plex_count * 100
        if percent > max_percent:
            return {
                "pass": False,
                "detail": (
                    f"{count} items is {percent:.1f}% of the active library, "
                    f"above the configured {max_percent:g}% limit"
                ),
            }
    return {
        "pass": True,
        "detail": f"{count} items within configured deletion limits",
    }


def _handle_checks_failed(config, instance, library, all_checks, failed):
    failed_names = ", ".join(failed.keys())
    msg = f"Checks failed ({failed_names}) — trash empty skipped"
    logger.warning(f"[{instance.name} / {library.name}] {msg}")
    _record(instance.name, library.name, "skipped", all_checks, msg)
    if config.notify.on_health_fail:
        notifications.dispatch_health_fail(
            config, instance.name, library.name, failed, all_checks,
        )


def _handle_dry_run(instance, library, trash_items, all_checks, headline_count):
    trash_count = len(trash_items)
    if trash_count > 0:
        msg = f"[DRY RUN] Would remove {_breakdown(trash_items)} from trash — no action taken"
    else:
        msg = "[DRY RUN] Trash is already empty"
    logger.info(f"[{instance.name} / {library.name}] {msg}")
    _record(instance.name, library.name, "dry_run", all_checks, msg,
            trash_items, removed_count=headline_count)


def _handle_empty_failed(config, instance, library, result, all_checks):
    msg = f"emptyTrash failed: {result.get('error', result.get('http'))}"
    logger.error(f"[{instance.name} / {library.name}] {msg}")
    _record(instance.name, library.name, "error", all_checks, msg,
            removed_items=[], removed_count=0)
    if config.notify.on_error:
        notifications.dispatch_error(
            config, instance.name, library.name,
            str(result.get('error', result.get('http'))), all_checks,
        )


def _handle_empty_success(config, instance, library, trash_items, all_checks,
                          removed_items):
    trash_count = len(trash_items)
    removed_count = len(removed_items)

    if removed_count > 0:
        msg = f"Emptied {_breakdown(removed_items)} from trash"
    elif trash_count > 0:
        msg = (f"emptyTrash completed, but Plex still reports "
               f"{_breakdown(trash_items)} in trash")
    else:
        msg = "Trash was already empty"
    logger.info(f"[{instance.name} / {library.name}] {msg}")
    _record(instance.name, library.name, "success", all_checks, msg,
            removed_items if removed_items else [],
            removed_count=_headline_count(removed_items))
    if removed_count > 0 and config.notify.on_emptied:
        notifications.dispatch_emptied(
            config, instance.name, library.name, removed_items, all_checks,
            breakdown=_breakdown(removed_items),
        )
    elif trash_count == 0 and config.notify.on_clean:
        notifications.dispatch_clean(
            config, instance.name, library.name, all_checks,
        )


def _scheduling_blocked(dry_run: bool, manual: bool) -> bool:
    return not dry_run and not manual and not get_scheduling_enabled()


def _handle_section_not_found(config, instance, library):
    msg = f"Could not find Plex section for '{library.name}'"
    logger.warning(f"[{instance.name} / {library.name}] {msg}")
    _record(instance.name, library.name, "error", {}, msg)
    if config.notify.on_skip:
        notifications.dispatch_skip(
            config, instance.name, library.name, msg,
        )


def _collect_library_checks(instance: PlexInstanceConfig,
                            library: LibraryConfig,
                            config: AppConfig,
                            plex: PlexClient,
                            plex_checks: Optional[Dict] = None,
                            section_id: Optional[str] = None) -> tuple[Dict, Optional[int]]:
    all_checks = dict(plex_checks or run_instance_checks(instance, plex))
    plex_count = plex.get_library_item_count(
        section_id or library.section_id or plex.find_section_id(library.name)
    )
    is_mixed = library.type == "mixed"
    for path_cfg in library.paths:
        all_checks.update(_run_path_checks(
            path_cfg, plex_count, config, skip_threshold=is_mixed
        ))
    if is_mixed and library.paths:
        all_checks["Files (combined)"] = _run_mixed_threshold(library, plex_count)
    return all_checks, plex_count


def _failed_checks(checks: Dict) -> Dict:
    return {name: result for name, result in checks.items() if not result["pass"]}


def _record_inventory_error(config, instance, library, checks, message):
    logger.error(f"[{instance.name} / {library.name}] {message}")
    _record(instance.name, library.name, "error", checks, message)
    if config.notify.on_error:
        notifications.dispatch_error(
            config, instance.name, library.name, message, checks,
        )


def _confirm_preflight(config: AppConfig, instance: PlexInstanceConfig,
                       library: LibraryConfig, plex: PlexClient,
                       section_id: str, original_items: List[Dict]):
    checks, plex_count = _collect_library_checks(
        instance, library, config, plex, section_id=section_id,
    )
    failed = _failed_checks(checks)
    if failed:
        _handle_checks_failed(config, instance, library, checks, failed)
        return None, checks

    previous_items = original_items
    confirmed_items = None
    changes = []
    for attempt in range(TRASH_SNAPSHOT_MAX_RETRIES + 1):
        current_items = plex.get_trash_items(section_id)
        if current_items is None:
            _record_inventory_error(
                config, instance, library, checks,
                "Final trash inventory failed — refusing to empty",
            )
            return None, checks
        if _trash_snapshots_match(previous_items, current_items):
            confirmed_items = current_items
            break

        appeared, disappeared = _snapshot_difference(previous_items, current_items)
        change_detail = (
            f"appeared: {_item_summary(appeared)}; "
            f"disappeared: {_item_summary(disappeared)}"
        )
        changes.append(change_detail)
        logger.warning(
            f"[{instance.name} / {library.name}] "
            f"Trash inventory changed ({change_detail})"
        )
        previous_items = current_items
        if attempt < TRASH_SNAPSHOT_MAX_RETRIES:
            time.sleep(TRASH_SNAPSHOT_RETRY_DELAY)

    snapshot_stable = confirmed_items is not None
    checks["Trash snapshot"] = {
        "pass": snapshot_stable,
        "detail": (
            (
                "Two consecutive trash snapshots matched"
                + (f" after changes ({' | '.join(changes)})" if changes else "")
            )
            if snapshot_stable
            else (
                "Trash did not stabilize after brief retries — refusing to empty"
                + (f" ({' | '.join(changes)})" if changes else "")
            )
        ),
    }
    limit_items = (
        confirmed_items if confirmed_items is not None else previous_items
    )
    checks["Deletion limit"] = _deletion_limit_check(
        config, limit_items, plex_count,
    )
    failed = _failed_checks(checks)
    if failed:
        _handle_checks_failed(config, instance, library, checks, failed)
        return None, checks
    return confirmed_items, checks


def run_library(instance: PlexInstanceConfig, library: LibraryConfig,
                config: AppConfig, plex: PlexClient,
                plex_checks: Optional[Dict] = None,
                dry_run: bool = False,
                manual: bool = False):
    """Serialize work so scheduled and manual runs cannot overlap."""
    key = f"{instance.name}::{library.name}"
    with _lock:
        run_lock = _run_locks.setdefault(key, threading.Lock())
    if not run_lock.acquire(blocking=False):
        _record(instance.name, library.name, "skipped", {},
                "A run is already in progress")
        return
    try:
        refresh_hold = (
            _library_refresh_guard(instance.name, library.name)
            if _library_refresh_guard else None
        )
        if refresh_hold and not dry_run:
            message = f"Plex library refresh in progress — {refresh_hold}; trash empty skipped"
            logger.info(f"[{instance.name} / {library.name}] {message}")
            _record(instance.name, library.name, "skipped", {}, message)
            return
        with lease(
            instance.name,
            operation="empty_trash",
            queue_empty_trash=True,
        ) as (acquired, reason):
            if not acquired:
                message = f"Plex maintenance busy — {reason}; trash empty skipped"
                logger.warning(f"[{instance.name} / {library.name}] {message}")
                _record(instance.name, library.name, "skipped", {}, message)
                return
            _run_library(instance, library, config, plex, plex_checks, dry_run, manual)
    finally:
        run_lock.release()


def _run_library(instance: PlexInstanceConfig, library: LibraryConfig,
                 config: AppConfig, plex: PlexClient,
                 plex_checks: Optional[Dict] = None,
                 dry_run: bool = False,
                 manual: bool = False):
    """Full run for one library after acquiring its execution lock."""
    mode = "DRY RUN" if dry_run else "run"
    logger.info(f"[{instance.name} / {library.name}] Starting {mode}{'  (manual)' if manual else ''}")

    # Scheduling gate — only applies to cron-triggered runs, not manual or dry run
    if _scheduling_blocked(dry_run, manual):
        logger.info(f"[{instance.name} / {library.name}] Scheduling paused — skipping")
        _record(instance.name, library.name, "skipped", {}, "Scheduling is paused")
        return

    # Resolve section ID
    section_id = library.section_id or plex.find_section_id(library.name)
    if not section_id:
        _handle_section_not_found(config, instance, library)
        return

    all_checks, _ = _collect_library_checks(
        instance, library, config, plex, plex_checks, section_id=section_id,
    )
    failed = _failed_checks(all_checks)
    if failed:
        _handle_checks_failed(config, instance, library, all_checks, failed)
        return

    trash_items = plex.get_trash_items(section_id)
    if trash_items is None:
        _record_inventory_error(
            config, instance, library, all_checks,
            "Could not inventory Plex trash — refusing to empty",
        )
        return
    if dry_run:
        _handle_dry_run(instance, library, trash_items, all_checks,
                        _headline_count(trash_items))
        return
    if not trash_items:
        _handle_empty_success(
            config, instance, library, trash_items, all_checks, [],
        )
        return

    logger.info(f"[{instance.name} / {library.name}] "
                f"{_breakdown(trash_items)} in trash snapshot; running final preflight")

    # Clean Bundles is a server-wide maintenance action and therefore opt-in.
    if config.clean_bundles_before_empty:
        clean_result = plex.clean_bundles()
        if not clean_result["ok"]:
            _handle_empty_failed(
                config, instance, library,
                {"error": "Clean Bundles failed: "
                          f"{clean_result.get('error', clean_result.get('http'))}"},
                all_checks,
            )
            return

    confirmed_items, all_checks = _confirm_preflight(
        config, instance, library, plex, section_id, trash_items,
    )
    if confirmed_items is None:
        return
    trash_items = confirmed_items
    if not trash_items:
        _handle_empty_success(
            config, instance, library, trash_items, all_checks, [],
        )
        return

    # Keep this as the single destructive Empty Trash call site.
    result = plex.empty_trash(section_id)

    if not result["ok"]:
        _handle_empty_failed(config, instance, library, result, all_checks)
        return

    time.sleep(2)
    remaining_items = plex.get_trash_items(section_id)
    if remaining_items is None:
        _record_inventory_error(
            config, instance, library, all_checks,
            "emptyTrash succeeded, but verification inventory failed",
        )
        return
    removed_items = _items_removed(trash_items, remaining_items)

    _handle_empty_success(config, instance, library, trash_items, all_checks,
                          removed_items)
