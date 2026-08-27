"""Persistent, idempotent background processing for Sonarr import webhooks."""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.storage import atomic_write_json


logger = logging.getLogger("mediamender.mark_watched")


class PlexEpisodePending(Exception):
    """Raised when Sonarr is finished but Plex has not matched the episode yet."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_sonarr_download(payload: dict) -> dict | None:
    """Return the stable subset of a finalized Sonarr Download event."""
    event_type = str(payload.get("eventType", "")).strip().lower()
    if event_type == "test":
        return None
    if event_type != "download":
        raise ValueError("Only finalized Sonarr Download events are accepted")
    series = payload.get("series")
    episode_file = payload.get("episodeFile")
    episodes = payload.get("episodes")
    if not isinstance(series, dict) or not series.get("title"):
        raise ValueError("Sonarr series metadata is required")
    if not isinstance(episode_file, dict) or not (
        episode_file.get("id") or episode_file.get("path") or episode_file.get("relativePath")
    ):
        raise ValueError("A completed Sonarr episodeFile is required")
    if not isinstance(episodes, list) or not episodes:
        raise ValueError("At least one imported episode is required")

    normalized_episodes = []
    for episode in episodes:
        if not isinstance(episode, dict):
            continue
        try:
            season = int(episode["seasonNumber"])
            number = int(episode["episodeNumber"])
        except (KeyError, TypeError, ValueError):
            continue
        normalized_episodes.append({
            "id": episode.get("id"),
            "season": season,
            "episode": number,
            "title": str(episode.get("title", "")),
        })
    if not normalized_episodes:
        raise ValueError("Imported episodes need seasonNumber and episodeNumber")
    normalized_episodes.sort(key=lambda item: (item["season"], item["episode"]))
    return {
        "series": {
            "id": series.get("id"),
            "title": str(series["title"]),
            "tvdb_id": series.get("tvdbId"),
            "year": series.get("year"),
        },
        "episode_file": {
            "id": episode_file.get("id"),
            "path": str(episode_file.get("path") or episode_file.get("relativePath") or ""),
        },
        "episodes": normalized_episodes,
        "is_upgrade": bool(payload.get("isUpgrade", False)),
        "rule_user": str(payload.get("_mediamender_user", "")),
        "source_connection": str(payload.get("_mediamender_connection", "")),
    }


def webhook_key(event: dict) -> str:
    identity = {
        "series": event["series"],
        "episode_file": event["episode_file"],
        "episodes": event["episodes"],
        "rule_user": event.get("rule_user", ""),
        "source_connection": event.get("source_connection", ""),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class MarkWatchedManager:
    """Own a durable queue while ensuring each webhook identity runs once."""

    def __init__(
        self,
        data_dir: str,
        processor: Callable[[dict], dict] | None = None,
        retry_delays: tuple[float, ...] = (10, 30, 60, 120, 300),
        *,
        autostart: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.path = Path(data_dir) / "mark-watched-jobs.json"
        self.processor = processor
        self.retry_delays = retry_delays
        self._sleep = sleep
        self._lock = threading.RLock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._records = self._load()
        for job_id, record in self._records.items():
            if record.get("status") in {"queued", "retrying", "processing"}:
                record["status"] = "queued"
                self._queue.put(job_id)
        self._thread = None
        if autostart:
            self.start()

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        atomic_write_json(str(self.path), self._records)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="mark-watched-worker",
        )
        self._thread.start()

    def set_processor(self, processor: Callable[[dict], dict]) -> None:
        self.processor = processor

    def enqueue(self, payload: dict) -> tuple[dict | None, bool]:
        event = normalize_sonarr_download(payload)
        if event is None:
            return None, False
        job_id = webhook_key(event)
        with self._lock:
            existing = self._records.get(job_id)
            if existing:
                return dict(existing), False
            now = _utc_now()
            record = {
                "id": job_id,
                "status": "queued",
                "attempts": 0,
                "message": "Finalized Sonarr import queued",
                "created_at": now,
                "updated_at": now,
                "event": event,
            }
            self._records[job_id] = record
            self._save()
            self._queue.put(job_id)
            logger.info(
                "Queued Sonarr import %s for %s",
                job_id[:12], event["series"]["title"],
            )
            return dict(record), True

    def enqueue_manual(self, event: dict) -> dict:
        """Queue an explicitly confirmed manual Plex history update."""
        event = dict(event)
        event["source"] = "manual"
        event["request_id"] = secrets.token_urlsafe(12)
        encoded = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
        job_id = hashlib.sha256(encoded).hexdigest()
        now = _utc_now()
        scope = event.get("manual", {}).get("scope", "show")
        record = {
            "id": job_id,
            "status": "queued",
            "attempts": 0,
            "message": f"Manual {scope} watch update queued",
            "created_at": now,
            "updated_at": now,
            "event": event,
        }
        with self._lock:
            self._records[job_id] = record
            self._save()
            self._queue.put(job_id)
        logger.info(
            "Queued manual %s update %s for %s",
            scope, job_id[:12], event.get("series", {}).get("title", "Plex show"),
        )
        return dict(record)

    def _update(self, job_id: str, **changes) -> None:
        with self._lock:
            record = self._records[job_id]
            record.update(changes)
            record["updated_at"] = _utc_now()
            self._save()

    def process(self, job_id: str) -> dict:
        with self._lock:
            record = self._records[job_id]
            event = dict(record["event"])
        if self.processor is None:
            self._update(job_id, status="failed", message="No Plex processor configured")
            return self.get(job_id)

        delays = (0,) + tuple(self.retry_delays)
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                self._update(
                    job_id, status="retrying", next_retry_seconds=delay,
                    message=f"Plex match pending; retrying in {delay:g} seconds",
                )
                logger.info(
                    "Job %s waiting %g seconds for Plex match", job_id[:12], delay,
                )
                self._sleep(delay)
            self._update(
                job_id, status="processing", attempts=attempt,
                next_retry_seconds=None, message=f"Checking Plex (attempt {attempt})",
            )
            try:
                logger.info("Processing Mark-it-Watched job %s (attempt %s)",
                            job_id[:12], attempt)
                result = self.processor(event) or {}
                self._update(
                    job_id, status="succeeded", result=result,
                    message=result.get("message", "Marked matched Plex episode watched"),
                )
                logger.info("Mark-it-Watched job %s succeeded: %s", job_id[:12],
                            result.get("message", "completed"))
                return self.get(job_id)
            except PlexEpisodePending as exc:
                if attempt == len(delays):
                    self._update(
                        job_id, status="failed",
                        message=f"Plex match was not available after {attempt} attempts: {exc}",
                    )
                    logger.error("Mark-it-Watched job %s exhausted Plex retries: %s",
                                 job_id[:12], exc)
                    return self.get(job_id)
            except Exception as exc:
                logging.getLogger("mediamender").exception("Mark-it-Watched job failed")
                self._update(
                    job_id, status="failed",
                    message=f"Plex processing failed: {type(exc).__name__}: {exc}",
                )
                logger.error("Mark-it-Watched job %s failed: %s: %s",
                             job_id[:12], type(exc).__name__, exc)
                return self.get(job_id)
        return self.get(job_id)

    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self.process(job_id)
            finally:
                self._queue.task_done()

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            record = self._records.get(job_id)
            return dict(record) if record else None

    def status(self, limit: int = 50) -> dict:
        with self._lock:
            records = sorted(
                self._records.values(), key=lambda item: item.get("updated_at", ""), reverse=True,
            )[:limit]
            return {"jobs": [dict(record) for record in records]}


class MarkWatchedRuleStore:
    """Persist per-user show defaults and explicit season overrides."""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "mark-watched-rules.json"
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"users": {}}
        except (OSError, ValueError):
            return {"users": {}}

    def _save(self) -> None:
        atomic_write_json(str(self.path), self._data)

    @staticmethod
    def _show_key(instance: str, library: str, show_rating_key: str) -> str:
        return f"{instance}::{library}::{show_rating_key}"

    def _user(self, username: str, create: bool = False) -> dict:
        users = self._data.setdefault("users", {})
        if create:
            return users.setdefault(username, {"shows": {}, "seasons": {}})
        return users.get(username, {"shows": {}, "seasons": {}})

    def set_show(self, username: str, instance: str, library: str,
                 show_rating_key: str, enabled: bool) -> None:
        with self._lock:
            user = self._user(username, create=True)
            user["shows"][self._show_key(instance, library, show_rating_key)] = bool(enabled)
            self._save()

    def set_season(self, username: str, instance: str, library: str,
                   show_rating_key: str, season_index: int,
                   enabled: bool | None) -> None:
        key = f"{self._show_key(instance, library, show_rating_key)}::{int(season_index)}"
        with self._lock:
            user = self._user(username, create=True)
            if enabled is None:
                user["seasons"].pop(key, None)
            else:
                user["seasons"][key] = bool(enabled)
            self._save()

    def rule(self, username: str, instance: str, library: str,
             show_rating_key: str, season_index: int) -> dict:
        show_key = self._show_key(instance, library, show_rating_key)
        season_key = f"{show_key}::{int(season_index)}"
        with self._lock:
            user = self._user(username)
            show_enabled = bool(user.get("shows", {}).get(show_key, False))
            seasons = user.get("seasons", {})
            explicit = season_key in seasons
            return {
                "enabled": bool(seasons[season_key]) if explicit else show_enabled,
                "source": "season" if explicit else "show",
                "show_enabled": show_enabled,
                "season_override": seasons.get(season_key) if explicit else None,
            }

    def enabled_for_any_user(self, instance: str, library: str,
                             show_rating_key: str, season_index: int) -> bool:
        with self._lock:
            usernames = list(self._data.get("users", {}))
        return any(self.rule(
            username, instance, library, show_rating_key, season_index,
        )["enabled"] for username in usernames)

    def all_for_user(self, username: str) -> dict:
        with self._lock:
            user = self._user(username)
            return json.loads(json.dumps(user))

    def usernames(self) -> list[str]:
        with self._lock:
            return list(self._data.get("users", {}))

    def delete_user(self, username: str) -> None:
        with self._lock:
            self._data.get("users", {}).pop(username, None)
            self._save()

    def set_all(self, username: str, show_keys: list[tuple[str, str, str]],
                enabled: bool) -> None:
        with self._lock:
            user = self._user(username, create=True)
            for instance, library, rating_key in show_keys:
                user["shows"][self._show_key(instance, library, rating_key)] = bool(enabled)
            user["seasons"] = {}
            self._save()


def process_plex_event(event: dict, app_config, clients: dict,
                       rules: MarkWatchedRuleStore) -> dict:
    """Find imported episodes across configured TV sections, then apply rules."""
    matched = []
    marked = []
    expected_coordinates = {
        (episode["season"], episode["episode"]) for episode in event["episodes"]
    }
    matched_coordinates = set()
    configured_visibility = app_config.mark_watched.visible_libraries
    visible = set(configured_visibility or [])
    for instance in app_config.instances:
        plex = clients.get(instance.name)
        if plex is None:
            continue
        for library in instance.libraries:
            library_key = f"{instance.name}::{library.name}"
            if configured_visibility is not None and library_key not in visible:
                continue
            section_id = library.section_id or plex.find_section_id(library.name)
            if not section_id or plex.get_section_type(str(section_id)) != "show":
                continue
            for episode in event["episodes"]:
                item = plex.find_episode(
                    str(section_id), event["series"]["title"],
                    episode["season"], episode["episode"],
                )
                if item is None:
                    continue
                matched.append(item)
                item["instance_name"] = instance.name
                item["library_name"] = library.name
                item["plex"] = plex
                matched_coordinates.add((episode["season"], episode["episode"]))
    missing = expected_coordinates - matched_coordinates
    if missing:
        coordinates = ", ".join(
            f"S{season:02d}E{episode:02d}" for season, episode in sorted(missing)
        )
        raise PlexEpisodePending(f"{event['series']['title']} {coordinates}")
    for item in matched:
        rule_user = str(event.get("rule_user", ""))
        enabled = rules.rule(
            rule_user, item["instance_name"], item["library_name"],
            item["show_rating_key"], item["season_index"],
        )["enabled"] if rule_user else rules.enabled_for_any_user(
            item["instance_name"], item["library_name"],
            item["show_rating_key"], item["season_index"],
        )
        if not enabled:
            continue
        item["plex"].mark_watched(item["rating_key"])
        marked.append(item)
    if not marked:
        return {
            "message": "Plex matched the import; no automatic watch rule was enabled",
            "matched": len(matched), "marked": 0,
        }
    return {
        "message": f"Marked {len(marked)} matched Plex episode(s) watched",
        "matched": len(matched), "marked": len(marked),
        "rating_keys": [item["rating_key"] for item in marked],
    }


def process_manual_event(event: dict, app_config, clients: dict) -> dict:
    """Apply a confirmed manual show or season update to existing Plex history."""
    manual = event.get("manual", {})
    instance_name = str(manual.get("instance", ""))
    library_name = str(manual.get("library", ""))
    show_key = str(manual.get("show_rating_key", ""))
    scope = str(manual.get("scope", ""))
    if scope not in {"show", "season"} or not show_key.isdigit():
        raise ValueError("Invalid manual Mark-it-Watched request")

    instance = next(
        (item for item in app_config.instances if item.name == instance_name), None,
    )
    library = next(
        (item for item in instance.libraries if item.name == library_name), None,
    ) if instance else None
    plex = clients.get(instance_name)
    if instance is None or library is None or plex is None:
        raise ValueError("Configured Plex TV library was not found")
    configured_visibility = app_config.mark_watched.visible_libraries
    if configured_visibility is not None and (
        f"{instance_name}::{library_name}" not in set(configured_visibility)
    ):
        raise ValueError("This Plex library is hidden in Settings")
    section_id = library.section_id or plex.find_section_id(library.name)
    if not section_id or plex.get_section_type(str(section_id)) != "show":
        raise ValueError("Manual Mark-it-Watched supports TV libraries only")

    episodes = plex.list_show_episodes(show_key)
    if scope == "season":
        season_index = int(manual["season_index"])
        episodes = [
            episode for episode in episodes
            if episode["season_index"] == season_index
        ]
    if not episodes:
        raise ValueError("Plex returned no episodes for the selected scope")

    unwatched = [episode for episode in episodes if episode["view_count"] < 1]
    if unwatched:
        plex.mark_watched_many([episode["rating_key"] for episode in unwatched])
    already_watched = len(episodes) - len(unwatched)
    scope_label = "season" if scope == "season" else "show"
    return {
        "message": (
            f"Manual {scope_label} update marked {len(unwatched)} episode(s) watched; "
            f"{already_watched} were already watched"
        ),
        "matched": len(episodes),
        "marked": len(unwatched),
        "already_watched": already_watched,
        "rating_keys": [episode["rating_key"] for episode in unwatched],
    }


def process_mark_watched_event(event: dict, app_config, clients: dict,
                               rules: MarkWatchedRuleStore) -> dict:
    """Dispatch durable automatic and manual Mark-it-Watched jobs."""
    if event.get("source") == "manual":
        return process_manual_event(event, app_config, clients)
    return process_plex_event(event, app_config, clients, rules)
