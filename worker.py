import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, request

from src.timestamp_repair import TimestampRepairManager, _inside
from src.worker_auth import SignatureVerifier, signed_headers


logger = logging.getLogger("emptyarr.repair_worker")


def _roots(name: str, default: str) -> list[str]:
    return [
        os.path.abspath(value.strip())
        for value in os.environ.get(name, default).split(",")
        if value.strip()
    ]


def _inside_any(path: str, roots: list[str]) -> bool:
    return any(_inside(path, root) for root in roots)


def _repair_config(payload: dict, database_roots: list[str],
                   media_roots: list[str]):
    raw = payload.get("repair", {})
    database_path = str(raw.get("database_path", "")).strip()
    prefixes = [str(item).strip() for item in raw.get("allowed_prefixes", [])]
    if not database_path or not _inside_any(database_path, database_roots):
        raise ValueError("Database path is outside this worker's read-only database roots")
    if not prefixes or any(not _inside_any(path, media_roots) for path in prefixes):
        raise ValueError("Repair prefix is outside this worker's writable media roots")
    return SimpleNamespace(
        enabled=True,
        database_path=database_path,
        allowed_prefixes=prefixes,
        max_files_per_folder=max(1, min(int(raw.get("max_files_per_folder", 5)), 100)),
        scan_timeout_seconds=max(30, min(int(raw.get("scan_timeout_seconds", 1800)), 3600)),
        poll_interval_seconds=max(1, min(int(raw.get("poll_interval_seconds", 5)), 60)),
        heartbeat_seconds=max(5, min(int(raw.get("heartbeat_seconds", 30)), 300)),
    )


def _instance(payload: dict):
    libraries = [
        SimpleNamespace(
            name=str(item.get("name", "")),
            section_id=str(item.get("section_id", "")) or None,
        )
        for item in payload.get("libraries", [])
    ]
    return SimpleNamespace(name=str(payload.get("instance", "")), libraries=libraries)


class _ControllerPlexProxy:
    def __init__(self, controller_url: str, worker_name: str, secret: str,
                 run_id: str):
        parsed = urlparse(controller_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Controller callback URL is invalid")
        self.base_url = controller_url.rstrip("/")
        self.worker_name = worker_name
        self.secret = secret
        self.run_id = run_id

    def scan_path(self, section_id: str, folder: str) -> dict:
        path = f"/api/timestamp-repair/worker-scan/{self.run_id}"
        body = json.dumps(
            {"section_id": str(section_id), "folder": folder},
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        headers = signed_headers(
            self.secret, self.worker_name, "POST", path, body,
        )
        headers["Content-Type"] = "application/json"
        try:
            response = requests.post(
                self.base_url + path, data=body, headers=headers, timeout=30,
            )
            result = response.json()
        except (requests.RequestException, ValueError):
            return {"ok": False, "http": 502}
        return result if response.ok else {"ok": False, "http": response.status_code}


def create_worker_app(secret: str | None = None, worker_name: str | None = None,
                      data_dir: str | None = None,
                      register_recovery_check: bool = True) -> Flask:
    app = Flask(__name__)
    configured_secret = secret if secret is not None else os.environ.get("EMPTYARR_WORKER_TOKEN", "")
    configured_name = worker_name or os.environ.get("EMPTYARR_WORKER_NAME", "repair-worker")
    database_roots = _roots("EMPTYARR_WORKER_DATABASE_ROOTS", "/plex-db")
    media_roots = _roots("EMPTYARR_WORKER_MEDIA_ROOTS", "/repair-media")
    manager = TimestampRepairManager(
        data_dir or os.environ.get("WORKER_DATA_DIR", "data/worker"),
        register_recovery_check=register_recovery_check,
    )
    verifier = SignatureVerifier()
    recovery = manager.recover()
    if not recovery.get("ok"):
        logger.error("Repair worker startup recovery requires attention")

    @app.before_request
    def authenticate_worker_request():
        if len(configured_secret) < 32:
            return jsonify({"ok": False, "error": "Worker token is not configured"}), 503
        ok, error = verifier.verify(
            configured_secret, configured_name, request.method, request.path,
            request.get_data(cache=True), request.headers,
        )
        if not ok:
            return jsonify({"ok": False, "error": error}), 401
        return None

    @app.get("/api/v1/health")
    def health():
        active = manager.active_transaction()
        return jsonify({
            "ok": True,
            "role": "repair-worker",
            "name": configured_name,
            "database_roots": database_roots,
            "media_roots": media_roots,
            "recovery_required": bool(active),
        })

    @app.get("/api/v1/databases")
    def databases():
        found = []
        for root in database_roots:
            base = Path(root)
            if not base.is_dir():
                continue
            try:
                found.extend(str(path) for path in base.rglob(
                    "com.plexapp.plugins.library.db"
                ) if path.is_file())
            except OSError:
                continue
        return jsonify({"ok": True, "databases": sorted(set(found))})

    @app.post("/api/v1/audit")
    def audit():
        payload = request.get_json(silent=True) or {}
        try:
            config = _repair_config(payload, database_roots, media_roots)
            return jsonify(manager.audit(_instance(payload), config))
        except Exception as exc:
            logger.warning("Worker audit failed (%s)", type(exc).__name__)
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.post("/api/v1/run")
    def run():
        payload = request.get_json(silent=True) or {}
        try:
            config = _repair_config(payload, database_roots, media_roots)
            instance = _instance(payload)
            section_id = str(payload.get("library_section_id", ""))
            library = next(
                (item for item in instance.libraries
                 if str(item.section_id) == section_id), None,
            )
            if not instance.name or library is None:
                raise ValueError("Worker instance or library is invalid")
            proxy = _ControllerPlexProxy(
                str(payload.get("controller_url", "")), configured_name,
                configured_secret, str(payload.get("run_id", "")),
            )
            result = manager.run_folder(
                instance, library, config, proxy,
                str(payload.get("folder", "")), section_id=section_id,
                expected_files=set(payload.get("expected_files", [])),
                batch_position=str(payload.get("batch_position", "1/1")),
            )
            return jsonify(result), (200 if result.get("ok") else 400)
        except Exception as exc:
            logger.error("Worker repair failed (%s)", type(exc).__name__)
            return jsonify({"ok": False, "error": str(exc)}), 400

    @app.get("/api/v1/status")
    def status():
        return jsonify({"ok": True, **manager.status()})

    @app.post("/api/v1/cancel")
    def cancel():
        manager.cancel()
        return jsonify({"ok": True})

    @app.post("/api/v1/recover")
    def recover():
        payload = request.get_json(silent=True) or {}
        result = manager.recover(str(payload.get("instance", "")) or None)
        return jsonify(result), (200 if result.get("ok") else 409)

    app.repair_manager = manager
    return app


app = create_worker_app(
    register_recovery_check=os.environ.get("EMPTYARR_ROLE") == "repair-worker",
)
