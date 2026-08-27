import json
import os
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

import app
from src.config import AppConfig, AppUser, MarkWatchedConfig
from src.auth import hash_password
from src.sonarr_client import (
    SonarrClient, SonarrConnectionStore, SonarrError, WEBHOOK_NAME,
    normalize_callback_url,
)


def webhook_schema():
    return {
        "name": "Webhook",
        "implementation": "Webhook",
        "implementationName": "Webhook",
        "configContract": "WebhookSettings",
        "fields": [
            {"name": "url", "value": ""},
            {"name": "method", "value": 1},
            {"name": "username", "value": ""},
            {"name": "password", "value": ""},
            {"name": "headers", "value": []},
        ],
        "onGrab": False,
        "onDownload": False,
        "onUpgrade": False,
        "onImportComplete": False,
        "onRename": False,
        "tags": [],
    }


class FakeResponse:
    def __init__(self, value, status_code=200):
        self.value = value
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.content = b"" if value is None else json.dumps(value).encode()

    def json(self):
        return self.value


class FakeSession:
    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class SonarrClientTests(unittest.TestCase):
    def test_http_error_includes_safe_sonarr_validation_message(self):
        session = FakeSession([FakeResponse([{
            "propertyName": "",
            "errorMessage": (
                "Unable to send test message: callback returned 401 for api-sensitive"
            ),
            "attemptedValue": "must-not-display",
        }], 400)])
        client = SonarrClient("http://sonarr:8989", "api-sensitive", session=session)
        with self.assertRaisesRegex(
            SonarrError, "Unable to send test message: callback returned 401",
        ) as raised:
            client._request("POST", "/notification/test", {})
        self.assertNotIn("must-not-display", str(raised.exception))
        self.assertNotIn("api-sensitive", str(raised.exception))

    def test_provision_creates_and_tests_schema_driven_webhook(self):
        session = FakeSession([
            FakeResponse({"version": "5.0.1", "instanceName": "TV"}),
            FakeResponse([webhook_schema()]),
            FakeResponse([]),
            FakeResponse(None),
            FakeResponse({"id": 42}),
        ])
        client = SonarrClient("http://sonarr:8989", "api-sensitive", session=session)
        result = client.provision_webhook(
            "http://mediamender:8222", "webhook-sensitive",
        )

        self.assertEqual(result["action"], "created")
        self.assertEqual(result["notification_id"], 42)
        self.assertEqual(session.headers["X-Api-Key"], "api-sensitive")
        self.assertEqual(
            [(call[0], call[1].split("/api/v3", 1)[1]) for call in session.calls],
            [("GET", "/system/status"), ("GET", "/notification/schema"),
             ("GET", "/notification"), ("POST", "/notification/test"),
             ("POST", "/notification")],
        )
        test_payload = session.calls[3][2]["json"]
        fields = {field["name"]: field["value"] for field in test_payload["fields"]}
        self.assertEqual(test_payload["name"], WEBHOOK_NAME)
        self.assertTrue(test_payload["onDownload"])
        self.assertTrue(test_payload["onUpgrade"])
        self.assertFalse(test_payload["onImportComplete"])
        self.assertEqual(fields["url"], "http://mediamender:8222/api/webhooks/sonarr")
        self.assertEqual(fields["headers"], [{
            "key": "X-Sonarr-Webhook-Secret", "value": "webhook-sensitive",
        }])
        self.assertNotIn("api-sensitive", json.dumps(test_payload))

    def test_provision_updates_existing_managed_webhook(self):
        existing = {"id": 7, "name": WEBHOOK_NAME, "implementation": "Webhook"}
        session = FakeSession([
            FakeResponse({"version": "4.0.0"}),
            FakeResponse([webhook_schema()]),
            FakeResponse([existing]),
            FakeResponse(None),
            FakeResponse({"id": 7}),
        ])
        result = SonarrClient(
            "http://sonarr:8989", "key", session=session,
        ).provision_webhook("http://mediamender:8222/api/webhooks/sonarr", "secret")
        self.assertEqual(result["action"], "updated")
        self.assertEqual(session.calls[-1][0], "PUT")
        self.assertTrue(session.calls[-1][1].endswith("/notification/7"))
        self.assertEqual(session.calls[-1][2]["json"]["id"], 7)

    def test_reconnect_tests_with_the_existing_id_so_the_name_stays_unique(self):
        """Sonarr validates a test payload as though it were being saved, and its Name uniqueness
        rule only excludes the record carrying the same Id. Testing an unidentified payload named
        after a connection that already exists is rejected with "Should be unique" — which made the
        first connect succeed and every later one fail permanently."""
        existing = {"id": 7, "name": WEBHOOK_NAME, "implementation": "Webhook"}

        class UniqueNameSession(FakeSession):
            def request(self, method, url, **kwargs):
                if url.endswith("/notification/test"):
                    payload = kwargs.get("json") or {}
                    if payload.get("name") == existing["name"] and payload.get("id") != existing["id"]:
                        self.calls.append((method, url, kwargs))
                        self.responses.pop(0)
                        return FakeResponse(
                            [{"propertyName": "Name", "errorMessage": "Should be unique"}], 400,
                        )
                return super().request(method, url, **kwargs)

        session = UniqueNameSession([
            FakeResponse({"version": "4.0.0"}),
            FakeResponse([webhook_schema()]),
            FakeResponse([existing]),
            FakeResponse(None),
            FakeResponse({"id": 7}),
        ])
        result = SonarrClient(
            "http://sonarr:8989", "key", session=session,
        ).provision_webhook("http://mediamender:8222/api/webhooks/sonarr", "secret")

        self.assertEqual(result["action"], "updated")
        tested = next(
            call for call in session.calls if call[1].endswith("/notification/test")
        )
        self.assertEqual(tested[2]["json"]["id"], 7)

    def test_provision_adds_connection_identity_header(self):
        session = FakeSession([
            FakeResponse({"version": "5.0.1"}),
            FakeResponse([webhook_schema()]), FakeResponse([]),
            FakeResponse(None), FakeResponse({"id": 42}),
        ])
        SonarrClient(
            "http://sonarr:8989", "key", session=session,
        ).provision_webhook(
            "http://mediamender:8222", "secret",
            connection_id="connection-1",
        )
        fields = {
            field["name"]: field["value"]
            for field in session.calls[3][2]["json"]["fields"]
        }
        self.assertIn({
            "key": "X-MediaMender-Connection-ID", "value": "connection-1",
        }, fields["headers"])

    def test_remove_deletes_only_mediamender_webhooks(self):
        session = FakeSession([
            FakeResponse([
                {"id": 7, "name": WEBHOOK_NAME, "implementation": "Webhook"},
                {"id": 8, "name": "Another webhook", "implementation": "Webhook"},
            ]),
            FakeResponse(None, 204),
        ])
        removed = SonarrClient(
            "http://sonarr:8989", "key", session=session,
        ).remove_webhook()
        self.assertEqual(removed, 1)
        self.assertEqual(session.calls[-1][0], "DELETE")
        self.assertTrue(session.calls[-1][1].endswith("/notification/7"))

    def test_callback_path_is_fixed_and_private_hosts_are_allowed(self):
        self.assertEqual(
            normalize_callback_url("http://192.168.1.20:8222"),
            "http://192.168.1.20:8222/api/webhooks/sonarr",
        )
        with self.assertRaisesRegex(ValueError, "/api/webhooks/sonarr"):
            normalize_callback_url("http://mediamender:8222/not-the-webhook")

    def test_status_store_never_contains_api_or_webhook_secrets(self):
        root = Path("tests/.sonarr-status-runtime")
        root.mkdir(exist_ok=True)
        try:
            store = SonarrConnectionStore(str(root))
            store.success("http://sonarr:8989", {
                "action": "created", "notification_id": 9,
                "callback_url": "http://mediamender:8222/api/webhooks/sonarr",
                "sonarr_version": "5.0.1", "sonarr_instance": "TV",
                "api_key": "must-not-persist", "secret": "also-not-persist",
            })
            content = store.path.read_text(encoding="utf-8")
        finally:
            (root / "sonarr-webhook.json").unlink(missing_ok=True)
            root.rmdir()
        self.assertNotIn("must-not-persist", content)
        self.assertNotIn("also-not-persist", content)

    def test_status_store_tracks_multiple_instances_and_owners(self):
        root = Path("tests/.sonarr-status-runtime")
        root.mkdir(exist_ok=True)
        try:
            store = SonarrConnectionStore(str(root))
            first = store.prepare("http://sonarr-anime:8989", "alice")
            store.success("http://sonarr-anime:8989", {
                "action": "created", "sonarr_instance": "Anime",
                "callback_url": "http://mediamender:8222/api/webhooks/sonarr",
            }, owner="alice", connection_id=first["connection_id"])
            second = store.prepare("http://sonarr-unlimited:8989", "bob")
            store.success("http://sonarr-unlimited:8989", {
                "action": "created", "sonarr_instance": "Unlimited",
                "callback_url": "http://mediamender:8222/api/webhooks/sonarr",
            }, owner="bob", connection_id=second["connection_id"])
            status = store.status()
            self.assertEqual(len(status["connections"]), 2)
            self.assertEqual(store.owner_for(first["connection_id"]), "alice")
            self.assertEqual(store.owner_for(second["connection_id"]), "bob")
            self.assertEqual(
                store.get("http://sonarr-anime:8989")["sonarr_instance"], "Anime",
            )
            self.assertTrue(store.remove("http://sonarr-anime:8989"))
            self.assertIsNone(store.get("http://sonarr-anime:8989"))
        finally:
            (root / "sonarr-webhook.json").unlink(missing_ok=True)
            root.rmdir()

    def test_status_store_ignores_malformed_connection_records(self):
        root = Path("tests/.sonarr-status-runtime")
        root.mkdir(exist_ok=True)
        try:
            path = root / "sonarr-webhook.json"
            path.write_text(json.dumps({"connections": {
                "http://bad": "not-a-record",
                "http://good": {"sonarr_url": "http://good", "status": "connected"},
            }}), encoding="utf-8")
            status = SonarrConnectionStore(str(root)).status()
        finally:
            (root / "sonarr-webhook.json").unlink(missing_ok=True)
            root.rmdir()
        self.assertEqual(len(status["connections"]), 1)
        self.assertEqual(status["connections"][0]["sonarr_url"], "http://good")


class SonarrProvisioningApiTests(unittest.TestCase):
    def _client(self, role="admin", permissions=None):
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session.update({
                "authenticated": True,
                "username": role,
                "role": role,
                "permissions": permissions or ["*"],
                "_csrf_token": "known-token",
            })
        return client

    def test_status_endpoint_remains_bound_to_status_handler(self):
        store = Mock()
        store.status.return_value = {"connections": []}
        with patch.object(app, "sonarr_connection", store):
            response = self._client().get("/api/mark-watched/sonarr")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["connections"], [])

    def test_url_paired_environment_keys_are_isolated_per_instance(self):
        environment = {
            "SONARR_MAIN_URL": "http://sonarr:8989/",
            "SONARR_MAIN_API_KEY": "main-key",
            "SONARR_UNLIMITED_URL": "http://sonarr-unlimited:8989",
            "SONARR_UNLIMITED_API_KEY": "unlimited-key",
            "SONARR_API_KEY": "fallback-key",
        }
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(
                app._sonarr_api_key({}, "http://sonarr:8989"), "main-key",
            )
            self.assertEqual(
                app._sonarr_api_key({}, "http://sonarr-unlimited:8989"),
                "unlimited-key",
            )
            self.assertEqual(
                app._sonarr_api_key({}, "http://sonarr-anime:8989"),
                "fallback-key",
            )
            self.assertEqual(
                app._sonarr_api_key(
                    {"api_key": "typed-key"}, "http://sonarr:8989",
                ),
                "typed-key",
            )

    def test_status_reports_environment_key_availability_per_connection(self):
        store = Mock()
        store.status.return_value = {"connections": [
            {"sonarr_url": "http://sonarr:8989"},
            {"sonarr_url": "http://sonarr-anime:8989"},
        ]}
        environment = {
            "SONARR_MAIN_URL": "http://sonarr:8989",
            "SONARR_MAIN_API_KEY": "main-key",
        }
        with patch.dict(os.environ, environment, clear=True), \
             patch.object(app, "sonarr_connection", store):
            response = self._client().get("/api/mark-watched/sonarr")
        connections = response.get_json()["connections"]
        self.assertTrue(connections[0]["api_key_available"])
        self.assertFalse(connections[1]["api_key_available"])

    def test_status_failure_is_returned_as_json(self):
        with patch.object(
            app, "_mark_watched_sonarr_status_response",
            side_effect=RuntimeError("broken status"),
        ):
            response = self._client().get("/api/mark-watched/sonarr")
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.is_json)
        self.assertIn("broken status", response.get_json()["error"])

    def test_status_lists_environment_instances_before_they_are_connected(self):
        store = Mock()
        store.status.return_value = {"connections": []}
        environment = {
            "SONARR_MAIN_URL": "http://sonarr:8989",
            "SONARR_MAIN_API_KEY": "main-key",
            "SONARR_ANIME_URL": "http://sonarr-anime:8989",
            "SONARR_ANIME_API_KEY": "anime-key",
        }
        with patch.dict(os.environ, environment, clear=True), \
             patch.object(app, "sonarr_connection", store):
            response = self._client().get("/api/mark-watched/sonarr")
        connections = response.get_json()["connections"]
        self.assertEqual(
            {item["sonarr_url"] for item in connections},
            {"http://sonarr:8989", "http://sonarr-anime:8989"},
        )
        self.assertTrue(all(item["status"] == "not_connected" for item in connections))
        self.assertTrue(all(item["configured_from_environment"] for item in connections))
        self.assertTrue(all(item["api_key_available"] for item in connections))
        self.assertNotIn("main-key", response.get_data(as_text=True))
        self.assertNotIn("anime-key", response.get_data(as_text=True))

    def test_status_merges_saved_connection_with_environment_instance(self):
        store = Mock()
        store.status.return_value = {"connections": [{
            "sonarr_url": "http://sonarr:8989", "status": "connected",
            "sonarr_instance": "Main Sonarr", "notification_id": 42,
        }]}
        environment = {
            "SONARR_MAIN_URL": "http://sonarr:8989",
            "SONARR_MAIN_API_KEY": "main-key",
        }
        with patch.dict(os.environ, environment, clear=True), \
             patch.object(app, "sonarr_connection", store):
            response = self._client().get("/api/mark-watched/sonarr")
        connections = response.get_json()["connections"]
        self.assertEqual(len(connections), 1)
        self.assertEqual(connections[0]["status"], "connected")
        self.assertTrue(connections[0]["saved_record"])
        self.assertTrue(connections[0]["configured_from_environment"])

    def test_missing_key_names_the_exact_suggested_environment_pair(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(app, "SonarrClient") as constructor:
            response = self._client().post(
                "/api/mark-watched/sonarr/connect",
                json={"sonarr_url": "http://sonarr-unlimited:8989",
                      "callback_url": "http://mediamender:8222"},
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("SONARR_UNLIMITED_URL", response.get_json()["error"])
        self.assertIn("SONARR_UNLIMITED_API_KEY", response.get_json()["error"])
        constructor.assert_not_called()

    def test_connect_uses_url_paired_environment_key(self):
        sonarr = Mock()
        sonarr.system_status.return_value = {"version": "5.0.1"}
        sonarr.provision_webhook.return_value = {
            "action": "created", "notification_id": 12,
            "callback_url": "http://mediamender:8222/api/webhooks/sonarr",
        }
        store = Mock()
        store.prepare.return_value = {"connection_id": "connection-1"}
        store.success.return_value = {"status": "connected"}
        environment = {
            "SONARR_ANIME_URL": "http://sonarr-anime:8989",
            "SONARR_ANIME_API_KEY": "anime-key",
        }
        with patch.dict(os.environ, environment, clear=True), \
             patch.object(app, "SonarrClient", return_value=sonarr) as constructor, \
             patch.object(app, "sonarr_connection", store), \
             patch.object(app, "_ensure_sonarr_webhook_secret", return_value="secret"):
            response = self._client().post(
                "/api/mark-watched/sonarr/connect",
                json={"sonarr_url": "http://sonarr-anime:8989",
                      "callback_url": "http://mediamender:8222"},
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 200)
        constructor.assert_called_once_with("http://sonarr-anime:8989", "anime-key")

    def test_connect_is_available_to_the_authenticated_mark_watched_user(self):
        config = AppConfig(instances=[], users=[AppUser(
            "user", hash_password("password123"), "user", ["mark_watched"],
        )])
        sonarr = Mock()
        sonarr.system_status.return_value = {"version": "5.0.1"}
        sonarr.provision_webhook.return_value = {
            "action": "created", "notification_id": 12,
            "callback_url": "http://mediamender:8222/api/webhooks/sonarr",
            "sonarr_version": "5.0.1", "sonarr_instance": "TV",
        }
        store = Mock()
        store.prepare.return_value = {"connection_id": "connection-1"}
        store.success.return_value = {"status": "connected"}
        with patch.object(app, "config", config), \
             patch.object(app, "SonarrClient", return_value=sonarr), \
             patch.object(app, "sonarr_connection", store), \
             patch.object(app, "_ensure_sonarr_webhook_secret", return_value="secret"):
            response = self._client("user", ["mark_watched"]).post(
                "/api/mark-watched/sonarr/connect",
                json={"sonarr_url": "http://sonarr:8989", "api_key": "key",
                      "callback_url": "http://mediamender:8222"},
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 200)
        store.prepare.assert_called_once_with("http://sonarr:8989", "user")

    def test_connect_uses_key_once_and_returns_no_secrets(self):
        config = AppConfig(
            instances=[], mark_watched=MarkWatchedConfig(webhook_secret="webhook-secret"),
        )
        client = Mock()
        client.system_status.return_value = {"version": "5.0.1"}
        client.provision_webhook.return_value = {
            "action": "created", "notification_id": 12,
            "callback_url": "http://mediamender:8222/api/webhooks/sonarr",
            "sonarr_version": "5.0.1", "sonarr_instance": "TV",
        }
        store = Mock()
        store.prepare.return_value = {"connection_id": "connection-1"}
        store.success.return_value = {"status": "connected", "notification_id": 12}
        with patch.object(app, "config", config), \
             patch.object(app, "SonarrClient", return_value=client) as constructor, \
             patch.object(app, "sonarr_connection", store):
            response = self._client().post(
                "/api/mark-watched/sonarr/connect",
                json={"sonarr_url": "http://sonarr:8989", "api_key": "api-sensitive",
                      "callback_url": "http://mediamender:8222"},
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 200)
        constructor.assert_called_once_with("http://sonarr:8989", "api-sensitive")
        client.provision_webhook.assert_called_once_with(
            "http://mediamender:8222/api/webhooks/sonarr", "webhook-secret",
            status={"version": "5.0.1"},
            connection_id="connection-1",
        )
        store.success.assert_called_once_with(
            "http://sonarr:8989", client.provision_webhook.return_value,
            owner="default", connection_id="connection-1",
        )
        body = response.get_data(as_text=True)
        self.assertNotIn("api-sensitive", body)
        self.assertNotIn("webhook-secret", body)

    def test_connect_generates_webhook_secret_when_missing(self):
        root = Path("tests/.sonarr-settings-runtime")
        root.mkdir(exist_ok=True)
        try:
            config_path = root / "config.yml"
            config_path.write_text(yaml.safe_dump({"mark_watched": {}}), encoding="utf-8")
            runtime = AppConfig(instances=[], mark_watched=MarkWatchedConfig())
            client = Mock()
            client.system_status.return_value = {"version": "5.0.1"}
            client.provision_webhook.return_value = {
                "action": "created", "notification_id": 12,
                "callback_url": "http://mediamender:8222/api/webhooks/sonarr",
                "sonarr_version": "5.0.1", "sonarr_instance": "Sonarr",
            }
            store = Mock()
            store.prepare.return_value = {"connection_id": "connection-1"}
            store.success.return_value = {"status": "connected"}
            with patch.object(app, "CONFIG_PATH", str(config_path)), \
                 patch.object(app, "config", runtime), \
                 patch.object(app, "SonarrClient", return_value=client), \
                 patch.object(app, "sonarr_connection", store), \
                 patch.object(app, "_save_and_apply") as save:
                response = self._client().post(
                    "/api/mark-watched/sonarr/connect",
                    json={"sonarr_url": "http://sonarr:8989", "api_key": "key",
                          "callback_url": "http://mediamender:8222"},
                    headers={"X-CSRF-Token": "known-token"},
                )
        finally:
            config_path.unlink(missing_ok=True)
            root.rmdir()
        self.assertEqual(response.status_code, 200)
        generated = save.call_args.args[0]["mark_watched"]["webhook_secret"]
        self.assertGreaterEqual(len(generated), 32)
        self.assertEqual(client.provision_webhook.call_args.args[1], generated)

    def test_remove_connected_instance_deletes_remote_webhook_then_status(self):
        store = Mock()
        store.get.return_value = {
            "sonarr_url": "http://sonarr:8989", "status": "connected",
            "notification_id": 12,
        }
        sonarr = Mock()
        sonarr.remove_webhook.return_value = 1
        with patch.object(app, "sonarr_connection", store), \
             patch.object(app, "SonarrClient", return_value=sonarr) as constructor:
            response = self._client().delete(
                "/api/mark-watched/sonarr",
                json={"sonarr_url": "http://sonarr:8989", "api_key": "one-time-key"},
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 200)
        constructor.assert_called_once_with("http://sonarr:8989", "one-time-key")
        sonarr.remove_webhook.assert_called_once_with()
        store.remove.assert_called_once_with("http://sonarr:8989")

    def test_remove_failed_instance_needs_no_api_key(self):
        store = Mock()
        store.get.return_value = {
            "sonarr_url": "http://sonarr:8989", "status": "failed",
        }
        with patch.object(app, "sonarr_connection", store), \
             patch.object(app, "SonarrClient") as constructor:
            response = self._client().delete(
                "/api/mark-watched/sonarr",
                json={"sonarr_url": "http://sonarr:8989"},
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 200)
        constructor.assert_not_called()
        store.remove.assert_called_once_with("http://sonarr:8989")


if __name__ == "__main__":
    unittest.main()
