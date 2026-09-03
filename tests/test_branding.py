import os
import unittest
from unittest.mock import patch

import app
from src.auth import _verify_password
from src.branding import PRODUCT_NAME, PRODUCT_SLUG, get_env
from src.version import __version__


class BrandingTests(unittest.TestCase):
    def test_visible_product_brand_is_mediamender(self):
        self.assertEqual(PRODUCT_NAME, "mediaMender")
        self.assertEqual(PRODUCT_SLUG, "mediamender")

    def test_mediamender_environment_name_is_read(self):
        with patch.dict(os.environ, {"MEDIAMENDER_USERNAME": "new"}, clear=False):
            self.assertEqual(get_env("MEDIAMENDER_USERNAME"), "new")

    def test_previous_environment_password_hash_remains_valid_during_migration(self):
        import hashlib

        stored = hashlib.sha256(b"emptyarr:correct horse battery staple").hexdigest()
        self.assertTrue(_verify_password("correct horse battery staple", stored))

    def test_branding_is_exposed_in_ui_and_status(self):
        response = app.app.test_client().get("/")
        html = response.get_data(as_text=True)
        self.assertIn("mediaMender", html)
        self.assertIn("/static/mediamender.png", html)
        status = app.app.test_client().get("/api/status").get_json()
        self.assertEqual(status["product"], "mediaMender")
        self.assertEqual(__version__, "2.4.0")
        self.assertEqual(status["version"], __version__)
        self.assertIn("Version 2.4.0", html)

    def test_new_logo_route_is_raster_png(self):
        response = app.app.test_client().get("/favicon.png")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content_type.startswith("image/png"))
        self.assertTrue(response.data.startswith(b"\x89PNG\r\n\x1a\n"))
        response.close()


if __name__ == "__main__":
    unittest.main()
