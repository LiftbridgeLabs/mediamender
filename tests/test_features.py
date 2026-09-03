import unittest
from unittest.mock import Mock, patch

import app
from src.config import AppConfig, FeatureConfig, MarkWatchedConfig
from src.features import FEATURES, FEATURES_BY_KEY, feature_label, permission_prefixes


def download_payload():
    return {
        "eventType": "Download",
        "series": {"id": 1, "title": "Example Show"},
        "episodes": [{"seasonNumber": 1, "episodeNumber": 1}],
        "episodeFile": {"id": 1, "path": "/tv/S01E01.mkv"},
    }


class FeatureRegistryTests(unittest.TestCase):
    def test_registry_matches_the_config_dataclass(self):
        self.assertEqual(
            sorted(FEATURES_BY_KEY),
            sorted(vars(FeatureConfig())),
        )

    def test_every_feature_route_prefix_is_unique(self):
        prefixes = [p for f in FEATURES for p in f.route_prefixes]
        self.assertEqual(len(prefixes), len(set(prefixes)))

    def test_permission_prefixes_are_ordered_longest_first(self):
        lengths = [len(prefix) for prefix, _ in permission_prefixes()]
        self.assertEqual(lengths, sorted(lengths, reverse=True))

    def test_longest_prefix_wins_over_a_shorter_shadowing_one(self):
        pairs = permission_prefixes()
        resolved = next(
            permission for prefix, permission in pairs
            if "/api/mark-watched/rules".startswith(prefix)
        )
        self.assertEqual(resolved, "mark_watched")

    def test_a_config_predating_a_feature_defaults_it_on(self):
        import app as _app
        parsed = _app._validate_raw_config({
            "features": {"mark_watched": False},
            "plex_instances": [],
        }, require_paths=False)
        self.assertFalse(parsed.features.mark_watched)
        for key in FEATURES_BY_KEY:
            if key != "mark_watched":
                self.assertTrue(
                    getattr(parsed.features, key),
                    f"{key} should default on when the config predates it",
                )

    def test_the_ui_renders_a_toggle_for_every_feature(self):
        html = app.app.test_client().get("/").get_data(as_text=True)
        for feature in FEATURES:
            self.assertIn(f"toggleFeatureSetting('{feature.key}')", html)
            self.assertIn(feature.label, html)

    def test_label_falls_back_for_an_unknown_key(self):
        self.assertEqual(feature_label("mark_watched"), "Mark-it-Watched")
        self.assertEqual(feature_label("some_new_thing"), "Some New Thing")


class FeatureGateTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def _config(self, **flags):
        return AppConfig(
            instances=[], features=FeatureConfig(**flags),
            mark_watched=MarkWatchedConfig(webhook_secret="secret"),
        )

    def test_every_feature_route_carries_a_gate(self):
        ungated = []
        for rule in app.app.url_map.iter_rules():
            path = str(rule)
            owner = next((
                feature.key for feature in FEATURES
                for prefix in feature.route_prefixes
                if path.startswith(prefix)
            ), None)
            if owner is None or path == "/api/webhooks/sonarr":
                continue  # the webhook gates inline, after its secret check
            view = app.app.view_functions[rule.endpoint]
            if getattr(view, "_required_features", None) != (owner,):
                ungated.append(path)
        self.assertEqual(ungated, [])

    def test_disabled_feature_refuses_its_routes(self):
        with patch.object(app, "config", self._config(mark_watched=False)), \
             patch.object(app, "has_valid_api_token", return_value=True):
            response = self.client.get("/api/mark-watched/status")
        self.assertEqual(response.status_code, 409)
        self.assertIn("Mark-it-Watched is disabled", response.get_json()["error"])

    def test_enabled_feature_allows_its_routes(self):
        manager = Mock()
        manager.status.return_value = {"jobs": []}
        manager.workers = 4
        manager.live_workers.return_value = 4
        with patch.object(app, "config", self._config(mark_watched=True)), \
             patch.object(app, "mark_watched", manager), \
             patch.object(app, "has_valid_api_token", return_value=True):
            response = self.client.get("/api/mark-watched/status")
        self.assertEqual(response.status_code, 200)

    def test_disabled_feature_stops_the_sonarr_webhook(self):
        manager = Mock()
        with patch.object(app, "config", self._config(mark_watched=False)), \
             patch.object(app, "mark_watched", manager):
            response = self.client.post(
                "/api/webhooks/sonarr", json=download_payload(),
                headers={"X-Sonarr-Webhook-Secret": "secret"},
            )
        self.assertEqual(response.status_code, 409)
        manager.enqueue.assert_not_called()

    def test_a_bad_secret_is_rejected_before_the_feature_is_revealed(self):
        with patch.object(app, "config", self._config(mark_watched=False)):
            response = self.client.post(
                "/api/webhooks/sonarr", json=download_payload(),
                headers={"X-Sonarr-Webhook-Secret": "wrong"},
            )
        self.assertEqual(response.status_code, 401)

    def test_gate_runs_after_authentication(self):
        # An unauthenticated caller must not learn which features are on.
        # require_auth resolves these in src.auth, not in app's namespace.
        with patch.object(app, "config", self._config(mark_watched=False)), \
             patch("src.auth.auth_enabled", return_value=True), \
             patch("src.auth.is_authenticated", return_value=False), \
             patch("src.auth.has_valid_api_token", return_value=False):
            response = self.client.get("/api/mark-watched/status")
        # The exact rejection (redirect, 401 or 403) depends on how far the
        # request gets; what matters is that 409 never reaches a stranger,
        # because that response names which features are switched off.
        self.assertNotEqual(response.status_code, 409)
        self.assertIn(response.status_code, (302, 401, 403))


if __name__ == "__main__":
    unittest.main()
