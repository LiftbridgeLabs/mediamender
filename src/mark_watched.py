"""Persistent, idempotent background processing for Sonarr import webhooks."""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from src.storage import atomic_write_json


logger = logging.getLogger("mediamender.mark_watched")

# Enough of a trail to explain a job without letting a record grow unbounded.
LOG_TRAIL_LIMIT = 60


class ImportVanished(Exception):
    """The imported file is gone, so there is nothing left to wait for."""


class PlexEpisodePending(Exception):
    """Raised when Sonarr is finished but Plex has not matched the episode yet."""

    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.details = list(details or [])


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _humanize(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds / 3600:.1f} hours"


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


class WebhookLog:
    """Remember every inbound Sonarr request, accepted or not.

    Only accepted imports became job records, so a webhook rejected for a bad
    secret or an event type we do not queue left no trace at all. An operator
    whose imports were never arriving saw exactly the same empty activity list
    as one whose Sonarr was never calling.
    """

    def __init__(self, data_dir: str, limit: int = 50):
        self.path = Path(data_dir) / "sonarr-webhook-log.json"
        self.limit = int(limit)
        self._lock = threading.RLock()
        self._attempts = self._load()

    def _load(self) -> list:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        attempts = value.get("attempts") if isinstance(value, dict) else value
        return list(attempts) if isinstance(attempts, list) else []

    def record(self, *, outcome: str, detail: str = "", event_type: str = "",
               series: str = "", remote: str = "") -> None:
        entry = {
            "at": _utc_now(),
            "outcome": outcome,
            "detail": detail,
            "event_type": event_type,
            "series": series,
            "remote": remote,
        }
        with self._lock:
            self._attempts.insert(0, entry)
            del self._attempts[self.limit:]
            try:
                atomic_write_json(str(self.path), {"attempts": self._attempts})
            except OSError:
                logger.warning("Could not persist the Sonarr webhook log")
        logger.info("Sonarr webhook %s from %s (%s) %s",
                    outcome, remote or "unknown", event_type or "no event type", detail)

    def recent(self, limit: int = 20) -> list:
        with self._lock:
            return [dict(entry) for entry in self._attempts[:limit]]

    def summary(self) -> dict:
        with self._lock:
            attempts = list(self._attempts)
        counts: dict[str, int] = {}
        for entry in attempts:
            counts[entry.get("outcome", "?")] = counts.get(entry.get("outcome", "?"), 0) + 1
        return {
            "total": len(attempts),
            "outcomes": counts,
            "last_at": attempts[0]["at"] if attempts else "",
        }


class MarkWatchedManager:
    """Own a durable queue while ensuring each webhook identity runs once."""

    def __init__(
        self,
        data_dir: str,
        processor: Callable[[dict], dict] | None = None,
        retry_delays: tuple[float, ...] = (10, 30, 60, 120, 300),
        *,
        autostart: bool = True,
        workers: int = 4,
        poll_seconds: float = 15,
        give_up_after_hours: float = 0,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = _utc_now_dt,
    ):
        self.path = Path(data_dir) / "mark-watched-jobs.json"
        self.processor = processor
        self.retry_delays = retry_delays
        # Each job spends nearly all of its life asleep between Plex polls, so
        # a pool keeps one waiting import from stalling every later webhook.
        self.workers = max(1, int(workers))
        self.poll_seconds = max(1.0, float(poll_seconds))
        # 0 means keep checking indefinitely. A library with no real-time scan
        # trigger can take hours, and a job that gave up stayed unwatched for
        # good because each webhook identity is only ever queued once.
        self.give_up_after_hours = max(0.0, float(give_up_after_hours))
        self._sleep = sleep
        self._now = now
        self._lock = threading.RLock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._inflight: set[str] = set()
        self._records = self._load()
        for job_id, record in self._records.items():
            # A job interrupted mid-attempt is re-run; one that was waiting on
            # Plex keeps its due time across the restart.
            if record.get("status") in {"queued", "retrying", "processing"}:
                record["status"] = "queued"
                self._queue.put(job_id)
        self._threads: list[threading.Thread] = []
        self._scheduler: threading.Thread | None = None
        self._stopped = threading.Event()
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
        """Bring the pool up to strength, replacing any thread that died."""
        with self._lock:
            if self._scheduler is None or not self._scheduler.is_alive():
                self._scheduler = threading.Thread(
                    target=self._schedule, daemon=True,
                    name="mark-watched-scheduler",
                )
                self._scheduler.start()
            self._threads = [
                thread for thread in self._threads if thread.is_alive()
            ]
            while len(self._threads) < self.workers:
                thread = threading.Thread(
                    target=self._run, daemon=True,
                    name=f"mark-watched-worker-{len(self._threads) + 1}",
                )
                self._threads.append(thread)
                thread.start()

    def stop(self) -> None:
        """Ask the scheduler to finish. Workers are daemons and end with the process."""
        self._stopped.set()

    def live_workers(self) -> int:
        with self._lock:
            return sum(1 for thread in self._threads if thread.is_alive())

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
            now = self._stamp()
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
        now = self._stamp()
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

    def _stamp(self) -> str:
        return self._now().isoformat()

    def _update(self, job_id: str, **changes) -> None:
        with self._lock:
            record = self._records[job_id]
            record.update(changes)
            record["updated_at"] = self._stamp()
            self._save()

    def _log(self, job_id: str, message: str, details: list[str] | None = None) -> None:
        """Keep a readable trail on the record so the UI can explain a job."""
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return
            trail = record.setdefault("log", [])
            stamp = self._stamp()
            trail.append({"at": stamp, "message": message})
            for detail in details or []:
                trail.append({"at": stamp, "message": f"  {detail}"})
            if len(trail) > LOG_TRAIL_LIMIT:
                del trail[:-LOG_TRAIL_LIMIT]
            self._save()

    def process(self, job_id: str) -> dict:
        """Make one attempt. A job that is still waiting on Plex is rescheduled.

        Attempts used to run in a loop that slept between them, which held a
        worker for the whole window and ended in permanent failure. A library
        with no real-time scan trigger can take far longer than any fixed
        window, so an unmatched job is parked with a due time instead and the
        scheduler brings it back.
        """
        with self._lock:
            record = self._records[job_id]
            event = dict(record["event"])
            attempt = int(record.get("attempts", 0)) + 1
            self._inflight.add(job_id)
        try:
            return self._attempt(job_id, event, attempt)
        finally:
            with self._lock:
                self._inflight.discard(job_id)

    def _backoff(self, attempt: int) -> float:
        """Seconds to wait before attempt ``attempt`` + 1, the last step repeating."""
        delays = tuple(self.retry_delays) or (60,)
        return float(delays[min(attempt, len(delays)) - 1])

    def _expired(self, record: dict) -> bool:
        if not self.give_up_after_hours:
            return False
        try:
            created = datetime.fromisoformat(record["created_at"])
        except (KeyError, ValueError):
            return False
        age = (self._now() - created).total_seconds()
        return age > self.give_up_after_hours * 3600

    def _attempt(self, job_id: str, event: dict, attempt: int) -> dict:
        if self.processor is None:
            self._update(job_id, status="failed", message="No Plex processor configured")
            return self.get(job_id)

        self._update(
            job_id, status="processing", attempts=attempt,
            next_attempt_at=None, message=f"Checking Plex (attempt {attempt})",
        )
        try:
            logger.info("Processing Mark-it-Watched job %s (attempt %s)",
                        job_id[:12], attempt)
            result = self.processor(event) or {}
            message = result.get("message", "Marked matched Plex episode watched")
            self._update(job_id, status="succeeded", result=result, message=message)
            self._log(job_id, f"Attempt {attempt}: {message}", result.get("details"))
            logger.info("Mark-it-Watched job %s succeeded: %s", job_id[:12], message)
        except ImportVanished as exc:
            self._update(
                job_id, status="failed",
                message=f"Stopped waiting: {exc}",
            )
            self._log(job_id, f"Attempt {attempt}: stopped waiting, {exc}")
            logger.info("Mark-it-Watched job %s abandoned: %s", job_id[:12], exc)
        except PlexEpisodePending as exc:
            self._log(job_id, f"Attempt {attempt}: Plex has not matched {exc}",
                      getattr(exc, "details", None))
            with self._lock:
                record = self._records[job_id]
                expired = self._expired(record)
            if expired:
                self._update(
                    job_id, status="failed",
                    message=(
                        f"Plex still had no match after "
                        f"{self.give_up_after_hours:g}h and {attempt} attempts: {exc}"
                    ),
                )
                logger.error("Mark-it-Watched job %s gave up: %s", job_id[:12], exc)
            else:
                delay = self._backoff(attempt)
                due = self._now() + timedelta(seconds=delay)
                self._update(
                    job_id, status="waiting", next_attempt_at=due.isoformat(),
                    message=(
                        f"Plex has not scanned this episode yet; "
                        f"checking again in {_humanize(delay)}"
                    ),
                )
                logger.info("Mark-it-Watched job %s waiting %s for a Plex match",
                            job_id[:12], _humanize(delay))
        except Exception as exc:
            logging.getLogger("mediamender").exception("Mark-it-Watched job failed")
            self._update(
                job_id, status="failed",
                message=f"Plex processing failed: {type(exc).__name__}: {exc}",
            )
            self._log(job_id, f"Attempt {attempt} failed: {type(exc).__name__}: {exc}")
            logger.error("Mark-it-Watched job %s failed: %s: %s",
                         job_id[:12], type(exc).__name__, exc)
        return self.get(job_id)

    def due_jobs(self) -> list[str]:
        """Waiting jobs whose next attempt has come round."""
        now = self._now()
        due = []
        with self._lock:
            for job_id, record in self._records.items():
                if record.get("status") != "waiting" or job_id in self._inflight:
                    continue
                stamp = record.get("next_attempt_at")
                if not stamp:
                    due.append(job_id)
                    continue
                try:
                    if datetime.fromisoformat(stamp) <= now:
                        due.append(job_id)
                except ValueError:
                    due.append(job_id)
        return due

    def promote_due(self) -> list[str]:
        """Move every due job back onto the queue. Returns the ids moved."""
        moved = []
        for job_id in self.due_jobs():
            with self._lock:
                record = self._records.get(job_id)
                if record is None or record.get("status") != "waiting":
                    continue
                record["status"] = "queued"
                self._save()
            self._queue.put(job_id)
            moved.append(job_id)
        return moved

    def _schedule(self) -> None:
        """Return due jobs to the queue, forever.

        Waits on an Event rather than the injected sleep, so a test double that
        returns immediately cannot turn this into a busy loop.
        """
        while not self._stopped.is_set():
            try:
                self.promote_due()
            except Exception:
                logger.exception("Mark-it-Watched scheduler pass failed")
            self._stopped.wait(self.poll_seconds)

    def retry_unfinished(self) -> dict:
        """Re-queue every job that has not succeeded so the worker retries it.

        Sonarr only sends a webhook identity once, and enqueue() is idempotent
        on that identity, so a job that gave up would otherwise stay failed
        forever with no way to fire it again. A job merely waiting on Plex is
        brought forward rather than left until its due time.
        """
        requeued: list[str] = []
        pending = 0
        in_flight = 0
        with self._lock:
            for job_id, record in self._records.items():
                status = record.get("status")
                if status == "succeeded":
                    continue
                if job_id in self._inflight:
                    in_flight += 1
                    continue
                if status == "queued":
                    pending += 1
                    continue
                record.update({
                    "status": "queued",
                    "attempts": 0,
                    "next_attempt_at": None,
                    "message": "Re-queued by a manual Mark-it-Watched retry",
                    "updated_at": _utc_now(),
                })
                requeued.append(job_id)
            if requeued:
                self._save()
        for job_id in requeued:
            self._log(job_id, "Re-queued by Run pending jobs now")
        for job_id in requeued:
            self._queue.put(job_id)
        if requeued:
            logger.info(
                "Re-queued %s unfinished Mark-it-Watched job(s): %s",
                len(requeued), ", ".join(job_id[:12] for job_id in requeued),
            )
        return {
            "requeued": len(requeued),
            "already_queued": pending,
            "in_flight": in_flight,
            "job_ids": requeued,
        }

    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self.process(job_id)
            except Exception:
                # A dead worker thread would silently strand every later
                # webhook, so keep draining the queue no matter what.
                logger.exception(
                    "Mark-it-Watched worker could not process job %s", job_id[:12],
                )
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
    """Persist show defaults and explicit season overrides for the install.

    Rules are global. Mark-it-Watched writes Plex history through each server's
    configured token, so a rule has always belonged to that Plex account rather
    than to whoever happened to be signed in to mediaMender.
    """

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "mark-watched-rules.json"
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"shows": {}, "seasons": {}}
        if not isinstance(value, dict):
            return {"shows": {}, "seasons": {}}
        if "users" in value:
            return self._flatten_users(value)
        return {
            "shows": dict(value.get("shows", {}) or {}),
            "seasons": dict(value.get("seasons", {}) or {}),
        }

    @staticmethod
    def _flatten_users(value: dict) -> dict:
        """Fold a per-user rule file into one global set, keeping every On.

        Rules used to be keyed by the mediaMender account that saved them, and
        an import was attributed to whichever account connected Sonarr. Those
        names are set independently, so an enabled rule is the operator's real
        intent no matter which name recorded it.
        """
        shows: dict = {}
        seasons: dict = {}
        for user in (value.get("users", {}) or {}).values():
            if not isinstance(user, dict):
                continue
            for key, enabled in (user.get("shows", {}) or {}).items():
                shows[key] = bool(enabled) or bool(shows.get(key, False))
            for key, enabled in (user.get("seasons", {}) or {}).items():
                seasons[key] = bool(enabled) or bool(seasons.get(key, False))
        logger.info(
            "Migrated %s show and %s season Mark-it-Watched rules to one "
            "global rule set", len(shows), len(seasons),
        )
        return {"shows": shows, "seasons": seasons}

    def _save(self) -> None:
        atomic_write_json(str(self.path), self._data)

    @staticmethod
    def _show_key(instance: str, library: str, show_rating_key: str) -> str:
        return f"{instance}::{library}::{show_rating_key}"

    def set_show(self, instance: str, library: str,
                 show_rating_key: str, enabled: bool) -> None:
        with self._lock:
            self._data["shows"][
                self._show_key(instance, library, show_rating_key)
            ] = bool(enabled)
            self._save()

    def set_season(self, instance: str, library: str, show_rating_key: str,
                   season_index: int, enabled: bool | None) -> None:
        key = f"{self._show_key(instance, library, show_rating_key)}::{int(season_index)}"
        with self._lock:
            if enabled is None:
                self._data["seasons"].pop(key, None)
            else:
                self._data["seasons"][key] = bool(enabled)
            self._save()

    def rule(self, instance: str, library: str,
             show_rating_key: str, season_index: int) -> dict:
        show_key = self._show_key(instance, library, show_rating_key)
        season_key = f"{show_key}::{int(season_index)}"
        with self._lock:
            shows = self._data["shows"]
            seasons = self._data["seasons"]
            show_enabled = bool(shows.get(show_key, False))
            explicit = season_key in seasons
            return {
                "enabled": bool(seasons[season_key]) if explicit else show_enabled,
                "source": "season" if explicit else "show",
                "show_enabled": show_enabled,
                "season_override": seasons.get(season_key) if explicit else None,
            }

    def all_rules(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def set_all(self, show_keys: list[tuple[str, str, str]],
                enabled: bool) -> None:
        with self._lock:
            for instance, library, rating_key in show_keys:
                self._data["shows"][
                    self._show_key(instance, library, rating_key)
                ] = bool(enabled)
            self._data["seasons"] = {}
            self._save()


def imported_file_missing(event: dict) -> str:
    """Return a reason when Sonarr's imported file is provably gone.

    Sonarr and mediaMender do not always share a mount, so an unreadable path
    means "cannot tell" and never "missing" - only a parent directory this
    container can actually see makes the file's absence meaningful.
    """
    path = str(event.get("episode_file", {}).get("path", ""))
    if not path:
        return ""
    try:
        target = Path(path)
        if target.exists():
            return ""
        parent = target.parent
        if not parent.exists():
            return ""
        return f"the imported file is gone from {parent}"
    except OSError:
        return ""


class ScanThrottle:
    """Rate-limit Plex scan requests per library.

    A job now waits indefinitely for a slow library, so without this it would
    ask Plex to scan on every attempt - and the fallback for a path Plex will
    not accept is a full library refresh, which is expensive.
    """

    def __init__(self, interval_seconds: float = 900):
        self.interval_seconds = float(interval_seconds)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        with self._lock:
            previous = self._last.get(key)
            if previous is not None and moment - previous < self.interval_seconds:
                return False
            self._last[key] = moment
            return True

    def reset(self) -> None:
        with self._lock:
            self._last.clear()


scan_throttle = ScanThrottle()


def request_plex_scan(scannable: list[tuple], event: dict) -> list[str]:
    """Ask Plex to scan the imported episode's folder, or the whole section.

    Plex only accepts a path-limited scan for a path it can see itself. Sonarr
    reports the path as Sonarr sees it, which is the same path in the usual
    single-mount setup and a different one when the two containers map media
    differently, so a rejected path falls back to a section refresh.
    """
    notes: list[str] = []
    folder = ""
    path = str(event.get("episode_file", {}).get("path", ""))
    if path:
        separator = "\\" if "\\" in path and "/" not in path else "/"
        folder = separator.join(path.split(separator)[:-1])
    for library_key, plex, section_id in scannable:
        if not scan_throttle.allow(library_key):
            notes.append(f"{library_key}: a scan was requested recently; not asking again")
            continue
        result = plex.scan_path(section_id, folder) if folder else {"ok": False}
        if result.get("ok"):
            notes.append(f"{library_key}: asked Plex to scan {folder}")
            continue
        result = plex.refresh_section(section_id)
        notes.append(
            f"{library_key}: asked Plex to refresh the whole library"
            if result.get("ok") else
            f"{library_key}: Plex refused a scan request "
            f"({result.get('http') or result.get('error', 'unknown')})"
        )
    return notes


def process_plex_event(event: dict, app_config, clients: dict,
                       rules: MarkWatchedRuleStore) -> dict:
    """Find imported episodes across configured TV sections, then apply rules."""
    matched = []
    marked = []
    details: list[str] = []
    expected_coordinates = {
        (episode["season"], episode["episode"]) for episode in event["episodes"]
    }
    matched_coordinates = set()
    searched = []
    scannable: list[tuple] = []
    for instance in app_config.instances:
        plex = clients.get(instance.name)
        if plex is None:
            details.append(f"{instance.name}: skipped, no connected Plex client")
            continue
        for library in instance.libraries:
            library_key = f"{instance.name}::{library.name}"
            if not app_config.mark_watched.shows_library(instance.name, library.name):
                details.append(f"{library_key}: skipped, hidden in Settings")
                continue
            section_id = library.section_id or plex.find_section_id(library.name)
            if not section_id or plex.get_section_type(str(section_id)) != "show":
                details.append(f"{library_key}: skipped, not a Plex TV library")
                continue
            searched.append(library_key)
            scannable.append((library_key, plex, str(section_id)))
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
                plex_title = str(item.get("show_title", ""))
                renamed = (
                    f", Plex calls this show '{plex_title}'"
                    if plex_title and plex_title.strip().casefold()
                    != event["series"]["title"].strip().casefold() else ""
                )
                details.append(
                    f"{library_key}: matched S{episode['season']:02d}"
                    f"E{episode['episode']:02d} (show ratingKey "
                    f"{item['show_rating_key']}, episode {item['rating_key']}"
                    f"{renamed})"
                )
    missing = expected_coordinates - matched_coordinates
    if missing:
        # An import that will never appear is usually one that was replaced or
        # removed behind the symlink. When the path is visible from here, that
        # is knowable now rather than after the give-up window.
        vanished = imported_file_missing(event)
        if vanished:
            raise ImportVanished(vanished)
        coordinates = ", ".join(
            f"S{season:02d}E{episode:02d}" for season, episode in sorted(missing)
        )
        details.append("Searched TV libraries: " + (", ".join(searched) or "none"))
        # Sonarr finishes an import the moment the file lands, which for a
        # symlinked debrid library is long before Plex has scanned it. Waiting
        # passively is why these jobs used to expire unmatched, so ask Plex to
        # look at the imported folder instead.
        if app_config.mark_watched.scan_on_import:
            details.extend(request_plex_scan(scannable, event))
        raise PlexEpisodePending(
            f"{event['series']['title']} {coordinates}", details,
        )
    for item in matched:
        library_key = f"{item['instance_name']}::{item['library_name']}"
        location = (
            f"{library_key} S{item['season_index']:02d}"
            f"E{item['episode_index']:02d} (show ratingKey {item['show_rating_key']})"
        )
        decision = rules.rule(
            item["instance_name"], item["library_name"],
            item["show_rating_key"], item["season_index"],
        )
        enabled = decision["enabled"]
        reason = (
            f"season override {decision['season_override']}"
            if decision["source"] == "season"
            else f"show default {decision['show_enabled']}"
        )
        if not enabled:
            details.append(f"{location}: no watch rule enabled ({reason})")
            continue
        item["plex"].mark_watched(item["rating_key"])
        marked.append(item)
        details.append(f"{location}: marked watched ({reason})")
    if not marked:
        return {
            "message": "Plex matched the import; no automatic watch rule was enabled",
            "matched": len(matched), "marked": 0, "details": details,
        }
    return {
        "message": f"Marked {len(marked)} matched Plex episode(s) watched",
        "matched": len(matched), "marked": len(marked), "details": details,
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
    if not app_config.mark_watched.shows_library(instance_name, library_name):
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
