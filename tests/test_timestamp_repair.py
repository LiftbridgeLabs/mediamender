import json
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app
from ui_source import rendered_ui
from src.config import (AppConfig, LibraryConfig, PathConfig,
                        PlexInstanceConfig, RepairWorkerConfig,
                        TimestampRepairConfig, parse_config)
from src.timestamp_repair import (AffectedPart, TimestampRepairManager,
                                  _inside, temporary_name)
from src import runner


class TimestampRepairDetectionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / ".runtime-timestamp-detection"
        self.root.mkdir(exist_ok=True)
        self.database = self.root / "plex.db"
        self.database.unlink(missing_ok=True)
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
                INSERT INTO media_parts VALUES (30, 20, '/links/provider/Movie/Movie.mkv', -5, NULL);
                INSERT INTO media_parts VALUES (31, 20, '/links/provider/Movie/Movie.mkv', -6, NULL);
                INSERT INTO media_parts VALUES (32, 20, '/links/other/Other.mkv', -7, NULL);
                INSERT INTO media_parts VALUES (33, 20, '/links/provider/Good.mkv', 8, NULL);
            """)
            connection.commit()
        self.manager = TimestampRepairManager(str(self.root / "data"), sleep=lambda _: None)
        self.config = TimestampRepairConfig(
            enabled=True, database_path=str(self.database),
            allowed_prefixes=["/links/provider"],
        )

    def tearDown(self):
        self.database.unlink(missing_ok=True)
        (self.root / "data" / "timestamp-repair" / "audit.json").unlink(
            missing_ok=True,
        )

    def test_read_only_detector_is_generic_and_deduplicates_files(self):
        parts = self.manager.detect(self.config, "2")
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].file_path, "/links/provider/Movie/Movie.mkv")
        self.assertEqual(parts[0].folder, "/links/provider/Movie")

    def test_audit_separates_missing_broken_and_regular_paths(self):
        real_stat = os.stat
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executemany(
                "INSERT INTO media_parts VALUES (?, 20, ?, ?, NULL)",
                [
                    (40, "/links/provider/Missing.mkv", -8),
                    (41, "/links/provider/Broken.mkv", -9),
                    (42, "/links/provider/Regular.mkv", -10),
                ],
            )
            connection.commit()
        instance = SimpleNamespace(
            name="Plex",
            libraries=[LibraryConfig(
                "Movies", "physical", [], section_id="2",
            )],
        )
        with patch("src.timestamp_repair.os.path.lexists",
                   side_effect=lambda path: not path.endswith("Missing.mkv")), \
             patch("src.timestamp_repair.os.path.islink",
                   side_effect=lambda path: not path.endswith("Regular.mkv")), \
             patch("src.timestamp_repair.os.path.exists",
                   side_effect=lambda path: not path.endswith("Broken.mkv")), \
             patch("src.timestamp_repair.os.stat",
                   side_effect=lambda path, *args, **kwargs: (
                       SimpleNamespace(st_mtime=1_722_124_800)
                       if str(path).startswith("/links/provider/")
                       else real_stat(path, *args, **kwargs)
                   )):
            audit = self.manager.audit(instance, self.config)

        self.assertEqual(audit["database_distinct_files"], 4)
        self.assertEqual(audit["distinct_files"], 1)
        self.assertEqual(audit["path_state_counts"], {
            "repairable_timestamp": 1,
            "missing_path": 1,
            "broken_symlink": 1,
            "regular_file": 1,
        })
        self.assertEqual(
            {issue["path_state"] for issue in audit["path_issues"]},
            {"missing_path", "broken_symlink", "regular_file"},
        )
        self.assertTrue(audit["folders"][0]["files"][0]["filesystem_timestamp_iso"])
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO media_parts VALUES (43, 20, ?, -11, NULL)",
                ("/links/provider/NewAfterAudit.mkv",),
            )
            connection.commit()
        live_audit = self.manager.status()["audits"]["Plex"]
        self.assertTrue(live_audit["database_count_changed"])
        self.assertEqual(live_audit["live_database_distinct_files"], 5)

    def test_prefix_containment_rejects_sibling_prefix_attack(self):
        self.assertTrue(_inside("/links/provider/Movie/file.mkv", "/links/provider"))
        self.assertFalse(_inside("/links/provider-evil/file.mkv", "/links/provider"))

    def test_temporary_names_preserve_final_extension(self):
        self.assertEqual(temporary_name("/media/A.B.mkv"), "/media/A.B.plexfix.mkv")
        self.assertEqual(temporary_name("/media/README"), "/media/README.plexfix")


class TimestampRepairTransactionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / ".runtime-timestamp-transactions"
        self.root.mkdir(exist_ok=True)
        repair_root = self.root / "timestamp-repair"
        for name in ("active.json", "audit.json", "history.json"):
            (repair_root / name).unlink(missing_ok=True)
        self.manager = TimestampRepairManager(str(self.root), sleep=lambda _: None)
        self.repair = TimestampRepairConfig(
            enabled=True, database_path="/plex-db/library.db",
            allowed_prefixes=["/links"], max_files_per_folder=5,
            scan_timeout_seconds=30, poll_interval_seconds=1,
            heartbeat_seconds=1,
        )
        self.library = LibraryConfig(
            "Movies", "debrid", [PathConfig("/links", "debrid")],
            section_id="2",
        )
        self.instance = PlexInstanceConfig(
            "Plex", "http://plex", "token", [self.library],
            timestamp_repair=self.repair,
        )

    def tearDown(self):
        repair_root = self.root / "timestamp-repair"
        for name in ("active.json", "audit.json", "history.json"):
            (repair_root / name).unlink(missing_ok=True)

    def test_manifest_is_persisted_before_first_rename(self):
        part = AffectedPart("2", 10, 20, 30, "/links/Movie/file.mkv", -5,
                            "/links/Movie", "Movie")
        plex = Mock()
        plex.scan_path.return_value = {"ok": True, "http": 200}
        rename = {
            "original": part.file_path,
            "temporary": "/links/Movie/file.plexfix.mkv",
            "target": "/targets/file.mkv",
            "resolved_target": "/targets/file.mkv",
            "mtime": 100,
        }
        with patch.object(self.manager, "detect", return_value=[part]), \
             patch.object(self.manager, "_validate_file", return_value=rename), \
             patch.object(self.manager, "_file_states",
                          side_effect=[{part.file_path: []}, {part.file_path: [100]},
                                       {part.file_path: [100]}]), \
             patch.object(self.manager, "_restore", return_value=True), \
             patch("src.timestamp_repair._rename_symlink") as replace:
            def manifest_exists(*_):
                self.assertTrue(self.manager.active_path.exists())
            replace.side_effect = manifest_exists
            result = self.manager.run_folder(
                self.instance, self.library, self.repair, plex, part.folder,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(plex.scan_path.call_count, 2)
        self.assertFalse(self.manager.active_path.exists())
        history = json.loads(self.manager.history_path.read_text(encoding="utf-8"))
        self.assertEqual(history[0]["state"], "completed")
        self.assertEqual(history[0]["timestamp_changes"], [{
            "file_path": part.file_path, "before": -5, "after": 100,
        }])

    def test_status_counts_only_completed_repaired_files(self):
        self.manager.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.manager.history_path.write_text(json.dumps([
            {"instance": "Plex", "state": "completed", "renames": [{}, {}]},
            {"instance": "Plex", "state": "failed", "renames": [{}]},
            {"instance": "Other", "state": "completed", "renames": [{}]},
        ]), encoding="utf-8")

        totals = self.manager.status()["repair_totals"]

        self.assertEqual(totals, {"Plex": 2, "Other": 1})

    def test_completed_folder_is_removed_without_invalidating_remaining_review(self):
        self.manager.audit_path.parent.mkdir(parents=True, exist_ok=True)
        self.manager.audit_path.write_text(json.dumps({"Plex": {
            "instance": "Plex", "negative_rows": 3,
            "database_distinct_files": 3, "distinct_files": 3,
            "affected_folders": 2,
            "path_state_counts": {"repairable_timestamp": 3},
            "folders": [
                {"library_section_id": "2", "folder": "/links/One",
                 "files": [{"file_path": "/links/One/a.mkv"}]},
                {"library_section_id": "2", "folder": "/links/Two",
                 "files": [{"file_path": "/links/Two/a.mkv"},
                           {"file_path": "/links/Two/b.mkv"}]},
            ],
        }}), encoding="utf-8")

        self.manager.complete_audited_folder("Plex", "2", "/links/One")

        audit = json.loads(self.manager.audit_path.read_text(
            encoding="utf-8",
        ))["Plex"]
        self.assertEqual(audit["distinct_files"], 2)
        self.assertEqual(audit["affected_folders"], 1)
        self.assertEqual(audit["database_distinct_files"], 2)
        self.assertEqual(audit["folders"][0]["folder"], "/links/Two")
        self.assertFalse(audit["database_count_changed"])

    def test_ambiguous_recovery_keeps_manifest_for_operator(self):
        transaction = {
            "transaction_id": "tx", "instance": "Plex", "library": "Movies",
            "state": "renamed", "renames": [{
                "original": "/links/a.mkv", "temporary": "/links/a.plexfix.mkv",
                "target": "/target/a.mkv",
            }],
        }
        self.manager._write_active(transaction)
        with patch("src.timestamp_repair.os.path.lexists", return_value=True):
            result = self.manager.recover()
        self.assertFalse(result["ok"])
        self.assertEqual(self.manager.active_transaction()["state"], "recovery_required")

    def test_changed_files_after_audit_require_fresh_review(self):
        part = AffectedPart("2", 10, 20, 30, "/links/Movie/new.mkv", -5,
                            "/links/Movie", "Movie")
        plex = Mock()
        with patch.object(self.manager, "detect", return_value=[part]), \
             patch.object(self.manager, "_validate_file") as validate:
            result = self.manager.run_folder(
                self.instance, self.library, self.repair, plex, part.folder,
                expected_files={"/links/Movie/reviewed.mkv"},
            )
        self.assertFalse(result["ok"])
        self.assertIn("fresh audit", result["error"])
        validate.assert_not_called()
        plex.scan_path.assert_not_called()

    def test_scan_timeout_restores_names_and_requests_reconciliation_scan(self):
        part = AffectedPart("2", 10, 20, 30, "/links/Movie/file.mkv", -5,
                            "/links/Movie", "Movie")
        plex = Mock()
        plex.scan_path.return_value = {"ok": True, "http": 200}
        rename = {
            "original": part.file_path,
            "temporary": "/links/Movie/file.plexfix.mkv",
            "target": "/targets/file.mkv", "resolved_target": "/targets/file.mkv",
            "mtime": 100,
        }
        def restored(transaction):
            transaction["state"] = "restored"
            return True
        with patch.object(self.manager, "detect", return_value=[part]), \
             patch.object(self.manager, "_validate_file", return_value=rename), \
             patch.object(self.manager, "_wait_for", return_value=False), \
             patch.object(self.manager, "_restore", side_effect=restored), \
             patch("src.timestamp_repair._rename_symlink"):
            result = self.manager.run_folder(
                self.instance, self.library, self.repair, plex, part.folder,
            )
        self.assertFalse(result["ok"])
        self.assertIn("timed out", result["error"])
        self.assertEqual(plex.scan_path.call_count, 2)
        self.assertFalse(self.manager.active_path.exists())

    def test_corrupt_active_manifest_blocks_automatic_recovery(self):
        self.manager.active_path.parent.mkdir(parents=True, exist_ok=True)
        self.manager.active_path.write_text("not-json", encoding="utf-8")
        self.assertTrue(self.manager.has_active_transaction("Plex"))
        result = self.manager.recover()
        self.assertFalse(result["ok"])
        self.assertTrue(self.manager.active_path.exists())

    def test_saved_recovery_only_blocks_empty_trash_for_owning_instance(self):
        transaction = {
            "transaction_id": "tx", "instance": "Plex", "library": "Movies",
            "state": "renamed", "renames": [],
        }
        self.manager._write_active(transaction)

        self.assertTrue(self.manager.has_active_transaction(
            "Plex", "empty_trash",
        ))
        self.assertFalse(self.manager.has_active_transaction(
            "Other Plex", "empty_trash",
        ))
        self.assertTrue(self.manager.has_active_transaction(
            "Other Plex", "timestamp_repair",
        ))

    def test_active_transaction_blocks_empty_trash_before_health_checks(self):
        transaction = {
            "transaction_id": "tx", "instance": "Plex", "library": "Movies",
            "state": "renamed", "renames": [],
        }
        self.manager._write_active(transaction)
        plex = Mock()
        config = AppConfig(instances=[self.instance])

        runner.run_library(self.instance, self.library, config, plex, manual=True)

        plex.check_reachable.assert_not_called()
        plex.empty_trash.assert_not_called()

    def test_active_transaction_does_not_block_another_plex_instance(self):
        transaction = {
            "transaction_id": "tx", "instance": "Plex", "library": "Movies",
            "state": "renamed", "renames": [],
        }
        self.manager._write_active(transaction)
        other_library = LibraryConfig(
            "Movies", "physical", [], section_id="2",
        )
        other_instance = PlexInstanceConfig(
            "Other Plex", "http://other-plex", "token", [other_library],
        )
        plex = Mock()

        with patch.object(runner, "_run_library") as execute:
            runner.run_library(
                other_instance, other_library,
                AppConfig(instances=[other_instance]), plex, manual=True,
            )

        execute.assert_called_once()


class TimestampRepairConfigTests(unittest.TestCase):
    def test_feature_is_disabled_by_default(self):
        parsed = parse_config({"plex_instances": [{
            "name": "Plex", "url": "http://plex", "libraries": [],
        }]})
        self.assertFalse(parsed.instances[0].timestamp_repair.enabled)

    def test_enabled_feature_requires_database_and_allowlist(self):
        raw = {"plex_instances": [{
            "name": "Plex", "url": "http://plex", "libraries": [],
            "timestamp_repair": {"enabled": True},
        }]}
        with self.assertRaisesRegex(ValueError, "database path and at least one repair folder"):
            app._validate_raw_config(raw)


class TimestampRepairApiTests(unittest.TestCase):
    def test_repair_ui_explains_scope_evidence_and_phases(self):
        html = rendered_ui(app.app.test_client())
        self.assertIn("Repair movie folder", html)
        self.assertIn("Repair season folder", html)
        self.assertIn("Filesystem timestamp", html)
        self.assertIn("Negative Plex part timestamp", html)
        self.assertIn("underlying provider/NZBDAV object", html)
        self.assertIn("Temporary rename", html)
        self.assertIn("Second Plex scan", html)
        self.assertIn("Excluded from automatic repair", html)
        self.assertIn("Recovery blocked", html)
        self.assertIn("Repair selected (0)", html)
        self.assertIn("runSelectedRepairFolders", html)
        self.assertIn("Each folder will receive a fresh safety check", html)

    def test_audit_enrichment_adds_complete_library_total(self):
        audit = {"libraries": [
            {"library_section_id": "1", "library": "Movies"},
            {"library_section_id": "2", "library": "TV Shows"},
        ]}
        plex = Mock()
        plex.get_sections.return_value = [
            {"id": "1", "type": "movie"},
            {"id": "2", "type": "show"},
        ]
        plex.get_library_item_count.side_effect = [1200, 3400]

        result = app._enrich_repair_audit(audit, plex)

        self.assertEqual(result["total_library_items"], 4600)
        self.assertEqual(result["libraries"][1]["total_items"], 3400)
        self.assertEqual(result["libraries"][0]["type"], "movie")

    def test_audit_enrichment_avoids_partial_library_total(self):
        audit = {"libraries": [
            {"library_section_id": "1", "library": "Movies"},
            {"library_section_id": "2", "library": "TV Shows"},
        ]}
        plex = Mock()
        plex.get_library_item_count.side_effect = [1200, None]

        result = app._enrich_repair_audit(audit, plex)

        self.assertIsNone(result["total_library_items"])

    def test_run_rejects_folder_not_returned_by_latest_audit(self):
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "known-token"
        instance = PlexInstanceConfig("Plex", "http://plex", "token", [])
        with patch.object(app, "_timestamp_runtime",
                          return_value=(instance, Mock(), Mock())), \
             patch.object(app.timestamp_repair, "audited_folder",
                          return_value=False):
            response = client.post(
                "/api/timestamp-repair/run",
                json={"instance": "Plex", "library_section_id": "2",
                      "folder": "/arbitrary/path"},
                headers={"X-CSRF-Token": "known-token"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("latest server-side audit", response.get_json()["error"])

    def test_remote_persisted_transaction_is_exposed_as_recovery_blocker(self):
        worker = RepairWorkerConfig(
            "altmount-worker", "http://worker:8223", "z" * 32,
            "http://controller:8222",
        )
        instance = PlexInstanceConfig(
            "vm-altmount", "http://plex", "token", [],
            timestamp_repair=TimestampRepairConfig(
                enabled=True, worker=worker.name,
                database_path="/plex-db/library.db",
                allowed_prefixes=["/links"],
            ),
        )
        local_status = {
            "running": False, "state": "idle", "active_transaction": None,
            "audits": {}, "history": [], "repair_totals": {},
        }
        remote_status = {
            "running": False, "audits": {},
            "active_transaction": {
                "state": "renamed", "instance": "vm-altmount",
                "library": "TV Shows", "folder": "/links/Show",
            },
        }
        with patch.object(app, "config", AppConfig(
            instances=[instance], repair_workers=[worker],
        )), patch.object(
            app.timestamp_repair, "status", return_value=local_status,
        ), patch(
            "app.RepairWorkerClient.status", return_value=remote_status,
        ), patch.object(
            app, "_repair_readiness", return_value=(True, "Connected"),
        ):
            response = app.app.test_client().get(
                "/api/timestamp-repair/status",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["active_transaction"]["state"],
                         "recovery_required")
        self.assertEqual(payload["active_transaction"]["recovery_state"],
                         "renamed")
        self.assertEqual(payload["active_transaction"]["worker"],
                         "altmount-worker")
        self.assertTrue(payload["instances"][0]["blocked"])

    def test_remote_recovery_blocks_run_before_starting_thread(self):
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "known-token"
        library = LibraryConfig("TV Shows", "physical", [], section_id="2")
        instance = PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
            timestamp_repair=TimestampRepairConfig(
                enabled=True, database_path="/plex-db/library.db",
                allowed_prefixes=["/links"],
            ),
        )
        with patch.object(app, "config", AppConfig(instances=[instance])), \
             patch.object(app, "_timestamp_runtime",
                          return_value=(instance, library, Mock())), \
             patch.object(app, "_remote_recovery_required", return_value=True), \
             patch("app.threading.Thread") as thread, \
             self.assertLogs("mediamender", level="WARNING") as logs:
            response = client.post(
                "/api/timestamp-repair/run",
                json={"instance": "Plex", "library_section_id": "2",
                      "folder": "/links/Show"},
                headers={"X-CSRF-Token": "known-token"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("recovery is required", response.get_json()["error"])
        self.assertTrue(any("Timestamp repair blocked" in line for line in logs.output))
        thread.assert_not_called()

    def test_selected_folders_run_sequentially_from_one_reviewed_audit(self):
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "known-token"
        library = LibraryConfig("TV Shows", "physical", [], section_id="2")
        instance = PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
            timestamp_repair=TimestampRepairConfig(
                enabled=True, database_path="/plex-db/library.db",
                allowed_prefixes=["/links"],
            ),
        )
        plex = Mock()
        thread = Mock()
        with patch.object(app, "config", AppConfig(instances=[instance])), \
             patch.object(app, "_timestamp_runtime",
                          return_value=(instance, library, plex)), \
             patch.object(app, "_remote_recovery_required", return_value=False), \
             patch.object(app.timestamp_repair, "status", return_value={
                 "running": False, "active_transaction": None,
             }), \
             patch.object(app.timestamp_repair, "audited_folder",
                          return_value=True), \
             patch.object(app.timestamp_repair, "audited_files",
                          side_effect=[{"/links/One/a.mkv"},
                                       {"/links/Two/a.mkv"}]), \
             patch.object(app.timestamp_repair, "run_folder",
                          side_effect=[{"ok": True}, {"ok": True}]) as run, \
             patch.object(app.timestamp_repair,
                          "complete_audited_folder") as complete, \
             patch("app.threading.Thread", return_value=thread) as factory:
            response = client.post(
                "/api/timestamp-repair/run",
                json={"instance": "Plex", "folders": [
                    {"library_section_id": "2", "folder": "/links/One"},
                    {"library_section_id": "2", "folder": "/links/Two"},
                ]},
                headers={"X-CSRF-Token": "known-token"},
            )
            factory.call_args.kwargs["target"]()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["folders"], 2)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].kwargs["batch_position"], "1/2")
        self.assertEqual(run.call_args_list[1].kwargs["batch_position"], "2/2")
        self.assertEqual(complete.call_count, 2)
        self.assertEqual(app._repair_batch["state"], "completed")

    def test_repair_batch_stops_after_first_failed_folder(self):
        client = app.app.test_client()
        with client.session_transaction() as browser_session:
            browser_session["_csrf_token"] = "known-token"
        library = LibraryConfig("TV Shows", "physical", [], section_id="2")
        instance = PlexInstanceConfig(
            "Plex", "http://plex", "token", [library],
            timestamp_repair=TimestampRepairConfig(
                enabled=True, database_path="/plex-db/library.db",
                allowed_prefixes=["/links"],
            ),
        )
        thread = Mock()
        with patch.object(app, "config", AppConfig(instances=[instance])), \
             patch.object(app, "_timestamp_runtime",
                          return_value=(instance, library, Mock())), \
             patch.object(app, "_remote_recovery_required", return_value=False), \
             patch.object(app.timestamp_repair, "status", return_value={
                 "running": False, "active_transaction": None,
             }), \
             patch.object(app.timestamp_repair, "audited_folder",
                          return_value=True), \
             patch.object(app.timestamp_repair, "audited_files",
                          return_value={"/links/file.mkv"}), \
             patch.object(app.timestamp_repair, "run_folder",
                          return_value={"ok": False, "error": "changed"}) as run, \
             patch.object(app.timestamp_repair,
                          "complete_audited_folder") as complete, \
             patch("app.threading.Thread", return_value=thread) as factory:
            response = client.post(
                "/api/timestamp-repair/run",
                json={"instance": "Plex", "folders": [
                    {"library_section_id": "2", "folder": "/links/One"},
                    {"library_section_id": "2", "folder": "/links/Two"},
                ]}, headers={"X-CSRF-Token": "known-token"},
            )
            factory.call_args.kwargs["target"]()

        self.assertEqual(response.status_code, 202)
        run.assert_called_once()
        complete.assert_not_called()
        self.assertEqual(app._repair_batch["state"], "failed")
        self.assertEqual(app._repair_batch["completed"], 0)


if __name__ == "__main__":
    unittest.main()
