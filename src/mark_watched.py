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
LOG_TRAIL_LIMIT = 12


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
        try:
            # Anime libraries are routinely scanned by this instead.
            absolute = int(episode["absoluteEpisodeNumber"])
        except (KeyError, TypeError, ValueError):
            absolute = None
        normalized_episodes.append({
            "id": episode.get("id"),
            "season": season,
            "episode": number,
            "absolute": absolute,
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
        # The log keeps only the newest `limit` entries, so len() stops growing
        # once it is full and cannot be used to notice an arrival. Count every
        # receipt separately.
        self._received = len(self._attempts)

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
            self._received += 1
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

    def received(self) -> int:
        """Requests seen since this process started, never truncated."""
        with self._lock:
            return self._received

    def summary(self) -> dict:
        with self._lock:
            attempts = list(self._attempts)
        counts: dict[str, int] = {}
        for entry in attempts:
            counts[entry.get("outcome", "?")] = counts.get(entry.get("outcome", "?"), 0) + 1
        return {
            "total": len(attempts),
            "received": self._received,
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
            superseded = self._supersede_older(job_id, event)
            self._save()
            self._queue.put(job_id)
            logger.info(
                "Queued Sonarr import %s for %s%s",
                job_id[:12], event["series"]["title"],
                f" (superseding {superseded} earlier job(s))" if superseded else "",
            )
            return dict(record), True

    @staticmethod
    def _coordinates(event: dict) -> tuple:
        """The episodes an import covers, as an identity independent of the file."""
        return (
            str(event.get("series", {}).get("title", "")),
            tuple(sorted(
                (int(item.get("season", -1)), int(item.get("episode", -1)))
                for item in event.get("episodes", []) or []
            )),
        )

    def _supersede_older(self, job_id: str, event: dict) -> int:
        """Retire unfinished jobs covering the episodes this import replaces.

        An upgrade arrives as a new file, and so as a new webhook identity. The
        job waiting on the old file is then still queued to mark the very same
        episode, which shows up as the same import listed twice and has two
        workers chasing one episode.
        """
        target = self._coordinates(event)
        if not target[1]:
            return 0
        retired = 0
        for other_id, record in self._records.items():
            if other_id == job_id or other_id in self._inflight:
                continue
            other = record.get("event") or {}
            if other.get("source") == "manual" or record.get("status") in {
                "succeeded", "failed",
            }:
                continue
            if self._coordinates(other) != target:
                continue
            # Its own status, not "failed": a superseded job is finished, and
            # Run pending jobs now must not drag it back onto the queue.
            record.update({
                "status": "superseded",
                "next_attempt_at": None,
                "message": "Superseded by a newer import of the same episode",
                "updated_at": _utc_now(),
            })
            retired += 1
        return retired

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
        """Record a job's reasoning, in the log file and briefly on the record.

        The detail belongs in the log: which libraries were searched, what each
        holds, which scans were asked for. Keeping all of it on the record put
        forty lines of it on screen per episode, on a page that refreshes every
        few seconds. The record keeps a short tail so a job can still explain
        itself at a glance; the log file keeps everything.
        """
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return
            title = (record.get("event", {}).get("series", {}) or {}).get("title", "?")
            trail = record.setdefault("log", [])
            stamp = self._stamp()
            trail.append({"at": stamp, "message": message})
            for detail in details or []:
                trail.append({"at": stamp, "message": f"  {detail}"})
            if len(trail) > LOG_TRAIL_LIMIT:
                del trail[:-LOG_TRAIL_LIMIT]
            self._save()
        logger.info("[%s] %s: %s", job_id[:12], title, message)
        for detail in details or []:
            logger.debug("[%s] %s:     %s", job_id[:12], title, detail)

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
            self._update(job_id, status="succeeded", result=result,
                         message=message, settled=False)
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
                settled = bool(self._records[job_id].get("settled"))
            if settled:
                # This job had already finished; it was only re-checked in case
                # a rule had changed. Plex no longer having the episode is not
                # news, and is certainly not a reason to start waiting on an
                # import that completed long ago.
                self._update(
                    job_id, status="succeeded", settled=False,
                    next_attempt_at=None,
                    message=(
                        "Re-checked after the earlier result: Plex no longer "
                        "has this episode, so the original outcome stands"
                    ),
                )
                logger.info("Mark-it-Watched job %s re-checked and left as it was",
                            job_id[:12])
                return self.get(job_id)
            self._retry_or_give_up(
                job_id, attempt,
                waiting="Plex has not scanned this episode yet",
                give_up=(
                    f"Plex still had no match after "
                    f"{self.give_up_after_hours:g}h and {attempt} attempts: {exc}"
                ),
            )
        except Exception as exc:
            logging.getLogger("mediamender").exception("Mark-it-Watched job failed")
            self._log(job_id, f"Attempt {attempt} failed: {type(exc).__name__}: {exc}")
            logger.error("Mark-it-Watched job %s failed: %s: %s",
                         job_id[:12], type(exc).__name__, exc)
            self._retry_or_give_up(
                job_id, attempt,
                waiting=f"Plex processing failed ({type(exc).__name__}: {exc})",
                give_up=(
                    f"Plex processing kept failing for "
                    f"{self.give_up_after_hours:g}h: {type(exc).__name__}: {exc}"
                ),
            )
        return self.get(job_id)

    def _retry_or_give_up(self, job_id: str, attempt: int, *,
                          waiting: str, give_up: str) -> None:
        """End a job for one reason only: the give-up window ran out.

        An unexpected error used to end it on its first attempt instead, so a
        Plex restart, a dropped connection, or a single 500 abandoned an import
        that would have succeeded minutes later - the opposite of waiting for a
        debrid library to appear.
        """
        with self._lock:
            expired = self._expired(self._records[job_id])
        if expired:
            self._update(job_id, status="failed", message=give_up)
            logger.error("Mark-it-Watched job %s gave up: %s", job_id[:12], give_up)
            return
        delay = self._backoff(attempt)
        due = self._now() + timedelta(seconds=delay)
        self._update(
            job_id, status="waiting", next_attempt_at=due.isoformat(),
            message=f"{waiting}; checking again in {_humanize(delay)}",
        )
        logger.info("Mark-it-Watched job %s waiting %s: %s",
                    job_id[:12], _humanize(delay), waiting)

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

    @staticmethod
    def _matched_but_marked_nothing(record: dict) -> bool:
        """A finished import that found the episode and marked none of it.

        The job did everything asked of it, so it is recorded as succeeded -
        but the reason it marked nothing was the rule as it stood at the time,
        and that is precisely what changes when someone switches a show on. A
        manual catch-up marking nothing means the opposite, that there was
        nothing left to do, so those are left alone.
        """
        if record.get("status") != "succeeded":
            return False
        if (record.get("event") or {}).get("source") == "manual":
            return False
        result = record.get("result") or {}
        return bool(result.get("matched")) and not result.get("marked")

    def cancel(self, job_id: str) -> dict | None:
        """Stop waiting on one job, without waiting out the give-up window.

        An episode Plex numbers differently from Sonarr never arrives, so the
        job retries until the window closes - days of a stuck record at the top
        of the list with no way to dismiss it.
        """
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return None
            if record.get("status") in {"succeeded", "superseded", "cancelled"}:
                return dict(record)
            record.update({
                "status": "cancelled",
                "next_attempt_at": None,
                "message": "Stopped by request",
                "updated_at": _utc_now(),
            })
            self._save()
        self._log(job_id, "Stopped by request")
        logger.info("Mark-it-Watched job %s cancelled", job_id[:12])
        return self.get(job_id)

    def retry_unfinished(self) -> dict:
        """Re-queue jobs worth another attempt, and say what was re-queued.

        Sonarr only sends a webhook identity once, and enqueue() is idempotent
        on that identity, so a job that gave up would otherwise stay failed
        forever with no way to fire it again. A job merely waiting on Plex is
        brought forward rather than left until its due time.

        Imports that matched an episode but marked nothing are included, even
        though they succeeded: they are the ones a newly enabled rule changes
        the answer for, and re-running one costs a single Plex lookup.
        """
        requeued: list[str] = []
        reconsidered = 0
        pending = 0
        in_flight = 0
        with self._lock:
            for job_id, record in self._records.items():
                status = record.get("status")
                skipped = self._matched_but_marked_nothing(record)
                # Both are finished states someone or something chose
                # deliberately; this button must not undo that.
                if status in {"superseded", "cancelled"}:
                    continue
                if status == "succeeded" and not skipped:
                    continue
                if job_id in self._inflight:
                    in_flight += 1
                    continue
                if status == "queued":
                    pending += 1
                    continue
                if skipped:
                    reconsidered += 1
                record.update({
                    "status": "queued",
                    "attempts": 0,
                    "next_attempt_at": None,
                    "message": "Re-queued by a manual Mark-it-Watched retry",
                    "updated_at": _utc_now(),
                    # Re-checking a job that had already finished must not be
                    # able to leave it worse off. Without this, an old import
                    # whose episode Plex no longer holds turned into a job
                    # waiting for days and asking for scans the whole time.
                    "settled": skipped,
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
            "reconsidered": reconsidered,
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
    def _show_key(instance: str, library: str, show_rating_key: str,
                  tvdb_id: str = "") -> str:
        """Identify a show by something that outlives its Plex ratingKey.

        Plex issues a new ratingKey whenever an item is removed and re-added,
        which a symlinked debrid library does as a matter of course. A rule
        stored against the old key is then orphaned: the import sees no rule at
        all, while the page that set it still shows the show switched on. The
        TVDB id survives a re-add, and Sonarr names the same one in its
        webhook, so both sides agree without having to consult Plex.

        A show Plex cannot identify still falls back to its ratingKey, which is
        the best available and no worse than before.
        """
        identity = f"tvdb-{tvdb_id}" if tvdb_id else str(show_rating_key)
        return f"{instance}::{library}::{identity}"

    def _resolve(self, instance: str, library: str, show_rating_key: str,
                 tvdb_id: str) -> str:
        """The key this show's rule lives under.

        Prefers the durable one, but honours a ratingKey rule written before
        the install knew any better, so nothing has to be re-entered.
        """
        preferred = self._show_key(instance, library, show_rating_key, tvdb_id)
        if not tvdb_id or preferred in self._data["shows"]:
            return preferred
        legacy = self._show_key(instance, library, show_rating_key)
        return legacy if legacy in self._data["shows"] else preferred

    def _migrate_legacy(self, instance: str, library: str,
                        show_rating_key: str, key: str) -> None:
        """Retire a ratingKey entry now that a durable one has replaced it."""
        legacy = self._show_key(instance, library, show_rating_key)
        if legacy == key:
            return
        self._data["shows"].pop(legacy, None)
        prefix = f"{legacy}::"
        for old in [k for k in self._data["seasons"] if k.startswith(prefix)]:
            self._data["seasons"][f"{key}::{old[len(prefix):]}"] = \
                self._data["seasons"].pop(old)

    def set_show(self, instance: str, library: str, show_rating_key: str,
                 enabled: bool, tvdb_id: str = "") -> None:
        with self._lock:
            key = self._show_key(instance, library, show_rating_key, tvdb_id)
            self._data["shows"][key] = bool(enabled)
            if tvdb_id:
                self._migrate_legacy(instance, library, show_rating_key, key)
            self._save()

    def set_season(self, instance: str, library: str, show_rating_key: str,
                   season_index: int, enabled: bool | None,
                   tvdb_id: str = "") -> None:
        with self._lock:
            show_key = self._resolve(instance, library, show_rating_key, tvdb_id)
            key = f"{show_key}::{int(season_index)}"
            if enabled is None:
                self._data["seasons"].pop(key, None)
            else:
                self._data["seasons"][key] = bool(enabled)
            self._save()

    def rule(self, instance: str, library: str, show_rating_key: str,
             season_index: int, tvdb_id: str = "") -> dict:
        with self._lock:
            show_key = self._resolve(instance, library, show_rating_key, tvdb_id)
            season_key = f"{show_key}::{int(season_index)}"
            shows = self._data["shows"]
            seasons = self._data["seasons"]
            show_enabled = bool(shows.get(show_key, False))
            explicit = season_key in seasons
            return {
                "enabled": bool(seasons[season_key]) if explicit else show_enabled,
                "source": "season" if explicit else "show",
                "show_enabled": show_enabled,
                # "switched off" and "never stored" are different problems, and
                # only one of them has a fix the operator can act on.
                "show_known": show_key in shows,
                "season_override": seasons.get(season_key) if explicit else None,
            }

    def migrate_identities(self, instance: str, library: str,
                           tvdb_by_rating_key: dict) -> int:
        """Re-key this library's ratingKey rules onto their TVDB ids.

        Switching every show off and on by hand is not a migration, and the
        bulk buttons cannot stand in for one: they set a rule for every show in
        the library, not only the ones that already had one. Plex can supply
        the mapping in a single listing, so do it without the operator having
        to touch anything.

        A rule whose ratingKey Plex no longer knows is left exactly as it is.
        Nothing can identify it any more, and discarding it would silently
        change what the install does.
        """
        moved = 0
        with self._lock:
            for rating_key, tvdb_id in tvdb_by_rating_key.items():
                if not tvdb_id:
                    continue
                legacy = self._show_key(instance, library, str(rating_key))
                if legacy not in self._data["shows"]:
                    continue
                key = self._show_key(instance, library, str(rating_key), tvdb_id)
                if key == legacy or key in self._data["shows"]:
                    continue
                self._data["shows"][key] = self._data["shows"][legacy]
                self._migrate_legacy(instance, library, str(rating_key), key)
                moved += 1
            if moved:
                self._save()
        return moved

    def has_season_override(self, instance: str, library: str,
                            show_rating_key: str, tvdb_id: str = "") -> bool:
        """Whether any season of this show departs from the show's own rule."""
        with self._lock:
            prefix = self._resolve(
                instance, library, show_rating_key, tvdb_id,
            ) + "::"
            return any(key.startswith(prefix) for key in self._data["seasons"])

    def legacy_rating_keys(self, instance: str, library: str) -> set:
        """ratingKeys this library still has rules against."""
        prefix = f"{instance}::{library}::"
        with self._lock:
            return {
                key[len(prefix):] for key in self._data["shows"]
                if key.startswith(prefix) and not key[len(prefix):].startswith("tvdb-")
            }

    def all_rules(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def set_all(self, show_keys: list[tuple[str, str, str]],
                enabled: bool) -> None:
        """Set a rule for each named show, and clear only their overrides.

        Every season override used to be discarded, including those belonging
        to libraries this call never touched. An entry may carry a TVDB id as a
        fourth element; one without is keyed by ratingKey as before.
        """
        with self._lock:
            affected = set()
            for entry in show_keys:
                instance, library, rating_key = entry[0], entry[1], entry[2]
                tvdb = entry[3] if len(entry) > 3 else ""
                key = self._show_key(instance, library, rating_key, tvdb)
                self._data["shows"][key] = bool(enabled)
                if tvdb:
                    self._migrate_legacy(instance, library, rating_key, key)
                affected.add(key + "::")
            self._data["seasons"] = {
                key: value for key, value in self._data["seasons"].items()
                if not any(key.startswith(prefix) for prefix in affected)
            }
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
# A path scan asks Plex to look at one folder. A section refresh asks it to walk
# the entire library, which on a symlinked debrid library of a thousand shows is
# minutes of work - far too much to repeat on the path-scan cadence, which is
# what kept a server scanning without pause while any job was waiting.
refresh_throttle = ScanThrottle(interval_seconds=6 * 3600)


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
        # Plex would not take the folder, so the only remaining lever is a full
        # library walk. It is expensive enough that it gets its own, much
        # longer, throttle rather than the path-scan cadence.
        if not refresh_throttle.allow(library_key):
            notes.append(
                f"{library_key}: Plex would not scan {folder or 'the imported folder'}, "
                f"and a full library refresh was already requested recently"
            )
            continue
        logger.info(
            "Asking Plex to refresh all of %s; it would not scan %s",
            library_key, folder or "the imported folder",
        )
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
                    absolute=episode.get("absolute"),
                    episode_title=episode.get("title", ""),
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
                # Anime routinely matches on a different number than Sonarr
                # reported, so say which one actually found it.
                how = str(item.get("matched_by", ""))
                via = f", found by {how}" if how and how != "season and episode" else ""
                details.append(
                    f"{library_key}: matched S{episode['season']:02d}"
                    f"E{episode['episode']:02d} (show ratingKey "
                    f"{item['show_rating_key']}, episode {item['rating_key']}"
                    f"{via}{renamed})"
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
        # Waiting only makes sense while the episode might still arrive. Say
        # what each library actually holds, so a season Plex numbers
        # differently from Sonarr is visible rather than waited out.
        holding = []
        for library_key, plex, section_id in scannable:
            coverage = plex.describe_show(section_id, event["series"]["title"])
            if coverage:
                details.append(f"{library_key} {coverage}")
            if coverage and coverage != "does not have this show":
                holding.append((library_key, plex, section_id))
        # Sonarr finishes an import the moment the file lands, which for a
        # symlinked debrid library is long before Plex has scanned it. Waiting
        # passively is why these jobs used to expire unmatched, so ask Plex to
        # look at the imported folder instead.
        #
        # Only the libraries that hold the show: asking all of them meant one
        # waiting job kept every library on the server scanning, including ones
        # that could not possibly gain this episode. A show no library has yet
        # is the one case where there is nothing better to go on.
        if app_config.mark_watched.scan_on_import:
            details.extend(request_plex_scan(holding or scannable, event))
        raise PlexEpisodePending(
            f"{event['series']['title']} {coordinates}", details,
        )
    unmatched_rules = []
    for item in matched:
        library_key = f"{item['instance_name']}::{item['library_name']}"
        location = (
            f"{library_key} S{item['season_index']:02d}"
            f"E{item['episode_index']:02d} (show ratingKey {item['show_rating_key']})"
        )
        # Sonarr names the TVDB id in every webhook, so the rule can be found
        # without asking Plex what its current ratingKey means.
        decision = rules.rule(
            item["instance_name"], item["library_name"],
            item["show_rating_key"], item["season_index"],
            tvdb_id=str(event["series"].get("tvdb_id") or ""),
        )
        enabled = decision["enabled"]
        reason = (
            f"season override {decision['season_override']}"
            if decision["source"] == "season"
            else f"show default {decision['show_enabled']}"
        )
        if not enabled:
            if decision["source"] == "show" and not decision["show_known"]:
                # Only raise the orphan possibility where it is actually
                # possible. Rules have been keyed by TVDB id since 2.12, so a
                # show with no rule has usually never had one - and telling
                # someone their rule was lost, about a show they only just
                # started downloading, is worse than saying nothing.
                orphanable = bool(rules.legacy_rating_keys(
                    item["instance_name"], item["library_name"],
                ))
                reason = (
                    f"no rule is stored for this show"
                    + ("; this library still has rules keyed by ratingKey, and "
                       "Plex issues a new one when an item is re-added, so a "
                       "rule set earlier may have been left behind"
                       if orphanable else "")
                )
            details.append(f"{location}: no watch rule enabled ({reason})")
            unmatched_rules.append((item, decision))
            continue
        item["plex"].mark_watched(item["rating_key"])
        marked.append(item)
        details.append(f"{location}: marked watched ({reason})")
    if not marked:
        # Name the library and key that were checked. "No rule was enabled" on
        # its own sent operators looking at a rule page that showed the show
        # switched on, with nothing to connect the two.
        item, decision = unmatched_rules[0] if unmatched_rules else (None, {})
        where = (
            f" in {item['instance_name']}::{item['library_name']} "
            f"(show ratingKey {item['show_rating_key']})" if item else ""
        )
        missing = (bool(item) and decision.get("source") == "show"
                   and not decision.get("show_known"))
        orphanable = missing and bool(rules.legacy_rating_keys(
            item["instance_name"], item["library_name"],
        ))
        if not missing:
            message = (f"Plex matched the import{where}; "
                       f"the automatic watch rule is switched off")
        elif orphanable:
            message = (
                f"Plex matched the import{where}, but no rule is stored for "
                f"that show. This library still has rules keyed by ratingKey, "
                f"and Plex issues a new one when an item is re-added, so a "
                f"rule set earlier may have been left behind."
            )
        else:
            message = (f"Plex matched the import{where}, but auto-watch has "
                       f"never been switched on for this show")
        return {
            "message": message,
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

    if scope == "season":
        # Ask Plex for the one season rather than the whole show. A catch-up
        # queues a season-scoped job precisely because the rest of the show has
        # nothing outstanding, so reading it was pure cost.
        season_index = int(manual["season_index"])
        episodes = [
            episode for episode in plex.list_season_episodes(show_key, season_index)
            if episode["season_index"] == season_index
        ]
    else:
        episodes = plex.list_show_episodes(show_key)
    if not episodes:
        raise ValueError("Plex returned no episodes for the selected scope")

    unwatched = [episode for episode in episodes if episode["view_count"] < 1]
    if unwatched:
        plex.mark_watched_many([episode["rating_key"] for episode in unwatched])
    already_watched = len(episodes) - len(unwatched)
    # An episode Plex already counts watched can still hold a resume point,
    # which is enough on its own to keep the show in Continue Watching. Marking
    # it watched again would do nothing; the offset is what has to go.
    stale = [
        episode for episode in episodes
        if episode["view_count"] >= 1 and episode.get("view_offset", 0) > 0
    ]
    for episode in stale:
        plex.clear_progress(episode["rating_key"])
    scope_label = "season" if scope == "season" else "show"
    return {
        "message": (
            f"Manual {scope_label} update marked {len(unwatched)} episode(s) watched; "
            f"{already_watched} were already watched"
            + (f"; cleared {len(stale)} stale resume point(s)" if stale else "")
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
