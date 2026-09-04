"""HTTP routes for Trash Removal runs, checks and scheduling.

Split out of app.py. State is reached through ``runtime`` rather than imported,
because app.py rebinds ``config`` on every reload and the tests patch it.
"""

from __future__ import annotations

import threading

from flask import Blueprint, jsonify, request

from src import runner
from src.auth import require_auth
from src.runner import set_scheduling_enabled
from src.web.context import requires_feature, runtime

bp = Blueprint("trash_removal", __name__)


@bp.route("/api/checks", methods=["GET"])
@require_auth
@requires_feature("trash_removal")
def api_checks():
    results = {}
    with runtime._runtime_lock:
        runtime = [(inst, runtime.plex_clients.get(inst.name))
                   for inst in runtime.config.instances]
    for inst, plex in runtime:
        if plex is None:
            continue
        results[inst.name] = runner.run_instance_checks(inst, plex)
    return jsonify(results)


@bp.route("/api/scheduling", methods=["POST"])
@require_auth
@requires_feature("trash_removal")
def api_scheduling():
    data    = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled", True))
    if enabled and not runtime.config.features.trash_removal:
        return jsonify({
            "scheduling_enabled": False,
            "error": "Trash Removal is disabled",
        }), 409
    set_scheduling_enabled(enabled)
    return jsonify({"scheduling_enabled": enabled})


@bp.route("/api/run/<instance_name>/<library_name>", methods=["POST"])
@require_auth
@requires_feature("trash_removal")
def api_run_library(instance_name: str, library_name: str):
    if runtime._trigger(instance_name, library_name):
        return jsonify({"status": "triggered"})
    return jsonify({"error": "not found"}), 404


@bp.route("/api/dryrun/<instance_name>/<library_name>", methods=["POST"])
@require_auth
@requires_feature("trash_removal")
def api_dryrun_library(instance_name: str, library_name: str):
    if runtime._trigger(instance_name, library_name, dry_run=True):
        return jsonify({"status": "dry_run_triggered"})
    return jsonify({"error": "not found"}), 404


@bp.route("/api/run/all", methods=["POST"])
@require_auth
@requires_feature("trash_removal")
def api_run_all():
    def _run():
        with runtime._runtime_lock:
            live_config = runtime.config
            runtime = [(inst, runtime.plex_clients.get(inst.name))
                       for inst in live_config.instances]
        for inst, plex in runtime:
            if plex is None:
                continue
            plex_checks = runner.run_instance_checks(inst, plex)
            for lib in inst.libraries:
                runner.run_library(inst, lib, live_config, plex,
                                   plex_checks=plex_checks, manual=True)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "triggered"})


@bp.route("/api/dryrun/all", methods=["POST"])
@require_auth
@requires_feature("trash_removal")
def api_dryrun_all():
    def _run():
        with runtime._runtime_lock:
            live_config = runtime.config
            runtime = [(inst, runtime.plex_clients.get(inst.name))
                       for inst in live_config.instances]
        for inst, plex in runtime:
            if plex is None:
                continue
            plex_checks = runner.run_instance_checks(inst, plex)
            for lib in inst.libraries:
                runner.run_library(inst, lib, live_config, plex,
                                   plex_checks=plex_checks, dry_run=True, manual=True)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "dry_run_triggered"})
