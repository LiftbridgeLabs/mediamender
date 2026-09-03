import json
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

import app
from src.auth import hash_password
from src.config import AppConfig, AppUser, LibraryConfig, MarkWatchedConfig, PlexInstanceConfig
from src.mark_watched import (
    LOG_TRAIL_LIMIT,
    MarkWatchedManager, MarkWatchedRuleStore,
    PlexEpisodePending,
    normalize_sonarr_download,
    process_manual_event, process_plex_event,
)
from src.plex_client import PlexClient, normalize_show_title


def sonarr_download():
    return {
        "eventType": "Download",
        "series": {"id": 12, "title": "Example Show", "tvdbId": 1234, "year": 2024},
        "episodes": [{"id": 45, "seasonNumber": 2, "episodeNumber": 3, "title": "Done"}],
        "episodeFile": {"id": 99, "path": "/tv/Example Show/S02E03.mkv"},
        "isUpgrade": False,
    }


class SonarrWebhookApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()
        self.config = AppConfig(
            instances=[], mark_watched=MarkWatchedConfig(webhook_secret="sonarr-secret"),
        )

    def test_webhook_requires_valid_secret(self):
        with patch.object(app, "config", self.config):
            response = self.client.post("/api/webhooks/sonarr", json=sonarr_download())
        self.assertEqual(response.status_code, 401)

    def test_finalized_download_is_queued_outside_request(self):
        manager = Mock()
        manager.enqueue.return_value = ({"id": "job-1", "status": "queued"}, True)
        with patch.object(app, "config", self.config), patch.object(app, "mark_watched", manager):
            response = self.client.post(
                "/api/webhooks/sonarr", json=sonarr_download(),
                headers={"X-Sonarr-Webhook-Secret": "sonarr-secret"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["queued"])
        manager.enqueue.assert_called_once()
        manager.process.assert_not_called()

    def test_retry_endpoint_reports_what_it_requeued(self):
        manager = Mock()
        manager.retry_unfinished.return_value = {
            "requeued": 2, "already_queued": 1, "in_flight": 0, "job_ids": ["a", "b"],
        }
        with patch.object(app, "config", self.config), \
             patch.object(app, "mark_watched", manager), \
             patch.object(app, "has_valid_api_token", return_value=True):
            response = self.client.post("/api/mark-watched/retry")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["requeued"], 2)
        self.assertEqual(payload["message"], "Re-queued 2 job(s); 1 already waiting")
        manager.retry_unfinished.assert_called_once_with()

    def test_retry_endpoint_requires_authentication(self):
        with patch.object(app, "config", self.config):
            response = self.client.post("/api/mark-watched/retry")
        self.assertIn(response.status_code, (302, 401, 403))

    def test_managed_connection_binds_job_to_its_rule_user(self):
        manager = Mock()
        manager.enqueue.return_value = ({"id": "job-1", "status": "queued"}, True)
        connection = Mock()
        connection.owner_for.return_value = "alice"
        with patch.object(app, "config", self.config), \
             patch.object(app, "mark_watched", manager), \
             patch.object(app, "sonarr_connection", connection):
            response = self.client.post(
                "/api/webhooks/sonarr", json=sonarr_download(),
                headers={"X-Sonarr-Webhook-Secret": "sonarr-secret",
                         "X-MediaMender-Connection-ID": "connection-1"},
            )
        self.assertEqual(response.status_code, 202)
        queued = manager.enqueue.call_args.args[0]
        self.assertEqual(queued["_mediamender_user"], "alice")
        self.assertEqual(queued["_mediamender_connection"], "connection-1")

    def test_non_final_event_is_rejected(self):
        payload = sonarr_download()
        payload["eventType"] = "Grab"
        with patch.object(app, "config", self.config):
            response = self.client.post(
                "/api/webhooks/sonarr", json=payload,
                headers={"Authorization": "Bearer sonarr-secret"},
            )
        self.assertEqual(response.status_code, 400)


class MarkWatchedUiApiTests(unittest.TestCase):
    def _client(self):
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "known-token"
        return client

    def test_library_api_returns_proxy_posters_without_plex_token(self):
        library = LibraryConfig("TV", "physical", [], section_id="7")
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "top-secret-token", [library],
        )])
        plex = Mock()
        plex.get_section_type.return_value = "show"
        plex.list_tv_shows.return_value = [{
            "rating_key": "10", "title": "Example Show", "year": 2024,
            "thumb": "/library/metadata/10/thumb/1", "leaf_count": 8,
            "viewed_leaf_count": 2,
        }]
        with patch.object(app, "config", config), \
             patch.object(app, "plex_clients", {"Plex": plex}), \
             patch.object(app.mark_watched_rules, "rule", return_value={
                 "show_enabled": True,
             }):
            response = self._client().get("/api/mark-watched/libraries")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("/api/mark-watched/poster", body)
        self.assertNotIn("top-secret-token", body)

    def test_options_loads_only_selected_server_and_excludes_movies(self):
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "token", [
                LibraryConfig("TV", "physical", [], section_id="7"),
                LibraryConfig("Movies", "physical", [], section_id="8"),
            ],
        )])
        plex = Mock()
        plex.get_sections.return_value = [
            {"id": "7", "title": "TV", "type": "show"},
            {"id": "8", "title": "Movies", "type": "movie"},
        ]
        with patch.object(app, "config", config), \
             patch.object(app, "plex_clients", {"Plex": plex}):
            response = self._client().get("/api/mark-watched/options?instance=Plex")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["libraries"], [{
            "name": "TV", "section_id": "7",
        }])
        plex.get_sections.assert_called_once_with()

    def test_show_api_requests_one_bounded_plex_page(self):
        library = LibraryConfig("TV", "physical", [], section_id="7")
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "secret-token", [library],
        )])
        plex = Mock()
        plex.get_section_type.return_value = "show"
        plex.list_tv_shows_page.return_value = {"shows": [{
            "rating_key": "10", "title": "Example", "year": 2024,
            "thumb": "/thumb", "leaf_count": 10, "viewed_leaf_count": 1,
        }], "total": 50}
        with patch.object(app, "config", config), \
             patch.object(app, "plex_clients", {"Plex": plex}), \
             patch.object(app.mark_watched_rules, "rule", return_value={
                 "show_enabled": False,
             }):
            response = self._client().get(
                "/api/mark-watched/shows?instance=Plex&library=TV&page=2&page_size=24"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["pages"], 3)
        plex.list_tv_shows_page.assert_called_once_with("7", 24, 24)
        self.assertNotIn("secret-token", response.get_data(as_text=True))

    def test_show_search_uses_plex_section_search_with_pagination(self):
        library = LibraryConfig("TV", "physical", [], section_id="7")
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
        )])
        plex = Mock()
        plex.get_section_type.return_value = "show"
        plex.list_tv_shows_page.return_value = {"shows": [], "total": 0}
        with patch.object(app, "config", config), \
             patch.object(app, "plex_clients", {"Plex": plex}):
            response = self._client().get(
                "/api/mark-watched/shows?instance=Plex&library=TV"
                "&page=2&page_size=12&q=Star%20Trek"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["search"], "Star Trek")
        plex.list_tv_shows_page.assert_called_once_with(
            "7", 12, 12, query="Star Trek",
        )

    def test_show_search_rejects_unbounded_query(self):
        response = self._client().get(
            "/api/mark-watched/shows?instance=Plex&library=TV&q=" + ("x" * 101),
        )
        self.assertEqual(response.status_code, 400)

    def test_season_rule_api_persists_explicit_override(self):
        library = LibraryConfig("TV", "physical", [], section_id="7")
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
        )])
        with patch.object(app, "config", config), \
             patch.object(app, "plex_clients", {"Plex": Mock()}), \
             patch.object(app.mark_watched_rules, "set_season") as save:
            response = self._client().post(
                "/api/mark-watched/rules",
                json={"scope": "season", "enabled": False, "season_index": 2,
                      "instance": "Plex", "library": "TV", "show_rating_key": "10"},
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 200)
        save.assert_called_once_with("Plex", "TV", "10", 2, False)

    def test_manual_apply_requires_destructive_confirmation(self):
        response = self._client().post(
            "/api/mark-watched/apply",
            json={"scope": "show", "instance": "Plex", "library": "TV",
                  "show_rating_key": "10"},
            headers={"X-CSRF-Token": "known-token"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("MARK WATCHED NOW", response.get_json()["error"])

    def test_manual_season_apply_is_queued_outside_request(self):
        library = LibraryConfig("TV", "physical", [], section_id="7")
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
        )])
        manager = Mock()
        manager.enqueue_manual.return_value = {"id": "job-1", "status": "queued",
                                               "message": "queued"}
        plex = Mock()
        with patch.object(app, "config", config), \
             patch.object(app, "plex_clients", {"Plex": plex}), \
             patch.object(app, "mark_watched", manager):
            response = self._client().post(
                "/api/mark-watched/apply",
                json={"scope": "season", "confirm": "MARK WATCHED NOW",
                      "instance": "Plex", "library": "TV",
                      "show_rating_key": "10", "show_title": "Example Show",
                      "season_index": 2},
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 202)
        event = manager.enqueue_manual.call_args.args[0]
        self.assertEqual(event["manual"]["season_index"], 2)
        self.assertEqual(event["series"]["title"], "Example Show")
        plex.mark_watched.assert_not_called()

    def test_page_has_navigation_poster_and_inheritance_controls(self):
        html = Path("templates/index.html").read_text(encoding="utf-8")
        self.assertIn('id="nav-mark-watched"', html)
        self.assertIn('id="page-mark-watched"', html)
        self.assertIn("Explicit season override", html)
        self.assertIn("Inherited from show", html)
        self.assertNotIn("Application Users", html)
        self.assertIn("Plex: ${h(_markWatchedData.instance)}", html)
        self.assertIn("removeSonarrConnection", html)
        self.assertIn('id="mark-watched-search"', html)
        self.assertIn('id="mark-watched-pagination-top"', html)
        self.assertIn('id="mark-watched-pagination-bottom"', html)
        self.assertIn("Mark show watched now", html)
        self.assertIn("Mark season watched now", html)
        self.assertIn("Mark-it-Watched activity", html)

    def test_download_without_episode_file_is_rejected(self):
        payload = sonarr_download()
        payload.pop("episodeFile")
        webhook_config = AppConfig(
            instances=[], mark_watched=MarkWatchedConfig(webhook_secret="sonarr-secret"),
        )
        with patch.object(app, "config", webhook_config):
            response = self._client().post(
                "/api/webhooks/sonarr", json=payload,
                headers={"X-Sonarr-Webhook-Secret": "sonarr-secret"},
            )
        self.assertEqual(response.status_code, 400)


class MarkWatchedQueueTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Path("tests/.mark-watched-runtime")
        self.runtime.mkdir(exist_ok=True)
        (self.runtime / "mark-watched-jobs.json").unlink(missing_ok=True)

    def tearDown(self):
        (self.runtime / "mark-watched-jobs.json").unlink(missing_ok=True)
        (self.runtime / "mark-watched-rules.json").unlink(missing_ok=True)
        self.runtime.rmdir()

    def test_duplicate_webhooks_create_one_job(self):
        manager = MarkWatchedManager(str(self.runtime), autostart=False)
        first, created = manager.enqueue(sonarr_download())
        duplicate, created_again = manager.enqueue(sonarr_download())
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(len(manager.status()["jobs"]), 1)

    def test_pending_match_retries_with_bounded_backoff(self):
        attempts = []

        def process(_event):
            attempts.append(True)
            if len(attempts) < 3:
                raise PlexEpisodePending("not scanned")
            return {"message": "Episode marked watched"}

        sleeps = []
        manager = MarkWatchedManager(
            str(self.runtime), processor=process, retry_delays=(1, 2, 3),
            autostart=False, sleep=sleeps.append,
        )
        record, _ = manager.enqueue(sonarr_download())
        result = manager.process(record["id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(sleeps, [1, 2])

    def test_failed_jobs_are_requeued_on_demand(self):
        def process(_event):
            raise PlexEpisodePending("not scanned")

        manager = MarkWatchedManager(
            str(self.runtime), processor=process, retry_delays=(),
            autostart=False, sleep=lambda _delay: None,
        )
        record, _ = manager.enqueue(sonarr_download())
        # Mimic the running worker taking the job off the queue.
        manager._queue.get_nowait()
        self.assertEqual(manager.process(record["id"])["status"], "failed")

        summary = manager.retry_unfinished()
        self.assertEqual(summary["requeued"], 1)
        self.assertEqual(summary["job_ids"], [record["id"]])
        requeued = manager.get(record["id"])
        self.assertEqual(requeued["status"], "queued")
        self.assertEqual(requeued["attempts"], 0)
        self.assertEqual(manager._queue.get_nowait(), record["id"])

    def test_retry_leaves_succeeded_and_waiting_jobs_alone(self):
        manager = MarkWatchedManager(
            str(self.runtime), processor=lambda _event: {"message": "done"},
            autostart=False, sleep=lambda _delay: None,
        )
        done, _ = manager.enqueue(sonarr_download())
        manager._queue.get_nowait()
        manager.process(done["id"])
        waiting = manager.enqueue_manual({
            "series": {"title": "Example Show"}, "manual": {"scope": "show"},
        })

        summary = manager.retry_unfinished()
        self.assertEqual(summary["requeued"], 0)
        self.assertEqual(summary["already_queued"], 1)
        self.assertEqual(manager.get(done["id"])["status"], "succeeded")
        self.assertEqual(manager.get(waiting["id"])["status"], "queued")

    def test_retry_skips_a_job_the_worker_is_still_running(self):
        summaries = []

        def process(_event):
            summaries.append(manager.retry_unfinished())
            return {"message": "done"}

        manager = MarkWatchedManager(
            str(self.runtime), processor=process,
            autostart=False, sleep=lambda _delay: None,
        )
        record, _ = manager.enqueue(sonarr_download())
        manager._queue.get_nowait()
        manager.process(record["id"])
        self.assertEqual(summaries[0]["in_flight"], 1)
        self.assertEqual(summaries[0]["requeued"], 0)

    def test_worker_survives_a_job_that_disappears(self):
        manager = MarkWatchedManager(str(self.runtime), autostart=False, workers=1)
        manager._queue.put("missing-job")
        with self.assertLogs("mediamender.mark_watched", level="ERROR"):
            manager.start()
            manager._queue.join()
        self.assertEqual(manager.live_workers(), 1)
        self.assertEqual(manager.status()["jobs"], [])

    def test_pool_runs_jobs_concurrently(self):
        import threading

        started = threading.Barrier(3, timeout=5)

        def process(_event):
            started.wait()
            return {"message": "done"}

        manager = MarkWatchedManager(
            str(self.runtime), processor=process, autostart=False, workers=3,
        )
        for season in (1, 2):
            payload = sonarr_download()
            payload["episodes"] = [{"seasonNumber": season, "episodeNumber": 1}]
            payload["episodeFile"] = {"id": season, "path": f"/tv/S{season}.mkv"}
            manager.enqueue(payload)
        manager.start()
        # A single worker would deadlock here; three let both jobs meet.
        started.wait(timeout=5)
        manager._queue.join()
        self.assertTrue(all(
            job["status"] == "succeeded" for job in manager.status()["jobs"]
        ))

    def test_a_slow_job_does_not_block_the_next_one(self):
        import threading

        release = threading.Event()
        finished = []

        def process(event):
            if event["episodes"][0]["season"] == 1:
                release.wait(timeout=5)
            finished.append(event["episodes"][0]["season"])
            return {"message": "done"}

        manager = MarkWatchedManager(
            str(self.runtime), processor=process, autostart=False, workers=2,
        )
        slow = sonarr_download()
        slow["episodes"] = [{"seasonNumber": 1, "episodeNumber": 1}]
        fast = sonarr_download()
        fast["episodes"] = [{"seasonNumber": 2, "episodeNumber": 1}]
        fast["episodeFile"] = {"id": 2, "path": "/tv/S02.mkv"}
        manager.enqueue(slow)
        manager.enqueue(fast)
        manager.start()
        for _ in range(500):
            if finished:
                break
            time.sleep(0.01)
        # The second job completed while the first was still blocked.
        self.assertEqual(finished, [2])
        release.set()
        manager._queue.join()

    def test_job_records_a_readable_log_trail(self):
        attempts = []

        def process(_event):
            attempts.append(True)
            if len(attempts) < 2:
                raise PlexEpisodePending("Example Show S02E03", ["Plex::TV: no match"])
            return {"message": "Marked 1", "details": ["Plex::TV: marked watched"]}

        manager = MarkWatchedManager(
            str(self.runtime), processor=process, retry_delays=(5,),
            autostart=False, sleep=lambda _delay: None,
        )
        record, _ = manager.enqueue(sonarr_download())
        manager._queue.get_nowait()
        manager.process(record["id"])
        trail = [entry["message"] for entry in manager.get(record["id"])["log"]]
        self.assertIn("Attempt 1: Plex has not matched Example Show S02E03", trail)
        self.assertIn("  Plex::TV: no match", trail)
        self.assertIn("Waiting 5s for Plex to scan the import", trail)
        self.assertIn("  Plex::TV: marked watched", trail)

    def test_log_trail_is_capped(self):
        manager = MarkWatchedManager(str(self.runtime), autostart=False)
        record, _ = manager.enqueue(sonarr_download())
        for index in range(LOG_TRAIL_LIMIT + 20):
            manager._log(record["id"], f"line {index}")
        trail = manager.get(record["id"])["log"]
        self.assertEqual(len(trail), LOG_TRAIL_LIMIT)
        self.assertEqual(trail[-1]["message"], f"line {LOG_TRAIL_LIMIT + 19}")

    def test_normalizer_accepts_sonarr_test_without_queueing(self):
        self.assertIsNone(normalize_sonarr_download({"eventType": "Test"}))

    def test_manual_action_uses_the_durable_background_queue(self):
        manager = MarkWatchedManager(str(self.runtime), autostart=False)
        record = manager.enqueue_manual({
            "series": {"title": "Example Show"},
            "manual": {"scope": "show"},
        })
        self.assertEqual(record["status"], "queued")
        self.assertEqual(record["event"]["source"], "manual")
        self.assertEqual(manager.status()["jobs"][0]["id"], record["id"])


class MarkWatchedRuleTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Path("tests/.mark-watched-rule-runtime")
        self.runtime.mkdir(exist_ok=True)
        (self.runtime / "mark-watched-rules.json").unlink(missing_ok=True)
        self.rules = MarkWatchedRuleStore(str(self.runtime))

    def tearDown(self):
        (self.runtime / "mark-watched-rules.json").unlink(missing_ok=True)
        self.runtime.rmdir()

    def test_season_inherits_show_until_explicitly_overridden(self):
        self.rules.set_show("Plex", "TV", "10", True)
        inherited = self.rules.rule("Plex", "TV", "10", 2)
        self.assertEqual(inherited, {
            "enabled": True, "source": "show", "show_enabled": True,
            "season_override": None,
        })
        self.rules.set_season("Plex", "TV", "10", 2, False)
        explicit = self.rules.rule("Plex", "TV", "10", 2)
        self.assertFalse(explicit["enabled"])
        self.assertEqual(explicit["source"], "season")

    def test_clearing_season_override_restores_inheritance(self):
        self.rules.set_show("Plex", "TV", "10", False)
        self.rules.set_season("Plex", "TV", "10", 1, True)
        self.rules.set_season("Plex", "TV", "10", 1, None)
        self.assertEqual(
            self.rules.rule("Plex", "TV", "10", 1)["source"], "show",
        )

    def test_a_rule_file_from_the_per_user_era_is_flattened(self):
        (self.runtime / "mark-watched-rules.json").write_text(json.dumps({"users": {
            "default": {"shows": {"Plex::TV::10": False}, "seasons": {}},
            "jason": {
                "shows": {"Plex::TV::10": True, "Plex::TV::11": False},
                "seasons": {"Plex::TV::10::2": True},
            },
        }}), encoding="utf-8")
        migrated = MarkWatchedRuleStore(str(self.runtime))
        # An On saved under any old account name survives the migration.
        self.assertTrue(migrated.rule("Plex", "TV", "10", 1)["enabled"])
        self.assertTrue(migrated.rule("Plex", "TV", "10", 2)["enabled"])
        self.assertFalse(migrated.rule("Plex", "TV", "11", 1)["enabled"])

    def test_flattened_rules_are_written_back_on_the_next_save(self):
        (self.runtime / "mark-watched-rules.json").write_text(json.dumps({"users": {
            "jason": {"shows": {"Plex::TV::10": True}, "seasons": {}},
        }}), encoding="utf-8")
        migrated = MarkWatchedRuleStore(str(self.runtime))
        migrated.set_show("Plex", "TV", "12", True)
        saved = json.loads(
            (self.runtime / "mark-watched-rules.json").read_text(encoding="utf-8"),
        )
        self.assertNotIn("users", saved)
        self.assertEqual(saved["shows"], {"Plex::TV::10": True, "Plex::TV::12": True})

    def test_processor_marks_only_when_rule_is_enabled(self):
        library = LibraryConfig("TV", "physical", [], section_id="7")
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
        )])
        plex = Mock()
        plex.get_section_type.return_value = "show"
        plex.find_episode.return_value = {
            "rating_key": "30", "show_rating_key": "10",
            "season_rating_key": "20", "season_index": 2,
            "episode_index": 3, "title": "Done",
        }
        self.rules.set_show("Plex", "TV", "10", True)
        result = process_plex_event(sonarr_download() | {
            "series": sonarr_download()["series"],
            "episode_file": {"id": 99, "path": "/tv/file.mkv"},
            "episodes": [{"season": 2, "episode": 3, "title": "Done"}],
        }, config, {"Plex": plex}, self.rules)
        self.assertEqual(result["marked"], 1)
        plex.mark_watched.assert_called_once_with("30")

    def test_webhook_owner_does_not_select_a_rule_set(self):
        library = LibraryConfig("TV", "physical", [], section_id="7")
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
        )])
        plex = Mock()
        plex.get_section_type.return_value = "show"
        plex.find_episode.return_value = {
            "rating_key": "30", "show_rating_key": "10",
            "season_rating_key": "20", "season_index": 2,
            "episode_index": 3, "title": "Done",
        }
        self.rules.set_show("Plex", "TV", "10", True)
        # Whoever connected Sonarr, the install's rule is the one that applies.
        for owner in ("", "alice", "some-other-account"):
            plex.mark_watched.reset_mock()
            result = process_plex_event({
                "series": {"title": "Example Show"},
                "episodes": [{"season": 2, "episode": 3}],
                "rule_user": owner,
            }, config, {"Plex": plex}, self.rules)
            self.assertEqual(result["marked"], 1)
            plex.mark_watched.assert_called_once_with("30")

    def test_skipped_rule_explains_which_show_it_checked(self):
        library = LibraryConfig("TV", "physical", [], section_id="7")
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
        )])
        plex = Mock()
        plex.get_section_type.return_value = "show"
        plex.find_episode.return_value = {
            "rating_key": "30", "show_rating_key": "10",
            "season_rating_key": "20", "season_index": 2,
            "episode_index": 3, "title": "Done",
        }
        self.rules.set_show("Plex", "TV", "10", False)
        result = process_plex_event({
            "series": {"title": "Example Show"},
            "episodes": [{"season": 2, "episode": 3}],
            "rule_user": "alice",
        }, config, {"Plex": plex}, self.rules)
        self.assertEqual(result["marked"], 0)
        details = "\n".join(result["details"])
        self.assertIn("matched S02E03 (show ratingKey 10", details)
        self.assertIn(
            "Plex::TV S02E03 (show ratingKey 10): no watch rule enabled "
            "(show default False)",
            details,
        )

    def test_season_override_off_beats_an_enabled_show_default(self):
        library = LibraryConfig("TV", "physical", [], section_id="7")
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
        )])
        plex = Mock()
        plex.get_section_type.return_value = "show"
        plex.find_episode.return_value = {
            "rating_key": "30", "show_rating_key": "10",
            "season_rating_key": "20", "season_index": 2,
            "episode_index": 3, "title": "Done",
        }
        self.rules.set_show("Plex", "TV", "10", True)
        self.rules.set_season("Plex", "TV", "10", 2, False)
        result = process_plex_event({
            "series": {"title": "Example Show"},
            "episodes": [{"season": 2, "episode": 3}],
        }, config, {"Plex": plex}, self.rules)
        self.assertEqual(result["marked"], 0)
        plex.mark_watched.assert_not_called()
        self.assertIn(
            "no watch rule enabled (season override False)",
            "\n".join(result["details"]),
        )

    def test_pending_match_reports_the_libraries_it_searched(self):
        library = LibraryConfig("TV", "physical", [], section_id="7")
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
        )])
        plex = Mock()
        plex.get_section_type.return_value = "show"
        plex.find_episode.return_value = None
        with self.assertRaises(PlexEpisodePending) as caught:
            process_plex_event({
                "series": {"title": "Example Show"},
                "episodes": [{"season": 2, "episode": 3}],
            }, config, {"Plex": plex}, self.rules)
        self.assertIn(
            "Searched TV libraries: Plex::TV", "\n".join(caught.exception.details),
        )

    def test_hidden_library_is_reported_as_skipped(self):
        library = LibraryConfig("TV", "physical", [], section_id="7")
        config = AppConfig(
            instances=[PlexInstanceConfig(
                "Plex", "http://plex", "token", [library],
            )],
            mark_watched=MarkWatchedConfig(visible_libraries=[]),
        )
        plex = Mock()
        with self.assertRaises(PlexEpisodePending) as caught:
            process_plex_event({
                "series": {"title": "Example Show"},
                "episodes": [{"season": 2, "episode": 3}],
            }, config, {"Plex": plex}, self.rules)
        details = "\n".join(caught.exception.details)
        self.assertIn("Plex::TV: skipped, hidden in Settings", details)
        self.assertIn("Searched TV libraries: none", details)

    def test_multi_episode_import_waits_before_marking_partial_match(self):
        library = LibraryConfig("TV", "physical", [], section_id="7")
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
        )])
        plex = Mock()
        plex.get_section_type.return_value = "show"
        plex.find_episode.side_effect = [{
            "rating_key": "30", "show_rating_key": "10",
            "season_rating_key": "20", "season_index": 2,
            "episode_index": 3, "title": "Part One",
        }, None]
        event = {
            "series": {"title": "Example Show"},
            "episodes": [
                {"season": 2, "episode": 3}, {"season": 2, "episode": 4},
            ],
        }
        with self.assertRaises(PlexEpisodePending):
            process_plex_event(event, config, {"Plex": plex}, self.rules)
        plex.mark_watched.assert_not_called()

    def test_manual_season_marks_only_unwatched_episodes_in_that_season(self):
        library = LibraryConfig("TV", "physical", [], section_id="7")
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
        )])
        plex = Mock()
        plex.get_section_type.return_value = "show"
        plex.list_show_episodes.return_value = [
            {"rating_key": "21", "season_index": 2, "episode_index": 1,
             "view_count": 0},
            {"rating_key": "22", "season_index": 2, "episode_index": 2,
             "view_count": 1},
            {"rating_key": "31", "season_index": 3, "episode_index": 1,
             "view_count": 0},
        ]
        result = process_manual_event({"manual": {
            "scope": "season", "instance": "Plex", "library": "TV",
            "show_rating_key": "10", "season_index": 2,
        }}, config, {"Plex": plex})
        plex.mark_watched_many.assert_called_once_with(["21"])
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["marked"], 1)
        self.assertEqual(result["already_watched"], 1)


class PlexMarkWatchedClientTests(unittest.TestCase):
    def test_find_episode_uses_exact_show_and_coordinates(self):
        client = PlexClient("http://plex", "token")
        response = Mock()
        response.json.return_value = {"MediaContainer": {"Metadata": [{
            "type": "episode", "ratingKey": "30", "grandparentRatingKey": "10",
            "parentRatingKey": "20", "grandparentTitle": "Example Show",
            "parentIndex": 2, "index": 3, "title": "Done",
        }]}}
        with patch.object(client, "_get", return_value=response) as get:
            result = client.find_episode("7", "Example Show", 2, 3)
        self.assertEqual(result["rating_key"], "30")
        self.assertEqual(get.call_args.kwargs["params"]["type"], 4)

    def test_find_episode_falls_back_when_plex_spells_the_title_differently(self):
        client = PlexClient("http://plex", "token")
        empty = Mock()
        empty.json.return_value = {"MediaContainer": {}}
        coordinate = Mock()
        coordinate.json.return_value = {"MediaContainer": {"Metadata": [
            {"type": "episode", "ratingKey": "99", "grandparentRatingKey": "11",
             "grandparentTitle": "Some Other Show", "parentIndex": 3, "index": 18},
            {"type": "episode", "ratingKey": "30", "grandparentRatingKey": "10",
             "parentRatingKey": "20", "grandparentTitle": "Big City Greens (2018)",
             "parentIndex": 3, "index": 18, "title": "The Move"},
        ]}}
        with patch.object(client, "_get", side_effect=[empty, coordinate]) as get:
            result = client.find_episode("7", "Big City Greens", 3, 18)
        self.assertEqual(result["rating_key"], "30")
        self.assertEqual(result["show_title"], "Big City Greens (2018)")
        # The fallback drops the title filter but keeps the coordinate.
        params = get.call_args.kwargs["params"]
        self.assertNotIn("grandparentTitle", params)
        self.assertEqual((params["parentIndex"], params["index"]), (3, 18))

    def test_find_episode_does_not_match_a_different_show(self):
        client = PlexClient("http://plex", "token")
        empty = Mock()
        empty.json.return_value = {"MediaContainer": {}}
        coordinate = Mock()
        coordinate.json.return_value = {"MediaContainer": {"Metadata": [
            {"type": "episode", "ratingKey": "99", "grandparentRatingKey": "11",
             "grandparentTitle": "Some Other Show", "parentIndex": 3, "index": 18},
        ]}}
        with patch.object(client, "_get", side_effect=[empty, coordinate]):
            self.assertIsNone(client.find_episode("7", "Big City Greens", 3, 18))

    def test_show_title_normalization(self):
        self.assertEqual(
            normalize_show_title("Big City Greens (2018)"), "big city greens",
        )
        self.assertEqual(
            normalize_show_title("The Office (US)"), "office",
        )
        self.assertEqual(
            normalize_show_title("Marvel's Agents of S.H.I.E.L.D."),
            "marvel s agents of s h i e l d",
        )
        self.assertNotEqual(
            normalize_show_title("Big City Greens"),
            normalize_show_title("Big City Blues"),
        )

    def test_mark_watched_uses_advertised_scrobble_endpoint(self):
        client = PlexClient("http://plex", "token")
        provider = Mock()
        provider.json.return_value = {"MediaContainer": {"MediaProvider": [{
            "identifier": "com.plexapp.plugins.library",
            "Feature": [{"type": "timeline", "scrobbleKey": "/:/scrobble"}],
        }]}}
        scrobble = Mock()
        with patch.object(client, "_get", side_effect=[provider, scrobble]) as get:
            client.mark_watched("30")
        self.assertEqual(get.call_args.args[0], "/:/scrobble")
        self.assertEqual(get.call_args.kwargs["params"]["key"], "30")

    def test_list_show_episodes_returns_watch_state_for_exact_show(self):
        client = PlexClient("http://plex", "token")
        response = Mock()
        response.json.return_value = {"MediaContainer": {"Metadata": [
            {"type": "episode", "ratingKey": "21", "grandparentRatingKey": "10",
             "grandparentTitle": "Example", "parentIndex": 2, "index": 1,
             "viewCount": 1},
            {"type": "episode", "ratingKey": "99", "grandparentRatingKey": "11",
             "parentIndex": 2, "index": 1},
        ]}}
        with patch.object(client, "_get", return_value=response) as get:
            episodes = client.list_show_episodes("10")
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["view_count"], 1)
        self.assertEqual(get.call_args.args[0], "/library/metadata/10/allLeaves")

    def test_mark_watched_many_discovers_scrobble_endpoint_once(self):
        client = PlexClient("http://plex", "token")
        responses = [Mock(), Mock()]
        with patch.object(client, "_scrobble_endpoint", return_value=(
            "/:/scrobble", "com.plexapp.plugins.library",
        )) as endpoint, patch.object(client, "_get", side_effect=responses) as get:
            client.mark_watched_many(["21", "22"])
        endpoint.assert_called_once_with()
        self.assertEqual(get.call_count, 2)

    def test_list_tv_shows_page_uses_plex_container_pagination(self):
        client = PlexClient("http://plex", "token")
        response = Mock()
        response.json.return_value = {"MediaContainer": {
            "totalSize": 80,
            "Metadata": [{"ratingKey": "10", "title": "Example"}],
        }}
        with patch.object(client, "_get", return_value=response) as get:
            result = client.list_tv_shows_page("7", 24, 24)
        self.assertEqual(result["total"], 80)
        self.assertEqual(len(result["shows"]), 1)
        self.assertEqual(get.call_args.kwargs["params"]["X-Plex-Container-Start"], 24)
        self.assertEqual(get.call_args.kwargs["params"]["X-Plex-Container-Size"], 24)

    def test_list_tv_shows_page_searches_within_selected_section(self):
        client = PlexClient("http://plex", "token")
        response = Mock()
        response.json.return_value = {"MediaContainer": {
            "totalSize": 1,
            "Metadata": [{"ratingKey": "10", "title": "Star Trek"}],
        }}
        with patch.object(client, "_get", return_value=response) as get:
            result = client.list_tv_shows_page("7", 0, 12, query="Star Trek")
        self.assertEqual(result["total"], 1)
        self.assertEqual(get.call_args.args[0], "/library/sections/7/search")
        self.assertEqual(get.call_args.kwargs["params"]["query"], "Star Trek")
        self.assertEqual(get.call_args.kwargs["params"]["type"], 2)


class MarkWatchedPermissionTests(unittest.TestCase):
    def _user_client(self, permissions):
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session.update({
                "authenticated": True, "username": "viewer", "role": "user",
                "permissions": permissions, "_csrf_token": "known-token",
            })
        return client

    def test_server_denies_feature_without_permission(self):
        config = AppConfig(instances=[], users=[AppUser(
            "viewer", hash_password("password123"), "user", ["dashboard"],
        )])
        with patch.object(app, "config", config):
            response = self._user_client(["dashboard"]).get("/api/mark-watched/libraries")
        self.assertEqual(response.status_code, 403)

    def test_server_allows_granted_mark_watched_permission(self):
        config = AppConfig(instances=[], users=[AppUser(
            "viewer", hash_password("password123"), "user", ["mark_watched"],
        )])
        with patch.object(app, "config", config):
            response = self._user_client(["mark_watched"]).get("/api/mark-watched/libraries")
        self.assertEqual(response.status_code, 200)

    def test_bulk_all_updates_only_active_rule_set_and_never_plex_history(self):
        library = LibraryConfig("TV", "physical", [], section_id="7")
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
        )], users=[AppUser(
            "viewer", hash_password("password123"), "user", ["mark_watched"],
        )])
        plex = Mock()
        plex.get_section_type.return_value = "show"
        plex.list_tv_shows.return_value = [{"rating_key": "10"}, {"rating_key": "11"}]
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session.update({"authenticated": True, "username": "viewer",
                                    "role": "user", "permissions": ["mark_watched"],
                                    "_csrf_token": "known-token"})
        with patch.object(app, "config", config), \
             patch.object(app, "plex_clients", {"Plex": plex}), \
             patch.object(app.mark_watched_rules, "set_all") as set_all:
            response = client.post(
                "/api/mark-watched/all", json={"enabled": False, "confirm": "ALL OFF"},
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 200)
        set_all.assert_called_once_with(
            [("Plex", "TV", "10"), ("Plex", "TV", "11")], False,
        )
        plex.mark_watched.assert_not_called()


class MarkWatchedSettingsTests(unittest.TestCase):
    def setUp(self):
        self.path = Path("tests/.mark-watched-settings.yml")
        self.path.unlink(missing_ok=True)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def _client(self):
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "known-token"
        return client

    def test_config_load_redacts_plex_token_and_webhook_secret(self):
        self.path.write_text(yaml.safe_dump({
            "mark_watched": {"webhook_secret": "webhook-sensitive"},
            "plex_instances": [{"name": "Plex", "url": "http://plex",
                                 "token": "plex-sensitive", "libraries": []}],
        }), encoding="utf-8")
        runtime = AppConfig(
            instances=[], mark_watched=MarkWatchedConfig(webhook_secret="webhook-sensitive"),
        )
        with patch.object(app, "CONFIG_PATH", str(self.path)), patch.object(app, "config", runtime):
            response = self._client().get("/api/config/load")
        body = response.get_data(as_text=True)
        self.assertNotIn("plex-sensitive", body)
        self.assertNotIn("webhook-sensitive", body)
        self.assertTrue(response.get_json()["config"]["plex_instances"][0]["token_configured"])

    def test_config_load_recovers_live_plex_instances_when_file_list_is_empty(self):
        self.path.write_text(yaml.safe_dump({"plex_instances": []}), encoding="utf-8")
        runtime = AppConfig(instances=[PlexInstanceConfig(
            "Live Plex", "http://plex", "runtime-sensitive",
            [LibraryConfig("TV", "physical", [], section_id="7")],
        )])
        with patch.object(app, "CONFIG_PATH", str(self.path)), \
             patch.object(app, "config", runtime):
            response = self._client().get("/api/config/load")
        body = response.get_json()
        self.assertTrue(body["recovered_instances"])
        self.assertEqual(body["config"]["plex_instances"][0]["name"], "Live Plex")
        self.assertTrue(body["config"]["plex_instances"][0]["token_configured"])
        self.assertNotIn("runtime-sensitive", response.get_data(as_text=True))

    def test_settings_save_preserves_blank_redacted_secrets_and_visibility(self):
        self.path.write_text(yaml.safe_dump({
            "mark_watched": {"webhook_secret": "keep-webhook"},
            "plex_instances": [{"name": "Plex", "url": "http://plex",
                                 "token": "keep-plex", "libraries": []}],
        }), encoding="utf-8")
        payload = {
            "store_tokens": True, "save_scope": "mark-watched",
            "mark_watched": {"webhook_secret": "", "visible_libraries": []},
            "instances": [{"name": "Plex", "url": "http://plex", "token": "",
                           "libraries": []}],
        }
        with patch.object(app, "CONFIG_PATH", str(self.path)), \
             patch.object(app, "_save_and_apply") as save:
            response = self._client().post(
                "/api/wizard/save", json=payload,
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 200)
        saved = save.call_args.args[0]
        self.assertEqual(saved["mark_watched"]["webhook_secret"], "keep-webhook")
        self.assertEqual(saved["mark_watched"]["visible_libraries"], [])
        self.assertEqual(saved["plex_instances"][0]["token"], "keep-plex")

    def test_mark_watched_save_cannot_erase_instances_after_ui_load_failure(self):
        # The section save owns only the mark_watched block, so an instance
        # list the browser failed to render is simply not its to write.
        self.path.write_text(yaml.safe_dump({
            "plex_instances": [{
                "name": "Live Plex", "url": "http://plex", "token": "stored-token",
                "libraries": [{"name": "TV", "type": "physical", "paths": []}],
            }],
            "mark_watched": {"webhook_secret": "keep-me"},
        }), encoding="utf-8")
        runtime = AppConfig(instances=[PlexInstanceConfig(
            "Live Plex", "http://plex", "runtime-token",
            [LibraryConfig("TV", "physical", [], section_id="7")],
        )])
        with patch.object(app, "CONFIG_PATH", str(self.path)), \
             patch.object(app, "config", runtime), \
             patch.object(app, "_save_and_apply") as save:
            response = self._client().patch(
                "/api/settings/mark-watched",
                json={"mark_watched": {"visible_libraries": []}, "plex_instances": []},
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 200)
        saved = save.call_args.args[0]
        self.assertEqual(len(saved["plex_instances"]), 1)
        self.assertEqual(saved["plex_instances"][0]["name"], "Live Plex")
        self.assertEqual(saved["plex_instances"][0]["token"], "stored-token")
        # The blank secret field must not wipe the stored secret.
        self.assertEqual(saved["mark_watched"]["webhook_secret"], "keep-me")
        self.assertEqual(saved["mark_watched"]["visible_libraries"], [])

    def test_settings_navigation_does_not_call_plex(self):
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "token", [LibraryConfig("TV", "physical", [])],
        )])
        plex = Mock()
        with patch.object(app, "config", config), patch.object(app, "plex_clients", {"Plex": plex}):
            response = self._client().get("/")
        self.assertEqual(response.status_code, 200)
        plex.assert_not_called()
        plex._get.assert_not_called()

    def test_settings_ui_keeps_active_section_and_uses_non_overlapping_refresh_grid(self):
        html = Path("templates/index.html").read_text(encoding="utf-8")
        self.assertIn("document.querySelector('.settings-section.active')", html)
        self.assertIn('class="library-refresh-control-grid"', html)
        self.assertIn('id="tab-mark-watched-configure"', html)
        self.assertNotIn('id="ss-mark-watched"', html)
        self.assertIn('id="mark-watched-sonarr-connect"', html)
        self.assertIn(
            "async function connectSonarr(configuredUrl = '', actionButton = null)",
            html,
        )
        self.assertIn("Install webhook", html)
        self.assertIn("Repair / test", html)
        self.assertIn("readJsonResponse", html)
        self.assertIn('id="mark-watched-instance"', html)
        self.assertIn('id="mark-watched-library"', html)
        self.assertIn("loadMarkWatchedPage", html)
        self.assertIn('<div class="rollup-title">Empty Trash</div>', html)


if __name__ == "__main__":
    unittest.main()
