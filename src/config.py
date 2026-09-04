import os
import yaml
import logging
from dataclasses import dataclass, field
from typing import List, Optional
from src.branding import PRODUCT_SLUG, get_env

logger = logging.getLogger(PRODUCT_SLUG)


# ── Provider check ────────────────────────────────────────────────────────────

@dataclass
class ProviderCheck:
    type: str          # realdebrid | alldebrid | torbox | debridlink
    api_key: str = ""


# ── Path config ───────────────────────────────────────────────────────────────

@dataclass
class PathConfig:
    path: str
    type: str                                    # physical | debrid | usenet
    min_threshold: float = 0.90                  # ratio check — 0.90 = 90%
    provider_checks: List[ProviderCheck] = field(default_factory=list)


# ── Library config ────────────────────────────────────────────────────────────

@dataclass
class LibraryConfig:
    name: str
    type: str                                    # physical | debrid | usenet | mixed
    paths: List[PathConfig]
    cron: str = ""                                  # blank inherits AppConfig.default_cron
    section_id: Optional[str] = None            # auto-discovered if not set
    refresh_enabled: bool = False
    refresh_cron: str = "0 * * * *"
    refresh_guard_minutes: int = 15


@dataclass
class TimestampRepairConfig:
    enabled: bool = False
    worker: str = "local"
    database_path: str = ""
    allowed_prefixes: List[str] = field(default_factory=list)
    max_files_per_folder: int = 5
    scan_timeout_seconds: int = 1800
    poll_interval_seconds: int = 5
    heartbeat_seconds: int = 30


@dataclass
class MetadataHealthConfig:
    ignored_libraries: List[str] = field(default_factory=list)


@dataclass
class RepairWorkerConfig:
    name: str
    url: str
    token: str
    controller_url: str


@dataclass
class FeatureConfig:
    trash_removal: bool = True
    metadata_health: bool = True
    timestamp_repair: bool = True
    library_refresh: bool = True
    mark_watched: bool = True


@dataclass
class MarkWatchedConfig:
    webhook_secret: str = ""
    # Sonarr reports an import the instant the file lands; a symlinked debrid
    # library can take far longer than that to appear in Plex. These delays
    # span roughly an hour, because the previous 8.7 minutes expired before
    # Plex had scanned and the job then failed permanently.
    retry_delays: List[int] = field(
        default_factory=lambda: [15, 30, 60, 120, 300, 600, 900, 1200]
    )
    scan_on_import: bool = True
    # A scan should land within hours, so this is generous rather than
    # unlimited: an import that never appears is usually one that was replaced
    # or removed, and chasing it forever just accumulates work. 0 disables the
    # cap entirely.
    give_up_after_hours: float = 120  # five days
    # None means "every TV library"; an empty list means "none of them". That
    # distinction is easy to get wrong at a call site, so ask shows_library()
    # rather than comparing against None directly.
    visible_libraries: Optional[List[str]] = None
    workers: int = 4

    def shows_library(self, instance: str, library: str) -> bool:
        """Whether this Plex library is visible to Mark-it-Watched."""
        if self.visible_libraries is None:
            return True
        return f"{instance}::{library}" in set(self.visible_libraries)


@dataclass
class AppUser:
    username: str
    password_hash: str
    role: str = "user"
    permissions: List[str] = field(default_factory=list)


# ── Plex instance config ──────────────────────────────────────────────────────

@dataclass
class PlexInstanceConfig:
    name: str
    url: str
    token: str
    libraries: List[LibraryConfig]
    machine_id: Optional[str] = None
    timestamp_repair: TimestampRepairConfig = field(default_factory=TimestampRepairConfig)
    metadata_health: MetadataHealthConfig = field(default_factory=MetadataHealthConfig)


# ── Notification config ───────────────────────────────────────────────────────

@dataclass
class NotifyConfig:
    on_emptied:       bool = True   # trash was emptied (items removed)
    on_clean:         bool = False  # ran successfully, trash already empty
    on_health_fail:   bool = True   # mount/symlink/threshold checks failed
    on_error:         bool = True   # emptyTrash API call failed
    on_skip:          bool = False  # scheduling paused, config error, section not found


NOTIFICATION_EVENTS = (
    "emptied",
    "clean",
    "health_fail",
    "error",
    "skip",
)


@dataclass
class NotificationDestination:
    name: str
    url: str
    service: str = "custom"
    enabled: bool = True
    events: List[str] = field(default_factory=lambda: list(NOTIFICATION_EVENTS))


# ── Top-level app config ──────────────────────────────────────────────────────

@dataclass
class AppConfig:
    instances: List[PlexInstanceConfig]
    features: FeatureConfig = field(default_factory=FeatureConfig)
    mark_watched: MarkWatchedConfig = field(default_factory=MarkWatchedConfig)
    repair_workers: List[RepairWorkerConfig] = field(default_factory=list)
    discord_webhook: str = ""
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    notification_destinations: List[NotificationDestination] = field(default_factory=list)
    log_level: str = "INFO"
    config_missing: bool = False    # True when no config.yml — UI shows setup prompt
    auth_username: str = ""
    auth_password_hash: str = ""    # bcrypt (or legacy SHA-256) hash, set via Settings UI
    auth_api_token_hash: str = ""   # SHA-256 of a random, independently rotatable API token
    users: List[AppUser] = field(default_factory=list)
    providers: dict = field(default_factory=dict)  # {realdebrid: {api_key: ...}, ...}
    clean_bundles_before_empty: bool = False
    max_trash_items: int = 1000
    max_trash_percent: float = 25.0
    default_cron: str = "0 * * * *"
    log_max_file_size_mb: int = 5
    log_max_total_size_mb: int = 50
    log_retention_days: int = 14


# ── Internal helpers ──────────────────────────────────────────────────────────

def _env_keys() -> dict:
    """Collect debrid API keys from environment."""
    return {
        "realdebrid": os.environ.get("RD_API_KEY", ""),
        "alldebrid":  os.environ.get("AD_API_KEY", ""),
        "torbox":     os.environ.get("TB_API_KEY", ""),
        "debridlink": os.environ.get("DL_API_KEY", ""),
    }


def _env_override(name: str, fallback: str = "") -> str:
    """Use a non-empty environment override, otherwise keep file configuration."""
    value = get_env(name)
    return value if value else fallback


def _load_provider_checks(raw: list) -> List[ProviderCheck]:
    keys = _env_keys()
    checks = []
    for pc in (raw or []):
        ptype   = pc.get("type", "")
        api_key = pc.get("api_key", "") or keys.get(ptype, "")
        checks.append(ProviderCheck(type=ptype, api_key=api_key))
    return checks


def _load_path(raw: dict, lib_type: str,
               lib_min_threshold: float) -> PathConfig:
    pc_raw = raw.get("provider_checks", raw.get("provider_check", None))
    if isinstance(pc_raw, dict):
        pc_raw = [pc_raw]
    return PathConfig(
        path            = raw["path"],
        type            = raw.get("type", lib_type),
        min_threshold   = float(raw.get("min_threshold", lib_min_threshold * 100)) / 100.0,
        provider_checks = _load_provider_checks(pc_raw or []),
    )


def _load_library(raw: dict) -> LibraryConfig:
    lib_type          = raw.get("type", "physical")
    lib_min_threshold = float(raw.get("min_threshold", 90)) / 100.0
    cron              = raw.get("cron", "")
    raw_paths         = raw.get("paths", [])

    parsed_paths = []
    for p in raw_paths:
        if isinstance(p, str):
            parsed_paths.append(PathConfig(
                path          = p,
                type          = lib_type if lib_type != "mixed" else "physical",
                min_threshold = lib_min_threshold,
            ))
        elif isinstance(p, dict):
            parsed_paths.append(_load_path(p, lib_type, lib_min_threshold))

    # Shorthand: single path string at library level
    if not parsed_paths and raw.get("path"):
        single     = raw["path"]
        paths_list = single if isinstance(single, list) else [single]
        for p in paths_list:
            parsed_paths.append(PathConfig(
                path          = p,
                type          = lib_type,
                min_threshold = lib_min_threshold,
            ))

    return LibraryConfig(
        name       = raw["name"],
        type       = lib_type,
        paths      = parsed_paths,
        cron       = cron,
        section_id = raw.get("section_id", None),
        refresh_enabled = bool(raw.get("refresh_enabled", False)),
        refresh_cron = str(raw.get("refresh_cron", "0 * * * *")),
        refresh_guard_minutes = int(raw.get("refresh_guard_minutes", 15)),
    )


def _load_instance(raw: dict) -> PlexInstanceConfig:
    safe  = raw["name"].upper().replace(" ", "_").replace("-", "_")
    url   = _env_override(
        f"PLEX_URL_{safe}",
        _env_override("PLEX_URL", raw.get("url", "")),
    )
    token = _env_override(
        f"PLEX_TOKEN_{safe}",
        _env_override("PLEX_TOKEN", raw.get("token", "")),
    )
    repair_raw = raw.get("timestamp_repair", {})
    if not isinstance(repair_raw, dict):
        repair_raw = {}
    allowed_prefixes = repair_raw.get("allowed_prefixes", [])
    if not isinstance(allowed_prefixes, list):
        allowed_prefixes = []
    repair = TimestampRepairConfig(
        enabled=bool(repair_raw.get("enabled", False)),
        worker=str(repair_raw.get("worker", "local")).strip() or "local",
        database_path=str(repair_raw.get("database_path", "")),
        allowed_prefixes=[str(path) for path in allowed_prefixes],
        max_files_per_folder=int(repair_raw.get("max_files_per_folder", 5)),
        scan_timeout_seconds=int(repair_raw.get("scan_timeout_seconds", 1800)),
        poll_interval_seconds=int(repair_raw.get("poll_interval_seconds", 5)),
        heartbeat_seconds=int(repair_raw.get("heartbeat_seconds", 30)),
    )
    metadata_raw = raw.get("metadata_health", {})
    if not isinstance(metadata_raw, dict):
        metadata_raw = {}
    ignored_libraries = metadata_raw.get("ignored_libraries", [])
    if not isinstance(ignored_libraries, list):
        ignored_libraries = []
    return PlexInstanceConfig(
        name      = raw["name"],
        url       = url,
        token     = token,
        libraries = [_load_library(lib) for lib in raw.get("libraries", [])],
        machine_id = raw.get("machine_id"),
        timestamp_repair = repair,
        metadata_health = MetadataHealthConfig(
            ignored_libraries=[str(name) for name in ignored_libraries],
        ),
    )


def _load_notification_destination(raw: dict) -> NotificationDestination:
    configured_events = raw.get("events", NOTIFICATION_EVENTS)
    if not isinstance(configured_events, list):
        configured_events = list(NOTIFICATION_EVENTS)
    events = [
        event for event in configured_events
        if event in NOTIFICATION_EVENTS
    ]
    return NotificationDestination(
        name=str(raw.get("name", "")).strip(),
        service=str(raw.get("service", "custom")).strip().lower(),
        url=str(raw.get("url", "")).strip(),
        enabled=bool(raw.get("enabled", True)),
        events=events,
    )


# ── Public loader ─────────────────────────────────────────────────────────────

def parse_config(raw: dict, config_missing: bool = False) -> AppConfig:
    """Parse an already-loaded configuration mapping."""
    discord   = _env_override("DISCORD_WEBHOOK", "")
    log_level = _env_override("LOG_LEVEL", "INFO")
    if not raw:
        return AppConfig(
            instances       = [],
            discord_webhook = discord,
            log_level       = log_level,
            config_missing  = config_missing,
        )

    discord   = _env_override("DISCORD_WEBHOOK", raw.get("discord_webhook", ""))
    log_level = _env_override("LOG_LEVEL", raw.get("log_level", "INFO"))

    notify_raw = raw.get("notify", {})
    notify = NotifyConfig(
        on_emptied     = notify_raw.get("on_emptied",     notify_raw.get("on_success", True)),
        on_clean       = notify_raw.get("on_clean",       False),
        on_health_fail = notify_raw.get("on_health_fail", notify_raw.get("on_failure", True)),
        on_error       = notify_raw.get("on_error",       True),
        on_skip        = notify_raw.get("on_skip",        False),
    )
    notifications_raw = raw.get("notifications", {})
    if not isinstance(notifications_raw, dict):
        notifications_raw = {}
    destinations_raw = notifications_raw.get("destinations", [])
    if not isinstance(destinations_raw, list):
        destinations_raw = []
    notification_destinations = [
        _load_notification_destination(destination)
        for destination in destinations_raw
        if isinstance(destination, dict)
    ]

    auth_raw = raw.get("auth", {})
    auth_username      = auth_raw.get("username", "")
    auth_password_hash = auth_raw.get("password_hash", "")
    auth_api_token_hash = auth_raw.get("api_token_hash", "")
    users = []
    for user in auth_raw.get("users", []) if isinstance(auth_raw.get("users", []), list) else []:
        if not isinstance(user, dict) or not str(user.get("username", "")).strip():
            continue
        role = str(user.get("role", "user")).lower()
        if role not in {"admin", "user"}:
            role = "user"
        permissions = user.get("permissions", [])
        if not isinstance(permissions, list):
            permissions = []
        users.append(AppUser(
            username=str(user["username"]).strip(),
            password_hash=str(user.get("password_hash", "")),
            role=role,
            permissions=[str(value) for value in permissions],
        ))

    providers_raw = raw.get("providers", {})
    features_raw = raw.get("features", {})
    if not isinstance(features_raw, dict):
        features_raw = {}
    # Every feature defaults to on, so a config written before a feature
    # existed keeps working without naming it.
    features = FeatureConfig(**{
        key: features_raw.get(key, True) is not False
        for key in vars(FeatureConfig())
    })
    mark_raw = raw.get("mark_watched", {})
    if not isinstance(mark_raw, dict):
        mark_raw = {}
    default_delays = MarkWatchedConfig().retry_delays
    retry_delays = mark_raw.get("retry_delays", default_delays)
    if not isinstance(retry_delays, list) or not retry_delays:
        retry_delays = default_delays
    mark_watched = MarkWatchedConfig(
        webhook_secret=_env_override(
            "MEDIAMENDER_SONARR_WEBHOOK_SECRET",
            str(mark_raw.get("webhook_secret", "")),
        ),
        retry_delays=[max(0, int(value)) for value in retry_delays[:10]],
        workers=min(16, max(1, int(mark_raw.get("workers", 4) or 4))),
        scan_on_import=mark_raw.get("scan_on_import", True) is not False,
        give_up_after_hours=max(0.0, float(mark_raw.get("give_up_after_hours", 120))),
        visible_libraries=(
            [str(value) for value in mark_raw.get("visible_libraries", [])]
            if "visible_libraries" in mark_raw and isinstance(mark_raw.get("visible_libraries"), list)
            else None
        ),
    )
    clean_bundles_before_empty = bool(raw.get("clean_bundles_before_empty", False))
    max_trash_items = int(raw.get("max_trash_items", 1000))
    max_trash_percent = float(raw.get("max_trash_percent", 25))
    schedule_raw = raw.get("schedule", {})
    if not isinstance(schedule_raw, dict):
        schedule_raw = {}
    default_cron = schedule_raw.get("default_cron", "0 * * * *")
    logging_raw = raw.get("logging", {})
    if not isinstance(logging_raw, dict):
        logging_raw = {}
    log_max_file_size_mb = int(logging_raw.get("max_file_size_mb", 5))
    log_max_total_size_mb = int(logging_raw.get("max_total_size_mb", 50))
    log_retention_days = int(logging_raw.get("retention_days", 14))

    workers_raw = raw.get("timestamp_repair_workers", [])
    if not isinstance(workers_raw, list):
        workers_raw = []
    repair_workers = []
    for item in workers_raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        safe = name.upper().replace(" ", "_").replace("-", "_")
        repair_workers.append(RepairWorkerConfig(
            name=name,
            url=str(item.get("url", "")).strip(),
            token=_env_override(
                f"MEDIAMENDER_WORKER_TOKEN_{safe}", str(item.get("token", "")),
            ),
            controller_url=str(item.get("controller_url", "")).strip(),
        ))

    instances = [_load_instance(inst) for inst in raw.get("plex_instances", [])]

    if not instances:
        logger.warning("config.yml loaded but no plex_instances defined.")

    return AppConfig(
        instances           = instances,
        features            = features,
        mark_watched        = mark_watched,
        repair_workers      = repair_workers,
        discord_webhook     = discord,
        notify              = notify,
        notification_destinations = notification_destinations,
        log_level           = log_level,
        config_missing      = False,
        auth_username       = auth_username,
        auth_password_hash  = auth_password_hash,
        auth_api_token_hash = auth_api_token_hash,
        users               = users,
        providers           = providers_raw,
        clean_bundles_before_empty = clean_bundles_before_empty,
        max_trash_items      = max_trash_items,
        max_trash_percent    = max_trash_percent,
        default_cron         = default_cron,
        log_max_file_size_mb = log_max_file_size_mb,
        log_max_total_size_mb = log_max_total_size_mb,
        log_retention_days   = log_retention_days,
    )


def load_config(path: str = "data/config.yml") -> AppConfig:
    """
    Load configuration. If config.yml does not exist the app still starts —
    returns AppConfig with config_missing=True so the UI shows setup instructions.
    """
    if not os.path.exists(path):
        logger.warning(
            f"No config file found at '{path}'. "
            "Mount a config.yml to get started. "
            "UI will show setup instructions."
        )
        return parse_config({}, config_missing=True)

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not raw:
        logger.warning("config.yml is empty — showing setup wizard.")
        return parse_config({}, config_missing=True)
    return parse_config(raw)
