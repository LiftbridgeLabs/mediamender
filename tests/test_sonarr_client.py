import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

import app
from src.config import AppConfig, AppUser, MarkWatchedConfig
from src.auth import hash_password
from src.sonarr_client import (
    SonarrClient, SonarrConnectionStore, WEBHOOK_NAME,
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
    def test_provision_creates_and_tests_schema_driven_webhook(self):
        session = FakeSession([
            FakeResponse({"version": "5.0.1", "instanceName": "TV"}),
            FakeResponse([webhook_schema()]),
            FakeResponse(None),
            FakeResponse([]),
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
             ("POST", "/notification/test"), ("GET", "/notification"),
             ("POST", "/notification")],
        )
        test_payload = session.calls[2][2]["json"]
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
            FakeResponse(None),
            FakeResponse([existing]),
            FakeResponse({"id": 7}),
        ])
        result = SonarrClient(
            "http://sonarr:8989", "key", session=session,
        ).provision_webhook("http://mediamender:8222/api/webhooks/sonarr", "secret")
        self.assertEqual(result["action"], "updated")
        self.assertEqual(session.calls[-1][0], "PUT")
        self.assertTrue(session.calls[-1][1].endswith("/notification/7"))
        self.assertEqual(session.calls[-1][2]["json"]["id"], 7)

    def test_provision_adds_connection_identity_header(self):
        session = FakeSession([
            FakeResponse({"version": "5.0.1"}),
            FakeResponse([webhook_schema()]), FakeResponse(None),
            FakeResponse([]), FakeResponse({"id": 42}),
        ])
        SonarrClient(
            "http://sonarr:8989", "key", session=session,
        ).provision_webhook(
            "http://mediamender:8222", "secret",
            connection_id="connection-1",
        )
        fields = {
            field["name"]: field["value"]
            for field in session.calls[2][2]["json"]["fields"]
        }
        self.assertIn({
            "key": "X-MediaMender-Connection-ID", "value": "connection-1",
        }, fields["headers"])

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
        finally:
            (root / "sonarr-webhook.json").unlink(missing_ok=True)
            root.rmdir()


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

    def test_connect_is_admin_only(self):
        config = AppConfig(instances=[], users=[AppUser(
            "user", hash_password("password123"), "user", ["mark_watched"],
        )])
        with patch.object(app, "config", config):
            response = self._client("user", ["mark_watched"]).post(
                "/api/mark-watched/sonarr/connect",
                json={"sonarr_url": "http://sonarr:8989", "api_key": "key",
                      "callback_url": "http://mediamender:8222"},
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 403)

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


if __name__ == "__main__":
    unittest.main()
