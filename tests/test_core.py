import logging
import os
import shutil
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import yaml

_TEST_ROOT = Path(__file__).resolve().parent
os.environ.setdefault("LOG_DIR", str(_TEST_ROOT / ".runtime-logs"))
os.environ.setdefault("CONFIG_PATH", str(_TEST_ROOT / ".runtime-bootstrap.yml"))
os.environ.setdefault("STATE_FILE", str(_TEST_ROOT / ".runtime-state.json"))
os.environ.setdefault("PLEX_CLIENT_ID_FILE", str(_TEST_ROOT / ".runtime-client.json"))

import app
from src.checks import check_debrid_mount, check_file_threshold, check_mountpoint
from src.config import (AppConfig, FeatureConfig, LibraryConfig, PathConfig,
                        PlexInstanceConfig, ProviderCheck, parse_config)
from src.plex_client import PlexClient, trash_item_key
from src import runner
from src import plex_auth
from src.auth import hash_api_token
from src.logging_manager import LogManager


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.directory = _TEST_ROOT / ".runtime-log-manager"
        if self.directory.exists():
            shutil.rmtree(self.directory)
        self.manager = LogManager(
            str(self.directory),
            logging.Formatter("%(levelname)s %(message)s"),
            max_file_size_mb=1,
            max_total_size_mb=3,
            retention_days=14,
        )
        self.logger = logging.getLogger(f"mediamender-test-{id(self)}")
        self.logger.handlers = [self.manager.handler]
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO)

    def tearDown(self):
        self.logger.handlers = []
        self.manager.handler.close()
        if self.directory.exists():
            shutil.rmtree(self.directory)

    def test_rotation_uses_readable_log_filenames(self):
        payload = "x" * (600 * 1024)
        self.logger.info(payload)
        self.logger.info(payload)
        names = {item["name"] for item in self.manager.list_files()}
        self.assertIn("mediamender.log", names)
        self.assertIn("mediamender.1.log", names)

    def test_retention_removes_expired_rotated_logs(self):
        expired = self.directory / "mediamender.9.log"
        expired.write_text("old", encoding="utf-8")
        old = time.time() - (2 * 86400)
        os.utime(expired, (old, old))
        self.manager.configure(1, 3, 1)
        self.assertFalse(expired.exists())

    def test_total_storage_removes_oldest_rotated_logs(self):
        first = self.directory / "mediamender.8.log"
        second = self.directory / "mediamender.9.log"
        first.write_bytes(b"a" * (700 * 1024))
        second.write_bytes(b"b" * (700 * 1024))
        os.utime(first, (time.time() - 20, time.time() - 20))
        os.utime(second, (time.time() - 10, time.time() - 10))
        self.manager.configure(1, 1, 14)
        total = sum(item["size_bytes"] for item in self.manager.list_files())
        self.assertLessEqual(total, 1024 * 1024)
        self.assertFalse(first.exists())

    def test_log_api_lists_reads_and_rejects_unknown_files(self):
        app.logger.info("log-api-test-marker")
        app.log_manager.handler.flush()
        client = app.app.test_client()
        listing = client.get("/api/logs")
        self.assertEqual(listing.status_code, 200)
        names = [item["name"] for item in listing.get_json()["files"]]
        self.assertIn("mediamender.log", names)
        content = client.get("/api/logs/mediamender.log")
        self.assertEqual(content.status_code, 200)
        self.assertIn("log-api-test-marker", content.get_json()["content"])
        download = client.get("/api/logs/mediamender.log/download")
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["Content-Disposition"])
        download.close()
        self.assertEqual(
            client.get("/api/logs/not-a-log.txt").status_code,
            404,
        )


class WebSecurityTests(unittest.TestCase):
    def test_ui_renders_with_security_headers(self):
        response = app.app.test_client().get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])

    def test_state_change_requires_csrf_for_browser_session(self):
        client = app.app.test_client()
        self.assertEqual(client.post("/api/scheduling", json={"enabled": True}).status_code, 403)
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "known-token"
        response = client.post(
            "/api/scheduling",
            json={"enabled": True},
            headers={"X-CSRF-Token": "known-token"},
        )
        self.assertEqual(response.status_code, 200)

    def test_invalid_api_token_does_not_bypass_csrf(self):
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["authenticated"] = True
        with patch.object(app.config, "auth_username", "admin"), \
             patch.object(app.config, "auth_password_hash", "password-hash"), \
             patch.object(app.config, "auth_api_token_hash",
                          hash_api_token("configured-api-token")):
            response = client.post(
                "/api/scheduling",
                json={"enabled": True},
                headers={"X-API-Token": "incorrect-api-token"},
            )
        self.assertEqual(response.status_code, 403)

    def test_valid_api_token_authenticates_without_csrf(self):
        client = app.app.test_client()
        with patch.object(app.config, "auth_username", "admin"), \
             patch.object(app.config, "auth_password_hash", "password-hash"), \
             patch.object(app.config, "auth_api_token_hash",
                          hash_api_token("configured-api-token")):
            response = client.post(
                "/api/scheduling",
                json={"enabled": True},
                headers={"X-API-Token": "configured-api-token"},
            )
        self.assertEqual(response.status_code, 200)

    def test_password_hash_is_not_an_api_token(self):
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["authenticated"] = True
        with patch.object(app.config, "auth_username", "admin"), \
             patch.object(app.config, "auth_password_hash", "password-hash"), \
             patch.object(app.config, "auth_api_token_hash",
                          hash_api_token("configured-api-token")):
            response = client.post(
                "/api/scheduling",
                json={"enabled": True},
                headers={"X-API-Token": "password-hash"},
            )
        self.assertEqual(response.status_code, 403)

    def test_generated_api_token_is_revealed_once(self):
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["authenticated"] = True
            browser_session["_csrf_token"] = "known-token"
        headers = {"X-CSRF-Token": "known-token"}
        with patch.object(app.config, "auth_username", "admin"), \
             patch.object(app.config, "auth_password_hash", "password-hash"), \
             patch.object(app, "generate_api_token",
                          return_value="mediamender_new-secret"), \
             patch.object(app, "_update_api_token_hash") as update:
            generated = client.post("/api/auth/token", headers=headers)
            status = client.get("/api/auth/token")
        self.assertEqual(generated.status_code, 200)
        self.assertEqual(generated.get_json()["token"], "mediamender_new-secret")
        update.assert_called_once_with(hash_api_token("mediamender_new-secret"))
        self.assertNotIn("token", status.get_json())

    def test_generated_api_token_persists_only_its_hash(self):
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["authenticated"] = True
            browser_session["_csrf_token"] = "known-token"
        config_path = _TEST_ROOT / ".runtime-token-config.yml"
        config_path.write_text(
            yaml.safe_dump({
                "auth": {
                    "username": "admin",
                    "password_hash": "password-hash",
                },
                "plex_instances": [],
            }),
            encoding="utf-8",
        )
        with patch.object(app, "CONFIG_PATH", str(config_path)), \
             patch.object(app.config, "auth_username", "admin"), \
             patch.object(app.config, "auth_password_hash", "password-hash"), \
             patch.object(app, "generate_api_token",
                          return_value="mediamender_new-secret"), \
             patch.object(app, "_apply_runtime_config"):
            response = client.post(
                "/api/auth/token",
                headers={"X-CSRF-Token": "known-token"},
            )
        saved_text = config_path.read_text(encoding="utf-8")
        saved = yaml.safe_load(saved_text)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            saved["auth"]["api_token_hash"],
            hash_api_token("mediamender_new-secret"),
        )
        self.assertNotIn("mediamender_new-secret", saved_text)

    def test_metadata_address_is_rejected(self):
        ok, _ = app._is_valid_plex_url("http://169.254.10.10:32400")
        self.assertFalse(ok)

    def test_browse_opens_at_allowed_roots_and_stays_inside_them(self):
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "known-token"
        headers = {"X-CSRF-Token": "known-token"}

        first = os.path.abspath("browse-root-one")
        second = os.path.abspath("browse-root-two")
        child = os.path.join(first, "Movies")
        directory = Mock()
        directory.name = "Movies"
        directory.path = child
        directory.is_dir.return_value = True
        directory.is_symlink.return_value = False

        with patch.dict(os.environ, {"BROWSE_ROOTS": f"{first},{second}"}), \
             patch("app.os.path.isdir", return_value=True), \
             patch("app.os.path.islink", return_value=False), \
             patch("app.os.path.exists", return_value=True), \
             patch("app.os.scandir", return_value=[directory]):
            response = client.post("/api/wizard/browse", json={}, headers=headers)
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertFalse(payload["selectable"])
            self.assertEqual(
                {entry["path"] for entry in payload["entries"]},
                {os.path.realpath(first), os.path.realpath(second)},
            )

            response = client.post(
                "/api/wizard/browse",
                json={"path": first},
                headers=headers,
            )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["selectable"])
            self.assertEqual(payload["parent"], "")
            self.assertEqual(payload["entries"][0]["name"], "Movies")

            response = client.post(
                "/api/wizard/browse",
                json={"path": str(Path(first).parent)},
                headers=headers,
            )
            self.assertEqual(response.status_code, 403)


class ConfigPrecedenceTests(unittest.TestCase):
    def test_empty_environment_values_do_not_override_file_settings(self):
        raw = {
            "discord_webhook": "https://discord.example/hook",
            "log_level": "DEBUG",
            "plex_instances": [{
                "name": "Plex",
                "url": "http://plex:32400",
                "token": "saved-token",
                "libraries": [],
            }],
        }
        overrides = {
            "DISCORD_WEBHOOK": "",
            "LOG_LEVEL": "",
            "PLEX_URL": "",
            "PLEX_TOKEN": "",
            "PLEX_URL_PLEX": "",
            "PLEX_TOKEN_PLEX": "",
        }
        with patch.dict(os.environ, overrides):
            parsed = parse_config(raw)
        self.assertEqual(parsed.discord_webhook, raw["discord_webhook"])
        self.assertEqual(parsed.log_level, "DEBUG")
        self.assertEqual(parsed.instances[0].url, "http://plex:32400")
        self.assertEqual(parsed.instances[0].token, "saved-token")

    def test_session_key_is_generated_once_and_persisted(self):
        key_path = Path("tests/.runtime-session-key").resolve()
        key_path.unlink(missing_ok=True)
        overrides = {
            "MEDIAMENDER_SECRET_KEY": "",
            "MEDIAMENDER_SECRET_KEY_FILE": str(key_path),
        }
        try:
            with patch.dict(os.environ, overrides):
                first = app._load_session_key()
                second = app._load_session_key()
            self.assertEqual(first, second)
            self.assertEqual(key_path.read_text(encoding="utf-8"), first)
        finally:
            key_path.unlink(missing_ok=True)


class PlexAuthTests(unittest.TestCase):
    def test_connections_prefer_local_non_relay_and_parse_string_booleans(self):
        connections = plex_auth._connections({
            "connections": [
                {"uri": "https://relay", "local": "0", "relay": "1", "protocol": "https"},
                {"uri": "http://local", "local": "1", "relay": "0", "protocol": "http"},
            ],
        })
        self.assertEqual(connections[0]["uri"], "http://local")
        self.assertTrue(connections[0]["local"])
        self.assertFalse(connections[0]["relay"])

    def tearDown(self):
        with plex_auth._lock:
            plex_auth._sessions.clear()

    def test_rate_limit_stays_pending_and_honors_bounded_retry_after(self):
        with plex_auth._lock:
            plex_auth._sessions["state"] = {
                "created_at": time.time(),
                "pin_id": 123,
                "code": "secret-pin-code",
            }
        response = Mock(status_code=429, headers={"Retry-After": "17"})
        error = requests.HTTPError(
            "429 Client Error for https://plex.tv/pins/123?code=secret-pin-code",
            response=response,
        )
        response.raise_for_status.side_effect = error
        with patch("src.plex_auth.requests.get", return_value=response):
            result = plex_auth.poll_auth("state")
        self.assertEqual(
            result,
            {"ok": True, "pending": True, "retry_after": 17},
        )
        self.assertNotIn("secret-pin-code", str(result))

    def test_cancel_forgets_pending_authorization(self):
        with plex_auth._lock:
            plex_auth._sessions["state"] = {
                "created_at": time.time(), "pin_id": 123, "code": "code",
            }
        self.assertTrue(plex_auth.cancel_auth("state"))
        self.assertFalse(plex_auth.cancel_auth("state"))

    def test_cancel_endpoint_forgets_pending_authorization(self):
        with plex_auth._lock:
            plex_auth._sessions["state"] = {
                "created_at": time.time(), "pin_id": 123, "code": "code",
            }
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "known-token"
        response = client.post(
            "/api/plex/auth/cancel/state",
            headers={"X-CSRF-Token": "known-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["ok"])
        with plex_auth._lock:
            self.assertNotIn("state", plex_auth._sessions)

    def test_auth_endpoint_sanitizes_unexpected_poll_failure(self):
        client = app.app.test_client()
        secret = "do-not-display-this-pin"
        with patch.object(
            plex_auth,
            "poll_auth",
            side_effect=RuntimeError(f"failure https://plex.tv/pins?code={secret}"),
        ):
            response = client.get("/api/plex/auth/status/state")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json()["error"],
            "Plex authorization could not be completed",
        )
        self.assertNotIn(secret, response.get_data(as_text=True))

    def test_auth_ui_cancels_closed_popup_and_requests_real_popup(self):
        html = app.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("popup=yes,width=720,height=760", html)
        self.assertIn("const popupWasClosed = Boolean(popup && popup.closed)", html)
        self.assertIn("if (popupWasClosed)", html)
        self.assertIn("/api/plex/auth/cancel/", html)


class PlexClientTests(unittest.TestCase):
    def test_trash_identity_ignores_volatile_media_version_id(self):
        before = {
            "type": "movie", "title": "One", "rating_key": "42",
            "media_id": "old-version",
        }
        after = dict(before, media_id="new-version")

        self.assertEqual(trash_item_key(before), trash_item_key(after))

    def test_tv_count_uses_episode_type(self):
        client = PlexClient("http://plex:32400", "token")
        response = Mock()
        response.json.return_value = {"MediaContainer": {"totalSize": 123}}
        response.raise_for_status.return_value = None
        with patch.object(client, "get_section_type", return_value="show"), \
             patch.object(client, "_get", return_value=response) as get:
            self.assertEqual(client.get_library_item_count("7"), 123)
        self.assertEqual(get.call_args.kwargs["params"]["type"], 4)

    def test_count_failure_is_not_reported_as_zero(self):
        client = PlexClient("http://plex:32400", "token")
        with patch.object(client, "get_section_type", return_value="show"), \
             patch.object(client, "_get", side_effect=RuntimeError("boom")):
            self.assertIsNone(client.get_library_item_count("7"))

    def test_trash_inventory_failure_is_explicit(self):
        client = PlexClient("http://plex:32400", "token")
        with patch.object(client, "get_section_type", return_value="movie"), \
             patch.object(client, "_fetch_deleted_xml", return_value=None):
            self.assertIsNone(client.get_trash_items("1"))

    def test_trash_inventory_keeps_same_title_with_distinct_plex_ids(self):
        client = PlexClient("http://plex:32400", "token")
        duplicate_titles = [
            {"title": "Pilot", "type": "episode", "rating_key": "1"},
            {"title": "Pilot", "type": "episode", "rating_key": "2"},
        ]
        legacy = Mock(status_code=200)
        legacy.json.return_value = {"MediaContainer": {"Metadata": []}}
        with patch.object(client, "get_section_type", return_value="show"), \
             patch.object(client, "_fetch_deleted_xml",
                          side_effect=[duplicate_titles, [], []]), \
             patch.object(client, "_get", return_value=legacy):
            items = client.get_trash_items("1")
        self.assertEqual(len(items), 2)

    def test_unmatched_items_use_one_bulk_request_and_local_guid(self):
        client = PlexClient("http://plex:32400", "token")
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"MediaContainer": {
            "totalSize": 3,
            "Metadata": [
                {"title": "Matched", "ratingKey": "1",
                 "key": "/library/metadata/1", "guid": "plex://movie/abc"},
                {"title": "Needs Match", "year": 2026, "ratingKey": "2",
                 "key": "/library/metadata/2", "guid": "local://2"},
                {"title": "Also Matched", "ratingKey": "3",
                 "guid": "tmdb://3"},
            ],
        }}

        with patch.object(client, "_get", return_value=response) as get:
            result = client.get_unmatched_items("7")

        self.assertEqual(result["total_items"], 3)
        self.assertEqual([item["rating_key"] for item in result["items"]], ["2"])
        get.assert_called_once_with(
            "/library/sections/7/all",
            params={
                "X-Plex-Container-Start": 0,
                "X-Plex-Container-Size": 100000,
            },
            timeout=60,
        )


class FeatureToggleTests(unittest.TestCase):
    def test_feature_flags_round_trip_and_default_on(self):
        defaults = parse_config({"plex_instances": []})
        self.assertTrue(defaults.features.trash_removal)
        self.assertTrue(defaults.features.metadata_health)
        self.assertTrue(defaults.features.timestamp_repair)

        parsed = parse_config({
            "features": {
                "trash_removal": False,
                "metadata_health": False,
                "timestamp_repair": False,
            },
            "plex_instances": [],
        })
        self.assertFalse(parsed.features.trash_removal)
        self.assertFalse(parsed.features.metadata_health)
        self.assertFalse(parsed.features.timestamp_repair)

    def test_disabled_trash_removal_has_no_jobs_or_status_refresh(self):
        target = AppConfig(
            instances=[],
            features=FeatureConfig(trash_removal=False),
        )
        with patch.object(app.scheduler, "remove_all_jobs") as remove_jobs, \
             patch.object(app.scheduler, "add_job") as add_job, \
             patch.object(app.threading, "Thread") as thread:
            app._setup_scheduler(target)
            app._start_readonly_status_refresh(target, {})
        remove_jobs.assert_called_once_with()
        add_job.assert_not_called()
        thread.assert_not_called()

    def test_disabled_features_reject_manual_operations(self):
        target = AppConfig(
            instances=[],
            features=FeatureConfig(
                trash_removal=False,
                metadata_health=False,
                timestamp_repair=False,
            ),
        )
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "known-token"
        headers = {"X-CSRF-Token": "known-token"}
        with patch.object(app, "config", target), \
             patch.object(app.threading, "Thread") as thread:
            responses = [
                client.post("/api/run/all", headers=headers),
                client.post("/api/dryrun/all", headers=headers),
                client.post("/api/run/Plex/Movies", headers=headers),
                client.post("/api/scheduling", json={"enabled": True}, headers=headers),
                client.post("/api/metadata-audit/run", json={"instance": "Plex"}, headers=headers),
                client.post("/api/timestamp-repair/audit", json={"instance": "Plex"}, headers=headers),
                client.post("/api/timestamp-repair/run", json={"instance": "Plex"}, headers=headers),
            ]
        self.assertTrue(all(response.status_code == 409 for response in responses))
        thread.assert_not_called()


class MetadataAuditTests(unittest.TestCase):
    def test_metadata_health_ignores_round_trip_through_config_builder(self):
        raw = {
            "name": "Plex",
            "url": "http://plex:32400",
            "token": "token",
            "metadata_health": {"ignored_libraries": ["Youtube"]},
            "libraries": [],
        }
        parsed = parse_config({"plex_instances": [raw]})
        self.assertEqual(
            parsed.instances[0].metadata_health.ignored_libraries,
            ["Youtube"],
        )
        built = app._build_instance_cfg(raw, True, [])
        self.assertEqual(
            built["metadata_health"]["ignored_libraries"],
            ["Youtube"],
        )

    def test_audit_returns_direct_links_without_touching_trash(self):
        library = LibraryConfig("Movies", "physical", [], section_id="1")
        instance = PlexInstanceConfig(
            "Unlimited", "http://plex:32400", "token", [library],
            machine_id="server id",
        )
        target_config = AppConfig(instances=[instance])
        plex = Mock()
        plex.get_sections.return_value = [
            {"id": "1", "title": "Movies", "type": "movie"},
        ]
        plex.get_unmatched_items.return_value = {
            "total_items": 500,
            "items": [{
                "title": "Needs Match",
                "year": 2026,
                "type": "movie",
                "rating_key": "42",
                "metadata_key": "/library/metadata/42",
                "guid": "local://42",
            }],
        }
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "known-token"

        with patch.object(app, "config", target_config), \
             patch.object(app, "plex_clients", {instance.name: plex}), \
             patch.object(app, "_read_metadata_audits", return_value={}), \
             patch.object(app, "atomic_write_json") as write:
            response = client.post(
                "/api/metadata-audit/run",
                json={"instance": instance.name},
                headers={"X-CSRF-Token": "known-token"},
            )

        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(result["unmatched_count"], 1)
        self.assertEqual(result["total_items"], 500)
        self.assertEqual(
            result["libraries"][0]["items"][0]["plex_url"],
            "https://app.plex.tv/desktop/#!/server/server%20id/details?key="
            "%2Flibrary%2Fmetadata%2F42",
        )
        saved = write.call_args.args[1]
        self.assertIn(instance.name, saved)
        plex.get_unmatched_items.assert_called_once_with("1")
        plex.get_trash_items.assert_not_called()
        plex.empty_trash.assert_not_called()

    def test_match_audit_ui_is_explicitly_manual_and_read_only(self):
        html = app.app.test_client().get("/").get_data(as_text=True)
        self.assertIn("Metadata Health", html)
        self.assertIn("Scan all servers", html)
        self.assertIn("Collapse all", html)
        self.assertIn("renderSingleServerMetadata", html)
        self.assertIn("Libraries on", html)
        self.assertIn("Custom cron expression", html)
        self.assertIn("data-schedule-cron", html)
        self.assertIn("Folders ${PRODUCT_NAME} may repair", html)
        self.assertIn("Use this database", html)
        self.assertIn("Plex timestamp", html)
        self.assertIn("Filesystem timestamp", html)
        self.assertIn("settingsSave('timestamp-repair')", html)
        self.assertIn("setAllRepairDetails(true)", html)
        self.assertIn("setAllRepairDetails(false)", html)
        self.assertIn("data-repair-toggle", html)
        self.assertNotIn("<strong style=\"color:var(--bright);\">Runtime:</strong>", html)
        self.assertIn("Show details", html)
        self.assertIn("read-only", html)
        self.assertIn("affect Empty Trash safety decisions", html)

    def test_settings_are_split_and_features_have_visibility_hooks(self):
        html = app.app.test_client().get("/").get_data(as_text=True)
        self.assertIn('id="ss-plex"', html)
        self.assertIn('id="ss-trash-removal"', html)
        self.assertIn('id="ss-features"', html)
        self.assertIn('data-feature="trash_removal"', html)
        self.assertIn('data-feature="metadata_health"', html)
        self.assertIn('data-feature="timestamp_repair"', html)
        self.assertIn("renderTrashRemovalSettings", html)
        self.assertIn("applyFeatureVisibility", html)
        self.assertIn("settingsSave('plex')", html)
        self.assertIn("settingsSave('metadata-health')", html)

    def test_metadata_status_includes_configured_libraries_before_scan(self):
        library = LibraryConfig("Movies", "physical", [], section_id="1")
        target_config = AppConfig(instances=[PlexInstanceConfig(
            "Plex", "http://plex:32400", "token", [library],
        )])
        with patch.object(app, "config", target_config), \
             patch.object(app, "_read_metadata_audits", return_value={}):
            result = app.app.test_client().get("/api/metadata-audit/status").get_json()
        self.assertEqual(result["libraries"]["Plex"], [
            {"name": "Movies", "type": "physical"},
        ])

    def test_ignored_metadata_library_is_not_requested(self):
        movies = LibraryConfig("Movies", "physical", [], section_id="1")
        youtube = LibraryConfig("Youtube", "physical", [], section_id="2")
        instance = PlexInstanceConfig(
            "Plex", "http://plex:32400", "token", [movies, youtube],
            machine_id="server-id",
        )
        instance.metadata_health.ignored_libraries = ["Youtube"]
        target_config = AppConfig(instances=[instance])
        plex = Mock()
        plex.get_sections.return_value = [
            {"id": "1", "title": "Movies", "type": "movie"},
            {"id": "2", "title": "Youtube", "type": "movie"},
        ]
        plex.get_unmatched_items.return_value = {"total_items": 10, "items": []}
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "known-token"

        with patch.object(app, "config", target_config), \
             patch.object(app, "plex_clients", {instance.name: plex}), \
             patch.object(app, "_read_metadata_audits", return_value={}), \
             patch.object(app, "atomic_write_json"):
            response = client.post(
                "/api/metadata-audit/run",
                json={"instance": instance.name},
                headers={"X-CSRF-Token": "known-token"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [library["name"] for library in response.get_json()["libraries"]],
            ["Movies"],
        )
        plex.get_unmatched_items.assert_called_once_with("1")

    def test_navigation_and_startup_progress_match_feature_layout(self):
        html = app.app.test_client().get("/").get_data(as_text=True)
        labels = ["Dashboard", "mediaMender", "Metadata Health",
                  "Timestamp Repair", "Settings"]
        positions = [html.index(f">\n      {label}\n") for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("mediamender-startup-progress", html)
        self.assertIn("dashboard-startup-progress", html)
        self.assertIn("moveProtectionControls", html)
        self.assertIn("showPage('dashboard'", html)

    def test_startup_status_refresh_runs_in_background(self):
        library = LibraryConfig("Movies", "physical", [], section_id="1")
        instance = PlexInstanceConfig(
            "Plex", "http://plex:32400", "token", [library],
        )
        target_config = AppConfig(instances=[instance])
        plex = Mock()
        checks = {"Plex": {"pass": True, "detail": "ok"}}

        with patch.object(app.threading, "Thread") as thread, \
             patch.object(app.runner, "run_instance_checks",
                          return_value=checks) as instance_checks, \
             patch.object(app.runner, "refresh_protection_status") as refresh:
            app._start_readonly_status_refresh(
                target_config, {instance.name: plex},
            )
            thread.call_args.kwargs["target"]()

        self.assertTrue(thread.call_args.kwargs["daemon"])
        thread.return_value.start.assert_called_once_with()
        instance_checks.assert_called_once_with(instance, plex)
        refresh.assert_called_once_with(
            instance, library, target_config, plex, checks,
        )
        self.assertFalse(app._status_refresh_progress["running"])
        self.assertEqual(app._status_refresh_progress["completed"], 1)
        self.assertEqual(app._status_refresh_progress["total"], 1)


class SafetyTests(unittest.TestCase):
    @staticmethod
    def _run_objects(max_items=1000, max_percent=25):
        path = PathConfig(path="/media", type="physical", min_threshold=0.9)
        library = LibraryConfig(
            "Movies", "physical", [path], section_id="1",
        )
        instance = PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
        )
        config = AppConfig(
            instances=[instance],
            max_trash_items=max_items,
            max_trash_percent=max_percent,
        )
        plex = Mock()
        plex.check_reachable.return_value = {"pass": True, "detail": "ok"}
        plex.get_library_item_count.return_value = 100
        plex.empty_trash.return_value = {"ok": True, "http": 200}
        return instance, library, config, plex

    @staticmethod
    def _run_with_checks(instance, library, config, plex,
                         mount_result=None, file_result=None, **kwargs):
        mount_result = mount_result or {"pass": True, "detail": "mounted"}
        file_result = file_result or {"pass": True, "detail": "files ok"}
        with patch("src.runner.check_mountpoint", return_value=mount_result), \
             patch("src.runner.check_file_threshold", return_value=file_result), \
             patch("src.runner.time.sleep"):
            runner.run_library(instance, library, config, plex, **kwargs)

    def test_missing_plex_count_fails_closed(self):
        with patch("src.checks.count_files", return_value=1):
            directory = "/media"
            result = check_file_threshold(directory, 0.9, None)
        self.assertFalse(result["pass"])
        self.assertIn("refusing", result["detail"])

    def test_mount_check_uses_single_findmnt_target_lookup(self):
        completed = Mock(
            returncode=0, stdout="/mnt/symlink_media\n",
        )
        with patch("src.checks.os.path.exists", return_value=True), \
             patch("src.checks.subprocess.run", return_value=completed) as run:
            result = check_mountpoint(
                "/mnt/symlink_media/symlinks/vm-altmount/tv",
            )

        self.assertTrue(result["pass"])
        self.assertIn("/mnt/symlink_media", result["detail"])
        run.assert_called_once_with(
            ["findmnt", "-T", "/mnt/symlink_media/symlinks/vm-altmount/tv",
             "-n", "-o", "TARGET"],
            capture_output=True, text=True, timeout=5,
        )

    def test_mount_check_findmnt_timeout_fails_closed(self):
        with patch("src.checks.os.path.exists", return_value=True), \
             patch("src.checks.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("findmnt", 5)):
            result = check_mountpoint("/mnt/symlink_media/tv")

        self.assertFalse(result["pass"])
        self.assertIn("timed out after 5 seconds", result["detail"])

    def test_mount_check_missing_findmnt_uses_mountpoint_fallback(self):
        missing_findmnt = FileNotFoundError()
        not_mount = Mock(returncode=32)
        mounted = Mock(returncode=0)
        with patch("src.checks.os.path.exists", return_value=True), \
             patch("src.checks.subprocess.run",
                   side_effect=[missing_findmnt, not_mount, mounted]) as run:
            result = check_mountpoint("/mnt/media/tv")

        self.assertTrue(result["pass"])
        self.assertIn("/mnt/media", result["detail"])
        self.assertEqual(run.call_count, 3)

    def test_debrid_mount_passes_when_discovered_mount_is_populated(self):
        with patch("src.checks.os.path.exists", return_value=True), \
             patch("src.checks._sample_symlink_targets",
                   return_value=["/mnt/cache/movie.mkv"]), \
             patch("src.checks._find_target_mount",
                   return_value=("/mnt/cache", "/mnt/cache")), \
             patch("src.checks.os.listdir", return_value=["movie.mkv"]):
            result = check_debrid_mount("/media")
        self.assertTrue(result["pass"])

    def test_debrid_mount_fails_when_discovered_mount_is_empty(self):
        with patch("src.checks.os.path.exists", return_value=True), \
             patch("src.checks._sample_symlink_targets",
                   return_value=["/mnt/cache/movie.mkv"]), \
             patch("src.checks._find_target_mount",
                   return_value=("/mnt/cache", "/mnt/cache")), \
             patch("src.checks.os.listdir", return_value=[]):
            result = check_debrid_mount("/media")
        self.assertFalse(result["pass"])

    def test_provider_checks_receive_live_config(self):
        config = AppConfig(
            instances=[],
            providers={"realdebrid": {"api_key": "saved-key"}},
        )
        path = PathConfig(
            path="/media",
            type="debrid",
            provider_checks=[ProviderCheck(type="realdebrid")],
        )
        with patch("src.runner.check_mountpoint",
                   return_value={"pass": True, "detail": "ok"}), \
             patch("src.runner.check_debrid_mount",
                   return_value={"pass": True, "detail": "ok"}), \
             patch("src.runner.check_file_threshold",
                   return_value={"pass": True, "detail": "ok"}), \
             patch("src.runner.check_provider",
                   return_value={"pass": True, "detail": "ok"}) as provider:
            runner._run_path_checks(path, 1, config)
        self.assertIs(provider.call_args.kwargs["config"], config)

    def test_overlapping_library_run_is_skipped(self):
        instance = PlexInstanceConfig("Plex", "http://plex", "token", [])
        library = LibraryConfig("Movies", "physical", [])
        config = AppConfig(instances=[instance])
        plex = Mock()
        started = threading.Event()
        release = threading.Event()

        def slow_run(*args, **kwargs):
            started.set()
            release.wait(2)

        with patch("src.runner._run_library", side_effect=slow_run), \
             patch("src.runner._record") as record:
            thread = threading.Thread(
                target=runner.run_library,
                args=(instance, library, config, plex),
            )
            thread.start()
            self.assertTrue(started.wait(1))
            runner.run_library(instance, library, config, plex)
            release.set()
            thread.join(2)
        record.assert_called_once()
        self.assertIn("already in progress", record.call_args.args[4])

    def test_distinct_library_runs_queue_instead_of_skipping(self):
        instance = PlexInstanceConfig("Plex", "http://plex", "token", [])
        movies = LibraryConfig("Movies", "physical", [])
        shows = LibraryConfig("TV Shows", "physical", [])
        config = AppConfig(instances=[instance])
        plex = Mock()
        first_started = threading.Event()
        release_first = threading.Event()
        second_attempting = threading.Event()
        completed = []

        def controlled_run(_instance, library, *_args, **_kwargs):
            if library.name == "Movies":
                first_started.set()
                release_first.wait(2)
            completed.append(library.name)

        def run_shows():
            second_attempting.set()
            runner.run_library(instance, shows, config, plex)

        with patch("src.runner._run_library", side_effect=controlled_run), \
             patch("src.runner._record") as record:
            first = threading.Thread(
                target=runner.run_library,
                args=(instance, movies, config, plex),
            )
            second = threading.Thread(target=run_shows)
            first.start()
            self.assertTrue(first_started.wait(1))
            second.start()
            self.assertTrue(second_attempting.wait(1))
            self.assertTrue(second.is_alive())
            release_first.set()
            first.join(2)
            second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(completed, ["Movies", "TV Shows"])
        record.assert_not_called()

    def test_failed_health_check_never_empties_trash(self):
        instance, library, config, plex = self._run_objects()
        self._run_with_checks(
            instance, library, config, plex,
            mount_result={"pass": False, "detail": "mount missing"},
        )
        plex.get_trash_items.assert_not_called()
        plex.empty_trash.assert_not_called()

    def test_readonly_status_preload_never_reads_or_empties_trash(self):
        instance, library, config, plex = self._run_objects()
        history_before = list(runner.get_history())
        with patch(
            "src.runner._collect_library_checks",
            return_value=({"Mount": {"pass": True, "detail": "ok"}}, 100),
        ):
            checks = runner.refresh_protection_status(
                instance, library, config, plex,
                {"Plex": {"pass": True, "detail": "ok"}},
            )

        self.assertTrue(checks["Mount"]["pass"])
        status = runner.get_instance_status()[instance.name][library.name]
        self.assertEqual(status["last_status"], "preflight_pass")
        self.assertEqual(status["status_source"], "preflight")
        self.assertEqual(runner.get_history(), history_before)
        plex.get_trash_items.assert_not_called()
        plex.empty_trash.assert_not_called()

    def test_unreachable_plex_never_empties_trash(self):
        instance, library, config, plex = self._run_objects()
        plex.check_reachable.return_value = {
            "pass": False, "detail": "Plex unreachable",
        }
        self._run_with_checks(instance, library, config, plex)
        plex.get_trash_items.assert_not_called()
        plex.empty_trash.assert_not_called()

    def test_missing_count_never_empties_trash(self):
        instance, library, config, plex = self._run_objects()
        plex.get_library_item_count.return_value = None
        with patch(
            "src.runner.check_mountpoint",
            return_value={"pass": True, "detail": "mounted"},
        ), patch("src.checks.count_files", return_value=100):
            runner.run_library(instance, library, config, plex)
        plex.get_trash_items.assert_not_called()
        plex.empty_trash.assert_not_called()

    def test_failed_provider_check_never_empties_trash(self):
        instance, library, config, plex = self._run_objects()
        library.paths[0].provider_checks = [
            ProviderCheck(type="realdebrid", api_key="key"),
        ]
        with patch(
            "src.runner.check_mountpoint",
            return_value={"pass": True, "detail": "mounted"},
        ), patch(
            "src.runner.check_file_threshold",
            return_value={"pass": True, "detail": "files ok"},
        ), patch(
            "src.runner.check_provider",
            return_value={"pass": False, "detail": "provider unavailable"},
        ):
            runner.run_library(instance, library, config, plex)
        plex.get_trash_items.assert_not_called()
        plex.empty_trash.assert_not_called()

    def test_missing_section_never_empties_trash(self):
        instance, library, config, plex = self._run_objects()
        library.section_id = None
        plex.find_section_id.return_value = None
        self._run_with_checks(instance, library, config, plex)
        plex.get_trash_items.assert_not_called()
        plex.empty_trash.assert_not_called()

    def test_failed_initial_inventory_never_empties_trash(self):
        instance, library, config, plex = self._run_objects()
        plex.get_trash_items.return_value = None
        self._run_with_checks(instance, library, config, plex)
        plex.empty_trash.assert_not_called()

    def test_dry_run_never_empties_trash(self):
        instance, library, config, plex = self._run_objects()
        plex.get_trash_items.return_value = [
            {"type": "movie", "title": "One", "rating_key": "1"},
        ]
        self._run_with_checks(
            instance, library, config, plex, dry_run=True, manual=True,
        )
        plex.empty_trash.assert_not_called()

    def test_clean_bundles_failure_never_empties_trash(self):
        instance, library, config, plex = self._run_objects()
        config.clean_bundles_before_empty = True
        plex.get_trash_items.return_value = [
            {"type": "movie", "title": "One", "rating_key": "1"},
        ]
        plex.clean_bundles.return_value = {"ok": False, "http": 500}
        self._run_with_checks(instance, library, config, plex)
        plex.empty_trash.assert_not_called()

    def test_paused_scheduling_never_empties_trash(self):
        instance, library, config, plex = self._run_objects()
        runner.set_scheduling_enabled(False)
        try:
            self._run_with_checks(instance, library, config, plex)
        finally:
            runner.set_scheduling_enabled(True)
        plex.empty_trash.assert_not_called()

    def test_failed_final_preflight_never_empties_trash(self):
        instance, library, config, plex = self._run_objects()
        items = [{"type": "movie", "title": "One", "rating_key": "1"}]
        plex.get_trash_items.return_value = items
        with patch(
            "src.runner.check_mountpoint",
            side_effect=[
                {"pass": True, "detail": "mounted"},
                {"pass": False, "detail": "mount disappeared"},
            ],
        ), patch(
            "src.runner.check_file_threshold",
            return_value={"pass": True, "detail": "files ok"},
        ), patch("src.runner.time.sleep"):
            runner.run_library(instance, library, config, plex)
        plex.empty_trash.assert_not_called()

    def test_changed_trash_snapshot_never_empties_trash(self):
        instance, library, config, plex = self._run_objects()
        initial = [{"type": "movie", "title": "One", "rating_key": "1"}]
        changed_once = initial + [
            {"type": "movie", "title": "Two", "rating_key": "2"},
        ]
        changed_twice = changed_once + [
            {"type": "movie", "title": "Three", "rating_key": "3"},
        ]
        changed_thrice = changed_twice + [
            {"type": "movie", "title": "Four", "rating_key": "4"},
        ]
        plex.get_trash_items.side_effect = [
            initial, changed_once, changed_twice, changed_thrice,
        ]
        self._run_with_checks(instance, library, config, plex)
        plex.empty_trash.assert_not_called()

    def test_changed_snapshot_retries_until_two_consecutive_match(self):
        instance, library, config, plex = self._run_objects()
        initial = [{"type": "movie", "title": "One", "rating_key": "1"}]
        changed = [
            {"type": "movie", "title": "Two", "rating_key": "2"},
        ]
        plex.get_trash_items.side_effect = [initial, changed, changed, []]

        self._run_with_checks(instance, library, config, plex)

        plex.empty_trash.assert_called_once_with("1")

    def test_media_version_change_does_not_destabilize_snapshot(self):
        instance, library, config, plex = self._run_objects()
        initial = [{
            "type": "movie", "title": "One", "rating_key": "1",
            "media_id": "old",
        }]
        refreshed = [dict(initial[0], media_id="new")]
        plex.get_trash_items.side_effect = [initial, refreshed, []]

        self._run_with_checks(instance, library, config, plex)

        plex.empty_trash.assert_called_once_with("1")

    def test_stabilized_empty_snapshot_never_calls_empty_trash(self):
        instance, library, config, plex = self._run_objects()
        initial = [{"type": "movie", "title": "Gone", "rating_key": "1"}]
        plex.get_trash_items.side_effect = [initial, [], []]

        self._run_with_checks(instance, library, config, plex)

        plex.empty_trash.assert_not_called()

    def test_snapshot_change_reports_appeared_and_disappeared_items(self):
        instance, library, config, plex = self._run_objects()
        initial = [{"type": "movie", "title": "Gone", "rating_key": "1"}]
        changed = [{"type": "movie", "title": "New", "rating_key": "2"}]
        plex.get_trash_items.side_effect = [initial, changed, changed, []]

        with self.assertLogs("mediamender", level="WARNING") as captured:
            self._run_with_checks(instance, library, config, plex)

        output = "\n".join(captured.output)
        self.assertIn("appeared: New [ratingKey 2]", output)
        self.assertIn("disappeared: Gone [ratingKey 1]", output)

    def test_deletion_limit_never_empties_oversized_snapshot(self):
        instance, library, config, plex = self._run_objects(
            max_items=1, max_percent=0,
        )
        items = [
            {"type": "movie", "title": "One", "rating_key": "1"},
            {"type": "movie", "title": "Two", "rating_key": "2"},
        ]
        plex.get_trash_items.side_effect = [items, items]
        self._run_with_checks(instance, library, config, plex)
        plex.empty_trash.assert_not_called()

    def test_percentage_limit_never_empties_oversized_snapshot(self):
        instance, library, config, plex = self._run_objects(
            max_items=0, max_percent=1,
        )
        items = [
            {"type": "movie", "title": "One", "rating_key": "1"},
            {"type": "movie", "title": "Two", "rating_key": "2"},
        ]
        plex.get_trash_items.side_effect = [items, items]
        self._run_with_checks(instance, library, config, plex)
        plex.empty_trash.assert_not_called()

    def test_empty_snapshot_does_not_call_empty_trash(self):
        instance, library, config, plex = self._run_objects()
        plex.get_trash_items.return_value = []
        self._run_with_checks(instance, library, config, plex)
        plex.empty_trash.assert_not_called()

    def test_manual_run_can_bypass_paused_scheduler(self):
        instance, library, config, plex = self._run_objects()
        items = [{"type": "movie", "title": "One", "rating_key": "1"}]
        plex.get_trash_items.side_effect = [items, items, []]
        runner.set_scheduling_enabled(False)
        try:
            self._run_with_checks(
                instance, library, config, plex, manual=True,
            )
        finally:
            runner.set_scheduling_enabled(True)
        plex.empty_trash.assert_called_once_with("1")

    def test_successful_run_has_one_destructive_call(self):
        instance, library, config, plex = self._run_objects()
        items = [{"type": "movie", "title": "One", "rating_key": "1"}]
        plex.get_trash_items.side_effect = [items, items, []]
        self._run_with_checks(instance, library, config, plex)
        plex.empty_trash.assert_called_once_with("1")


class LiveConfigTests(unittest.TestCase):
    def test_runtime_config_save_does_not_restart_readonly_health_refresh(self):
        old_config = app.config
        old_clients = app.plex_clients
        try:
            with patch.object(app, "_setup_scheduler"), \
                 patch.object(app, "_start_readonly_status_refresh") as refresh:
                app._apply_runtime_config(AppConfig(instances=[]))
            refresh.assert_not_called()
        finally:
            app.config = old_config
            app.plex_clients = old_clients

    def test_invalid_log_storage_policy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Total log storage"):
            app._validate_raw_config({
                "logging": {
                    "max_file_size_mb": 50,
                    "max_total_size_mb": 10,
                    "retention_days": 14,
                },
                "plex_instances": [],
            })

    def test_invalid_global_cron_is_rejected_before_apply(self):
        with self.assertRaises(ValueError):
            app._validate_raw_config({
                "schedule": {"default_cron": "not a cron"},
                "plex_instances": [],
            })

    def test_global_schedule_is_inherited_without_library_override(self):
        raw = {
            "schedule": {"default_cron": "*/30 * * * *"},
            "plex_instances": [{
                "name": "Plex",
                "url": "http://plex:32400",
                "libraries": [{
                    "name": "Movies",
                    "paths": [{"path": "/media", "type": "physical"}],
                }],
            }],
        }
        parsed = app._validate_raw_config(raw)
        library = parsed.instances[0].libraries[0]
        self.assertEqual(library.cron, "")
        self.assertEqual(app._effective_cron(parsed, library), "*/30 * * * *")

    def test_library_schedule_override_wins_over_global_default(self):
        raw = {
            "schedule": {"default_cron": "0 * * * *"},
            "plex_instances": [{
                "name": "Plex",
                "url": "http://plex:32400",
                "libraries": [{
                    "name": "Movies",
                    "cron": "0 */6 * * *",
                    "paths": [{"path": "/media", "type": "physical"}],
                }],
            }],
        }
        parsed = app._validate_raw_config(raw)
        library = parsed.instances[0].libraries[0]
        self.assertEqual(app._effective_cron(parsed, library), "0 */6 * * *")

    def test_next_run_is_available_before_first_library_run(self):
        old_config = app.config
        raw = {
            "schedule": {"default_cron": "*/30 * * * *"},
            "plex_instances": [{
                "name": "Schedule Test",
                "url": "http://plex:32400",
                "libraries": [{
                    "name": "Movies",
                    "paths": [{"path": "/media", "type": "physical"}],
                }],
            }],
        }
        try:
            parsed = app._validate_raw_config(raw)
            app._apply_runtime_config(parsed)
            instances = app._build_ui_instances()
            self.assertNotEqual(instances[0]["libraries"][0]["next_run"], "—")
            self.assertTrue(instances[0]["libraries"][0]["uses_global_schedule"])
            self.assertEqual(
                instances[0]["libraries"][0]["effective_cron"],
                "*/30 * * * *",
            )
        finally:
            app._apply_runtime_config(old_config)

    def test_invalid_cron_is_rejected_before_apply(self):
        raw = {
            "plex_instances": [{
                "name": "Plex",
                "url": "http://plex:32400",
                "token": "x",
                "libraries": [{
                    "name": "Movies",
                    "cron": "not a cron",
                    "paths": [{"path": "/media", "type": "physical"}],
                }],
            }],
        }
        with self.assertRaises(ValueError):
            app._validate_raw_config(raw)

    def test_library_without_safety_path_is_rejected(self):
        raw = {
            "plex_instances": [{
                "name": "Plex",
                "url": "http://plex:32400",
                "libraries": [{
                    "name": "Movies",
                    "cron": "0 * * * *",
                    "paths": [],
                }],
            }],
        }
        with self.assertRaisesRegex(ValueError, "at least one"):
            app._validate_raw_config(raw)

    def test_duplicate_plex_machine_identifier_is_rejected(self):
        raw = {
            "plex_instances": [
                {
                    "name": "Plex",
                    "machine_id": "server-123",
                    "url": "http://plex:32400",
                    "libraries": [],
                },
                {
                    "name": "Plex Backup",
                    "machine_id": "server-123",
                    "url": "http://plex-backup:32400",
                    "libraries": [],
                },
            ],
        }
        with self.assertRaisesRegex(ValueError, "Duplicate Plex server identifier"):
            app._validate_raw_config(raw)

    def test_live_apply_reconciles_jobs_and_removed_libraries(self):
        directory = str(Path("tests").resolve())
        old_path = app.CONFIG_PATH
        app.CONFIG_PATH = str(Path("tests/.runtime-config.yml").resolve())
        try:
            raw = {
                "plex_instances": [{
                    "name": "Plex",
                    "url": "http://plex:32400",
                    "token": "x",
                    "libraries": [{
                        "name": "Movies",
                        "type": "physical",
                        "cron": "0 * * * *",
                        "paths": [{
                            "path": directory,
                            "type": "physical",
                            "min_threshold": 90,
                        }],
                    }],
                }],
            }
            app._save_and_apply(raw)
            self.assertIsNotNone(app.scheduler.get_job("Plex::Movies"))
            raw["plex_instances"][0]["libraries"] = []
            app._save_and_apply(raw)
            self.assertIsNone(app.scheduler.get_job("Plex::Movies"))
            self.assertEqual(app.config.instances[0].libraries, [])
        finally:
            app.CONFIG_PATH = old_path
            Path("tests/.runtime-config.yml").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
