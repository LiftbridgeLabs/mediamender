"""Persistent, idempotent background processing for Sonarr import webhooks."""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.storage import atomic_write_json


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
    }


def webhook_key(event: dict) -> str:
    identity = {
        "series": event["series"],
        "episode_file": event["episode_file"],
        "episodes": event["episodes"],
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
            return dict(record), True

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
                self._sleep(delay)
            self._update(
                job_id, status="processing", attempts=attempt,
                next_retry_seconds=None, message=f"Checking Plex (attempt {attempt})",
            )
            try:
                result = self.processor(event) or {}
                self._update(
                    job_id, status="succeeded", result=result,
                    message=result.get("message", "Marked matched Plex episode watched"),
                )
                return self.get(job_id)
            except PlexEpisodePending as exc:
                if attempt == len(delays):
                    self._update(
                        job_id, status="failed",
                        message=f"Plex match was not available after {attempt} attempts: {exc}",
                    )
                    return self.get(job_id)
            except Exception as exc:
                logging.getLogger("mediamender").exception("Mark-it-Watched job failed")
                self._update(
                    job_id, status="failed",
                    message=f"Plex processing failed: {type(exc).__name__}: {exc}",
                )
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
