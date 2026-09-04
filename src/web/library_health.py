"""HTTP routes for Library Refresh and Metadata Health.

Split out of app.py. State is reached through ``runtime`` rather than imported,
because app.py rebinds ``config`` on every reload and the tests patch it.
"""

from __future__ import annotations

import threading

from flask import Blueprint, jsonify, request

from src.storage import atomic_write_json

from src.auth import require_auth
from src.web.context import requires_feature, runtime

bp = Blueprint("library_health", __name__)


@bp.route("/api/library-refresh/status", methods=["GET"])
@require_auth
@requires_feature("library_refresh")
def api_library_refresh_status():
    try:
        return runtime._library_refresh_status_response()
    except Exception as exc:
        runtime.logger.exception("Could not build Library Refresh status")
        return jsonify({
            "ok": False,
            "error": f"Could not load Library Refresh status: {type(exc).__name__}: {exc}",
        }), 500


@bp.route("/api/library-refresh/run", methods=["POST"])
@require_auth
@requires_feature("library_refresh")
def api_library_refresh_run():
    data = request.get_json(silent=True) or {}
    requested = data.get("libraries")
    if not isinstance(requested, list):
        requested = [{
            "instance": data.get("instance", ""),
            "library": data.get("library", ""),
        }]
    if data.get("enabled_only"):
        with runtime._runtime_lock:
            requested = [
                {"instance": instance.name, "library": library.name}
                for instance in runtime.config.instances
                for library in instance.libraries if library.refresh_enabled
            ]
    if not requested or len(requested) > 100:
        return jsonify({"error": "Select between 1 and 100 libraries"}), 400

    jobs = []
    seen = set()
    with runtime._runtime_lock:
        for item in requested:
            key = (str(item.get("instance", "")), str(item.get("library", "")))
            if not all(key) or key in seen:
                return jsonify({"error": "Each selected library must be unique"}), 400
            seen.add(key)
            instance = next((value for value in runtime.config.instances
                             if value.name == key[0]), None)
            library = next((value for value in instance.libraries
                            if value.name == key[1]), None) if instance else None
            plex = runtime.plex_clients.get(key[0])
            if not instance or not library or plex is None:
                return jsonify({"error": f"Plex library not found: {key[0]} / {key[1]}"}), 404
            jobs.append((instance, library, plex))

    with runtime._library_refresh_queue_lock:
        if runtime._library_refresh_queue.get("running"):
            return jsonify({"error": "A library refresh queue is already active"}), 409
        runtime._library_refresh_queue.clear()
        runtime._library_refresh_queue.update({
            "running": True, "state": "queued", "current": 0,
            "total": len(jobs), "completed": 0, "failed": 0,
            "started_at": runtime._utc_now(), "library": "",
        })

    def run_queue():
        completed = 0
        failed = 0
        try:
            for runtime.index, (instance, library, plex) in enumerate(jobs, 1):
                with runtime._library_refresh_queue_lock:
                    runtime._library_refresh_queue.update({
                        "state": "running", "current": runtime.index,
                        "library": f"{instance.name} / {library.name}",
                        "completed": completed, "failed": failed,
                    })
                result = runtime.library_refresh.run(
                    instance, library, plex, source="manual",
                )
                if result.get("ok"):
                    completed += 1
                else:
                    failed += 1
        except Exception as exc:
            runtime.logger.exception("Library refresh queue failed")
            failed += 1
            with runtime._library_refresh_queue_lock:
                runtime._library_refresh_queue["error"] = type(exc).__name__
        finally:
            with runtime._library_refresh_queue_lock:
                runtime._library_refresh_queue.update({
                    "running": False,
                    "state": "completed" if not failed else "completed_with_errors",
                    "completed": completed, "failed": failed,
                    "finished_at": runtime._utc_now(),
                })

    threading.Thread(
        target=run_queue, daemon=True, name="library-refresh",
    ).start()
    return jsonify({"status": "triggered", "libraries": len(jobs)}), 202


@bp.route("/api/metadata-audit/status", methods=["GET"])
@require_auth
@requires_feature("metadata_health")
def api_metadata_audit_status():
    with runtime._runtime_lock:
        instances = [instance.name for instance in runtime.config.instances]
        ignored = {
            instance.name: list(instance.metadata_health.ignored_libraries)
            for instance in runtime.config.instances
        }
        libraries = {
            instance.name: [
                {"name": library.name, "type": library.type}
                for library in instance.libraries
            ]
            for instance in runtime.config.instances
        }
    return jsonify({
        "instances": instances,
        "audits": runtime._read_metadata_audits(),
        "ignored_libraries": ignored,
        "libraries": libraries,
    })


@bp.route("/api/metadata-audit/run", methods=["POST"])
@require_auth
@requires_feature("metadata_health")
def api_metadata_audit_run():
    requested = str((request.get_json(silent=True) or {}).get("instance", ""))
    with runtime._runtime_lock:
        instance = next((item for item in runtime.config.instances
                         if item.name == requested), None)
        plex = runtime.plex_clients.get(requested)
    if instance is None or plex is None:
        return jsonify({"ok": False, "error": "Plex instance not found"}), 404

    try:
        sections = plex.get_sections()
    except Exception as exc:
        runtime.logger.warning("Metadata audit could not list Plex libraries for %s (%s)",
                       requested, type(exc).__name__)
        return jsonify({
            "ok": False,
            "error": "Plex libraries could not be read",
        }), 502

    by_id = {str(section["id"]): section for section in sections}
    by_name = {str(section["title"]).casefold(): section for section in sections}
    machine_id = instance.machine_id or plex.get_machine_identifier() or ""
    libraries = []
    total_items = 0
    unmatched_count = 0
    error_count = 0
    ignored_names = {
        name.casefold() for name in instance.metadata_health.ignored_libraries
    }
    for library in instance.libraries:
        if library.name.casefold() in ignored_names:
            continue
        section = (
            by_id.get(str(library.section_id))
            if library.section_id else by_name.get(library.name.casefold())
        )
        if not section or section.get("type") not in {"movie", "show"}:
            continue
        try:
            result = plex.get_unmatched_items(str(section["id"]))
            items = result["items"]
            for item in items:
                item["plex_url"] = runtime._plex_details_url(
                    machine_id, item["metadata_key"],
                )
            library_result = {
                "name": library.name,
                "section_id": str(section["id"]),
                "type": section["type"],
                "total_items": result["total_items"],
                "unmatched_count": len(items),
                "items": items,
            }
            total_items += result["total_items"]
            unmatched_count += len(items)
        except Exception as exc:
            runtime.logger.warning("Metadata audit failed for %s / %s (%s)",
                           requested, library.name, type(exc).__name__)
            library_result = {
                "name": library.name,
                "section_id": str(section["id"]),
                "type": section["type"],
                "error": "Plex library items could not be read",
                "unmatched_count": 0,
                "items": [],
            }
            error_count += 1
        libraries.append(library_result)

    audit = {
        "instance": instance.name,
        "machine_id": machine_id,
        "audited_at": runtime._utc_now(),
        "total_items": total_items,
        "unmatched_count": unmatched_count,
        "error_count": error_count,
        "libraries": libraries,
    }
    with runtime._metadata_audit_lock:
        audits = runtime._read_metadata_audits()
        audits[instance.name] = audit
        atomic_write_json(str(runtime._metadata_audit_path), audits)
    return jsonify({"ok": True, **audit})
