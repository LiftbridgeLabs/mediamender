import logging
import ipaddress
import json
import os
import secrets
import threading
import time
import urllib.parse
import yaml
from datetime import datetime, timezone
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from flask import (Flask, Response, jsonify, render_template, request, redirect,
                   url_for, session, send_from_directory)

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import (load_config, parse_config, AppConfig,
                        PlexInstanceConfig, LibraryConfig,
                        NotificationDestination, NOTIFICATION_EVENTS)
from src.plex_client import PlexClient
from src.auth import (require_auth, auth_enabled, check_credentials,
                      is_authenticated, hash_password, is_locked_out,
                      has_valid_api_token, generate_api_token, hash_api_token,
                      authenticate_user, current_identity, has_permission)
from src import runner
from src.runner import get_scheduling_enabled, set_scheduling_enabled
from src.providers import get_account_status, get_api_key
from src.providers import _ENV_KEYS as _PROVIDER_ENV_KEYS
from src.storage import atomic_write_json, atomic_write_yaml
from src.logging_manager import LogManager
from src import plex_auth
from src import notifications
from src.version import __version__
from src.features import FEATURES, feature_label, permission_prefixes
from src.settings_sections import apply_section, known_section
from src.web.context import config_file_lock, requires_feature, serialized_config_write
from src.timestamp_repair import TimestampRepairManager
from src.library_refresh import LibraryRefreshManager
from src.mark_watched import (
    MarkWatchedManager, MarkWatchedRuleStore, WebhookLog,
    process_mark_watched_event,
)
from src.sonarr_client import (
    SonarrClient, SonarrConnectionStore, SonarrError,
    normalize_callback_url, normalize_sonarr_url,
)
from src.maintenance import lease, set_recovery_check
from src.repair_worker_client import RepairWorkerClient, validate_worker_url
from src.worker_auth import SignatureVerifier
from src.branding import PRODUCT_NAME, PRODUCT_SLUG, get_env

LOG_DIR  = os.environ.get("LOG_DIR", "data/logs")
os.makedirs(LOG_DIR, exist_ok=True)

_log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Console handler
_console = logging.StreamHandler()
_console.setFormatter(_log_formatter)

log_manager = LogManager(LOG_DIR, _log_formatter)
_file_handler = log_manager.handler

logging.basicConfig(
    level=logging.INFO,
    handlers=[_console, _file_handler],
)
logger = logging.getLogger(PRODUCT_SLUG)

# ── Bootstrap ─────────────────────────────────────────────────────────────────
CONFIG_PATH = os.environ.get("CONFIG_PATH", "data/config.yml")
CONFIG_LOAD_ERROR = ""
try:
    config: AppConfig = load_config(CONFIG_PATH)
except Exception as exc:
    CONFIG_LOAD_ERROR = str(exc)
    logger.exception("Configuration could not be loaded; starting in recovery mode")
    config = AppConfig(instances=[], config_missing=True)
logging.getLogger().setLevel(config.log_level.upper())
log_manager.configure(
    config.log_max_file_size_mb,
    config.log_max_total_size_mb,
    config.log_retention_days,
)

plex_clients: dict[str, PlexClient] = {
    inst.name: PlexClient(inst.url, inst.token)
    for inst in config.instances
}
timestamp_repair = TimestampRepairManager(
    os.path.dirname(os.path.abspath(CONFIG_PATH)),
)
library_refresh = LibraryRefreshManager(
    os.path.dirname(os.path.abspath(CONFIG_PATH)),
)
mark_watched_rules = MarkWatchedRuleStore(
    os.path.dirname(os.path.abspath(CONFIG_PATH)),
)
mark_watched = MarkWatchedManager(
    os.path.dirname(os.path.abspath(CONFIG_PATH)),
    processor=lambda event: process_mark_watched_event(
        event, config, plex_clients, mark_watched_rules,
    ),
    retry_delays=tuple(config.mark_watched.retry_delays),
    workers=config.mark_watched.workers,
    give_up_after_hours=config.mark_watched.give_up_after_hours,
)
webhook_log = WebhookLog(os.path.dirname(os.path.abspath(CONFIG_PATH)))
sonarr_connection = SonarrConnectionStore(
    os.path.dirname(os.path.abspath(CONFIG_PATH)),
)
runner.set_library_refresh_guard(library_refresh.trash_hold_reason)
startup_recovery = timestamp_repair.recover()
if not startup_recovery.get("ok"):
    logger.error("Timestamp repair startup recovery requires attention: %s",
                 startup_recovery.get("error"))

_worker_signature_verifier = SignatureVerifier()
_remote_repair_lock = threading.RLock()
_remote_repair: dict = {}
_repair_batch_lock = threading.RLock()
_repair_batch: dict = {"running": False}
_worker_scan_contexts: dict[str, dict] = {}
_worker_recovery_cache: dict[str, tuple[float, str | None, bool]] = {}
_remote_pending_path = timestamp_repair.root / "controller-remote-active.json"
try:
    _remote_pending_repair = json.loads(
        _remote_pending_path.read_text(encoding="utf-8"),
    )
    if not isinstance(_remote_pending_repair, dict):
        _remote_pending_repair = {}
except (OSError, ValueError):
    _remote_pending_repair = {}
_metadata_audit_path = Path(CONFIG_PATH).resolve().parent / "metadata-audits.json"
_metadata_audit_lock = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_remote_pending(payload: dict) -> None:
    with _remote_repair_lock:
        _remote_pending_repair.clear()
        _remote_pending_repair.update(payload)
        atomic_write_json(str(_remote_pending_path), _remote_pending_repair)


def _clear_remote_pending(worker_name: str = "") -> None:
    with _remote_repair_lock:
        if worker_name and _remote_pending_repair.get("worker") != worker_name:
            return
        _remote_pending_repair.clear()
        try:
            _remote_pending_path.unlink()
        except FileNotFoundError:
            pass


def _read_metadata_audits() -> dict:
    try:
        value = json.loads(_metadata_audit_path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _plex_details_url(machine_id: str, metadata_key: str) -> str:
    if not machine_id or not metadata_key:
        return ""
    return (
        "https://app.plex.tv/desktop/#!/server/"
        + urllib.parse.quote(machine_id, safe="")
        + "/details?key="
        + urllib.parse.quote(metadata_key, safe="")
    )

app = Flask(__name__)


def _load_session_key() -> str:
    """Resolve a session key from an override or the persistent data directory."""
    configured = get_env("MEDIAMENDER_SECRET_KEY").strip()
    if configured:
        return configured

    key_path = get_env(
        "MEDIAMENDER_SECRET_KEY_FILE",
        os.path.join(os.path.dirname(os.path.abspath(CONFIG_PATH)), ".session-key"),
    )
    try:
        if os.path.exists(key_path):
            with open(key_path, "r", encoding="utf-8") as handle:
                persisted = handle.read().strip()
            if persisted:
                return persisted

        generated = secrets.token_hex(32)
        os.makedirs(os.path.dirname(os.path.abspath(key_path)), exist_ok=True)
        try:
            descriptor = os.open(
                key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(generated)
            logger.info("Created persistent session key at %s", key_path)
            return generated
        except FileExistsError:
            with open(key_path, "r", encoding="utf-8") as handle:
                return handle.read().strip() or generated
    except OSError as exc:
        logger.warning(
            "Could not persist the session key at %s (%s); sessions will reset on restart",
            key_path,
            exc,
        )
        return secrets.token_hex(32)


app.secret_key = _load_session_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"]   = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
scheduler      = BackgroundScheduler()
_next_runs: dict = {}
_library_refresh_next_runs: dict = {}
_library_refresh_queue_lock = threading.Lock()
_library_refresh_queue: dict = {"running": False}
_runtime_lock = threading.RLock()
_config_file_lock = config_file_lock  # shared with src.web.context
_status_refresh_lock = threading.Lock()
_status_refresh_progress_lock = threading.Lock()
_status_refresh_progress = {
    "running": False,
    "completed": 0,
    "total": 0,
    "current": "",
}


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _job_key(instance_name: str, library_name: str) -> str:
    return f"{instance_name}::{library_name}"


def _refresh_job_key(instance_name: str, library_name: str) -> str:
    return f"refresh::{instance_name}::{library_name}"


def make_job(inst: PlexInstanceConfig, lib: LibraryConfig):
    def job():
        with _runtime_lock:
            live_config = config
            live_inst = next((i for i in live_config.instances
                              if i.name == inst.name), None)
            live_lib = next((l for l in live_inst.libraries
                             if l.name == lib.name), None) if live_inst else None
            plex = plex_clients.get(inst.name)
        if not live_inst or not live_lib or plex is None:
            return
        plex_checks = runner.run_instance_checks(live_inst, plex)
        runner.run_library(live_inst, live_lib, live_config, plex,
                           plex_checks=plex_checks)
        _update_next(live_inst.name, live_lib.name)
    return job


def make_refresh_job(inst: PlexInstanceConfig, lib: LibraryConfig):
    def job():
        with _runtime_lock:
            live_inst = next((item for item in config.instances
                              if item.name == inst.name), None)
            live_lib = next((item for item in live_inst.libraries
                             if item.name == lib.name), None) if live_inst else None
            plex = plex_clients.get(inst.name)
        if (not live_inst or not live_lib or not live_lib.refresh_enabled
                or plex is None or not config.features.library_refresh):
            return
        library_refresh.run(live_inst, live_lib, plex, source="scheduled")
        _update_refresh_next(live_inst.name, live_lib.name)
    return job


def _update_next(instance_name: str, library_name: str):
    key = _job_key(instance_name, library_name)
    job = scheduler.get_job(key)
    if job:
        # APScheduler 3.x uses next_fire_time
        nft = getattr(job, 'next_fire_time', None) or getattr(job, 'next_run_time', None)
        if nft:
            _next_runs[key] = nft.isoformat()


def _update_refresh_next(instance_name: str, library_name: str):
    key = _refresh_job_key(instance_name, library_name)
    job = scheduler.get_job(key)
    if job:
        nft = getattr(job, 'next_fire_time', None) or getattr(job, 'next_run_time', None)
        if nft:
            _library_refresh_next_runs[_job_key(instance_name, library_name)] = nft.isoformat()


def _effective_cron(target: AppConfig, lib: LibraryConfig) -> str:
    return lib.cron or target.default_cron


def _refresh_next_runs():
    for inst in config.instances:
        for lib in inst.libraries:
            _update_next(inst.name, lib.name)
            _update_refresh_next(inst.name, lib.name)


def _start_readonly_status_refresh(target_config: AppConfig = None,
                                    clients: dict[str, PlexClient] = None) -> None:
    """Refresh dashboard safety state in the background without touching trash."""
    target = target_config or config
    if not target.features.trash_removal:
        with _status_refresh_progress_lock:
            _status_refresh_progress.update({"running": False, "completed": 0, "total": 0, "current": "Trash Removal disabled"})
        return
    target_clients = clients or plex_clients
    total = sum(len(instance.libraries) for instance in target.instances)
    with _status_refresh_progress_lock:
        _status_refresh_progress.update({
            "running": True,
            "completed": 0,
            "total": total,
            "current": "Waiting to start",
        })

    def refresh():
        _status_refresh_lock.acquire()
        try:
            with _status_refresh_progress_lock:
                _status_refresh_progress.update({
                    "running": True,
                    "completed": 0,
                    "total": total,
                    "current": "Connecting to Plex servers",
                })
            for instance in target.instances:
                plex = target_clients.get(instance.name)
                if plex is None:
                    with _status_refresh_progress_lock:
                        _status_refresh_progress["completed"] += len(
                            instance.libraries
                        )
                    continue
                plex_checks = runner.run_instance_checks(instance, plex)
                for library in instance.libraries:
                    with _status_refresh_progress_lock:
                        _status_refresh_progress["current"] = (
                            f"{instance.name} / {library.name}"
                        )
                    try:
                        runner.refresh_protection_status(
                            instance, library, target, plex, plex_checks,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[%s / %s] Read-only status refresh failed (%s)",
                            instance.name, library.name, type(exc).__name__,
                        )
                    finally:
                        with _status_refresh_progress_lock:
                            _status_refresh_progress["completed"] += 1
        finally:
            with _status_refresh_progress_lock:
                _status_refresh_progress["running"] = False
                _status_refresh_progress["current"] = "Safety status is current"
            _status_refresh_lock.release()

    threading.Thread(
        target=refresh, daemon=True, name="protection-status-refresh",
    ).start()


def _setup_scheduler(new_config: AppConfig = None):
    target = new_config or config
    scheduler.remove_all_jobs()
    _next_runs.clear()
    _library_refresh_next_runs.clear()

    if target.features.trash_removal:
        for inst in target.instances:
            for lib in inst.libraries:
                key = _job_key(inst.name, lib.name)
                scheduler.add_job(
                    make_job(inst, lib),
                    CronTrigger.from_crontab(_effective_cron(target, lib)),
                    id=key,
                    name=f"{inst.name} / {lib.name}",
                    replace_existing=True,
                )
    if target.features.library_refresh:
        for inst in target.instances:
            for lib in inst.libraries:
                if not lib.refresh_enabled:
                    continue
                key = _refresh_job_key(inst.name, lib.name)
                scheduler.add_job(
                    make_refresh_job(inst, lib),
                    CronTrigger.from_crontab(lib.refresh_cron),
                    id=key,
                    name=f"Refresh {inst.name} / {lib.name}",
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                )
    for inst in target.instances:
        for lib in inst.libraries:
            _update_next(inst.name, lib.name)
            _update_refresh_next(inst.name, lib.name)


try:
    _setup_scheduler()
except Exception as exc:
    CONFIG_LOAD_ERROR = CONFIG_LOAD_ERROR or str(exc)
    logger.exception("Schedules could not be loaded; starting without jobs")
    scheduler.remove_all_jobs()
scheduler.start()
_refresh_next_runs()
_start_readonly_status_refresh()


def _validate_provider_checks(checks, context: str) -> None:
    if not isinstance(checks, list):
        raise ValueError(f"{context}: provider_checks must be a list")
    for provider in checks:
        if not isinstance(provider, dict):
            raise ValueError(f"{context}: provider check must be an object")
        provider_type = str(provider.get("type", ""))
        if provider_type not in _PROVIDER_ENV_MAP:
            raise ValueError(f"{context}: unknown provider: {provider_type}")


def _validate_path(path, library_type: str, context: str) -> None:
    if not isinstance(path, dict):
        raise ValueError(f"{context}: every path must be an object")
    if not str(path.get("path", "")).strip():
        raise ValueError(f"{context}: path cannot be blank")
    path_type = str(path.get("type", library_type))
    if path_type not in {"physical", "debrid", "usenet"}:
        raise ValueError(f"{context}: invalid path type: {path_type}")
    threshold = float(path.get("min_threshold", 90))
    if not 0 < threshold <= 100:
        raise ValueError(f"{context}: threshold must be between 1 and 100")
    _validate_provider_checks(path.get("provider_checks", []), context)


def _validate_library(library, instance_name: str, names: set,
                      require_paths: bool = True) -> None:
    if not isinstance(library, dict):
        raise ValueError(f"{instance_name}: every library must be an object")
    name = str(library.get("name", "")).strip()
    if not name:
        raise ValueError(f"{instance_name}: every library needs a name")
    if name in names:
        raise ValueError(f"{instance_name}: duplicate library: {name}")
    names.add(name)
    context = f"{instance_name} / {name}"
    library_type = str(library.get("type", "physical"))
    if library_type not in {"physical", "debrid", "usenet", "mixed"}:
        raise ValueError(f"{context}: invalid library type")
    cron = str(library.get("cron", "")).strip()
    if cron:
        CronTrigger.from_crontab(cron)
    refresh_cron = str(library.get("refresh_cron", "0 * * * *")).strip()
    CronTrigger.from_crontab(refresh_cron)
    refresh_guard = int(library.get("refresh_guard_minutes", 15))
    if not 1 <= refresh_guard <= 240:
        raise ValueError(
            f"{context}: refresh safety hold must be between 1 and 240 minutes"
        )
    paths = library.get("paths", [])
    if not isinstance(paths, list):
        raise ValueError(f"{context}: paths must be a list")
    if require_paths and not paths:
        raise ValueError(f"{context}: configure at least one filesystem path")
    for path in paths:
        _validate_path(path, library_type, context)


def _validate_instance(instance, names: set, machine_ids: set,
                       worker_names: set[str], require_paths: bool = True) -> None:
    if not isinstance(instance, dict):
        raise ValueError("Every Plex instance must be an object")
    name = str(instance.get("name", "")).strip()
    if not name:
        raise ValueError("Every Plex instance needs a name")
    if name in names:
        raise ValueError(f"Duplicate Plex instance name: {name}")
    names.add(name)
    machine_id = str(instance.get("machine_id", "")).strip()
    if machine_id and machine_id in machine_ids:
        raise ValueError(f"Duplicate Plex server identifier: {machine_id}")
    if machine_id:
        machine_ids.add(machine_id)
    metadata_health = instance.get("metadata_health", {})
    if not isinstance(metadata_health, dict):
        raise ValueError(f"{name}: metadata_health must be an object")
    ignored_libraries = metadata_health.get("ignored_libraries", [])
    if not isinstance(ignored_libraries, list) or any(
        not str(library).strip() for library in ignored_libraries
    ):
        raise ValueError(
            f"{name}: ignored metadata libraries must be a list of names"
        )
    repair = instance.get("timestamp_repair", {})
    if not isinstance(repair, dict):
        raise ValueError(f"{name}: timestamp_repair must be an object")
    enabled = bool(repair.get("enabled", False))
    worker = str(repair.get("worker", "local")).strip() or "local"
    database_path = str(repair.get("database_path", "")).strip()
    prefixes = repair.get("allowed_prefixes", [])
    if not isinstance(prefixes, list) or any(not str(path).strip() for path in prefixes):
        raise ValueError(f"{name}: timestamp repair folders must be a list of paths")
    if enabled and (not database_path or not prefixes):
        raise ValueError(f"{name}: enabled timestamp repair requires a database path and at least one repair folder")
    if enabled and worker != "local" and worker not in worker_names:
        raise ValueError(f"{name}: timestamp repair worker '{worker}' is not configured")
    if not 1 <= int(repair.get("max_files_per_folder", 5)) <= 100:
        raise ValueError(f"{name}: timestamp repair file limit must be between 1 and 100")
    timeout = int(repair.get("scan_timeout_seconds", 1800))
    poll = int(repair.get("poll_interval_seconds", 5))
    heartbeat = int(repair.get("heartbeat_seconds", 30))
    if not 30 <= timeout <= 7200 or not 1 <= poll <= 60 or not poll <= heartbeat <= 300:
        raise ValueError(f"{name}: invalid timestamp repair polling settings")
    ok, reason = _is_valid_plex_url(str(instance.get("url", "")).strip())
    if not ok:
        raise ValueError(f"{name}: {reason}")
    libraries = instance.get("libraries", [])
    if not isinstance(libraries, list):
        raise ValueError(f"{name}: libraries must be a list")
    library_names = set()
    for library in libraries:
        _validate_library(library, name, library_names, require_paths)


def _validate_safety_limits(raw: dict) -> None:
    if int(raw.get("max_trash_items", 1000)) < 0:
        raise ValueError("max_trash_items cannot be negative")
    percent = float(raw.get("max_trash_percent", 25))
    if not 0 <= percent <= 100:
        raise ValueError("max_trash_percent must be between 0 and 100")


def _validate_schedule(raw: dict) -> None:
    schedule = raw.get("schedule", {})
    if not isinstance(schedule, dict):
        raise ValueError("schedule must be an object")
    CronTrigger.from_crontab(str(schedule.get("default_cron", "0 * * * *")))


def _validate_logging(raw: dict) -> None:
    settings = raw.get("logging", {})
    if not isinstance(settings, dict):
        raise ValueError("logging must be an object")
    max_file = int(settings.get("max_file_size_mb", 5))
    max_total = int(settings.get("max_total_size_mb", 50))
    retention = int(settings.get("retention_days", 14))
    if not 1 <= max_file <= 1024:
        raise ValueError("Log file size must be between 1 MB and 1,024 MB")
    if not max_file <= max_total <= 10240:
        raise ValueError(
            "Total log storage must be at least one log file and no more than 10,240 MB"
        )
    if not 1 <= retention <= 3650:
        raise ValueError("Log retention must be between 1 and 3,650 days")


_NOTIFICATION_SERVICES = {
    "telegram", "ntfy", "gotify", "email", "pushover", "webhook", "custom",
}


def _validate_notifications(raw: dict) -> None:
    settings = raw.get("notifications", {})
    if not isinstance(settings, dict):
        raise ValueError("notifications must be an object")
    destinations = settings.get("destinations", [])
    if not isinstance(destinations, list):
        raise ValueError("notification destinations must be a list")
    names = set()
    for index, destination in enumerate(destinations, 1):
        context = f"Notification destination {index}"
        if not isinstance(destination, dict):
            raise ValueError(f"{context} must be an object")
        name = str(destination.get("name", "")).strip()
        if not name:
            raise ValueError(f"{context} requires a name")
        if name.casefold() in names:
            raise ValueError(f"Notification destination name '{name}' is duplicated")
        names.add(name.casefold())
        service = str(destination.get("service", "custom")).strip().lower()
        if service not in _NOTIFICATION_SERVICES:
            raise ValueError(f"{context} has unsupported preset '{service}'")
        url = str(destination.get("url", "")).strip()
        if not url or "://" not in url or any(char.isspace() for char in url):
            raise ValueError(f"{context} requires a valid Apprise URL")
        if not notifications.is_valid_apprise_url(url):
            raise ValueError(f"{context} contains an invalid Apprise service URL")
        events = destination.get("events", [])
        if not isinstance(events, list) or not events:
            raise ValueError(f"{context} must route at least one event")
        unknown = set(events) - set(NOTIFICATION_EVENTS)
        if unknown:
            raise ValueError(
                f"{context} has unsupported events: {', '.join(sorted(unknown))}"
            )


def _validate_raw_config(raw: dict, require_paths: bool | None = None) -> AppConfig:
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be an object")
    instances = raw.get("plex_instances", [])
    if not isinstance(instances, list):
        raise ValueError("plex_instances must be a list")
    _validate_safety_limits(raw)
    _validate_schedule(raw)
    _validate_logging(raw)
    _validate_notifications(raw)
    workers = raw.get("timestamp_repair_workers", [])
    if not isinstance(workers, list):
        raise ValueError("timestamp_repair_workers must be a list")
    worker_names = set()
    for index, worker in enumerate(workers, 1):
        if not isinstance(worker, dict):
            raise ValueError(f"Timestamp repair worker {index} must be an object")
        name = str(worker.get("name", "")).strip()
        if not name or name == "local" or name in worker_names:
            raise ValueError(f"Timestamp repair worker {index} needs a unique non-local name")
        if not validate_worker_url(str(worker.get("url", "")).strip()):
            raise ValueError(f"{name}: worker URL must use HTTP or HTTPS")
        if not validate_worker_url(str(worker.get("controller_url", "")).strip()):
            raise ValueError(f"{name}: controller callback URL must use HTTP or HTTPS")
        if len(str(worker.get("token", ""))) < 32:
            raise ValueError(f"{name}: worker token must contain at least 32 characters")
        worker_names.add(name)
    instance_names = set()
    machine_ids = set()
    features = raw.get("features", {})
    if require_paths is None:
        require_paths = not isinstance(features, dict) or features.get(
            "trash_removal", True,
        ) is not False
    for instance in instances:
        _validate_instance(
            instance, instance_names, machine_ids, worker_names, require_paths,
        )
    return parse_config(raw)


def _apply_runtime_config(new_config: AppConfig) -> None:
    global config, plex_clients, CONFIG_LOAD_ERROR
    new_clients = {
        instance.name: PlexClient(instance.url, instance.token)
        for instance in new_config.instances
    }
    with _runtime_lock:
        old_config = config
        old_clients = plex_clients
        try:
            config = new_config
            plex_clients = new_clients
            _setup_scheduler(new_config)
        except Exception:
            config = old_config
            plex_clients = old_clients
            _setup_scheduler(old_config)
            raise
    valid = {
        (instance.name, library.name)
        for instance in new_config.instances
        for library in instance.libraries
    }
    runner.prune_runtime_state(valid)
    logging.getLogger().setLevel(new_config.log_level.upper())
    log_manager.configure(
        new_config.log_max_file_size_mb,
        new_config.log_max_total_size_mb,
        new_config.log_retention_days,
    )
    _worker_recovery_cache.clear()
    mark_watched.retry_delays = tuple(new_config.mark_watched.retry_delays)
    mark_watched.workers = new_config.mark_watched.workers
    mark_watched.give_up_after_hours = new_config.mark_watched.give_up_after_hours
    mark_watched.start()
    CONFIG_LOAD_ERROR = ""


def _save_and_apply(raw: dict, runtime_tokens: dict = None,
                    require_paths: bool | None = None) -> AppConfig:
    parsed = _validate_raw_config(raw, require_paths=require_paths)
    for instance in parsed.instances:
        if not instance.token and runtime_tokens:
            instance.token = runtime_tokens.get(instance.name, "")
    atomic_write_yaml(CONFIG_PATH, raw)
    _apply_runtime_config(parsed)
    return parsed


_serialized_config_write = serialized_config_write


def _csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.before_request
def protect_state_changes():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    # Blueprint routes carry a "blueprint.view" endpoint, so compare the view
    # name rather than the full endpoint.
    view_name = (request.endpoint or "").rsplit(".", 1)[-1]
    if view_name in {"login", "api_timestamp_repair_worker_scan", "api_sonarr_webhook"}:
        return None
    # Non-browser automations with a verified API token do not rely on cookies
    # and therefore are not susceptible to cookie-based CSRF.
    if has_valid_api_token(config):
        return None
    expected = session.get("_csrf_token", "")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        return jsonify({"error": "Invalid or missing CSRF token"}), 403


_PERMISSION_PREFIXES = permission_prefixes()


@app.before_request
def enforce_user_permissions():
    if request.path.startswith("/api/webhooks/sonarr") or has_valid_api_token(config):
        return None
    if auth_enabled(config) and not is_authenticated():
        return None
    for prefix, permission in _PERMISSION_PREFIXES:
        if request.path.startswith(prefix) and not has_permission(permission, config):
            return jsonify({"error": f"Permission required: {permission}"}), 403
    return None


@app.after_request
def normalize_api_envelope(response):
    """Guarantee every JSON API response carries an `ok` flag.

    Handlers were split between `{"ok": false, "error": ...}` and a bare
    `{"error": ...}`, sometimes within one feature, so the browser could not
    rely on the field and checked it in some places and the HTTP status in
    others. Deriving it here from the status code makes the contract uniform
    without rewriting a hundred return statements, and leaves any flag a
    handler set deliberately untouched.
    """
    if not request.path.startswith("/api/"):
        return response
    if response.direct_passthrough or not response.is_json:
        return response
    payload = response.get_json(silent=True)
    if not isinstance(payload, dict) or "ok" in payload:
        return response
    payload["ok"] = response.status_code < 400
    response.set_data(json.dumps(payload))
    response.headers["Content-Length"] = str(len(response.get_data()))
    return response


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
        "form-action 'self'",
    )
    return response


# ── Template context ──────────────────────────────────────────────────────────

def _build_ui_instances():
    _refresh_next_runs()
    with _runtime_lock:
        current_instances = list(config.instances)
    inst_status = runner.get_instance_status()
    result = []
    for inst in current_instances:
        libs = []
        for lib in inst.libraries:
            key = _job_key(inst.name, lib.name)
            libs.append({
                "name":     lib.name,
                "type":     lib.type,
                "paths":    [{"path": p.path, "type": p.type} for p in lib.paths],
                "cron":     lib.cron,
                "effective_cron": _effective_cron(config, lib),
                "uses_global_schedule": not bool(lib.cron),
                "next_run": _next_runs.get(key, "—"),
                "status":   inst_status.get(inst.name, {}).get(lib.name, {}),
            })
        result.append({
            "name":      inst.name,
            "url":       inst.url,
            "libraries": libs,
        })
    return result


def _active_config_overrides() -> list[str]:
    fixed = {
        "DISCORD_WEBHOOK",
        "LOG_LEVEL",
        "LOG_DIR",
        "MEDIAMENDER_USERNAME",
        "MEDIAMENDER_PASSWORD",
        "RD_API_KEY",
        "AD_API_KEY",
        "TB_API_KEY",
        "DL_API_KEY",
        "PLEX_URL",
        "PLEX_TOKEN",
    }
    return sorted(
        name for name, value in os.environ.items()
        if value and (
            name in fixed
            or name.startswith("PLEX_URL_")
            or name.startswith("PLEX_TOKEN_")
        )
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/favicon.ico", methods=["GET"])
def favicon():
    return send_from_directory(app.static_folder, "mediamender.png", mimetype="image/png")


@app.route("/favicon.png", methods=["GET"])
def favicon_png():
    return send_from_directory(app.static_folder, "mediamender.png", mimetype="image/png")


@app.route("/favicon.svg", methods=["GET"])
def favicon_svg_compat():
    # Preserve old bookmarks while the app uses the raster PNG everywhere.
    return send_from_directory(app.static_folder, "mediamender.png", mimetype="image/png")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth_enabled(config):
        return redirect(url_for("index"))
    if is_authenticated():
        return redirect(url_for("index"))
    # Keep the login endpoint on GET after a failed submission.  Rendering the
    # error directly from POST leaves browsers on a form-resubmission page, so
    # Ctrl+F5 prompts users to resend their credentials.
    error = session.pop("_login_error", None)
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        ip       = request.remote_addr or ""
        if is_locked_out(ip):
            error = "Too many failed attempts — try again in 10 minutes"
        else:
            identity = authenticate_user(username, password, config, ip=ip)
        if not error and identity:
            session["authenticated"] = True
            session["username"] = identity["username"]
            session["role"] = identity["role"]
            session["permissions"] = identity["permissions"]
            return redirect(url_for("index"))
        elif not error:
            error = "Invalid username or password"
        session["_login_error"] = error
        return redirect(url_for("login"))
    return render_template("login.html", error=error, app_version=__version__, product_name=PRODUCT_NAME)


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@require_auth
def index():
    ui_instances = _build_ui_instances()
    identity = current_identity(config)
    return render_template("index.html",
        instances=ui_instances,
        instance_count=len(ui_instances),
        library_count=sum(len(instance["libraries"]) for instance in ui_instances),
        config_missing=config.config_missing,
        auth_enabled=auth_enabled(config),
        config=config,
        csrf_token=_csrf_token(),
        config_error=CONFIG_LOAD_ERROR,
        app_version=__version__,
        config_path=CONFIG_PATH,
        config_overrides=_active_config_overrides(),
        scheduler_timezone=str(scheduler.timezone),
        product_name=PRODUCT_NAME,
        current_identity=identity,
        feature_registry=[
            {
                "key": feature.key,
                "label": feature.label,
                "page": feature.page,
                "description": feature.description,
                "enabled": getattr(config.features, feature.key, True),
            }
            for feature in FEATURES
        ],
    )


@app.route("/api/status", methods=["GET"])
@require_auth
def api_status():
    with _status_refresh_progress_lock:
        refresh_progress = dict(_status_refresh_progress)
    return jsonify({
        "product":            PRODUCT_NAME,
        "product_slug":       PRODUCT_SLUG,
        "instances":          _build_ui_instances(),
        "next_runs":          _next_runs,
        "global_checks":      runner.get_last_global_checks(),
        "history_count":      len(runner.get_history()),
        "scheduling_enabled": get_scheduling_enabled(),
        "config_missing":     config.config_missing,
        "auth_enabled":       auth_enabled(config),
        "version":            __version__,
        "startup_checks":     refresh_progress,
    })


def _current_username() -> str:
    return current_identity(config)["username"] or "default"


def _is_admin() -> bool:
    return has_valid_api_token(config) or current_identity(config)["role"] == "admin"


@app.route("/api/history", methods=["GET"])
@require_auth
def api_history():
    return jsonify(runner.get_history())


def _library_refresh_status_response():
    saved = library_refresh.status()
    with _library_refresh_queue_lock:
        queue = dict(_library_refresh_queue)
    with _runtime_lock:
        instances = [{
            "name": instance.name,
            "libraries": [{
                "name": library.name,
                "section_id": library.section_id,
                "enabled": library.refresh_enabled,
                "cron": library.refresh_cron,
                "guard_minutes": library.refresh_guard_minutes,
                "next_run": _library_refresh_next_runs.get(
                    _job_key(instance.name, library.name), "",
                ),
            } for library in instance.libraries],
        } for instance in config.instances]
    return jsonify({**saved, "queue": queue, "instances": instances})


def _timestamp_runtime(instance_name: str, section_id: str = ""):
    with _runtime_lock:
        instance = next(
            (item for item in config.instances if item.name == instance_name), None,
        )
        plex = plex_clients.get(instance_name)
        library = None
        if instance and section_id:
            for item in instance.libraries:
                configured_section = item.section_id
                if not configured_section and plex:
                    configured_section = plex.find_section_id(item.name)
                if str(configured_section) == str(section_id):
                    library = item
                    break
    return instance, library, plex


def _repair_worker(name: str):
    return next((worker for worker in config.repair_workers if worker.name == name), None)


def _resolved_repair_libraries(instance, plex) -> list[dict]:
    libraries = []
    for library in instance.libraries:
        section_id = library.section_id or (plex.find_section_id(library.name) if plex else None)
        if section_id:
            libraries.append({"name": library.name, "section_id": str(section_id)})
    return libraries


def _enrich_repair_audit(result: dict, plex) -> dict:
    """Add Plex library sizes to an audit without making them a safety input."""
    total = 0
    libraries = result.get("libraries", [])
    all_known = bool(libraries)
    try:
        sections = plex.get_sections()
        section_types = {
            str(section.get("id", "")): str(section.get("type", ""))
            for section in sections
        } if isinstance(sections, list) else {}
    except Exception:
        section_types = {}
    for library in libraries:
        section_id = str(library.get("library_section_id", ""))
        library["type"] = section_types.get(section_id, "")
        count = plex.get_library_item_count(
            section_id,
        )
        if isinstance(count, int) and count >= 0:
            library["total_items"] = count
            total += count
        else:
            all_known = False
    library_types = {
        str(library.get("library_section_id", "")): library.get("type", "")
        for library in libraries
    }
    for folder in result.get("folders", []):
        folder["library_type"] = library_types.get(
            str(folder.get("library_section_id", "")), "",
        )
    for issue in result.get("path_issues", []):
        issue["library_type"] = library_types.get(
            str(issue.get("library_section_id", "")), "",
        )
    result["total_library_items"] = total if all_known else None
    return result


def _worker_payload(instance, plex) -> dict:
    repair = instance.timestamp_repair
    return {
        "instance": instance.name,
        "libraries": _resolved_repair_libraries(instance, plex),
        "repair": {
            "database_path": repair.database_path,
            "allowed_prefixes": repair.allowed_prefixes,
            "max_files_per_folder": repair.max_files_per_folder,
            "scan_timeout_seconds": repair.scan_timeout_seconds,
            "poll_interval_seconds": repair.poll_interval_seconds,
            "heartbeat_seconds": repair.heartbeat_seconds,
        },
    }


def _remote_recovery_required(instance_name: str | None = None) -> bool:
    workers = {
        instance.timestamp_repair.worker
        for instance in config.instances
        if instance.timestamp_repair.enabled
        and instance.timestamp_repair.worker != "local"
    }
    now = time.monotonic()
    if instance_name:
        assigned = next((
            instance for instance in config.instances
            if instance.name == instance_name
        ), None)
        if (
            not assigned
            or not assigned.timestamp_repair.enabled
            or assigned.timestamp_repair.worker == "local"
        ):
            return False
        workers = {assigned.timestamp_repair.worker}
    for name in workers:
        with _remote_repair_lock:
            pending_instance = (
                str(_remote_pending_repair.get("instance", "")).strip() or None
                if _remote_pending_repair.get("worker") == name else None
            )
        cached = _worker_recovery_cache.get(name)
        if cached and now - cached[0] < 5:
            active_instance, unverified = cached[1], cached[2]
            if unverified and pending_instance:
                active_instance = pending_instance
            if (
                active_instance is not None
                and (instance_name is None or active_instance in {"*", instance_name})
            ):
                return True
            continue
        worker = _repair_worker(name)
        active_instance = None
        unverified = True
        if worker:
            try:
                status = RepairWorkerClient(worker, timeout=3).status()
                active = status.get("active_transaction")
                active_instance = (
                    str(active.get("instance", "")).strip() or "*"
                    if isinstance(active, dict) else None
                )
                unverified = False
                if active_instance is None:
                    _clear_remote_pending(name)
            except Exception:
                # Unavailability alone is not a recovery transaction. A
                # persisted dispatch marker is affirmative evidence that this
                # controller may have handed the worker a repair.
                active_instance = pending_instance
        _worker_recovery_cache[name] = (now, active_instance, unverified)
        if (
            active_instance is not None
            and (instance_name is None or active_instance in {"*", instance_name})
        ):
            return True
    return False


def _combined_recovery_required(instance_name: str,
                                operation: str = "maintenance") -> bool:
    scoped_instance = instance_name if operation == "empty_trash" else None
    return timestamp_repair.has_active_transaction(
        instance_name, operation,
    ) or _remote_recovery_required(scoped_instance)


set_recovery_check(_combined_recovery_required)


def _repair_readiness(instance) -> tuple[bool, str]:
    repair = instance.timestamp_repair
    if not repair.enabled:
        return False, "Setup required"
    if repair.worker == "local":
        if not os.path.isfile(repair.database_path):
            return False, "Database is not mounted"
        missing = [path for path in repair.allowed_prefixes if not os.path.isdir(path)]
        if missing:
            return False, "Repair media is not mounted"
        return True, "Ready to audit"
    worker = _repair_worker(repair.worker)
    if not worker:
        return False, "Worker is not configured"
    try:
        health = RepairWorkerClient(worker, timeout=3).health()
        if health.get("recovery_required"):
            return False, "Worker recovery required"
        return True, f"Worker {worker.name} connected"
    except Exception:
        return False, f"Worker {worker.name} unavailable"


@app.route("/api/logs", methods=["GET"])
@require_auth
def api_logs():
    return jsonify({
        "files": log_manager.list_files(),
        "directory": LOG_DIR,
        "policy": {
            "max_file_size_mb": config.log_max_file_size_mb,
            "max_total_size_mb": config.log_max_total_size_mb,
            "retention_days": config.log_retention_days,
        },
    })


@app.route("/api/logs/<path:filename>", methods=["GET"])
@require_auth
def api_log_content(filename: str):
    result = log_manager.read_tail(filename)
    if result is None:
        return jsonify({"error": "Log file not found"}), 404
    return jsonify(result)


@app.route("/api/logs/<path:filename>/download", methods=["GET"])
@require_auth
def api_log_download(filename: str):
    if log_manager.resolve_file(filename) is None:
        return jsonify({"error": "Log file not found"}), 404
    return send_from_directory(
        str(log_manager.log_dir),
        filename,
        as_attachment=True,
    )


def _trigger(instance_name: str, library_name: str, dry_run: bool = False):
    with _runtime_lock:
        live_config = config
        if not live_config.features.trash_removal:
            return False
        inst = next((i for i in live_config.instances if i.name == instance_name), None)
        lib = next((l for l in inst.libraries
                    if l.name == library_name), None) if inst else None
        plex = plex_clients.get(inst.name) if inst else None
    if not inst or not lib:
        return False
    if plex is None:
        return False
    def _run():
        plex_checks = runner.run_instance_checks(inst, plex)
        runner.run_library(inst, lib, live_config, plex,
                           plex_checks=plex_checks, dry_run=dry_run, manual=True)
    threading.Thread(target=_run, daemon=True).start()
    return True


# ── Wizard / Config endpoints ─────────────────────────────────────────────────

def _is_valid_plex_url(url: str) -> tuple[bool, str]:
    """
    Return (ok, reason). Accepts http(s) URLs pointing at a Plex server.
    Rejects non-http schemes and known cloud metadata endpoints.
    Port is intentionally unrestricted — Plex supports custom ports.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False, "Invalid URL"
    if parsed.scheme not in ("http", "https"):
        return False, "URL must use http or https"
    if not parsed.hostname:
        return False, "URL must include a hostname"
    if parsed.username or parsed.password:
        return False, "Credentials must not be embedded in the URL"
    # Block cloud metadata endpoints (AWS/GCP/Azure instance identity)
    host = parsed.hostname or ""
    _metadata_hosts = {"169.254.169.254", "metadata.google.internal", "fd00:ec2::254"}
    if host in _metadata_hosts:
        return False, "URL targets a cloud metadata address"
    try:
        address = ipaddress.ip_address(host)
        if (address.is_link_local or address.is_multicast or
                address.is_unspecified or address.is_reserved):
            return False, "URL targets a non-routable or reserved address"
    except ValueError:
        pass
    return True, ""


@app.route("/api/wizard/test-plex", methods=["POST"])
@require_auth
def api_test_plex():
    """Test a Plex connection and return available libraries."""
    data  = request.get_json(silent=True) or {}
    url   = data.get("url", "").rstrip("/")
    token = data.get("token", "")
    if not url or not token:
        return jsonify({"ok": False, "error": "URL and token are required"}), 400
    ok, reason = _is_valid_plex_url(url)
    if not ok:
        return jsonify({"ok": False, "error": reason}), 400
    try:
        plex = PlexClient(url, token)
        reachable = plex.check_reachable()
        if not reachable["pass"]:
            return jsonify({"ok": False, "error": reachable["detail"]})
        sections = plex.get_sections()
        return jsonify({"ok": True, "libraries": sections, "detail": reachable["detail"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/plex/auth/start", methods=["POST"])
@require_auth
def api_plex_auth_start():
    """Create a Plex PIN and return the official browser authorization URL."""
    try:
        return jsonify({"ok": True, **plex_auth.start_auth()})
    except plex_auth.PlexAuthError as exc:
        logger.warning("Could not start Plex authorization: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    except Exception as exc:
        logger.warning(
            "Could not start Plex authorization (%s)", type(exc).__name__
        )
        return jsonify({
            "ok": False,
            "error": "Plex authorization could not be started",
        }), 502


@app.route("/api/plex/auth/status/<state>", methods=["GET"])
@require_auth
def api_plex_auth_status(state: str):
    """Poll a Plex PIN and discover reachable servers once it is claimed."""
    try:
        return jsonify(plex_auth.poll_auth(state))
    except plex_auth.PlexAuthError as exc:
        logger.warning("Could not complete Plex authorization: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    except Exception as exc:
        logger.warning(
            "Could not complete Plex authorization (%s)", type(exc).__name__
        )
        return jsonify({
            "ok": False,
            "error": "Plex authorization could not be completed",
        }), 502


@app.route("/api/plex/auth/cancel/<state>", methods=["POST"])
@require_auth
def api_plex_auth_cancel(state: str):
    """Cancel a pending Plex PIN after the user closes or cancels sign-in."""
    plex_auth.cancel_auth(state)
    return jsonify({"ok": True})


@app.route("/api/wizard/browse", methods=["POST"])
@require_auth
def api_browse():
    """Browse filesystem directories for path selection.

    Restricted to BROWSE_ROOTS (comma-separated env var, default /mnt,/media,/data,/home).
    Requests for paths outside these roots are rejected.
    """
    roots_raw = os.environ.get("BROWSE_ROOTS", "/mnt,/media,/data,/home")
    browse_roots = [
        os.path.realpath(os.path.normpath(r.strip()))
        for r in roots_raw.split(",") if r.strip()
    ]
    data = request.get_json(silent=True) or {}
    raw_path = data.get("path")
    if raw_path is None or str(raw_path).strip() == "":
        return jsonify(_browse_root_response(browse_roots))

    try:
        path = os.path.realpath(os.path.normpath(raw_path))
        if not _path_within_roots(path, browse_roots):
            return jsonify({"ok": False, "error": "Path is outside allowed browse roots"}), 403
        if not os.path.exists(path):
            return jsonify({"ok": False, "error": f"Path does not exist: {path}"}), 400
        return jsonify({
            "ok": True,
            "path": path,
            "parent": _browse_parent(path, browse_roots),
            "entries": _browse_entries(path),
            "selectable": True,
        })
    except PermissionError:
        return jsonify({"ok": False, "error": f"Permission denied: {path}"}), 403
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _path_within_roots(path: str, roots: list[str]) -> bool:
    for root in roots:
        try:
            if os.path.commonpath((path, root)) == root:
                return True
        except ValueError:
            continue
    return False


def _browse_root_response(roots: list[str]) -> dict:
    entries = [
        {"name": root, "path": root, "is_link": os.path.islink(root)}
        for root in roots if os.path.isdir(root)
    ]
    return {
        "ok": True,
        "path": "",
        "parent": None,
        "entries": entries,
        "selectable": False,
        "empty_message": (
            "No allowed browse roots are available. Check the container "
            "volume mappings or BROWSE_ROOTS."
        ),
    }


def _browse_entries(path: str) -> list[dict]:
    return [
        {"name": entry.name, "path": entry.path, "is_link": entry.is_symlink()}
        for entry in sorted(os.scandir(path), key=lambda candidate: candidate.name)
        if entry.is_dir(follow_symlinks=False)
    ]


def _browse_parent(path: str, roots: list[str]):
    if path in roots:
        return ""
    parent = os.path.dirname(path)
    if parent != path and _path_within_roots(parent, roots):
        return parent
    return None


_PROVIDER_ENV_MAP = {
    "realdebrid": "RD_API_KEY",
    "alldebrid":  "AD_API_KEY",
    "torbox":     "TB_API_KEY",
    "debridlink": "DL_API_KEY",
}


def _build_path_cfg(p: dict, env_vars_needed: list) -> dict:
    path_cfg = {
        "path":          p.get("path", ""),
        "type":          p.get("type", "physical"),
        "min_threshold": int(p.get("min_threshold", 90)),
    }
    pcs = p.get("provider_checks", [])
    if not pcs:
        return path_cfg
    path_cfg["provider_checks"] = [
        {"type": pc.get("type", ""), "api_key": ""}
        for pc in pcs
    ]
    for pc in pcs:
        ptype    = pc.get("type", "")
        env_name = _PROVIDER_ENV_MAP.get(ptype)
        if env_name and not any(e["name"] == env_name for e in env_vars_needed):
            env_vars_needed.append({
                "name":        env_name,
                "description": f"{ptype.capitalize()} API key (optional — for provider health checks)",
                "value":       "",
            })
    return path_cfg


def _build_library_cfg(lib: dict, env_vars_needed: list) -> dict:
    lib_cfg = {
        "name":  lib.get("name", ""),
        "type":  lib.get("type", "physical"),
        "paths": [_build_path_cfg(p, env_vars_needed) for p in lib.get("paths", [])],
    }
    cron = str(lib.get("cron", "")).strip()
    if cron:
        lib_cfg["cron"] = cron
    if lib.get("section_id") is not None:
        lib_cfg["section_id"] = str(lib["section_id"])
    lib_cfg["refresh_enabled"] = bool(lib.get("refresh_enabled", False))
    lib_cfg["refresh_cron"] = str(lib.get("refresh_cron", "0 * * * *"))
    lib_cfg["refresh_guard_minutes"] = int(
        lib.get("refresh_guard_minutes", 15)
    )
    return lib_cfg


def _build_instance_cfg(inst: dict, store_tokens: bool, env_vars_needed: list) -> dict:
    inst_name = inst.get("name", "")
    token     = inst.get("token", "")
    safe_name = inst_name.upper().replace(" ", "_").replace("-", "_")

    if not store_tokens:
        env_vars_needed.append({
            "name":        f"PLEX_TOKEN_{safe_name}",
            "description": f"Plex token for '{inst_name}'",
            "value":       token,
        })

    instance_cfg = {
        "name":      inst_name,
        "url":       inst.get("url", ""),
        "token":     token if store_tokens else "",
        **({"machine_id": str(inst["machine_id"])}
           if inst.get("machine_id") else {}),
        "libraries": [_build_library_cfg(lib, env_vars_needed) for lib in inst.get("libraries", [])],
    }
    repair = inst.get("timestamp_repair", {})
    if isinstance(repair, dict):
        instance_cfg["timestamp_repair"] = {
            "enabled": bool(repair.get("enabled", False)),
            "worker": str(repair.get("worker", "local")) or "local",
            "database_path": str(repair.get("database_path", "")),
            "allowed_prefixes": [
                str(path).strip() for path in repair.get("allowed_prefixes", [])
                if str(path).strip()
            ],
            "max_files_per_folder": int(repair.get("max_files_per_folder", 5)),
            "scan_timeout_seconds": int(repair.get("scan_timeout_seconds", 1800)),
            "poll_interval_seconds": int(repair.get("poll_interval_seconds", 5)),
            "heartbeat_seconds": int(repair.get("heartbeat_seconds", 30)),
        }
    metadata_health = inst.get("metadata_health", {})
    if isinstance(metadata_health, dict):
        instance_cfg["metadata_health"] = {
            "ignored_libraries": [
                str(name).strip()
                for name in metadata_health.get("ignored_libraries", [])
                if str(name).strip()
            ],
        }
    return instance_cfg


def _repair_lock_active() -> bool:
    """Configuration must hold still while a repair transaction is open."""
    repair_status = timestamp_repair.status()
    with _remote_repair_lock:
        remote_running = bool(_remote_repair.get("running"))
    return bool(
        repair_status.get("running")
        or repair_status.get("active_transaction")
        or remote_running
    )


@app.route("/api/settings/<section>", methods=["PATCH"])
@require_auth
@_serialized_config_write
def api_settings_section_save(section: str):
    """Write only the fields the named Settings section owns.

    The wizard still posts a whole configuration during first-run setup. Every
    later edit comes through here, so a section can never overwrite a field it
    does not control - including one the browser failed to render.
    """
    section = str(section).strip().lower()
    if not known_section(section):
        return jsonify({"error": f"Unknown settings section: {section}"}), 404
    if _repair_lock_active():
        return jsonify({
            "error": "Configuration cannot change during timestamp repair or recovery",
        }), 409
    data = request.get_json(silent=True) or {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            existing = yaml.safe_load(handle) or {}
    except OSError:
        existing = {}
    if not isinstance(existing, dict):
        existing = {}

    merged = apply_section(existing, section, data)
    if section == "security":
        merged["auth"] = _merged_auth_block(existing, data)
    if section == "mark-watched":
        merged["mark_watched"] = _merged_mark_watched_block(existing, data)

    # Plex tokens are write-once from the browser: a blank field means
    # "unchanged", never "clear it".
    known_tokens = {
        str(item.get("name", "")): str(item.get("token", ""))
        for item in existing.get("plex_instances", []) if isinstance(item, dict)
    }
    known_tokens.update({
        instance.name: instance.token for instance in config.instances
        if not known_tokens.get(instance.name)
    })
    runtime_tokens = {}
    for item in merged.get("plex_instances", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        token = str(item.get("token", "")) or known_tokens.get(name, "")
        runtime_tokens[name] = token
        item["token"] = token

    try:
        _save_and_apply(
            merged, runtime_tokens=runtime_tokens,
            # Plex servers can be saved before Trash Removal paths exist;
            # pathless libraries stay fail-closed in the runner until set.
            require_paths=None if section == "trash-removal" else False,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("Could not save the %s settings section", section)
        return jsonify({"error": str(exc)}), 500
    return jsonify({
        "section": section,
        "message": f"{section.replace('-', ' ').title()} settings saved and applied.",
    })


def _merged_auth_block(existing: dict, data: dict) -> dict:
    """Keep the stored password and API token unless new ones are supplied."""
    current = existing.get("auth", {})
    current = dict(current) if isinstance(current, dict) else {}
    username = str(data.get("auth_username", "")).strip()
    password = str(data.get("auth_password", "")).strip()
    if username and password:
        merged = {"username": username, "password_hash": hash_password(password)}
        if current.get("api_token_hash"):
            merged["api_token_hash"] = current["api_token_hash"]
        if current.get("users"):
            merged["users"] = current["users"]
        return merged
    return current


def _merged_mark_watched_block(existing: dict, data: dict) -> dict:
    """Never let a blank webhook secret field erase the stored secret."""
    current = existing.get("mark_watched", {})
    current = dict(current) if isinstance(current, dict) else {}
    submitted = data.get("mark_watched", {})
    submitted = dict(submitted) if isinstance(submitted, dict) else {}
    merged = {**current, **submitted}
    merged.pop("webhook_secret_configured", None)
    if not str(submitted.get("webhook_secret", "")):
        if current.get("webhook_secret"):
            merged["webhook_secret"] = current["webhook_secret"]
        else:
            merged.pop("webhook_secret", None)
    return merged


# First-run setup only. Every later edit goes through /api/settings/<section>,
# which writes just that section's fields.
@app.route("/api/wizard/save", methods=["POST"])
@require_auth
@_serialized_config_write
def api_wizard_save():
    """
    Receive wizard form data and write config.yml.
    Expects JSON matching the config structure.
    If store_tokens=True, writes tokens directly to config (less secure but simpler).
    If store_tokens=False, leaves tokens blank and returns the env var names needed.
    """
    if _repair_lock_active():
        return jsonify({
            "ok": False,
            "error": "Configuration cannot change during timestamp repair or recovery",
        }), 409
    data         = request.get_json(silent=True) or {}
    store_tokens = bool(data.get("store_tokens", False))

    # Load existing config to preserve auth, providers and other blocks
    # that aren't managed by the wizard/settings form
    try:
        with open(CONFIG_PATH, "r") as f:
            existing = yaml.safe_load(f) or {}
    except Exception:
        existing = {}
    existing_logging = (
        existing.get("logging", {})
        if isinstance(existing.get("logging", {}), dict)
        else {}
    )
    existing_mark_watched = (
        existing.get("mark_watched", {})
        if isinstance(existing.get("mark_watched", {}), dict) else {}
    )
    submitted_mark_watched = data.get("mark_watched", existing_mark_watched)
    if not isinstance(submitted_mark_watched, dict):
        submitted_mark_watched = {}
    merged_mark_watched = {**existing_mark_watched, **submitted_mark_watched}
    merged_mark_watched.pop("webhook_secret_configured", None)
    if not str(submitted_mark_watched.get("webhook_secret", "")):
        if existing_mark_watched.get("webhook_secret"):
            merged_mark_watched["webhook_secret"] = existing_mark_watched["webhook_secret"]
        else:
            merged_mark_watched.pop("webhook_secret", None)

    cfg = {
        "features": data.get("features", existing.get("features", {})),
        "mark_watched": merged_mark_watched,
        "discord_webhook": data.get("discord_webhook", ""),
        "log_level": data.get("log_level", existing.get("log_level", "INFO")),
        "notify": {
            "on_emptied":     data.get("notify_emptied",     data.get("notify_success", True)),
            "on_health_fail": data.get("notify_health_fail", data.get("notify_failure", True)),
            "on_error":       data.get("notify_error",       True),
            "on_clean":       data.get("notify_clean",       False),
            "on_skip":        data.get("notify_skip",        False),
        },
        "notifications": {
            "destinations": data.get(
                "notification_destinations",
                existing.get("notifications", {}).get("destinations", [])
                if isinstance(existing.get("notifications", {}), dict)
                else [],
            ),
        },
        "plex_instances": [],
        "timestamp_repair_workers": data.get(
            "repair_workers", existing.get("timestamp_repair_workers", []),
        ),
        "clean_bundles_before_empty": bool(
            data.get(
                "clean_bundles_before_empty",
                existing.get("clean_bundles_before_empty", False),
            )
        ),
        "max_trash_items": int(
            data.get("max_trash_items", existing.get("max_trash_items", 1000))
        ),
        "max_trash_percent": float(
            data.get("max_trash_percent", existing.get("max_trash_percent", 25))
        ),
        "schedule": {
            "default_cron": str(
                data.get(
                    "default_cron",
                    existing.get("schedule", {}).get("default_cron", "0 * * * *"),
                )
            ),
        },
        "logging": {
            "max_file_size_mb": int(
                data.get(
                    "log_max_file_size_mb",
                    existing_logging.get("max_file_size_mb", 5),
                )
            ),
            "max_total_size_mb": int(
                data.get(
                    "log_max_total_size_mb",
                    existing_logging.get("max_total_size_mb", 50),
                )
            ),
            "retention_days": int(
                data.get(
                    "log_retention_days",
                    existing_logging.get("retention_days", 14),
                )
            ),
        },
    }

    # Preserve existing auth block unless new credentials are being set
    wiz_user = data.get("auth_username", "").strip()
    wiz_pass = data.get("auth_password", "").strip()
    if wiz_user and wiz_pass:
        cfg["auth"] = {"username": wiz_user, "password_hash": hash_password(wiz_pass)}
        if isinstance(existing.get("auth"), dict) and existing["auth"].get("api_token_hash"):
            cfg["auth"]["api_token_hash"] = existing["auth"]["api_token_hash"]
    elif "auth" in existing:
        cfg["auth"] = existing["auth"]

    if "providers" in existing:
        cfg["providers"] = existing["providers"]

    env_vars_needed: list = []
    existing_tokens = {
        str(instance.get("name", "")): str(instance.get("token", ""))
        for instance in existing.get("plex_instances", [])
        if isinstance(instance, dict)
    }
    existing_tokens.update({
        instance.name: instance.token for instance in config.instances
        if instance.name not in existing_tokens
    })
    submitted_instances = []
    submitted_source = data.get("instances", [])
    if not isinstance(submitted_source, list):
        submitted_source = []
    for submitted in submitted_source:
        item = dict(submitted)
        if not str(item.get("token", "")):
            item["token"] = existing_tokens.get(str(item.get("name", "")), "")
        submitted_instances.append(item)
    cfg["plex_instances"] = [
        _build_instance_cfg(inst, store_tokens, env_vars_needed)
        for inst in submitted_instances
    ]
    try:
        runtime_tokens = {
            str(instance.get("name", "")): str(instance.get("token", ""))
            for instance in submitted_instances
        }
        # Plex settings can be staged before Trash Removal paths are known.
        # Pathless libraries remain fail-closed in the runner until configured.
        _save_and_apply(cfg, runtime_tokens=runtime_tokens)
        return jsonify({
            "ok":              True,
            "store_tokens":    store_tokens,
            "env_vars_needed": env_vars_needed,
            "message":         "Config saved and applied immediately.",
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/notifications/test", methods=["POST"])
@require_auth
def api_notifications_test():
    """Send a test through one unsaved Apprise destination."""
    data = request.get_json(silent=True) or {}
    try:
        raw = {"notifications": {"destinations": [data]}}
        _validate_notifications(raw)
        destination = NotificationDestination(
            name=str(data["name"]).strip(),
            service=str(data.get("service", "custom")).strip().lower(),
            url=str(data["url"]).strip(),
            enabled=True,
            events=list(data.get("events", NOTIFICATION_EVENTS)),
        )
        if notifications.test_destination(destination):
            return jsonify({"ok": True, "message": "Test notification sent."})
        return jsonify({
            "ok": False,
            "error": "Apprise rejected the destination or delivery failed.",
        }), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


def _runtime_instance_settings_dict(runtime_instance) -> dict:
    """Serialize one live Plex instance back to Settings-safe units."""
    instance = asdict(runtime_instance)
    for library in instance.get("libraries", []):
        for path in library.get("paths", []):
            threshold = float(path.get("min_threshold", 0.9))
            path["min_threshold"] = round(
                threshold * 100 if threshold <= 1 else threshold, 2,
            )
    return instance


@app.route("/api/config/load", methods=["GET"])
@require_auth
def api_config_load():
    """Return current config.yml contents for the settings editor."""
    load_warning = ""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.exception("Could not read Settings configuration")
        raw = {}
        load_warning = f"Could not read config.yml: {type(exc).__name__}: {exc}"
    try:
        recovered_instances = False
        raw_instances = raw.get("plex_instances")
        if (not isinstance(raw_instances, list) or not raw_instances) and config.instances:
            raw["plex_instances"] = [
                _runtime_instance_settings_dict(instance)
                for instance in config.instances
            ]
            recovered_instances = True
            logger.warning(
                "Settings config lacked Plex instances; recovered %s from runtime",
                len(raw["plex_instances"]),
            )
        # Do not send password hashes back to the browser.
        if isinstance(raw.get("auth"), dict):
            raw["auth"].pop("password_hash", None)
            raw["auth"].pop("api_token_hash", None)
            for user in raw["auth"].get("users", []):
                if isinstance(user, dict):
                    user.pop("password_hash", None)
        mark_settings = raw.get("mark_watched", {})
        if not isinstance(mark_settings, dict):
            mark_settings = {}
        raw["mark_watched"] = mark_settings
        mark_settings["webhook_secret_configured"] = bool(
            config.mark_watched.webhook_secret
        )
        mark_settings.pop("webhook_secret", None)
        # Show the values actually in force, not just what the file happens
        # to name, so a config predating these keys still renders correctly.
        mark_settings.setdefault(
            "give_up_after_hours", config.mark_watched.give_up_after_hours,
        )
        mark_settings.setdefault("workers", config.mark_watched.workers)
        mark_settings.setdefault(
            "scan_on_import", config.mark_watched.scan_on_import,
        )
        for instance in raw.get("plex_instances", []):
            if isinstance(instance, dict):
                instance["token_configured"] = bool(instance.get("token"))
                instance["token"] = ""
        return jsonify({
            "ok": True, "config": raw,
            "recovered_instances": recovered_instances,
            "warning": load_warning,
        })
    except Exception as e:
        logger.exception("Could not prepare Settings configuration")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/providers/status", methods=["GET"])
@require_auth
def api_providers_status():
    """Return account status for all configured providers."""
    result = {}
    for provider, env_name in _PROVIDER_ENV_KEYS.items():
        key = get_api_key(provider, config=config)
        if key:
            status = get_account_status(provider, key)
            if os.environ.get(env_name, ""):
                status["source"] = "env"
                status["source_name"] = env_name
            elif config.providers.get(provider, {}).get("api_key", ""):
                status["source"] = "config"
                status["source_name"] = "config.yml"
            else:
                status["source"] = "path"
                status["source_name"] = "path provider check"
            result[provider] = status
        else:
            result[provider] = {"ok": False, "error": "no_key"}
    return jsonify(result)


@app.route("/api/providers/save", methods=["POST"])
@require_auth
@_serialized_config_write
def api_providers_save():
    """Save provider API keys to config.yml providers block."""
    global config
    data = request.get_json(silent=True) or {}
    try:
        with open(CONFIG_PATH, "r") as f:
            raw = yaml.safe_load(f) or {}
        providers = raw.get("providers", {})
        for provider, key in data.items():
            key = key.strip()
            if key:
                providers[provider] = {"api_key": key}
            else:
                providers.pop(provider, None)
        if providers:
            raw["providers"] = providers
        else:
            raw.pop("providers", None)
        new_config = _validate_raw_config(raw)
        atomic_write_yaml(CONFIG_PATH, raw)
        _apply_runtime_config(new_config)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _require_browser_auth():
    if not auth_enabled(config) or not is_authenticated() or not _is_admin():
        return jsonify({
            "ok": False,
            "error": "Sign in as an administrator to manage API tokens",
        }), 403
    return None


_USER_PERMISSIONS = {
    "dashboard", "trash_removal", "library_refresh", "mark_watched",
    "metadata_health", "timestamp_repair", "settings",
}


@app.route("/api/users", methods=["GET"])
@require_auth
def api_users():
    if not _is_admin():
        return jsonify({"error": "Administrator role required"}), 403
    return jsonify({"users": [{
        "username": user.username, "role": user.role,
        "permissions": user.permissions,
    } for user in config.users], "permissions": sorted(_USER_PERMISSIONS)})


@app.route("/api/users", methods=["POST"])
@require_auth
@_serialized_config_write
def api_users_save():
    if not _is_admin():
        return jsonify({"ok": False, "error": "Administrator role required"}), 403
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    role = str(data.get("role", "user")).lower()
    permissions = data.get("permissions", [])
    if not username or role not in {"admin", "user"} or not isinstance(permissions, list):
        return jsonify({"ok": False, "error": "Valid username, role and permissions required"}), 400
    permissions = sorted(set(str(value) for value in permissions) & _USER_PERMISSIONS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        auth = raw.setdefault("auth", {})
        users = auth.setdefault("users", [])
        existing = next((user for user in users
                         if str(user.get("username", "")) == username), None)
        if existing is None:
            if len(password) < 8:
                return jsonify({"ok": False, "error": "New users require an 8-character password"}), 400
            existing = {"username": username}
            users.append(existing)
        elif not password:
            password_hash = existing.get("password_hash", "")
        if password:
            password_hash = hash_password(password)
        if not password_hash:
            return jsonify({"ok": False, "error": "Password required"}), 400
        existing.update({"username": username, "password_hash": password_hash,
                         "role": role, "permissions": permissions})
        if not any(user.get("role") == "admin" for user in users) and not (
            auth.get("username") or get_env("MEDIAMENDER_USERNAME")
        ):
            return jsonify({"ok": False, "error": "At least one administrator is required"}), 409
        _save_and_apply(raw, require_paths=False)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/users/<username>", methods=["DELETE"])
@require_auth
@_serialized_config_write
def api_user_delete(username: str):
    if not _is_admin():
        return jsonify({"ok": False, "error": "Administrator role required"}), 403
    if username == _current_username():
        return jsonify({"ok": False, "error": "You cannot delete your active account"}), 409
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        auth = raw.get("auth", {})
        users = auth.get("users", []) if isinstance(auth, dict) else []
        remaining = [user for user in users if str(user.get("username", "")) != username]
        if len(remaining) == len(users):
            return jsonify({"ok": False, "error": "User not found"}), 404
        if not any(user.get("role") == "admin" for user in remaining) and not auth.get("username"):
            return jsonify({"ok": False, "error": "At least one administrator is required"}), 409
        auth["users"] = remaining
        _save_and_apply(raw, require_paths=False)
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


def _update_api_token_hash(token_hash: str = ""):
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    auth = raw.get("auth")
    if not auth_enabled(config):
        raise ValueError("Set login credentials before managing an API token")
    if not isinstance(auth, dict):
        auth = {}
        raw["auth"] = auth
    if token_hash:
        auth["api_token_hash"] = token_hash
    else:
        auth.pop("api_token_hash", None)
    _save_and_apply(raw)


@app.route("/api/auth/token", methods=["GET"])
@require_auth
def api_auth_token():
    """Return API-token status without disclosing bearer credentials."""
    denied = _require_browser_auth()
    if denied:
        return denied
    configured_by_env = bool(get_env("MEDIAMENDER_API_TOKEN"))
    return jsonify({
        "ok": True,
        "configured": configured_by_env or bool(config.auth_api_token_hash),
        "source": "environment" if configured_by_env else "config",
    })


@app.route("/api/auth/token", methods=["POST"])
@require_auth
@_serialized_config_write
def api_auth_token_generate():
    """Generate or rotate an API token and reveal it exactly once."""
    denied = _require_browser_auth()
    if denied:
        return denied
    if get_env("MEDIAMENDER_API_TOKEN"):
        return jsonify({
            "ok": False,
            "error": "API token is managed by MEDIAMENDER_API_TOKEN",
        }), 409
    try:
        token = generate_api_token()
        _update_api_token_hash(hash_api_token(token))
        return jsonify({
            "ok": True,
            "token": token,
            "message": "Copy this token now; it cannot be shown again.",
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/auth/token", methods=["DELETE"])
@require_auth
@_serialized_config_write
def api_auth_token_revoke():
    """Revoke the independently stored API token."""
    denied = _require_browser_auth()
    if denied:
        return denied
    if get_env("MEDIAMENDER_API_TOKEN"):
        return jsonify({
            "ok": False,
            "error": "Remove MEDIAMENDER_API_TOKEN to revoke this token",
        }), 409
    try:
        _update_api_token_hash()
        return jsonify({"ok": True, "message": "API token revoked"})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/auth/save", methods=["POST"])
@require_auth
@_serialized_config_write
def api_auth_save():
    """Save or clear username/password in config.yml."""
    global config
    if auth_enabled(config) and not _is_admin():
        return jsonify({"ok": False, "error": "Administrator role required"}), 403
    data     = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    clear    = data.get("clear", False)

    try:
        with open(CONFIG_PATH, "r") as f:
            raw = yaml.safe_load(f) or {}

        if clear or (not username and not password):
            raw.pop("auth", None)
        else:
            if not username:
                return jsonify({"ok": False, "error": "Username required"}), 400
            if not password:
                return jsonify({"ok": False, "error": "Password required"}), 400
            existing_auth = raw.get("auth", {})
            raw["auth"] = {
                "username":      username,
                "password_hash": hash_password(password),
            }
            if isinstance(existing_auth, dict) and existing_auth.get("users"):
                raw["auth"]["users"] = existing_auth["users"]
            if isinstance(existing_auth, dict) and existing_auth.get("api_token_hash"):
                raw["auth"]["api_token_hash"] = existing_auth["api_token_hash"]

        new_config = _validate_raw_config(raw)
        atomic_write_yaml(CONFIG_PATH, raw)
        _apply_runtime_config(new_config)

        if clear or not username:
            session.pop("authenticated", None)
        else:
            session["authenticated"] = True
        action = "cleared" if (clear or not username) else f"set for '{username}'"
        return jsonify({"ok": True, "message": f"Auth {action} — takes effect immediately."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# Feature routes live in their own modules. Registered last, so every helper
# and every piece of runtime state they reach through `runtime` already exists.
from src.web import mark_watched as mark_watched_routes  # noqa: E402

app.register_blueprint(mark_watched_routes.bp)
from src.web import timestamp_repair as timestamp_repair_routes  # noqa: E402
app.register_blueprint(timestamp_repair_routes.bp)
from src.web import trash_removal as trash_removal_routes  # noqa: E402
app.register_blueprint(trash_removal_routes.bp)
from src.web import library_health as library_health_routes  # noqa: E402
app.register_blueprint(library_health_routes.bp)


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    app.run(host=host, port=8222, debug=False, use_reloader=False)
