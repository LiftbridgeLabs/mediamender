import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app
from src.auth import hash_password
from src.config import AppConfig, AppUser, LibraryConfig, MarkWatchedConfig, PlexInstanceConfig
from src.mark_watched import (
    MarkWatchedManager, MarkWatchedRuleStore,
    PlexEpisodePending,
    normalize_sonarr_download,
    process_plex_event,
)
from src.plex_client import PlexClient


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
        save.assert_called_once_with("default", "Plex", "TV", "10", 2, False)

    def test_page_has_navigation_poster_and_inheritance_controls(self):
        html = Path("templates/index.html").read_text(encoding="utf-8")
        self.assertIn('id="nav-mark-watched"', html)
        self.assertIn('id="page-mark-watched"', html)
        self.assertIn("Explicit season override", html)
        self.assertIn("Inherited from show", html)

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

    def test_normalizer_accepts_sonarr_test_without_queueing(self):
        self.assertIsNone(normalize_sonarr_download({"eventType": "Test"}))


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
        self.rules.set_show("alice", "Plex", "TV", "10", True)
        inherited = self.rules.rule("alice", "Plex", "TV", "10", 2)
        self.assertEqual(inherited, {
            "enabled": True, "source": "show", "show_enabled": True,
            "season_override": None,
        })
        self.rules.set_season("alice", "Plex", "TV", "10", 2, False)
        explicit = self.rules.rule("alice", "Plex", "TV", "10", 2)
        self.assertFalse(explicit["enabled"])
        self.assertEqual(explicit["source"], "season")

    def test_clearing_season_override_restores_inheritance(self):
        self.rules.set_show("alice", "Plex", "TV", "10", False)
        self.rules.set_season("alice", "Plex", "TV", "10", 1, True)
        self.rules.set_season("alice", "Plex", "TV", "10", 1, None)
        self.assertEqual(
            self.rules.rule("alice", "Plex", "TV", "10", 1)["source"], "show",
        )

    def test_user_rules_are_isolated(self):
        self.rules.set_show("alice", "Plex", "TV", "10", True)
        self.rules.set_show("bob", "Plex", "TV", "10", False)
        self.assertTrue(self.rules.rule("alice", "Plex", "TV", "10", 1)["enabled"])
        self.assertFalse(self.rules.rule("bob", "Plex", "TV", "10", 1)["enabled"])

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
        self.rules.set_show("alice", "Plex", "TV", "10", True)
        result = process_plex_event(sonarr_download() | {
            "series": sonarr_download()["series"],
            "episode_file": {"id": 99, "path": "/tv/file.mkv"},
            "episodes": [{"season": 2, "episode": 3, "title": "Done"}],
        }, config, {"Plex": plex}, self.rules)
        self.assertEqual(result["marked"], 1)
        plex.mark_watched.assert_called_once_with("30")


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

    def test_bulk_all_requires_admin_and_never_calls_plex_mark_watched(self):
        library = LibraryConfig("TV", "physical", [], section_id="7")
        config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
        )])
        plex = Mock()
        plex.get_section_type.return_value = "show"
        plex.list_tv_shows.return_value = [{"rating_key": "10"}, {"rating_key": "11"}]
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session.update({"authenticated": True, "username": "admin",
                                    "role": "admin", "permissions": ["*"],
                                    "_csrf_token": "known-token"})
        with patch.object(app, "config", config), \
             patch.object(app, "plex_clients", {"Plex": plex}), \
             patch.object(app.mark_watched_rules, "usernames", return_value=["admin"]), \
             patch.object(app.mark_watched_rules, "set_all") as set_all:
            response = client.post(
                "/api/mark-watched/all", json={"enabled": False, "confirm": "ALL OFF"},
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(set_all.call_count, 1)
        plex.mark_watched.assert_not_called()


if __name__ == "__main__":
    unittest.main()
