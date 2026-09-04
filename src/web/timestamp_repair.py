"""HTTP routes for Timestamp Repair.

Split out of app.py. The repair manager, the remote-worker state and their
locks stay on the app module, because the configuration save paths read them
to refuse a write while a repair transaction is open; they are reached here
through ``runtime``.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from flask import Blueprint, jsonify, request

from src import runner
from src.auth import require_auth
from src.maintenance import lease
from src.repair_worker_client import RepairWorkerClient
from src.web.context import requires_feature, runtime

bp = Blueprint("timestamp_repair", __name__)


@bp.route("/api/timestamp-repair/status", methods=["GET"])
@require_auth
@requires_feature("timestamp_repair")
def api_timestamp_repair_status():
    status = runtime.timestamp_repair.status()
    with runtime._repair_batch_lock:
        status["batch"] = dict(runtime._repair_batch)
    if status["batch"].get("running"):
        status["running"] = True
    local_active = status.get("active_transaction")
    if isinstance(local_active, dict) and not status.get("running"):
        local_active = {
            **local_active,
            "recovery_state": local_active.get("state", "prepared"),
            "state": "recovery_required",
        }
        status["active_transaction"] = local_active
        status["state"] = "recovery_required"
    with runtime._remote_repair_lock:
        external = dict(runtime._remote_repair)
    if external.get("running"):
        worker = runtime._repair_worker(str(external.get("worker", "")))
        if worker:
            try:
                worker_status = RepairWorkerClient(worker, timeout=3).status()
                active = worker_status.get("active_transaction") or worker_status.get("transaction")
                if isinstance(active, dict):
                    external.update({
                        "state": active.get("state", external.get("state")),
                        "last_heartbeat": active.get("last_heartbeat", external.get("last_heartbeat")),
                        "scan_elapsed_seconds": active.get("scan_elapsed_seconds", 0),
                    })
            except Exception:
                pass
        status.update({
            "running": True,
            "state": external.get("state", "running_on_worker"),
            "last_heartbeat": external.get("last_heartbeat"),
            "active_transaction": external,
        })
    worker_statuses = {}
    for instance in runtime.config.instances:
        worker_name = instance.timestamp_repair.worker
        if worker_name == "local" or worker_name in worker_statuses:
            continue
        worker = runtime._repair_worker(worker_name)
        if not worker:
            continue
        try:
            worker_statuses[worker_name] = RepairWorkerClient(
                worker, timeout=3,
            ).status()
        except Exception:
            worker_statuses[worker_name] = {}
    for instance in runtime.config.instances:
        worker_audit = worker_statuses.get(
            instance.timestamp_repair.worker, {},
        ).get("audits", {}).get(instance.name, {})
        local_audit = status.get("audits", {}).get(instance.name, {})
        if worker_audit and local_audit:
            for key in (
                "live_database_distinct_files", "database_count_changed",
            ):
                if key in worker_audit:
                    local_audit[key] = worker_audit[key]
    remote_recoveries = []
    for worker_name, worker_status in worker_statuses.items():
        active = worker_status.get("active_transaction")
        if isinstance(active, dict):
            recovery_state = active.get("state", "prepared")
            remote_recoveries.append({
                **active,
                **({
                    "recovery_state": recovery_state,
                    "state": "recovery_required",
                } if not worker_status.get("running") else {}),
                "worker": worker_name,
                "remote": True,
            })
    if remote_recoveries:
        status["remote_recoveries"] = remote_recoveries
        if not status.get("active_transaction"):
            status["active_transaction"] = remote_recoveries[0]
            status["state"] = remote_recoveries[0].get(
                "state", "recovery_required",
            )
    maintenance_blocked = bool(status.get("active_transaction"))
    status["instances"] = [
        {
            "name": instance.name,
            "enabled": instance.timestamp_repair.enabled,
            "worker": instance.timestamp_repair.worker,
            "ready": (readiness := runtime._repair_readiness(instance))[0],
            "readiness": readiness[1],
            "blocked": maintenance_blocked,
            "max_files_per_folder": instance.timestamp_repair.max_files_per_folder,
        }
        for instance in runtime.config.instances
    ]
    return jsonify(status)


@bp.route("/api/timestamp-repair/audit", methods=["POST"])
@require_auth
@requires_feature("timestamp_repair")
def api_timestamp_repair_audit():
    data = request.get_json(silent=True) or {}
    instance, _, plex = runtime._timestamp_runtime(str(data.get("instance", "")))
    if not instance:
        return jsonify({"error": "Plex instance not found"}), 404
    try:
        if instance.timestamp_repair.worker == "local":
            result = runtime.timestamp_repair.audit(
                instance, instance.timestamp_repair, plex,
            )
        else:
            worker = runtime._repair_worker(instance.timestamp_repair.worker)
            if not worker:
                raise ValueError("Configured repair worker was not found")
            result = RepairWorkerClient(worker).audit(runtime._worker_payload(instance, plex))
        runtime._enrich_repair_audit(result, plex)
        runtime.timestamp_repair.save_audit(result)
        return jsonify(result)
    except Exception as exc:
        runtime.logger.error("Timestamp repair audit failed for %s (%s)",
                     instance.name, type(exc).__name__)
        return jsonify({"error": str(exc)}), 400


@bp.route("/api/timestamp-repair/run", methods=["POST"])
@require_auth
@requires_feature("timestamp_repair")
def api_timestamp_repair_run():
    data = request.get_json(silent=True) or {}
    instance_name = str(data.get("instance", ""))
    requested = data.get("folders")
    if not isinstance(requested, list):
        requested = [{
            "library_section_id": data.get("library_section_id", ""),
            "folder": data.get("folder", ""),
        }]
    if not requested or len(requested) > 100:
        return jsonify({"error": "Select between 1 and 100 reviewed folders"}), 400
    if runtime._remote_recovery_required():
        runtime.logger.warning(
            "[%s] Timestamp repair blocked: a remote worker requires "
            "recovery or its recovery state cannot be verified",
            instance_name,
        )
        return jsonify({
            "error": "Remote repair worker recovery is required before "
                     "another timestamp repair can start",
        }), 409
    repair_status = runtime.timestamp_repair.status()
    with runtime._remote_repair_lock:
        remote_running = bool(runtime._remote_repair.get("running"))
    with runtime._repair_batch_lock:
        batch_running = bool(runtime._repair_batch.get("running"))
    if (repair_status.get("running") or repair_status.get("active_transaction")
            or remote_running or batch_running):
        return jsonify({"error": "A timestamp repair is already active"}), 409

    jobs = []
    seen = set()
    for item in requested:
        section_id = str(item.get("library_section_id", ""))
        folder = str(item.get("folder", ""))
        key = (section_id, folder)
        if not section_id or not folder or key in seen:
            return jsonify({"error": "Each selected folder must be unique and complete"}), 400
        seen.add(key)
        instance, library, plex = runtime._timestamp_runtime(instance_name, section_id)
        if not instance or not library or not plex:
            return jsonify({"error": "Configured Plex instance/library not found"}), 404
        if not runtime.timestamp_repair.audited_folder(instance_name, section_id, folder):
            return jsonify({
                "error": f"Folder is not present in the latest server-side audit: {folder}",
            }), 400
        expected_files = runtime.timestamp_repair.audited_files(
            instance_name, section_id, folder,
        )
        jobs.append((instance, library, plex, section_id, folder, expected_files))

    def _perform(job, position: str) -> dict:
        instance, library, plex, section_id, folder, expected_files = job
        repair_worker = instance.timestamp_repair.worker
        if repair_worker == "local":
            return runtime.timestamp_repair.run_folder(
                instance, library, instance.timestamp_repair, plex, folder,
                section_id,
                preflight=lambda: runner._collect_library_checks(
                    instance, library, runtime.config, plex, section_id=section_id,
                )[0],
                expected_files=expected_files,
                batch_position=position,
            )
        worker = runtime._repair_worker(repair_worker)
        if not worker:
            return {"ok": False, "error": "Configured repair worker was not found"}
        run_id = secrets.token_urlsafe(24)
        with runtime._remote_repair_lock:
            runtime._remote_repair.clear()
            runtime._remote_repair.update({
                "running": True, "state": "starting_worker",
                "transaction_id": run_id, "instance": instance.name,
                "library": library.name, "folder": folder,
                "worker": worker.name, "last_heartbeat": runtime._utc_now(),
                "batch_position": position,
            })
            runtime._worker_scan_contexts[run_id] = {
                "worker": worker.name, "instance": instance.name,
                "section_id": section_id, "folder": folder, "plex": plex,
            }
        result = {"ok": False, "error": "Worker repair did not start"}
        try:
            with lease(instance.name, operation="timestamp_repair") as (acquired, reason):
                if not acquired:
                    raise RuntimeError(reason)
                checks = runner._collect_library_checks(
                    instance, library, runtime.config, plex, section_id=section_id,
                )[0]
                failed = [name for name, check in checks.items() if not check.get("pass")]
                if failed:
                    raise RuntimeError("Safety checks failed: " + ", ".join(failed))
                payload = {
                    **runtime._worker_payload(instance, plex),
                    "run_id": run_id,
                    "controller_url": worker.controller_url,
                    "library_section_id": section_id,
                    "folder": folder,
                    "expected_files": sorted(expected_files),
                    "batch_position": position,
                }
                pre_dispatch_status = RepairWorkerClient(
                    worker, timeout=3,
                ).status()
                if pre_dispatch_status.get("active_transaction"):
                    raise RuntimeError("Repair worker recovery is required")
                with runtime._remote_repair_lock:
                    runtime._remote_repair.update({
                        "state": "running_on_worker", "last_heartbeat": runtime._utc_now(),
                    })
                runtime._set_remote_pending({
                    "worker": worker.name, "instance": instance.name,
                    "library": library.name, "folder": folder,
                    "transaction_id": run_id, "dispatched_at": runtime._utc_now(),
                })
                runtime._worker_recovery_cache.pop(worker.name, None)
                result = RepairWorkerClient(worker).run(payload)
                worker_status = RepairWorkerClient(worker, timeout=3).status()
                if not worker_status.get("active_transaction"):
                    runtime._clear_remote_pending(worker.name)
                runtime.timestamp_repair.merge_history(worker_status.get("history", []))
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
            runtime.logger.error("Remote timestamp repair failed for %s (%s)",
                         instance.name, type(exc).__name__)
        finally:
            with runtime._remote_repair_lock:
                runtime._worker_scan_contexts.pop(run_id, None)
                runtime._remote_repair.update({
                    "running": False,
                    "state": "completed" if result.get("ok") else "failed",
                    "error": result.get("error", ""),
                    "last_heartbeat": runtime._utc_now(),
                })
            runtime._worker_recovery_cache.pop(worker.name, None)
        return result

    def _run():
        total = len(jobs)
        completed = 0
        result = {"ok": True}
        try:
            for runtime.index, job in enumerate(jobs, start=1):
                with runtime._repair_batch_lock:
                    if runtime._repair_batch.get("cancel_requested"):
                        result = {
                            "ok": False,
                            "error": "Repair queue cancelled safely",
                        }
                        break
                position = f"{runtime.index}/{total}"
                with runtime._repair_batch_lock:
                    runtime._repair_batch.update({
                        "running": True, "state": "running",
                        "current": runtime.index, "total": total,
                        "completed": completed, "failed": 0,
                        "folder": job[4], "error": "",
                    })
                result = _perform(job, position)
                if not result.get("ok"):
                    break
                runtime.timestamp_repair.complete_audited_folder(
                    instance_name, job[3], job[4],
                )
                completed += 1
        except Exception as exc:
            runtime.logger.exception(
                "Timestamp repair queue failed while updating local state"
            )
            result = {
                "ok": False,
                "error": f"Repair queue state update failed ({type(exc).__name__})",
            }
        finally:
            with runtime._repair_batch_lock:
                runtime._repair_batch.update({
                    "running": False,
                    "state": "completed" if result.get("ok") else "failed",
                    "completed": completed,
                    "failed": 0 if result.get("ok") else 1,
                    "error": result.get("error", ""),
                    "finished_at": runtime._utc_now(),
                })

    with runtime._repair_batch_lock:
        if runtime._repair_batch.get("running"):
            return jsonify({"error": "A timestamp repair is already active"}), 409
        runtime._repair_batch.clear()
        runtime._repair_batch.update({
            "running": True, "state": "queued", "current": 0,
            "total": len(jobs), "completed": 0, "failed": 0,
            "folder": "", "error": "", "started_at": runtime._utc_now(),
            "cancel_requested": False,
        })
    threading.Thread(target=_run, daemon=True, name="timestamp-repair").start()
    return jsonify({"status": "triggered", "folders": len(jobs)}), 202


@bp.route("/api/timestamp-repair/cancel", methods=["POST"])
@require_auth
@requires_feature("timestamp_repair")
def api_timestamp_repair_cancel():
    with runtime._repair_batch_lock:
        if runtime._repair_batch.get("running"):
            runtime._repair_batch["cancel_requested"] = True
    runtime.timestamp_repair.cancel()
    with runtime._remote_repair_lock:
        worker_name = runtime._remote_repair.get("worker") if runtime._remote_repair.get("running") else None
    if worker_name:
        worker = runtime._repair_worker(worker_name)
        if worker:
            try:
                RepairWorkerClient(worker).cancel()
            except Exception:
                pass
    return jsonify({"ok": True, "message": "Cancellation requested; names will be restored at the next safe step"})


@bp.route("/api/timestamp-repair/recover", methods=["POST"])
@require_auth
@requires_feature("timestamp_repair")
def api_timestamp_repair_recover():
    result = runtime.timestamp_repair.recover()
    if result.get("ok") and result.get("message") == "No recovery is required":
        for instance in runtime.config.instances:
            repair = instance.timestamp_repair
            if not repair.enabled or repair.worker == "local":
                continue
            worker = runtime._repair_worker(repair.worker)
            if not worker:
                continue
            try:
                status = RepairWorkerClient(worker, timeout=3).status()
                if status.get("active_transaction"):
                    result = RepairWorkerClient(worker).recover(instance.name)
                    recovered_status = RepairWorkerClient(worker, timeout=3).status()
                    runtime.timestamp_repair.merge_history(recovered_status.get("history", []))
                    if result.get("ok") and not recovered_status.get("active_transaction"):
                        runtime._clear_remote_pending(worker.name)
                    runtime._worker_recovery_cache.pop(worker.name, None)
                    break
                runtime._clear_remote_pending(worker.name)
            except Exception as exc:
                result = {"ok": False, "error": f"Worker {worker.name} is unavailable"}
                runtime.logger.warning("Worker recovery check failed (%s)", type(exc).__name__)
                break
    return jsonify(result), (200 if result.get("ok") else 409)


@bp.route("/api/timestamp-repair/worker-scan/<run_id>", methods=["POST"])
@requires_feature("timestamp_repair")
def api_timestamp_repair_worker_scan(run_id: str):
    with runtime._remote_repair_lock:
        context = runtime._worker_scan_contexts.get(run_id)
    if not context:
        return jsonify({"ok": False, "error": "Unknown repair transaction"}), 404
    worker = runtime._repair_worker(context["worker"])
    if not worker:
        return jsonify({"ok": False, "error": "Unknown repair worker"}), 401
    ok, error = runtime._worker_signature_verifier.verify(
        worker.token, worker.name, request.method, request.path,
        request.get_data(cache=True), request.headers,
    )
    if not ok:
        return jsonify({"ok": False, "error": error}), 401
    payload = request.get_json(silent=True) or {}
    if (str(payload.get("section_id", "")) != context["section_id"]
            or str(payload.get("folder", "")) != context["folder"]):
        return jsonify({"ok": False, "error": "Scan request is outside the approved folder"}), 403
    return jsonify(context["plex"].scan_path(context["section_id"], context["folder"]))


@bp.route("/api/timestamp-repair/worker-test", methods=["POST"])
@require_auth
@requires_feature("timestamp_repair")
def api_timestamp_repair_worker_test():
    data = request.get_json(silent=True) or {}
    worker = SimpleNamespace(
        name=str(data.get("name", "")).strip(),
        url=str(data.get("url", "")).strip(),
        token=str(data.get("token", "")),
        controller_url=str(data.get("controller_url", "")).strip(),
    )
    try:
        return jsonify(RepairWorkerClient(worker, timeout=5).health())
    except Exception as exc:
        runtime.logger.warning("Repair worker test failed (%s)", type(exc).__name__)
        return jsonify({"ok": False, "error": "Worker could not be reached or authenticated"}), 400


@bp.route("/api/timestamp-repair/databases", methods=["POST"])
@require_auth
@requires_feature("timestamp_repair")
def api_timestamp_repair_databases():
    data = request.get_json(silent=True) or {}
    worker_name = str(data.get("worker", "local")) or "local"
    if worker_name != "local":
        worker = runtime._repair_worker(worker_name)
        if not worker:
            return jsonify({"ok": False, "error": "Repair worker is not configured"}), 404
        try:
            return jsonify(RepairWorkerClient(worker, timeout=10).discover())
        except Exception as exc:
            runtime.logger.warning("Worker database discovery failed (%s)", type(exc).__name__)
            return jsonify({"ok": False, "error": "Worker database discovery failed"}), 400
    roots = [
        value.strip() for value in os.environ.get(
            "TIMESTAMP_REPAIR_DATABASE_ROOTS", "/plex-db",
        ).split(",") if value.strip()
    ]
    found = []
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        try:
            found.extend(str(path) for path in base.rglob(
                "com.plexapp.plugins.library.db"
            ) if path.is_file())
        except OSError:
            continue
    return jsonify({"ok": True, "databases": sorted(set(found)), "roots": roots})
