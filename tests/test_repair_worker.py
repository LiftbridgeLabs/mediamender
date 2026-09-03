import json
import os
import sqlite3
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import app
from ui_source import rendered_ui
from src.config import (AppConfig, PlexInstanceConfig, RepairWorkerConfig,
                        TimestampRepairConfig, parse_config)
from src.worker_auth import SignatureVerifier, signed_headers
from worker import create_worker_app


class WorkerSignatureTests(unittest.TestCase):
    def test_signature_rejects_tampering_and_replay(self):
        secret = "s" * 32
        body = b'{"folder":"/approved"}'
        headers = signed_headers(
            secret, "worker", "POST", "/api/v1/run", body,
            now=1000, nonce="unique-nonce",
        )
        verifier = SignatureVerifier()
        self.assertTrue(verifier.verify(
            secret, "worker", "POST", "/api/v1/run", body, headers, now=1000,
        )[0])
        self.assertFalse(verifier.verify(
            secret, "worker", "POST", "/api/v1/run", body, headers, now=1000,
        )[0])
        fresh = signed_headers(
            secret, "worker", "POST", "/api/v1/run", body,
            now=1000, nonce="fresh-nonce",
        )
        self.assertFalse(verifier.verify(
            secret, "worker", "POST", "/api/v1/run", b"tampered", fresh,
            now=1000,
        )[0])


class RepairWorkerApiTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / ".runtime-repair-worker"
        self.database_root = self.root / "database"
        self.media_root = self.root / "media"
        self.data_root = self.root / "data"
        self.database_root.mkdir(parents=True, exist_ok=True)
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.database = self.database_root / "com.plexapp.plugins.library.db"
        self.database.unlink(missing_ok=True)
        media_file = str(self.media_root / "Movie" / "Movie.mkv")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript("""
                CREATE TABLE metadata_items (
                    id INTEGER PRIMARY KEY, library_section_id INTEGER,
                    parent_id INTEGER, title TEXT
                );
                CREATE TABLE media_items (
                    id INTEGER PRIMARY KEY, metadata_item_id INTEGER
                );
                CREATE TABLE media_parts (
                    id INTEGER PRIMARY KEY, media_item_id INTEGER,
                    file TEXT, updated_at INTEGER, deleted_at INTEGER
                );
                INSERT INTO metadata_items VALUES (10, 2, NULL, 'Movie');
                INSERT INTO media_items VALUES (20, 10);
            """)
            connection.execute(
                "INSERT INTO media_parts VALUES (30, 20, ?, -5, NULL)",
                (media_file,),
            )
            connection.commit()
        self.secret = "worker-secret-" * 3
        with patch.dict(os.environ, {
            "MEDIAMENDER_WORKER_DATABASE_ROOTS": str(self.database_root),
            "MEDIAMENDER_WORKER_MEDIA_ROOTS": str(self.media_root),
        }):
            worker_app = create_worker_app(
                self.secret, "altmount-worker", str(self.data_root),
                register_recovery_check=False,
            )
        self.worker_app = worker_app
        self.client = worker_app.test_client()

    def _request(self, method: str, path: str, payload=None, nonce=None):
        body = b"" if payload is None else json.dumps(
            payload, separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        headers = signed_headers(
            self.secret, "altmount-worker", method, path, body, nonce=nonce,
        )
        if body:
            headers["Content-Type"] = "application/json"
        return self.client.open(path, method=method, data=body or None, headers=headers)

    def test_health_requires_signature_and_reports_roots(self):
        self.assertEqual(self.client.get("/api/v1/health").status_code, 401)
        response = self._request("GET", "/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["role"], "repair-worker")
        self.assertIn(str(self.media_root), response.get_json()["media_roots"])

    def test_worker_discovers_mounted_database_and_audits_locally(self):
        discovered = self._request("GET", "/api/v1/databases").get_json()
        self.assertEqual(discovered["databases"], [str(self.database)])
        payload = {
            "instance": "vm-altmount",
            "libraries": [{"name": "Movies", "section_id": "2"}],
            "repair": {
                "database_path": str(self.database),
                "allowed_prefixes": [str(self.media_root)],
            },
        }
        response = self._request("POST", "/api/v1/audit", payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["distinct_files"], 0)
        self.assertEqual(
            response.get_json()["path_state_counts"]["missing_path"], 1,
        )

    def test_worker_rejects_controller_supplied_paths_outside_mount_roots(self):
        payload = {
            "instance": "vm-altmount", "libraries": [],
            "repair": {
                "database_path": str(self.database),
                "allowed_prefixes": [str(self.root / "not-mounted")],
            },
        }
        response = self._request("POST", "/api/v1/audit", payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("writable media roots", response.get_json()["error"])

    def test_worker_requests_controller_scan_without_receiving_plex_token(self):
        payload = {
            "instance": "vm-altmount",
            "libraries": [{"name": "Movies", "section_id": "2"}],
            "repair": {
                "database_path": str(self.database),
                "allowed_prefixes": [str(self.media_root)],
            },
            "run_id": "approved-run",
            "controller_url": "http://controller:8222",
            "library_section_id": "2",
            "folder": str(self.media_root / "Movie"),
            "expected_files": [str(self.media_root / "Movie" / "Movie.mkv")],
        }
        controller_response = Mock(status_code=200, ok=True)
        controller_response.json.return_value = {"ok": True, "http": 200}

        def exercise_proxy(_instance, _library, _config, plex, folder, **_kwargs):
            return plex.scan_path("2", folder)

        with patch.object(
            self.worker_app.repair_manager, "run_folder", side_effect=exercise_proxy,
        ), patch("worker.requests.post", return_value=controller_response) as post:
            response = self._request("POST", "/api/v1/run", payload)
        self.assertEqual(response.status_code, 200)
        callback_body = post.call_args.kwargs["data"]
        self.assertNotIn(b"token", callback_body.lower())
        callback_headers = post.call_args.kwargs["headers"]
        verifier = SignatureVerifier()
        self.assertTrue(verifier.verify(
            self.secret, "altmount-worker", "POST",
            "/api/timestamp-repair/worker-scan/approved-run",
            callback_body, callback_headers,
        )[0])


class RepairWorkerControllerTests(unittest.TestCase):
    def test_settings_payload_persists_repair_assignment(self):
        built = app._build_instance_cfg({
            "name": "vm-altmount", "url": "http://plex:32400",
            "token": "token", "libraries": [],
            "timestamp_repair": {
                "enabled": True, "worker": "altmount-worker",
                "database_path": "/plex-db/db",
                "allowed_prefixes": ["/links/provider"],
            },
        }, True, [])
        self.assertTrue(built["timestamp_repair"]["enabled"])
        self.assertEqual(built["timestamp_repair"]["worker"],
                         "altmount-worker")

    def test_settings_save_is_blocked_during_active_repair(self):
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "known-token"
        with patch.object(app.timestamp_repair, "status", return_value={
            "running": True, "active_transaction": {"state": "renamed"},
        }):
            response = client.post(
                "/api/wizard/save", json={"instances": []},
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("cannot change", response.get_json()["error"])

    def test_worker_config_parses_and_instance_routes_to_it(self):
        parsed = parse_config({
            "timestamp_repair_workers": [{
                "name": "altmount-worker", "url": "http://vm:8223",
                "controller_url": "http://unraid:8222", "token": "x" * 32,
            }],
            "plex_instances": [{
                "name": "vm-altmount", "url": "http://plex:32400",
                "libraries": [],
                "timestamp_repair": {
                    "enabled": True, "worker": "altmount-worker",
                    "database_path": "/plex-db/com.plexapp.plugins.library.db",
                    "allowed_prefixes": ["/mnt/symlinks"],
                },
            }],
        })
        self.assertEqual(parsed.instances[0].timestamp_repair.worker,
                         "altmount-worker")
        self.assertEqual(parsed.repair_workers[0].controller_url,
                         "http://unraid:8222")

    def test_validation_rejects_enabled_unknown_worker(self):
        raw = {
            "plex_instances": [{
                "name": "Plex", "url": "http://plex:32400", "libraries": [],
                "timestamp_repair": {
                    "enabled": True, "worker": "missing-worker",
                    "database_path": "/plex-db/db", "allowed_prefixes": ["/links"],
                },
            }],
        }
        with self.assertRaisesRegex(ValueError, "not configured"):
            app._validate_raw_config(raw)

    def test_worker_scan_callback_is_limited_to_approved_folder(self):
        worker = RepairWorkerConfig(
            "altmount-worker", "http://worker:8223", "z" * 32,
            "http://controller:8222",
        )
        plex = Mock()
        plex.scan_path.return_value = {"ok": True, "http": 200}
        context = {
            "worker": worker.name, "instance": "vm-altmount",
            "section_id": "2", "folder": "/approved", "plex": plex,
        }
        path = "/api/timestamp-repair/worker-scan/run-id"
        payload = {"section_id": "2", "folder": "/different"}
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        headers = signed_headers(worker.token, worker.name, "POST", path, body)
        headers["Content-Type"] = "application/json"
        with patch.object(app, "config", AppConfig(
            instances=[], repair_workers=[worker],
        )), app._remote_repair_lock:
            app._worker_scan_contexts["run-id"] = context
            response = app.app.test_client().post(path, data=body, headers=headers)
            app._worker_scan_contexts.pop("run-id", None)
        self.assertEqual(response.status_code, 403)
        plex.scan_path.assert_not_called()

    def test_unconfigured_unreachable_worker_does_not_invent_recovery(self):
        worker = RepairWorkerConfig(
            "altmount-worker", "http://worker:8223", "z" * 32,
            "http://controller:8222",
        )
        instance = Mock()
        instance.name = "vm-altmount"
        instance.timestamp_repair = TimestampRepairConfig(
            enabled=True, worker=worker.name,
            database_path="/plex-db/db", allowed_prefixes=["/links"],
        )
        with patch.object(app, "config", AppConfig(
            instances=[instance], repair_workers=[worker],
        )), patch(
            "app.RepairWorkerClient.status", side_effect=RuntimeError("offline"),
        ), patch.dict(app._remote_pending_repair, {}, clear=True):
            app._worker_recovery_cache.clear()
            self.assertFalse(app._combined_recovery_required("anything"))
            self.assertFalse(app._combined_recovery_required(
                "vm-altmount", "empty_trash",
            ))

    def test_unreachable_worker_only_blocks_trash_after_a_repair_was_dispatched(self):
        worker = RepairWorkerConfig(
            "altmount-worker", "http://worker:8223", "z" * 32,
            "http://controller:8222",
        )
        instance = PlexInstanceConfig(
            "vm-altmount", "http://plex", "token", [],
            timestamp_repair=TimestampRepairConfig(
                enabled=True, worker=worker.name,
                database_path="/plex-db/db", allowed_prefixes=["/links"],
            ),
        )
        pending = {"worker": worker.name, "instance": instance.name}
        with patch.object(app, "config", AppConfig(
            instances=[instance], repair_workers=[worker],
        )), patch(
            "app.RepairWorkerClient.status", side_effect=RuntimeError("offline"),
        ), patch.dict(app._remote_pending_repair, pending, clear=True):
            app._worker_recovery_cache.clear()
            self.assertTrue(app._combined_recovery_required(
                "vm-altmount", "empty_trash",
            ))

    def test_remote_dispatch_marker_is_persisted_and_cleared(self):
        marker = Path(__file__).parent / ".runtime-controller-remote-active.json"
        marker.unlink(missing_ok=True)
        with patch.object(app, "_remote_pending_path", marker), \
             patch.dict(app._remote_pending_repair, {}, clear=True):
            app._set_remote_pending({
                "worker": "altmount-worker", "instance": "vm-altmount",
            })
            self.assertEqual(
                json.loads(marker.read_text(encoding="utf-8"))["instance"],
                "vm-altmount",
            )
            app._clear_remote_pending("altmount-worker")
            self.assertFalse(marker.exists())

    def test_remote_recovery_is_scoped_for_empty_trash_but_global_for_repairs(self):
        worker = RepairWorkerConfig(
            "altmount-worker", "http://worker:8223", "z" * 32,
            "http://controller:8222",
        )
        remote = PlexInstanceConfig(
            "vm-altmount", "http://plex", "token", [],
            timestamp_repair=TimestampRepairConfig(
                enabled=True, worker=worker.name,
                database_path="/plex-db/db", allowed_prefixes=["/links"],
            ),
        )
        local = PlexInstanceConfig(
            "Streamstead", "http://local-plex", "token", [],
            timestamp_repair=TimestampRepairConfig(enabled=False),
        )
        worker_status = {
            "active_transaction": {
                "instance": "vm-altmount", "state": "recovery_required",
            },
        }
        with patch.object(app, "config", AppConfig(
            instances=[local, remote], repair_workers=[worker],
        )), patch(
            "app.RepairWorkerClient.status", return_value=worker_status,
        ):
            app._worker_recovery_cache.clear()
            self.assertFalse(app._combined_recovery_required(
                "Streamstead", "empty_trash",
            ))
            self.assertTrue(app._combined_recovery_required(
                "vm-altmount", "empty_trash",
            ))
            self.assertTrue(app._combined_recovery_required(
                "Streamstead", "timestamp_repair",
            ))

    def test_settings_render_worker_setup_without_config_file_editing(self):
        html = rendered_ui(app.app.test_client())
        self.assertIn("Remote repair workers", html)
        self.assertIn("Copy worker Compose", html)
        self.assertIn("Discover databases", html)
        self.assertIn("MEDIAMENDER_ROLE=repair-worker", html)
        self.assertIn("MEDIAMENDER_WORKER_DATA_DIR:?Set an absolute", html)
        self.assertIn("MEDIAMENDER_WORKER_TOKEN:?Paste the pairing secret", html)
        self.assertNotIn("./mediamender-worker-data:/app/data", html)
        self.assertIn("Assign at least one Plex instance to this worker", html)
        self.assertIn("1. Add a worker", html)
        self.assertIn("Assign &amp; configure", html)
        self.assertIn("function assignRepairWorker", html)
        self.assertIn("scrollToRepairInstance", html)
        self.assertIn("Complete both path fields to unlock Compose", html)

    def test_repair_dashboard_uses_large_instance_metric_layout(self):
        html = rendered_ui(app.app.test_client())
        self.assertIn('class="repair-instance-grid"', html)
        self.assertIn("Affected files", html)
        self.assertIn("Files fixed", html)
        self.assertIn("Library items", html)
        self.assertIn("View issues", html)


if __name__ == "__main__":
    unittest.main()
