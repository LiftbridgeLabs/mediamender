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
from src.timestamp_repair import TimestampRepairManager
from src.library_refresh import LibraryRefreshManager
from src.mark_watched import (
    MarkWatchedManager, MarkWatchedRuleStore, process_plex_event,
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
    processor=lambda event: process_plex_event(
        event, config, plex_clients, mark_watched_rules,
    ),
    retry_delays=tuple(config.mark_watched.retry_delays),
)
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
_config_file_lock = threading.Lock()
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


def _serialized_config_write(function):
    @wraps(function)
    def decorated(*args, **kwargs):
        with _config_file_lock:
            return function(*args, **kwargs)
    return decorated


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
    if request.endpoint in {"login", "api_timestamp_repair_worker_scan", "api_sonarr_webhook"}:
        return None
    # Non-browser automations with a verified API token do not rely on cookies
    # and therefore are not susceptible to cookie-based CSRF.
    if has_valid_api_token(config):
        return None
    expected = session.get("_csrf_token", "")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        return jsonify({"error": "Invalid or missing CSRF token"}), 403


_PERMISSION_PREFIXES = (
    ("/api/mark-watched", "mark_watched"),
    ("/api/library-refresh", "library_refresh"),
    ("/api/metadata-audit", "metadata_health"),
    ("/api/timestamp-repair", "timestamp_repair"),
    ("/api/run", "trash_removal"),
    ("/api/dryrun", "trash_removal"),
    ("/api/checks", "trash_removal"),
    ("/api/scheduling", "trash_removal"),
    ("/api/status", "dashboard"),
    ("/api/history", "dashboard"),
    ("/api/config", "settings"),
    ("/api/providers", "settings"),
    ("/api/notifications", "settings"),
    ("/api/plex", "settings"),
    ("/api/wizard", "settings"),
    ("/api/logs", "settings"),
    ("/api/users", "settings"),
    ("/api/auth", "settings"),
)


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


def _valid_sonarr_webhook_auth() -> bool:
    """Authenticate automation without granting it a browser session."""
    if has_valid_api_token(config):
        return True
    supplied = request.headers.get("X-Sonarr-Webhook-Secret", "")
    authorization = request.headers.get("Authorization", "")
    if not supplied and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    expected = config.mark_watched.webhook_secret
    return bool(expected and supplied and secrets.compare_digest(supplied, expected))


def _current_username() -> str:
    return current_identity(config)["username"] or "default"


def _is_admin() -> bool:
    return has_valid_api_token(config) or current_identity(config)["role"] == "admin"


def _mark_watched_library(instance_name: str, library_name: str):
    with _runtime_lock:
        for instance in config.instances:
            if instance.name != instance_name:
                continue
            for library in instance.libraries:
                if library.name == library_name:
                    return instance, library, plex_clients.get(instance.name)
    return None, None, None


def _ensure_sonarr_webhook_secret() -> str:
    """Return the runtime secret, generating and saving one when needed."""
    if config.mark_watched.webhook_secret:
        return config.mark_watched.webhook_secret
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except OSError:
        raw = {}
    mark_settings = raw.get("mark_watched")
    if not isinstance(mark_settings, dict):
        mark_settings = {}
        raw["mark_watched"] = mark_settings
    secret = secrets.token_urlsafe(32)
    mark_settings["webhook_secret"] = secret
    _save_and_apply(raw, require_paths=False)
    return secret


@app.route("/api/webhooks/sonarr", methods=["POST"])
def api_sonarr_webhook():
    """Accept only completed imports and hand them to the durable worker."""
    if not _valid_sonarr_webhook_auth():
        return jsonify({"ok": False, "error": "Unauthorized Sonarr webhook"}), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "A JSON webhook payload is required"}), 400
    connection_id = request.headers.get("X-MediaMender-Connection-ID", "").strip()
    if connection_id:
        owner = sonarr_connection.owner_for(connection_id)
        if not owner:
            return jsonify({"ok": False, "error": "Unknown Sonarr connection"}), 401
        payload = dict(payload)
        payload["_mediamender_user"] = owner
        payload["_mediamender_connection"] = connection_id
    try:
        record, created = mark_watched.enqueue(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if record is None:
        return jsonify({"ok": True, "test": True, "message": "Sonarr webhook authenticated"})
    return jsonify({
        "ok": True,
        "queued": created,
        "duplicate": not created,
        "job_id": record["id"],
        "status": record["status"],
    }), 202 if created else 200


@app.route("/api/mark-watched/status", methods=["GET"])
@require_auth
def api_mark_watched_status():
    return jsonify(mark_watched.status())


def _sonarr_environment_instances() -> list[dict]:
    """Return configured Sonarr URLs and key availability without exposing keys."""
    instances = []
    seen_urls = set()
    for url_variable in sorted(os.environ):
        if not url_variable.startswith("SONARR_") or not url_variable.endswith("_URL"):
            continue
        slug = url_variable[len("SONARR_"):-len("_URL")]
        if not slug:
            continue
        try:
            configured_url = normalize_sonarr_url(os.environ.get(url_variable, ""))
        except ValueError:
            continue
        if configured_url in seen_urls:
            continue
        key_variable = f"SONARR_{slug}_API_KEY"
        instances.append({
            "sonarr_url": configured_url,
            "environment_label": slug,
            "url_variable": url_variable,
            "key_variable": key_variable,
            "api_key": os.environ.get(key_variable, "").strip(),
        })
        seen_urls.add(configured_url)
    return instances


def _sonarr_environment_entry(sonarr_url: str) -> tuple[str, str, str] | None:
    """Return the URL variable, key variable, and key paired to this URL."""
    for instance in _sonarr_environment_instances():
        if instance["sonarr_url"] != sonarr_url:
            continue
        return (
            instance["url_variable"], instance["key_variable"],
            instance["api_key"],
        )
    return None


def _suggest_sonarr_environment_names(sonarr_url: str) -> tuple[str, str]:
    host = urllib.parse.urlparse(sonarr_url).hostname or "main"
    slug = "".join(
        character if character.isalnum() else "_" for character in host
    ).upper().strip("_")
    if slug == "SONARR":
        slug = "MAIN"
    elif slug.startswith("SONARR_"):
        slug = slug[len("SONARR_"):]
    slug = slug or "MAIN"
    return f"SONARR_{slug}_URL", f"SONARR_{slug}_API_KEY"


def _sonarr_api_key(data: dict, sonarr_url: str) -> str:
    """Resolve a typed key, URL-paired environment key, or global fallback."""
    typed = str(data.get("api_key", "")).strip()
    if typed:
        return typed
    entry = _sonarr_environment_entry(sonarr_url)
    if entry and entry[2]:
        return entry[2]
    return os.environ.get("SONARR_API_KEY", "").strip()


def _missing_sonarr_api_key_message(sonarr_url: str) -> str:
    entry = _sonarr_environment_entry(sonarr_url)
    if entry:
        url_variable, key_variable, _key = entry
    else:
        url_variable, key_variable = _suggest_sonarr_environment_names(sonarr_url)
    return (
        "Sonarr API key is required. Enter it here, set the fallback SONARR_API_KEY, "
        f"or pair {url_variable} with {key_variable}."
    )


@app.route("/api/mark-watched/sonarr", methods=["GET"])
@require_auth
def api_mark_watched_sonarr_status():
    saved = sonarr_connection.status()
    saved_by_url = {}
    for connection in saved.get("connections", []):
        try:
            sonarr_url = normalize_sonarr_url(connection.get("sonarr_url", ""))
        except ValueError:
            connection["api_key_available"] = False
            continue
        connection["sonarr_url"] = sonarr_url
        saved_by_url[sonarr_url] = connection

    connections = []
    for configured in _sonarr_environment_instances():
        sonarr_url = configured["sonarr_url"]
        connection = saved_by_url.pop(sonarr_url, None)
        saved_record = connection is not None
        connection = dict(connection or {
            "sonarr_url": sonarr_url,
            "sonarr_instance": configured["environment_label"].replace("_", " ").title(),
            "status": "not_connected",
        })
        connection["configured_from_environment"] = True
        connection["environment_label"] = configured["environment_label"]
        connection["saved_record"] = saved_record
        connection["api_key_available"] = bool(_sonarr_api_key({}, sonarr_url))
        connections.append(connection)

    for sonarr_url, connection in saved_by_url.items():
        connection["configured_from_environment"] = False
        connection["saved_record"] = True
        connection["api_key_available"] = bool(_sonarr_api_key({}, sonarr_url))
        connections.append(connection)

    return jsonify({"ok": True, "connections": connections})


@app.route("/api/mark-watched/sonarr/connect", methods=["POST"])
@require_auth
@_serialized_config_write
def api_mark_watched_sonarr_connect():
    """Use a Sonarr API key once to test and provision the managed webhook."""
    data = request.get_json(silent=True) or {}
    sonarr_url = ""
    callback_url = ""
    try:
        sonarr_url = normalize_sonarr_url(data.get("sonarr_url", ""))
        callback_url = normalize_callback_url(data.get("callback_url", ""))
        api_key = _sonarr_api_key(data, sonarr_url)
        if not api_key:
            raise ValueError(_missing_sonarr_api_key_message(sonarr_url))
        client = SonarrClient(sonarr_url, api_key)
        # Verify the key before creating a local webhook secret.
        sonarr_status = client.system_status()
        webhook_secret = _ensure_sonarr_webhook_secret()
        owner = _current_username()
        pending = sonarr_connection.prepare(sonarr_url, owner)
        result = client.provision_webhook(
            callback_url, webhook_secret, status=sonarr_status,
            connection_id=pending["connection_id"],
        )
        connection = sonarr_connection.success(
            sonarr_url, result, owner=owner,
            connection_id=pending["connection_id"],
        )
        public_connection = {
            key: value for key, value in connection.items()
            if key != "connection_id"
        }
        logger.info(
            "Sonarr webhook %s for %s (notification %s)",
            result["action"], result.get("sonarr_instance", "Sonarr"),
            result.get("notification_id", "unknown"),
        )
        return jsonify({
            "ok": True,
            "connection": public_connection,
            "message": (
                f"Sonarr webhook {result['action']} and its Test event succeeded."
            ),
        })
    except (ValueError, SonarrError) as exc:
        if sonarr_url and callback_url:
            try:
                sonarr_connection.failure(sonarr_url, callback_url, str(exc))
            except OSError:
                logger.warning("Could not persist Sonarr connection failure status")
        logger.warning("Sonarr webhook provisioning failed (%s)", type(exc).__name__)
        return jsonify({"ok": False, "error": str(exc)}), 400
    except OSError:
        logger.exception("Could not save Sonarr webhook configuration")
        return jsonify({
            "ok": False,
            "error": "Sonarr connected, but mediaMender could not save its local configuration",
        }), 500


@app.route("/api/mark-watched/sonarr", methods=["DELETE"])
@require_auth
def api_mark_watched_sonarr_remove():
    """Remove the managed Sonarr webhook, then forget its local status."""
    data = request.get_json(silent=True) or {}
    try:
        sonarr_url = normalize_sonarr_url(data.get("sonarr_url", ""))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    connection = sonarr_connection.get(sonarr_url)
    if connection is None:
        return jsonify({"ok": False, "error": "Sonarr connection was not found"}), 404
    api_key = _sonarr_api_key(data, sonarr_url)
    remote_record = (
        connection.get("status") == "connected"
        or bool(connection.get("notification_id"))
    )

    # Removing the local record always succeeds, even when the webhook in Sonarr can't be deleted.
    # Previously a missing key or an unreachable Sonarr aborted the whole thing, which left a
    # broken connection permanently stuck in the list: the one state you most want to remove was
    # the one you couldn't. Anything left behind in Sonarr is reported rather than hidden, since
    # that connection is now unmanaged and has to be deleted there by hand.
    removed_webhooks = 0
    leftover = ""
    if remote_record:
        if not api_key:
            leftover = "no Sonarr API key was available, so its webhook was left in place"
        else:
            try:
                removed_webhooks = SonarrClient(sonarr_url, api_key).remove_webhook()
            except (ValueError, SonarrError) as exc:
                leftover = f"its webhook could not be deleted from Sonarr ({exc})"

    try:
        sonarr_connection.remove(sonarr_url)
    except OSError:
        logger.exception("Could not remove saved Sonarr connection status")
        return jsonify({"ok": False, "error": "Could not remove saved Sonarr status"}), 500

    if leftover:
        message = f"Connection removed from mediaMender, but {leftover}"
    elif remote_record:
        message = "Managed Sonarr webhook and saved connection removed"
    else:
        message = "Failed Sonarr connection record removed"
    return jsonify({
        "ok": True,
        "removed_webhooks": removed_webhooks,
        "webhook_left_behind": bool(leftover),
        "message": message,
    })


@app.route("/api/mark-watched/libraries", methods=["GET"])
@require_auth
def api_mark_watched_libraries():
    username = _current_username()
    configured_visibility = config.mark_watched.visible_libraries
    visible = set(configured_visibility or [])
    result = []
    with _runtime_lock:
        instances = list(config.instances)
    for instance in instances:
        plex = plex_clients.get(instance.name)
        if plex is None:
            continue
        for library in instance.libraries:
            library_key = f"{instance.name}::{library.name}"
            if configured_visibility is not None and library_key not in visible:
                continue
            section_id = library.section_id or plex.find_section_id(library.name)
            if not section_id or plex.get_section_type(str(section_id)) != "show":
                continue
            try:
                shows = plex.list_tv_shows(str(section_id))
            except Exception as exc:
                logger.warning("Could not load Mark-it-Watched library %s (%s)",
                               library_key, type(exc).__name__)
                result.append({"instance": instance.name, "library": library.name,
                               "section_id": str(section_id), "shows": [],
                               "error": "Plex library could not be loaded"})
                continue
            for show in shows:
                rule = mark_watched_rules.rule(
                    username, instance.name, library.name, show["rating_key"], 0,
                )
                show["rule_enabled"] = rule["show_enabled"]
                show["poster_url"] = url_for(
                    "api_mark_watched_poster", instance_name=instance.name,
                    key=show.get("thumb", ""),
                ) if show.get("thumb") else ""
                show.pop("thumb", None)
            result.append({"instance": instance.name, "library": library.name,
                           "section_id": str(section_id), "shows": shows})
    return jsonify({"libraries": result, "jobs": mark_watched.status(10)["jobs"]})


@app.route("/api/mark-watched/options", methods=["GET"])
@require_auth
def api_mark_watched_options():
    """List configured servers, then TV libraries for one selected server."""
    requested = str(request.args.get("instance", "")).strip()
    configured_visibility = config.mark_watched.visible_libraries
    visible = set(configured_visibility or [])
    with _runtime_lock:
        instances = list(config.instances)
    available_instances = [{
        "name": instance.name,
        "library_count": sum(
            1 for library in instance.libraries
            if configured_visibility is None
            or f"{instance.name}::{library.name}" in visible
        ),
    } for instance in instances if any(
        configured_visibility is None
        or f"{instance.name}::{library.name}" in visible
        for library in instance.libraries
    )]
    if not requested:
        return jsonify({"instances": available_instances, "libraries": []})

    instance = next((item for item in instances if item.name == requested), None)
    plex = plex_clients.get(requested)
    if instance is None or plex is None:
        return jsonify({"error": "Unknown Plex server"}), 404
    try:
        sections = plex.get_sections()
    except Exception as exc:
        logger.warning("Could not list TV libraries for %s (%s)",
                       requested, type(exc).__name__)
        return jsonify({"error": "Plex TV libraries could not be loaded"}), 502
    by_id = {str(section.get("id", "")): section for section in sections}
    by_title = {str(section.get("title", "")): section for section in sections}
    libraries = []
    for library in instance.libraries:
        key = f"{instance.name}::{library.name}"
        if configured_visibility is not None and key not in visible:
            continue
        section = by_id.get(str(library.section_id or "")) or by_title.get(library.name)
        if not section or section.get("type") != "show":
            continue
        libraries.append({
            "name": library.name,
            "section_id": str(section["id"]),
        })
    return jsonify({"instances": available_instances, "libraries": libraries})


@app.route("/api/mark-watched/shows", methods=["GET"])
@require_auth
def api_mark_watched_shows():
    """Return one bounded page from one explicitly selected Plex TV library."""
    instance_name = str(request.args.get("instance", "")).strip()
    library_name = str(request.args.get("library", "")).strip()
    search = str(request.args.get("q", "")).strip()
    if len(search) > 100:
        return jsonify({"error": "Show search cannot exceed 100 characters"}), 400
    try:
        page = max(1, int(request.args.get("page", 1)))
        page_size = int(request.args.get("page_size", 12))
    except (TypeError, ValueError):
        return jsonify({"error": "Valid page and page_size are required"}), 400
    if page_size not in {12, 24, 36, 48}:
        return jsonify({"error": "page_size must be 12, 24, 36, or 48"}), 400
    instance, library, plex = _mark_watched_library(instance_name, library_name)
    if instance is None or library is None or plex is None:
        return jsonify({"error": "Unknown Plex TV library"}), 404
    configured_visibility = config.mark_watched.visible_libraries
    if configured_visibility is not None and (
        f"{instance_name}::{library_name}" not in set(configured_visibility)
    ):
        return jsonify({"error": "This Plex library is hidden in Settings"}), 404
    section_id = library.section_id or plex.find_section_id(library.name)
    if not section_id or plex.get_section_type(str(section_id)) != "show":
        return jsonify({"error": "Mark-it-Watched supports TV libraries only"}), 400
    try:
        if search:
            result = plex.list_tv_shows_page(
                str(section_id), (page - 1) * page_size, page_size,
                query=search,
            )
        else:
            result = plex.list_tv_shows_page(
                str(section_id), (page - 1) * page_size, page_size,
            )
    except Exception as exc:
        logger.warning("Could not load Mark-it-Watched page for %s::%s (%s)",
                       instance_name, library_name, type(exc).__name__)
        return jsonify({"error": "Plex shows could not be loaded"}), 502
    username = _current_username()
    shows = result["shows"]
    for show in shows:
        rule = mark_watched_rules.rule(
            username, instance_name, library_name, show["rating_key"], 0,
        )
        show["rule_enabled"] = rule["show_enabled"]
        show["poster_url"] = url_for(
            "api_mark_watched_poster", instance_name=instance_name,
            key=show.get("thumb", ""),
        ) if show.get("thumb") else ""
        show.pop("thumb", None)
    total = int(result.get("total", len(shows)))
    pages = max(1, (total + page_size - 1) // page_size)
    return jsonify({
        "instance": instance_name,
        "library": library_name,
        "section_id": str(section_id),
        "shows": shows,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "total": total,
        "search": search,
        "rule_user": username,
    })


@app.route("/api/mark-watched/seasons", methods=["GET"])
@require_auth
def api_mark_watched_seasons():
    instance_name = request.args.get("instance", "")
    library_name = request.args.get("library", "")
    show_key = request.args.get("show", "")
    _instance, _library, plex = _mark_watched_library(instance_name, library_name)
    if plex is None or not show_key.isdigit():
        return jsonify({"error": "Unknown Plex show"}), 404
    try:
        seasons = plex.list_show_seasons(show_key)
        username = _current_username()
        for season in seasons:
            rule = mark_watched_rules.rule(
                username, instance_name, library_name, show_key, season["index"],
            )
            season["rule"] = rule
            season["poster_url"] = url_for(
                "api_mark_watched_poster", instance_name=instance_name,
                key=season.get("thumb", ""),
            ) if season.get("thumb") else ""
            season.pop("thumb", None)
        return jsonify({"seasons": seasons})
    except Exception as exc:
        return jsonify({"error": f"Could not load Plex seasons: {type(exc).__name__}"}), 502


@app.route("/api/mark-watched/rules", methods=["POST"])
@require_auth
def api_mark_watched_rules():
    data = request.get_json(silent=True) or {}
    instance_name = str(data.get("instance", ""))
    library_name = str(data.get("library", ""))
    show_key = str(data.get("show_rating_key", ""))
    _instance, _library, plex = _mark_watched_library(instance_name, library_name)
    if plex is None or not show_key.isdigit():
        return jsonify({"ok": False, "error": "Unknown Plex show"}), 404
    username = _current_username()
    if data.get("scope") == "show" and isinstance(data.get("enabled"), bool):
        mark_watched_rules.set_show(
            username, instance_name, library_name, show_key, data["enabled"],
        )
    elif data.get("scope") == "season" and (
        isinstance(data.get("enabled"), bool) or data.get("enabled") is None
    ):
        try:
            season_index = int(data["season_index"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"ok": False, "error": "Valid season_index required"}), 400
        mark_watched_rules.set_season(
            username, instance_name, library_name, show_key,
            season_index, data.get("enabled"),
        )
    else:
        return jsonify({"ok": False, "error": "Invalid rule update"}), 400
    return jsonify({"ok": True})


@app.route("/api/mark-watched/all", methods=["POST"])
@require_auth
def api_mark_watched_all():
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled")
    expected = "ALL ON" if enabled is True else "ALL OFF" if enabled is False else ""
    if data.get("confirm") != expected:
        return jsonify({"ok": False, "error": f"Confirmation must be {expected or 'valid'}"}), 400
    show_keys = []
    configured_visibility = config.mark_watched.visible_libraries
    visible = set(configured_visibility or [])
    for instance in config.instances:
        plex = plex_clients.get(instance.name)
        if plex is None:
            continue
        for library in instance.libraries:
            key = f"{instance.name}::{library.name}"
            if configured_visibility is not None and key not in visible:
                continue
            section_id = library.section_id or plex.find_section_id(library.name)
            if not section_id or plex.get_section_type(str(section_id)) != "show":
                continue
            for show in plex.list_tv_shows(str(section_id)):
                show_keys.append((instance.name, library.name, show["rating_key"]))
    mark_watched_rules.set_all(_current_username(), show_keys, enabled)
    return jsonify({"ok": True, "enabled": enabled, "shows": len(show_keys),
                    "users": 1,
                    "message": "Future automatic rules updated; Plex history was not changed"})


@app.route("/api/mark-watched/poster", methods=["GET"])
@require_auth
def api_mark_watched_poster():
    instance_name = request.args.get("instance_name", "")
    artwork_key = request.args.get("key", "")
    plex = plex_clients.get(instance_name)
    if plex is None:
        return "", 404
    try:
        artwork = plex.get_artwork(artwork_key)
        return Response(
            artwork.content,
            content_type=artwork.headers.get("Content-Type", "image/jpeg"),
            headers={"Cache-Control": "private, max-age=3600"},
        )
    except Exception:
        return "", 404


@app.route("/api/history", methods=["GET"])
@require_auth
def api_history():
    return jsonify(runner.get_history())


@app.route("/api/library-refresh/status", methods=["GET"])
@require_auth
def api_library_refresh_status():
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


@app.route("/api/library-refresh/run", methods=["POST"])
@require_auth
def api_library_refresh_run():
    if not config.features.library_refresh:
        return jsonify({"error": "Library Refresh is disabled"}), 409
    data = request.get_json(silent=True) or {}
    requested = data.get("libraries")
    if not isinstance(requested, list):
        requested = [{
            "instance": data.get("instance", ""),
            "library": data.get("library", ""),
        }]
    if data.get("enabled_only"):
        with _runtime_lock:
            requested = [
                {"instance": instance.name, "library": library.name}
                for instance in config.instances
                for library in instance.libraries if library.refresh_enabled
            ]
    if not requested or len(requested) > 100:
        return jsonify({"error": "Select between 1 and 100 libraries"}), 400

    jobs = []
    seen = set()
    with _runtime_lock:
        for item in requested:
            key = (str(item.get("instance", "")), str(item.get("library", "")))
            if not all(key) or key in seen:
                return jsonify({"error": "Each selected library must be unique"}), 400
            seen.add(key)
            instance = next((value for value in config.instances
                             if value.name == key[0]), None)
            library = next((value for value in instance.libraries
                            if value.name == key[1]), None) if instance else None
            plex = plex_clients.get(key[0])
            if not instance or not library or plex is None:
                return jsonify({"error": f"Plex library not found: {key[0]} / {key[1]}"}), 404
            jobs.append((instance, library, plex))

    with _library_refresh_queue_lock:
        if _library_refresh_queue.get("running"):
            return jsonify({"error": "A library refresh queue is already active"}), 409
        _library_refresh_queue.clear()
        _library_refresh_queue.update({
            "running": True, "state": "queued", "current": 0,
            "total": len(jobs), "completed": 0, "failed": 0,
            "started_at": _utc_now(), "library": "",
        })

    def run_queue():
        completed = 0
        failed = 0
        try:
            for index, (instance, library, plex) in enumerate(jobs, 1):
                with _library_refresh_queue_lock:
                    _library_refresh_queue.update({
                        "state": "running", "current": index,
                        "library": f"{instance.name} / {library.name}",
                        "completed": completed, "failed": failed,
                    })
                result = library_refresh.run(
                    instance, library, plex, source="manual",
                )
                if result.get("ok"):
                    completed += 1
                else:
                    failed += 1
        except Exception as exc:
            logger.exception("Library refresh queue failed")
            failed += 1
            with _library_refresh_queue_lock:
                _library_refresh_queue["error"] = type(exc).__name__
        finally:
            with _library_refresh_queue_lock:
                _library_refresh_queue.update({
                    "running": False,
                    "state": "completed" if not failed else "completed_with_errors",
                    "completed": completed, "failed": failed,
                    "finished_at": _utc_now(),
                })

    threading.Thread(
        target=run_queue, daemon=True, name="library-refresh",
    ).start()
    return jsonify({"status": "triggered", "libraries": len(jobs)}), 202


@app.route("/api/metadata-audit/status", methods=["GET"])
@require_auth
def api_metadata_audit_status():
    with _runtime_lock:
        instances = [instance.name for instance in config.instances]
        ignored = {
            instance.name: list(instance.metadata_health.ignored_libraries)
            for instance in config.instances
        }
        libraries = {
            instance.name: [
                {"name": library.name, "type": library.type}
                for library in instance.libraries
            ]
            for instance in config.instances
        }
    return jsonify({
        "instances": instances,
        "audits": _read_metadata_audits(),
        "ignored_libraries": ignored,
        "libraries": libraries,
    })


@app.route("/api/metadata-audit/run", methods=["POST"])
@require_auth
def api_metadata_audit_run():
    if not config.features.metadata_health:
        return jsonify({"ok": False, "error": "Metadata Health is disabled"}), 409
    requested = str((request.get_json(silent=True) or {}).get("instance", ""))
    with _runtime_lock:
        instance = next((item for item in config.instances
                         if item.name == requested), None)
        plex = plex_clients.get(requested)
    if instance is None or plex is None:
        return jsonify({"ok": False, "error": "Plex instance not found"}), 404

    try:
        sections = plex.get_sections()
    except Exception as exc:
        logger.warning("Metadata audit could not list Plex libraries for %s (%s)",
                       requested, type(exc).__name__)
        return jsonify({
            "ok": False,
            "error": "Plex libraries could not be read",
        }), 502

    by_id = {str(section["id"]): section for section in sections}
    by_name = {str(section["title"]).casefold(): section for section in sections}
    machine_id = instance.machine_id or plex.get_machine_identifier() or ""
    libraries = []
    total_items = 0
    unmatched_count = 0
    error_count = 0
    ignored_names = {
        name.casefold() for name in instance.metadata_health.ignored_libraries
    }
    for library in instance.libraries:
        if library.name.casefold() in ignored_names:
            continue
        section = (
            by_id.get(str(library.section_id))
            if library.section_id else by_name.get(library.name.casefold())
        )
        if not section or section.get("type") not in {"movie", "show"}:
            continue
        try:
            result = plex.get_unmatched_items(str(section["id"]))
            items = result["items"]
            for item in items:
                item["plex_url"] = _plex_details_url(
                    machine_id, item["metadata_key"],
                )
            library_result = {
                "name": library.name,
                "section_id": str(section["id"]),
                "type": section["type"],
                "total_items": result["total_items"],
                "unmatched_count": len(items),
                "items": items,
            }
            total_items += result["total_items"]
            unmatched_count += len(items)
        except Exception as exc:
            logger.warning("Metadata audit failed for %s / %s (%s)",
                           requested, library.name, type(exc).__name__)
            library_result = {
                "name": library.name,
                "section_id": str(section["id"]),
                "type": section["type"],
                "error": "Plex library items could not be read",
                "unmatched_count": 0,
                "items": [],
            }
            error_count += 1
        libraries.append(library_result)

    audit = {
        "instance": instance.name,
        "machine_id": machine_id,
        "audited_at": _utc_now(),
        "total_items": total_items,
        "unmatched_count": unmatched_count,
        "error_count": error_count,
        "libraries": libraries,
    }
    with _metadata_audit_lock:
        audits = _read_metadata_audits()
        audits[instance.name] = audit
        atomic_write_json(str(_metadata_audit_path), audits)
    return jsonify({"ok": True, **audit})


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


@app.route("/api/timestamp-repair/status", methods=["GET"])
@require_auth
def api_timestamp_repair_status():
    status = timestamp_repair.status()
    with _repair_batch_lock:
        status["batch"] = dict(_repair_batch)
    if status["batch"].get("running"):
        status["running"] = True
    local_active = status.get("active_transaction")
    if isinstance(local_active, dict) and not status.get("running"):
        local_active = {
            **local_active,
            "recovery_state": local_active.get("state", "prepared"),
            "state": "recovery_required",
        }
        status["active_transaction"] = local_active
        status["state"] = "recovery_required"
    with _remote_repair_lock:
        external = dict(_remote_repair)
    if external.get("running"):
        worker = _repair_worker(str(external.get("worker", "")))
        if worker:
            try:
                worker_status = RepairWorkerClient(worker, timeout=3).status()
                active = worker_status.get("active_transaction") or worker_status.get("transaction")
                if isinstance(active, dict):
                    external.update({
                        "state": active.get("state", external.get("state")),
                        "last_heartbeat": active.get("last_heartbeat", external.get("last_heartbeat")),
                        "scan_elapsed_seconds": active.get("scan_elapsed_seconds", 0),
                    })
            except Exception:
                pass
        status.update({
            "running": True,
            "state": external.get("state", "running_on_worker"),
            "last_heartbeat": external.get("last_heartbeat"),
            "active_transaction": external,
        })
    worker_statuses = {}
    for instance in config.instances:
        worker_name = instance.timestamp_repair.worker
        if worker_name == "local" or worker_name in worker_statuses:
            continue
        worker = _repair_worker(worker_name)
        if not worker:
            continue
        try:
            worker_statuses[worker_name] = RepairWorkerClient(
                worker, timeout=3,
            ).status()
        except Exception:
            worker_statuses[worker_name] = {}
    for instance in config.instances:
        worker_audit = worker_statuses.get(
            instance.timestamp_repair.worker, {},
        ).get("audits", {}).get(instance.name, {})
        local_audit = status.get("audits", {}).get(instance.name, {})
        if worker_audit and local_audit:
            for key in (
                "live_database_distinct_files", "database_count_changed",
            ):
                if key in worker_audit:
                    local_audit[key] = worker_audit[key]
    remote_recoveries = []
    for worker_name, worker_status in worker_statuses.items():
        active = worker_status.get("active_transaction")
        if isinstance(active, dict):
            recovery_state = active.get("state", "prepared")
            remote_recoveries.append({
                **active,
                **({
                    "recovery_state": recovery_state,
                    "state": "recovery_required",
                } if not worker_status.get("running") else {}),
                "worker": worker_name,
                "remote": True,
            })
    if remote_recoveries:
        status["remote_recoveries"] = remote_recoveries
        if not status.get("active_transaction"):
            status["active_transaction"] = remote_recoveries[0]
            status["state"] = remote_recoveries[0].get(
                "state", "recovery_required",
            )
    maintenance_blocked = bool(status.get("active_transaction"))
    status["instances"] = [
        {
            "name": instance.name,
            "enabled": instance.timestamp_repair.enabled,
            "worker": instance.timestamp_repair.worker,
            "ready": (readiness := _repair_readiness(instance))[0],
            "readiness": readiness[1],
            "blocked": maintenance_blocked,
            "max_files_per_folder": instance.timestamp_repair.max_files_per_folder,
        }
        for instance in config.instances
    ]
    return jsonify(status)


@app.route("/api/timestamp-repair/audit", methods=["POST"])
@require_auth
def api_timestamp_repair_audit():
    if not config.features.timestamp_repair:
        return jsonify({"error": "Timestamp Repair is disabled"}), 409
    data = request.get_json(silent=True) or {}
    instance, _, plex = _timestamp_runtime(str(data.get("instance", "")))
    if not instance:
        return jsonify({"error": "Plex instance not found"}), 404
    try:
        if instance.timestamp_repair.worker == "local":
            result = timestamp_repair.audit(
                instance, instance.timestamp_repair, plex,
            )
        else:
            worker = _repair_worker(instance.timestamp_repair.worker)
            if not worker:
                raise ValueError("Configured repair worker was not found")
            result = RepairWorkerClient(worker).audit(_worker_payload(instance, plex))
        _enrich_repair_audit(result, plex)
        timestamp_repair.save_audit(result)
        return jsonify(result)
    except Exception as exc:
        logger.error("Timestamp repair audit failed for %s (%s)",
                     instance.name, type(exc).__name__)
        return jsonify({"error": str(exc)}), 400


@app.route("/api/timestamp-repair/run", methods=["POST"])
@require_auth
def api_timestamp_repair_run():
    if not config.features.timestamp_repair:
        return jsonify({"error": "Timestamp Repair is disabled"}), 409
    data = request.get_json(silent=True) or {}
    instance_name = str(data.get("instance", ""))
    requested = data.get("folders")
    if not isinstance(requested, list):
        requested = [{
            "library_section_id": data.get("library_section_id", ""),
            "folder": data.get("folder", ""),
        }]
    if not requested or len(requested) > 100:
        return jsonify({"error": "Select between 1 and 100 reviewed folders"}), 400
    if _remote_recovery_required():
        logger.warning(
            "[%s] Timestamp repair blocked: a remote worker requires "
            "recovery or its recovery state cannot be verified",
            instance_name,
        )
        return jsonify({
            "error": "Remote repair worker recovery is required before "
                     "another timestamp repair can start",
        }), 409
    repair_status = timestamp_repair.status()
    with _remote_repair_lock:
        remote_running = bool(_remote_repair.get("running"))
    with _repair_batch_lock:
        batch_running = bool(_repair_batch.get("running"))
    if (repair_status.get("running") or repair_status.get("active_transaction")
            or remote_running or batch_running):
        return jsonify({"error": "A timestamp repair is already active"}), 409

    jobs = []
    seen = set()
    for item in requested:
        section_id = str(item.get("library_section_id", ""))
        folder = str(item.get("folder", ""))
        key = (section_id, folder)
        if not section_id or not folder or key in seen:
            return jsonify({"error": "Each selected folder must be unique and complete"}), 400
        seen.add(key)
        instance, library, plex = _timestamp_runtime(instance_name, section_id)
        if not instance or not library or not plex:
            return jsonify({"error": "Configured Plex instance/library not found"}), 404
        if not timestamp_repair.audited_folder(instance_name, section_id, folder):
            return jsonify({
                "error": f"Folder is not present in the latest server-side audit: {folder}",
            }), 400
        expected_files = timestamp_repair.audited_files(
            instance_name, section_id, folder,
        )
        jobs.append((instance, library, plex, section_id, folder, expected_files))

    def _perform(job, position: str) -> dict:
        instance, library, plex, section_id, folder, expected_files = job
        repair_worker = instance.timestamp_repair.worker
        if repair_worker == "local":
            return timestamp_repair.run_folder(
                instance, library, instance.timestamp_repair, plex, folder,
                section_id,
                preflight=lambda: runner._collect_library_checks(
                    instance, library, config, plex, section_id=section_id,
                )[0],
                expected_files=expected_files,
                batch_position=position,
            )
        worker = _repair_worker(repair_worker)
        if not worker:
            return {"ok": False, "error": "Configured repair worker was not found"}
        run_id = secrets.token_urlsafe(24)
        with _remote_repair_lock:
            _remote_repair.clear()
            _remote_repair.update({
                "running": True, "state": "starting_worker",
                "transaction_id": run_id, "instance": instance.name,
                "library": library.name, "folder": folder,
                "worker": worker.name, "last_heartbeat": _utc_now(),
                "batch_position": position,
            })
            _worker_scan_contexts[run_id] = {
                "worker": worker.name, "instance": instance.name,
                "section_id": section_id, "folder": folder, "plex": plex,
            }
        result = {"ok": False, "error": "Worker repair did not start"}
        try:
            with lease(instance.name, operation="timestamp_repair") as (acquired, reason):
                if not acquired:
                    raise RuntimeError(reason)
                checks = runner._collect_library_checks(
                    instance, library, config, plex, section_id=section_id,
                )[0]
                failed = [name for name, check in checks.items() if not check.get("pass")]
                if failed:
                    raise RuntimeError("Safety checks failed: " + ", ".join(failed))
                payload = {
                    **_worker_payload(instance, plex),
                    "run_id": run_id,
                    "controller_url": worker.controller_url,
                    "library_section_id": section_id,
                    "folder": folder,
                    "expected_files": sorted(expected_files),
                    "batch_position": position,
                }
                pre_dispatch_status = RepairWorkerClient(
                    worker, timeout=3,
                ).status()
                if pre_dispatch_status.get("active_transaction"):
                    raise RuntimeError("Repair worker recovery is required")
                with _remote_repair_lock:
                    _remote_repair.update({
                        "state": "running_on_worker", "last_heartbeat": _utc_now(),
                    })
                _set_remote_pending({
                    "worker": worker.name, "instance": instance.name,
                    "library": library.name, "folder": folder,
                    "transaction_id": run_id, "dispatched_at": _utc_now(),
                })
                _worker_recovery_cache.pop(worker.name, None)
                result = RepairWorkerClient(worker).run(payload)
                worker_status = RepairWorkerClient(worker, timeout=3).status()
                if not worker_status.get("active_transaction"):
                    _clear_remote_pending(worker.name)
                timestamp_repair.merge_history(worker_status.get("history", []))
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            logger.error("Remote timestamp repair failed for %s (%s)",
                         instance.name, type(exc).__name__)
        finally:
            with _remote_repair_lock:
                _worker_scan_contexts.pop(run_id, None)
                _remote_repair.update({
                    "running": False,
                    "state": "completed" if result.get("ok") else "failed",
                    "error": result.get("error", ""),
                    "last_heartbeat": _utc_now(),
                })
            _worker_recovery_cache.pop(worker.name, None)
        return result

    def _run():
        total = len(jobs)
        completed = 0
        result = {"ok": True}
        try:
            for index, job in enumerate(jobs, start=1):
                with _repair_batch_lock:
                    if _repair_batch.get("cancel_requested"):
                        result = {
                            "ok": False,
                            "error": "Repair queue cancelled safely",
                        }
                        break
                position = f"{index}/{total}"
                with _repair_batch_lock:
                    _repair_batch.update({
                        "running": True, "state": "running",
                        "current": index, "total": total,
                        "completed": completed, "failed": 0,
                        "folder": job[4], "error": "",
                    })
                result = _perform(job, position)
                if not result.get("ok"):
                    break
                timestamp_repair.complete_audited_folder(
                    instance_name, job[3], job[4],
                )
                completed += 1
        except Exception as exc:
            logger.exception(
                "Timestamp repair queue failed while updating local state"
            )
            result = {
                "ok": False,
                "error": f"Repair queue state update failed ({type(exc).__name__})",
            }
        finally:
            with _repair_batch_lock:
                _repair_batch.update({
                    "running": False,
                    "state": "completed" if result.get("ok") else "failed",
                    "completed": completed,
                    "failed": 0 if result.get("ok") else 1,
                    "error": result.get("error", ""),
                    "finished_at": _utc_now(),
                })

    with _repair_batch_lock:
        if _repair_batch.get("running"):
            return jsonify({"error": "A timestamp repair is already active"}), 409
        _repair_batch.clear()
        _repair_batch.update({
            "running": True, "state": "queued", "current": 0,
            "total": len(jobs), "completed": 0, "failed": 0,
            "folder": "", "error": "", "started_at": _utc_now(),
            "cancel_requested": False,
        })
    threading.Thread(target=_run, daemon=True, name="timestamp-repair").start()
    return jsonify({"status": "triggered", "folders": len(jobs)}), 202


@app.route("/api/timestamp-repair/cancel", methods=["POST"])
@require_auth
def api_timestamp_repair_cancel():
    with _repair_batch_lock:
        if _repair_batch.get("running"):
            _repair_batch["cancel_requested"] = True
    timestamp_repair.cancel()
    with _remote_repair_lock:
        worker_name = _remote_repair.get("worker") if _remote_repair.get("running") else None
    if worker_name:
        worker = _repair_worker(worker_name)
        if worker:
            try:
                RepairWorkerClient(worker).cancel()
            except Exception:
                pass
    return jsonify({"ok": True, "message": "Cancellation requested; names will be restored at the next safe step"})


@app.route("/api/timestamp-repair/recover", methods=["POST"])
@require_auth
def api_timestamp_repair_recover():
    result = timestamp_repair.recover()
    if result.get("ok") and result.get("message") == "No recovery is required":
        for instance in config.instances:
            repair = instance.timestamp_repair
            if not repair.enabled or repair.worker == "local":
                continue
            worker = _repair_worker(repair.worker)
            if not worker:
                continue
            try:
                status = RepairWorkerClient(worker, timeout=3).status()
                if status.get("active_transaction"):
                    result = RepairWorkerClient(worker).recover(instance.name)
                    recovered_status = RepairWorkerClient(worker, timeout=3).status()
                    timestamp_repair.merge_history(recovered_status.get("history", []))
                    if result.get("ok") and not recovered_status.get("active_transaction"):
                        _clear_remote_pending(worker.name)
                    _worker_recovery_cache.pop(worker.name, None)
                    break
                _clear_remote_pending(worker.name)
            except Exception as exc:
                result = {"ok": False, "error": f"Worker {worker.name} is unavailable"}
                logger.warning("Worker recovery check failed (%s)", type(exc).__name__)
                break
    return jsonify(result), (200 if result.get("ok") else 409)


@app.route("/api/timestamp-repair/worker-scan/<run_id>", methods=["POST"])
def api_timestamp_repair_worker_scan(run_id: str):
    with _remote_repair_lock:
        context = _worker_scan_contexts.get(run_id)
    if not context:
        return jsonify({"ok": False, "error": "Unknown repair transaction"}), 404
    worker = _repair_worker(context["worker"])
    if not worker:
        return jsonify({"ok": False, "error": "Unknown repair worker"}), 401
    ok, error = _worker_signature_verifier.verify(
        worker.token, worker.name, request.method, request.path,
        request.get_data(cache=True), request.headers,
    )
    if not ok:
        return jsonify({"ok": False, "error": error}), 401
    payload = request.get_json(silent=True) or {}
    if (str(payload.get("section_id", "")) != context["section_id"]
            or str(payload.get("folder", "")) != context["folder"]):
        return jsonify({"ok": False, "error": "Scan request is outside the approved folder"}), 403
    return jsonify(context["plex"].scan_path(context["section_id"], context["folder"]))


@app.route("/api/timestamp-repair/worker-test", methods=["POST"])
@require_auth
def api_timestamp_repair_worker_test():
    data = request.get_json(silent=True) or {}
    worker = SimpleNamespace(
        name=str(data.get("name", "")).strip(),
        url=str(data.get("url", "")).strip(),
        token=str(data.get("token", "")),
        controller_url=str(data.get("controller_url", "")).strip(),
    )
    try:
        return jsonify(RepairWorkerClient(worker, timeout=5).health())
    except Exception as exc:
        logger.warning("Repair worker test failed (%s)", type(exc).__name__)
        return jsonify({"ok": False, "error": "Worker could not be reached or authenticated"}), 400


@app.route("/api/timestamp-repair/databases", methods=["POST"])
@require_auth
def api_timestamp_repair_databases():
    data = request.get_json(silent=True) or {}
    worker_name = str(data.get("worker", "local")) or "local"
    if worker_name != "local":
        worker = _repair_worker(worker_name)
        if not worker:
            return jsonify({"ok": False, "error": "Repair worker is not configured"}), 404
        try:
            return jsonify(RepairWorkerClient(worker, timeout=10).discover())
        except Exception as exc:
            logger.warning("Worker database discovery failed (%s)", type(exc).__name__)
            return jsonify({"ok": False, "error": "Worker database discovery failed"}), 400
    roots = [
        value.strip() for value in os.environ.get(
            "TIMESTAMP_REPAIR_DATABASE_ROOTS", "/plex-db",
        ).split(",") if value.strip()
    ]
    found = []
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            found.extend(str(path) for path in base.rglob(
                "com.plexapp.plugins.library.db"
            ) if path.is_file())
        except OSError:
            continue
    return jsonify({"ok": True, "databases": sorted(set(found)), "roots": roots})


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


@app.route("/api/checks", methods=["GET"])
@require_auth
def api_checks():
    results = {}
    with _runtime_lock:
        runtime = [(inst, plex_clients.get(inst.name))
                   for inst in config.instances]
    for inst, plex in runtime:
        if plex is None:
            continue
        results[inst.name] = runner.run_instance_checks(inst, plex)
    return jsonify(results)


@app.route("/api/scheduling", methods=["POST"])
@require_auth
def api_scheduling():
    data    = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    if enabled and not config.features.trash_removal:
        return jsonify({
            "scheduling_enabled": False,
            "error": "Trash Removal is disabled",
        }), 409
    set_scheduling_enabled(enabled)
    return jsonify({"scheduling_enabled": enabled})


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


@app.route("/api/run/<instance_name>/<library_name>", methods=["POST"])
@require_auth
def api_run_library(instance_name: str, library_name: str):
    if not config.features.trash_removal:
        return jsonify({"error": "Trash Removal is disabled"}), 409
    if _trigger(instance_name, library_name):
        return jsonify({"status": "triggered"})
    return jsonify({"error": "not found"}), 404


@app.route("/api/dryrun/<instance_name>/<library_name>", methods=["POST"])
@require_auth
def api_dryrun_library(instance_name: str, library_name: str):
    if not config.features.trash_removal:
        return jsonify({"error": "Trash Removal is disabled"}), 409
    if _trigger(instance_name, library_name, dry_run=True):
        return jsonify({"status": "dry_run_triggered"})
    return jsonify({"error": "not found"}), 404


@app.route("/api/run/all", methods=["POST"])
@require_auth
def api_run_all():
    if not config.features.trash_removal:
        return jsonify({"error": "Trash Removal is disabled"}), 409
    def _run():
        with _runtime_lock:
            live_config = config
            runtime = [(inst, plex_clients.get(inst.name))
                       for inst in live_config.instances]
        for inst, plex in runtime:
            if plex is None:
                continue
            plex_checks = runner.run_instance_checks(inst, plex)
            for lib in inst.libraries:
                runner.run_library(inst, lib, live_config, plex,
                                   plex_checks=plex_checks, manual=True)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "triggered"})


@app.route("/api/dryrun/all", methods=["POST"])
@require_auth
def api_dryrun_all():
    if not config.features.trash_removal:
        return jsonify({"error": "Trash Removal is disabled"}), 409
    def _run():
        with _runtime_lock:
            live_config = config
            runtime = [(inst, plex_clients.get(inst.name))
                       for inst in live_config.instances]
        for inst, plex in runtime:
            if plex is None:
                continue
            plex_checks = runner.run_instance_checks(inst, plex)
            for lib in inst.libraries:
                runner.run_library(inst, lib, live_config, plex,
                                   plex_checks=plex_checks, dry_run=True, manual=True)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "dry_run_triggered"})


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
    repair_status = timestamp_repair.status()
    with _remote_repair_lock:
        remote_running = bool(_remote_repair.get("running"))
    if repair_status.get("running") or repair_status.get("active_transaction") or remote_running:
        return jsonify({
            "ok": False,
            "error": "Configuration cannot change during timestamp repair or recovery",
        }), 409
    data         = request.get_json(silent=True) or {}
    save_scope   = str(data.get("save_scope", "")).strip().lower()
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
    submitted_instances = []
    for submitted in data.get("instances", []):
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
        _save_and_apply(
            cfg,
            runtime_tokens=runtime_tokens,
            require_paths=False if save_scope == "plex" else None,
        )
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


@app.route("/api/config/load", methods=["GET"])
@require_auth
def api_config_load():
    """Return current config.yml contents for the settings editor."""
    try:
        with open(CONFIG_PATH, "r") as f:
            raw = yaml.safe_load(f) or {}
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
        for instance in raw.get("plex_instances", []):
            if isinstance(instance, dict):
                instance["token_configured"] = bool(instance.get("token"))
                instance["token"] = ""
        return jsonify({"ok": True, "config": raw})
    except Exception as e:
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
        mark_watched_rules.delete_user(username)
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


if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    app.run(host=host, port=8222, debug=False, use_reloader=False)
