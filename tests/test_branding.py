import os
import unittest
from unittest.mock import patch

import app
from src.branding import PRODUCT_NAME, PRODUCT_SLUG, get_env


class BrandingTests(unittest.TestCase):
    def test_visible_product_brand_is_mediawarden(self):
        self.assertEqual(PRODUCT_NAME, "mediaWarden")
        self.assertEqual(PRODUCT_SLUG, "mediawarden")

    def test_new_environment_name_wins_over_legacy_alias(self):
        with patch.dict(os.environ, {
            "MEDIAWARDEN_USERNAME": "new",
            "EMPTYARR_USERNAME": "old",
        }, clear=False):
            self.assertEqual(get_env("EMPTYARR_USERNAME"), "new")

    def test_legacy_environment_alias_remains_supported(self):
        with patch.dict(os.environ, {"MEDIAWARDEN_USERNAME": "", "EMPTYARR_USERNAME": "old"}, clear=False):
            self.assertEqual(get_env("MEDIAWARDEN_USERNAME"), "old")

    def test_branding_is_exposed_in_ui_and_status(self):
        response = app.app.test_client().get("/")
        html = response.get_data(as_text=True)
        self.assertIn("mediaWarden", html)
        self.assertIn("/static/mediawarden.svg", html)
        status = app.app.test_client().get("/api/status").get_json()
        self.assertEqual(status["product"], "mediaWarden")

    def test_new_logo_route_is_svg(self):
        response = app.app.test_client().get("/favicon.svg")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content_type.startswith("image/svg+xml"))
        self.assertIn(b"mediaWarden", response.data)
        response.close()


if __name__ == "__main__":
    unittest.main()
