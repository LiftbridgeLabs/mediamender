"""The shape of the API and the config accessors that guard it."""

import unittest
from unittest.mock import patch

import app
from src.config import AppConfig, MarkWatchedConfig
from ui_source import script_text


class VisibilityAccessorTests(unittest.TestCase):
    """visible_libraries is a tri-state; ask it rather than compare to None."""

    def test_unset_shows_every_library(self):
        settings = MarkWatchedConfig()
        self.assertIsNone(settings.visible_libraries)
        self.assertTrue(settings.shows_library("Any", "Library"))

    def test_empty_list_hides_every_library(self):
        settings = MarkWatchedConfig(visible_libraries=[])
        self.assertFalse(settings.shows_library("Any", "Library"))

    def test_explicit_list_admits_only_what_it_names(self):
        settings = MarkWatchedConfig(visible_libraries=["Plex::TV"])
        self.assertTrue(settings.shows_library("Plex", "TV"))
        self.assertFalse(settings.shows_library("Plex", "Movies"))
        self.assertFalse(settings.shows_library("Other", "TV"))

    def test_no_call_site_compares_visibility_to_none(self):
        import pathlib
        repo = pathlib.Path(__file__).resolve().parent.parent
        for path in (repo / "src" / "mark_watched.py",
                     repo / "src" / "web" / "mark_watched.py"):
            body = path.read_text(encoding="utf-8")
            self.assertNotIn(
                "configured_visibility", body,
                f"{path.name} should ask shows_library() instead",
            )


class ApiEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.client = app.app.test_client()

    def test_success_responses_carry_ok_true(self):
        with patch.object(app, "config", AppConfig(instances=[])), \
             patch.object(app, "has_valid_api_token", return_value=True):
            response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.get_json()["ok"], True)

    def test_error_responses_carry_ok_false(self):
        with patch.object(app, "config", AppConfig(instances=[])), \
             patch.object(app, "has_valid_api_token", return_value=True):
            response = self.client.patch("/api/settings/not-a-section", json={})
        self.assertEqual(response.status_code, 404)
        body = response.get_json()
        self.assertIs(body["ok"], False)
        self.assertIn("error", body)

    def test_a_handler_that_sets_ok_itself_is_left_alone(self):
        with patch.object(app, "config", AppConfig(instances=[])):
            response = self.client.post("/api/webhooks/sonarr", json={})
        self.assertIs(response.get_json()["ok"], False)

    def test_non_api_routes_are_untouched(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("application/json", response.headers.get("Content-Type", ""))


class FrontendParsingTests(unittest.TestCase):
    def test_every_json_read_goes_through_the_defensive_helper(self):
        script = script_text()
        # A proxy or error page returning HTML otherwise surfaces to the user
        # as "SyntaxError: Unexpected token '<'".
        self.assertNotIn("await response.json()", script)
        self.assertNotIn("await r.json()", script)
        self.assertIn("async function readJsonResponse(", script)


if __name__ == "__main__":
    unittest.main()
