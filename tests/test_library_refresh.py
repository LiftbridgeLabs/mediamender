import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app
from src import runner
from src.config import AppConfig, FeatureConfig, LibraryConfig, PlexInstanceConfig
from src.library_refresh import LibraryRefreshManager
from src.plex_client import PlexClient


class LibraryRefreshManagerTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Path("tests/.library-refresh-runtime")
        self.runtime.mkdir(exist_ok=True)
        (self.runtime / "library-refresh.json").unlink(missing_ok=True)
        self.manager = LibraryRefreshManager(str(self.runtime))
        self.library = LibraryConfig(
            "Sports", "physical", [], section_id="9",
            refresh_enabled=True, refresh_guard_minutes=15,
        )
        self.instance = PlexInstanceConfig(
            "Plex", "http://plex", "token", [self.library],
        )

    def tearDown(self):
        (self.runtime / "library-refresh.json").unlink(missing_ok=True)

    def test_accepted_refresh_is_persisted_and_holds_only_that_library(self):
        plex = Mock()
        plex.refresh_section.return_value = {"ok": True, "http": 200}

        result = self.manager.run(self.instance, self.library, plex)

        self.assertTrue(result["ok"])
        plex.refresh_section.assert_called_once_with("9")
        self.assertIn("refresh hold", self.manager.trash_hold_reason(
            "Plex", "Sports",
        ))
        self.assertIsNone(self.manager.trash_hold_reason("Plex", "Movies"))
        status = self.manager.status()
        self.assertEqual(status["records"]["Plex::Sports"]["status"], "accepted")
        self.assertFalse(status["running"])

    def test_failed_refresh_does_not_create_trash_hold(self):
        plex = Mock()
        plex.refresh_section.return_value = {"ok": False, "http": 500}

        result = self.manager.run(self.instance, self.library, plex)

        self.assertFalse(result["ok"])
        self.assertIsNone(self.manager.trash_hold_reason("Plex", "Sports"))


class LibraryRefreshApiTests(unittest.TestCase):
    def setUp(self):
        with app._library_refresh_queue_lock:
            app._library_refresh_queue.clear()
            app._library_refresh_queue["running"] = False

    def _client(self):
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "known-token"
        return client

    def test_status_failure_is_returned_as_json(self):
        with patch.object(
            app, "_library_refresh_status_response",
            side_effect=RuntimeError("broken refresh status"),
        ):
            response = self._client().get("/api/library-refresh/status")
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.is_json)
        self.assertIn("broken refresh status", response.get_json()["error"])

    def test_manual_queue_refreshes_selected_libraries_sequentially(self):
        libraries = [
            LibraryConfig("Youtube", "physical", [], section_id="1"),
            LibraryConfig("Sports", "physical", [], section_id="2"),
        ]
        instance = PlexInstanceConfig(
            "Plex", "http://plex", "token", libraries,
        )
        thread = Mock()
        with patch.object(app, "config", AppConfig(instances=[instance])), \
             patch.object(app, "plex_clients", {"Plex": Mock()}), \
             patch.object(app.library_refresh, "run",
                          side_effect=[{"ok": True}, {"ok": True}]) as run, \
             patch("app.threading.Thread", return_value=thread) as factory:
            response = self._client().post(
                "/api/library-refresh/run",
                json={"libraries": [
                    {"instance": "Plex", "library": "Youtube"},
                    {"instance": "Plex", "library": "Sports"},
                ]},
                headers={"X-CSRF-Token": "known-token"},
            )
            factory.call_args.kwargs["target"]()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(app._library_refresh_queue["completed"], 2)
        self.assertEqual(app._library_refresh_queue["state"], "completed")

    def test_disabled_feature_rejects_refresh(self):
        disabled = AppConfig(
            instances=[], features=FeatureConfig(library_refresh=False),
        )
        with patch.object(app, "config", disabled):
            response = self._client().post(
                "/api/library-refresh/run", json={"enabled_only": True},
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 409)

    def test_scheduler_adds_refresh_when_trash_removal_is_disabled(self):
        library = LibraryConfig(
            "Youtube", "physical", [], section_id="1",
            refresh_enabled=True, refresh_cron="*/30 * * * *",
        )
        target = AppConfig(
            instances=[PlexInstanceConfig(
                "Plex", "http://plex", "token", [library],
            )],
            features=FeatureConfig(
                trash_removal=False, library_refresh=True,
            ),
        )
        with patch.object(app.scheduler, "remove_all_jobs"), \
             patch.object(app.scheduler, "add_job") as add_job, \
             patch.object(app, "_update_next"), \
             patch.object(app, "_update_refresh_next"):
            app._setup_scheduler(target)
        self.assertEqual(add_job.call_count, 1)
        self.assertEqual(
            add_job.call_args.kwargs["id"], "refresh::Plex::Youtube",
        )

    def test_page_exposes_manual_and_scheduled_refresh_controls(self):
        html = Path("templates/index.html").read_text(encoding="utf-8")
        self.assertIn('id="page-library-refresh"', html)
        self.assertIn("Refresh all scheduled", html)
        self.assertIn("refresh_guard_minutes", html)
        self.assertIn('id="ss-library-refresh"', html)

    def test_refresh_only_configuration_needs_no_media_mount(self):
        parsed = app._validate_raw_config({
            "features": {
                "trash_removal": False,
                "metadata_health": False,
                "timestamp_repair": False,
                "library_refresh": True,
            },
            "schedule": {"default_cron": "0 * * * *"},
            "plex_instances": [{
                "name": "Plex", "url": "http://plex:32400", "token": "token",
                "libraries": [{
                    "name": "Youtube", "section_id": "7", "paths": [],
                    "refresh_enabled": True,
                    "refresh_cron": "*/30 * * * *",
                }],
            }],
        })
        self.assertTrue(parsed.instances[0].libraries[0].refresh_enabled)
        self.assertEqual(parsed.instances[0].libraries[0].paths, [])


class PlexRefreshClientTests(unittest.TestCase):
    def test_refresh_section_uses_full_section_endpoint_without_path(self):
        client = PlexClient("http://plex", "token")
        response = Mock(status_code=202)
        with patch.object(client.session, "post", return_value=response) as post:
            result = client.refresh_section("7")
        self.assertTrue(result["ok"])
        post.assert_called_once_with(
            "http://plex/library/sections/7/refresh", timeout=30,
        )


class TrashRefreshHoldTests(unittest.TestCase):
    def test_empty_trash_is_skipped_during_library_refresh_hold(self):
        instance = PlexInstanceConfig("Plex", "http://plex", "token", [])
        library = LibraryConfig("Sports", "physical", [], section_id="9")
        old_guard = runner._library_refresh_guard
        runner.set_library_refresh_guard(
            lambda instance_name, library_name: "refresh hold (10m remaining)"
        )
        try:
            with patch.object(runner, "_record") as record:
                runner.run_library(instance, library, AppConfig([instance]), Mock())
            self.assertIn("refresh in progress", record.call_args.args[4])
        finally:
            runner.set_library_refresh_guard(old_guard)


if __name__ == "__main__":
    unittest.main()
