"""HTTP routes for Mark-it-Watched.

Split out of app.py, which held every feature's web layer in one module. State
is reached through ``runtime`` rather than imported, because app.py rebinds
``config`` on every reload and the tests patch it.
"""

from __future__ import annotations

import os
import secrets
import urllib.parse

import yaml
from flask import Blueprint, jsonify, request, url_for

from src.auth import current_identity, has_valid_api_token, require_auth
from src.sonarr_client import (
    SonarrClient, SonarrError, normalize_callback_url, normalize_sonarr_url,
)
from src.web.context import requires_feature, runtime, serialized_config_write

bp = Blueprint("mark_watched", __name__)

_serialized_config_write = serialized_config_write


def _valid_sonarr_webhook_auth() -> bool:
    """Authenticate automation without granting it a browser session."""
    if has_valid_api_token(runtime.config):
        return True
    supplied = request.headers.get("X-Sonarr-Webhook-Secret", "")
    authorization = request.headers.get("Authorization", "")
    if not supplied and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    expected = runtime.config.mark_watched.webhook_secret
    return bool(expected and supplied and secrets.compare_digest(supplied, expected))


def _mark_watched_library(instance_name: str, library_name: str):
    with runtime._runtime_lock:
        for instance in runtime.config.instances:
            if instance.name != instance_name:
                continue
            for library in instance.libraries:
                if library.name == library_name:
                    return instance, library, runtime.plex_clients.get(instance.name)
    return None, None, None


def _ensure_sonarr_webhook_secret() -> str:
    """Return the runtime secret, generating and saving one when needed."""
    if runtime.config.mark_watched.webhook_secret:
        return runtime.config.mark_watched.webhook_secret
    try:
        with open(runtime.CONFIG_PATH, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except OSError:
        raw = {}
    mark_settings = raw.get("mark_watched")
    if not isinstance(mark_settings, dict):
        mark_settings = {}
        raw["mark_watched"] = mark_settings
    secret = secrets.token_urlsafe(32)
    mark_settings["webhook_secret"] = secret
    runtime._save_and_apply(raw, require_paths=False)
    return secret


@bp.route("/api/webhooks/sonarr", methods=["POST"])
def api_sonarr_webhook():
    """Accept only completed imports and hand them to the durable worker."""
    if not _valid_sonarr_webhook_auth():
        return jsonify({"ok": False, "error": "Unauthorized Sonarr webhook"}), 401
    # Checked after the secret so an unauthenticated caller cannot learn
    # which features this install has switched on.
    if not runtime.config.features.mark_watched:
        return jsonify({"ok": False, "error": "Mark-it-Watched is disabled"}), 409
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "A JSON webhook payload is required"}), 400
    connection_id = request.headers.get("X-MediaMender-Connection-ID", "").strip()
    if connection_id:
        owner = runtime.sonarr_connection.owner_for(connection_id)
        if not owner:
            return jsonify({"ok": False, "error": "Unknown Sonarr connection"}), 401
        payload = dict(payload)
        payload["_mediamender_user"] = owner
        payload["_mediamender_connection"] = connection_id
    try:
        record, created = runtime.mark_watched.enqueue(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if record is None:
        return jsonify({"ok": True, "test": True, "message": "Sonarr webhook authenticated"})
    return jsonify({
        "ok": True,
        "queued": created,
        "duplicate": not created,
        "job_id": record["id"],
        "status": record["status"],
    }), 202 if created else 200


@bp.route("/api/mark-watched/status", methods=["GET"])
@require_auth
@requires_feature("mark_watched")
def api_mark_watched_status():
    return jsonify({
        **runtime.mark_watched.status(),
        "workers": runtime.mark_watched.workers,
        "live_workers": runtime.mark_watched.live_workers(),
    })


@bp.route("/api/mark-watched/retry", methods=["POST"])
@require_auth
@requires_feature("mark_watched")
def api_mark_watched_retry():
    """Force every job that has not succeeded back onto the durable queue."""
    try:
        # Revive the worker first so the button also recovers a stalled queue.
        runtime.mark_watched.start()
        summary = runtime.mark_watched.retry_unfinished()
    except OSError:
        runtime.logger.exception("Could not re-queue unfinished Mark-it-Watched jobs")
        return jsonify({
            "ok": False,
            "error": "Could not save the re-queued Mark-it-Watched jobs",
        }), 500
    requeued = summary["requeued"]
    parts = []
    if requeued:
        parts.append(f"Re-queued {requeued} job(s)")
    if summary["already_queued"]:
        parts.append(f"{summary['already_queued']} already waiting")
    if summary["in_flight"]:
        parts.append(f"{summary['in_flight']} already running")
    return jsonify({
        **summary,
        "ok": True,
        "message": "; ".join(parts) or "Every Mark-it-Watched job already succeeded",
    })


def _sonarr_environment_instances() -> list[dict]:
    """Return configured Sonarr URLs and key availability without exposing keys."""
    instances = []
    seen_urls = set()
    for url_variable in sorted(os.environ):
        if not url_variable.startswith("SONARR_") or not url_variable.endswith("_URL"):
            continue
        slug = url_variable[len("SONARR_"):-len("_URL")]
        if not slug:
            continue
        try:
            configured_url = normalize_sonarr_url(os.environ.get(url_variable, ""))
        except ValueError:
            continue
        if configured_url in seen_urls:
            continue
        key_variable = f"SONARR_{slug}_API_KEY"
        instances.append({
            "sonarr_url": configured_url,
            "environment_label": slug,
            "url_variable": url_variable,
            "key_variable": key_variable,
            "api_key": os.environ.get(key_variable, "").strip(),
        })
        seen_urls.add(configured_url)
    return instances


def _sonarr_environment_entry(sonarr_url: str) -> tuple[str, str, str] | None:
    """Return the URL variable, key variable, and key paired to this URL."""
    for instance in _sonarr_environment_instances():
        if instance["sonarr_url"] != sonarr_url:
            continue
        return (
            instance["url_variable"], instance["key_variable"],
            instance["api_key"],
        )
    return None


def _suggest_sonarr_environment_names(sonarr_url: str) -> tuple[str, str]:
    host = urllib.parse.urlparse(sonarr_url).hostname or "main"
    slug = "".join(
        character if character.isalnum() else "_" for character in host
    ).upper().strip("_")
    if slug == "SONARR":
        slug = "MAIN"
    elif slug.startswith("SONARR_"):
        slug = slug[len("SONARR_"):]
    slug = slug or "MAIN"
    return f"SONARR_{slug}_URL", f"SONARR_{slug}_API_KEY"


def _sonarr_api_key(data: dict, sonarr_url: str) -> str:
    """Resolve a typed key, URL-paired environment key, or global fallback."""
    typed = str(data.get("api_key", "")).strip()
    if typed:
        return typed
    entry = _sonarr_environment_entry(sonarr_url)
    if entry and entry[2]:
        return entry[2]
    return os.environ.get("SONARR_API_KEY", "").strip()


def _missing_sonarr_api_key_message(sonarr_url: str) -> str:
    entry = _sonarr_environment_entry(sonarr_url)
    if entry:
        url_variable, key_variable, _key = entry
    else:
        url_variable, key_variable = _suggest_sonarr_environment_names(sonarr_url)
    return (
        "Sonarr API key is required. Enter it here, set the fallback SONARR_API_KEY, "
        f"or pair {url_variable} with {key_variable}."
    )


@bp.route("/api/mark-watched/sonarr", methods=["GET"])
@require_auth
@requires_feature("mark_watched")
def api_mark_watched_sonarr_status():
    try:
        return _mark_watched_sonarr_status_response()
    except Exception as exc:
        runtime.logger.exception("Could not build Sonarr webhook connection status")
        return jsonify({
            "ok": False,
            "error": f"Could not load Sonarr connection status: {type(exc).__name__}: {exc}",
        }), 500


def _mark_watched_sonarr_status_response():
    saved = runtime.sonarr_connection.status()
    saved_by_url = {}
    for connection in saved.get("connections", []):
        try:
            sonarr_url = normalize_sonarr_url(connection.get("sonarr_url", ""))
        except ValueError:
            connection["api_key_available"] = False
            continue
        connection["sonarr_url"] = sonarr_url
        saved_by_url[sonarr_url] = connection

    connections = []
    for configured in _sonarr_environment_instances():
        sonarr_url = configured["sonarr_url"]
        connection = saved_by_url.pop(sonarr_url, None)
        saved_record = connection is not None
        connection = dict(connection or {
            "sonarr_url": sonarr_url,
            "sonarr_instance": configured["environment_label"].replace("_", " ").title(),
            "status": "not_connected",
        })
        connection["configured_from_environment"] = True
        connection["environment_label"] = configured["environment_label"]
        connection["saved_record"] = saved_record
        connection["api_key_available"] = bool(_sonarr_api_key({}, sonarr_url))
        connections.append(connection)

    for sonarr_url, connection in saved_by_url.items():
        connection["configured_from_environment"] = False
        connection["saved_record"] = True
        connection["api_key_available"] = bool(_sonarr_api_key({}, sonarr_url))
        connections.append(connection)

    return jsonify({"ok": True, "connections": connections})


@bp.route("/api/mark-watched/sonarr/connect", methods=["POST"])
@require_auth
@_serialized_config_write
@requires_feature("mark_watched")
def api_mark_watched_sonarr_connect():
    """Use a Sonarr API key once to test and provision the managed webhook."""
    data = request.get_json(silent=True) or {}
    sonarr_url = ""
    callback_url = ""
    try:
        sonarr_url = normalize_sonarr_url(data.get("sonarr_url", ""))
        callback_url = normalize_callback_url(data.get("callback_url", ""))
        api_key = _sonarr_api_key(data, sonarr_url)
        if not api_key:
            raise ValueError(_missing_sonarr_api_key_message(sonarr_url))
        client = SonarrClient(sonarr_url, api_key)
        # Verify the key before creating a local webhook secret.
        sonarr_status = client.system_status()
        webhook_secret = _ensure_sonarr_webhook_secret()
        owner = runtime._current_username()
        pending = runtime.sonarr_connection.prepare(sonarr_url, owner)
        result = client.provision_webhook(
            callback_url, webhook_secret, status=sonarr_status,
            connection_id=pending["connection_id"],
        )
        connection = runtime.sonarr_connection.success(
            sonarr_url, result, owner=owner,
            connection_id=pending["connection_id"],
        )
        public_connection = {
            key: value for key, value in connection.items()
            if key != "connection_id"
        }
        runtime.logger.info(
            "Sonarr webhook %s for %s (notification %s)",
            result["action"], result.get("sonarr_instance", "Sonarr"),
            result.get("notification_id", "unknown"),
        )
        return jsonify({
            "ok": True,
            "connection": public_connection,
            "message": (
                f"Sonarr webhook {result['action']} and its Test event succeeded."
            ),
        })
    except (ValueError, SonarrError) as exc:
        if sonarr_url and callback_url:
            try:
                runtime.sonarr_connection.failure(sonarr_url, callback_url, str(exc))
            except OSError:
                runtime.logger.warning("Could not persist Sonarr connection failure status")
        runtime.logger.warning("Sonarr webhook provisioning failed (%s)", type(exc).__name__)
        return jsonify({"ok": False, "error": str(exc)}), 400
    except OSError:
        runtime.logger.exception("Could not save Sonarr webhook configuration")
        return jsonify({
            "ok": False,
            "error": "Sonarr connected, but mediaMender could not save its local configuration",
        }), 500


@bp.route("/api/mark-watched/sonarr", methods=["DELETE"])
@require_auth
@requires_feature("mark_watched")
def api_mark_watched_sonarr_remove():
    """Remove the managed Sonarr webhook, then forget its local status."""
    data = request.get_json(silent=True) or {}
    try:
        sonarr_url = normalize_sonarr_url(data.get("sonarr_url", ""))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    connection = runtime.sonarr_connection.get(sonarr_url)
    if connection is None:
        return jsonify({"ok": False, "error": "Sonarr connection was not found"}), 404
    api_key = _sonarr_api_key(data, sonarr_url)
    remote_record = (
        connection.get("status") == "connected"
        or bool(connection.get("notification_id"))
    )

    # Removing the local record always succeeds, even when the webhook in Sonarr can't be deleted.
    # Previously a missing key or an unreachable Sonarr aborted the whole thing, which left a
    # broken connection permanently stuck in the list: the one state you most want to remove was
    # the one you couldn't. Anything left behind in Sonarr is reported rather than hidden, since
    # that connection is now unmanaged and has to be deleted there by hand.
    removed_webhooks = 0
    leftover = ""
    if remote_record:
        if not api_key:
            leftover = "no Sonarr API key was available, so its webhook was left in place"
        else:
            try:
                removed_webhooks = SonarrClient(sonarr_url, api_key).remove_webhook()
            except (ValueError, SonarrError) as exc:
                leftover = f"its webhook could not be deleted from Sonarr ({exc})"

    try:
        runtime.sonarr_connection.remove(sonarr_url)
    except OSError:
        runtime.logger.exception("Could not remove saved Sonarr connection status")
        return jsonify({"ok": False, "error": "Could not remove saved Sonarr status"}), 500

    if leftover:
        message = f"Connection removed from mediaMender, but {leftover}"
    elif remote_record:
        message = "Managed Sonarr webhook and saved connection removed"
    else:
        message = "Failed Sonarr connection record removed"
    return jsonify({
        "ok": True,
        "removed_webhooks": removed_webhooks,
        "webhook_left_behind": bool(leftover),
        "message": message,
    })


@bp.route("/api/mark-watched/libraries", methods=["GET"])
@require_auth
@requires_feature("mark_watched")
def api_mark_watched_libraries():
    configured_visibility = runtime.config.mark_watched.visible_libraries
    visible = set(configured_visibility or [])
    result = []
    with runtime._runtime_lock:
        instances = list(runtime.config.instances)
    for instance in instances:
        plex = runtime.plex_clients.get(instance.name)
        if plex is None:
            continue
        for library in instance.libraries:
            library_key = f"{instance.name}::{library.name}"
            if configured_visibility is not None and library_key not in visible:
                continue
            section_id = library.section_id or plex.find_section_id(library.name)
            if not section_id or plex.get_section_type(str(section_id)) != "show":
                continue
            try:
                shows = plex.list_tv_shows(str(section_id))
            except Exception as exc:
                runtime.logger.warning("Could not load Mark-it-Watched library %s (%s)",
                               library_key, type(exc).__name__)
                result.append({"instance": instance.name, "library": library.name,
                               "section_id": str(section_id), "shows": [],
                               "error": "Plex library could not be loaded"})
                continue
            for show in shows:
                rule = runtime.mark_watched_rules.rule(
                    instance.name, library.name, show["rating_key"], 0,
                )
                show["rule_enabled"] = rule["show_enabled"]
                show["poster_url"] = url_for(
                    ".api_mark_watched_poster", instance_name=instance.name,
                    key=show.get("thumb", ""),
                ) if show.get("thumb") else ""
                show.pop("thumb", None)
            result.append({"instance": instance.name, "library": library.name,
                           "section_id": str(section_id), "shows": shows})
    return jsonify({"libraries": result, "jobs": runtime.mark_watched.status(10)["jobs"]})


@bp.route("/api/mark-watched/options", methods=["GET"])
@require_auth
@requires_feature("mark_watched")
def api_mark_watched_options():
    """List configured servers, then TV libraries for one selected server."""
    requested = str(request.args.get("instance", "")).strip()
    configured_visibility = runtime.config.mark_watched.visible_libraries
    visible = set(configured_visibility or [])
    with runtime._runtime_lock:
        instances = list(runtime.config.instances)
    available_instances = [{
        "name": instance.name,
        "library_count": sum(
            1 for library in instance.libraries
            if configured_visibility is None
            or f"{instance.name}::{library.name}" in visible
        ),
    } for instance in instances if any(
        configured_visibility is None
        or f"{instance.name}::{library.name}" in visible
        for library in instance.libraries
    )]
    if not requested:
        return jsonify({"instances": available_instances, "libraries": []})

    instance = next((item for item in instances if item.name == requested), None)
    plex = runtime.plex_clients.get(requested)
    if instance is None or plex is None:
        return jsonify({"error": "Unknown Plex server"}), 404
    try:
        sections = plex.get_sections()
    except Exception as exc:
        runtime.logger.warning("Could not list TV libraries for %s (%s)",
                       requested, type(exc).__name__)
        return jsonify({"error": "Plex TV libraries could not be loaded"}), 502
    by_id = {str(section.get("id", "")): section for section in sections}
    by_title = {str(section.get("title", "")): section for section in sections}
    libraries = []
    for library in instance.libraries:
        key = f"{instance.name}::{library.name}"
        if configured_visibility is not None and key not in visible:
            continue
        section = by_id.get(str(library.section_id or "")) or by_title.get(library.name)
        if not section or section.get("type") != "show":
            continue
        libraries.append({
            "name": library.name,
            "section_id": str(section["id"]),
        })
    return jsonify({"instances": available_instances, "libraries": libraries})


@bp.route("/api/mark-watched/shows", methods=["GET"])
@require_auth
@requires_feature("mark_watched")
def api_mark_watched_shows():
    """Return one bounded page from one explicitly selected Plex TV library."""
    instance_name = str(request.args.get("instance", "")).strip()
    library_name = str(request.args.get("library", "")).strip()
    search = str(request.args.get("q", "")).strip()
    if len(search) > 100:
        return jsonify({"error": "Show search cannot exceed 100 characters"}), 400
    try:
        page = max(1, int(request.args.get("page", 1)))
        page_size = int(request.args.get("page_size", 12))
    except (TypeError, ValueError):
        return jsonify({"error": "Valid page and page_size are required"}), 400
    if page_size not in {12, 24, 36, 48}:
        return jsonify({"error": "page_size must be 12, 24, 36, or 48"}), 400
    instance, library, plex = _mark_watched_library(instance_name, library_name)
    if instance is None or library is None or plex is None:
        return jsonify({"error": "Unknown Plex TV library"}), 404
    configured_visibility = runtime.config.mark_watched.visible_libraries
    if configured_visibility is not None and (
        f"{instance_name}::{library_name}" not in set(configured_visibility)
    ):
        return jsonify({"error": "This Plex library is hidden in Settings"}), 404
    section_id = library.section_id or plex.find_section_id(library.name)
    if not section_id or plex.get_section_type(str(section_id)) != "show":
        return jsonify({"error": "Mark-it-Watched supports TV libraries only"}), 400
    try:
        if search:
            result = plex.list_tv_shows_page(
                str(section_id), (page - 1) * page_size, page_size,
                query=search,
            )
        else:
            result = plex.list_tv_shows_page(
                str(section_id), (page - 1) * page_size, page_size,
            )
    except Exception as exc:
        runtime.logger.warning("Could not load Mark-it-Watched page for %s::%s (%s)",
                       instance_name, library_name, type(exc).__name__)
        return jsonify({"error": "Plex shows could not be loaded"}), 502
    shows = result["shows"]
    for show in shows:
        rule = runtime.mark_watched_rules.rule(
            instance_name, library_name, show["rating_key"], 0,
        )
        show["rule_enabled"] = rule["show_enabled"]
        show["poster_url"] = url_for(
            ".api_mark_watched_poster", instance_name=instance_name,
            key=show.get("thumb", ""),
        ) if show.get("thumb") else ""
        show.pop("thumb", None)
    total = int(result.get("total", len(shows)))
    pages = max(1, (total + page_size - 1) // page_size)
    return jsonify({
        "instance": instance_name,
        "library": library_name,
        "section_id": str(section_id),
        "shows": shows,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "total": total,
        "search": search,
    })


@bp.route("/api/mark-watched/seasons", methods=["GET"])
@require_auth
@requires_feature("mark_watched")
def api_mark_watched_seasons():
    instance_name = request.args.get("instance", "")
    library_name = request.args.get("library", "")
    show_key = request.args.get("show", "")
    _instance, _library, plex = _mark_watched_library(instance_name, library_name)
    if plex is None or not show_key.isdigit():
        return jsonify({"error": "Unknown Plex show"}), 404
    try:
        seasons = plex.list_show_seasons(show_key)
        for season in seasons:
            rule = runtime.mark_watched_rules.rule(
                instance_name, library_name, show_key, season["index"],
            )
            season["rule"] = rule
            season["poster_url"] = url_for(
                ".api_mark_watched_poster", instance_name=instance_name,
                key=season.get("thumb", ""),
            ) if season.get("thumb") else ""
            season.pop("thumb", None)
        return jsonify({"seasons": seasons})
    except Exception as exc:
        return jsonify({"error": f"Could not load Plex seasons: {type(exc).__name__}"}), 502


@bp.route("/api/mark-watched/rules", methods=["POST"])
@require_auth
@requires_feature("mark_watched")
def api_mark_watched_rules():
    data = request.get_json(silent=True) or {}
    instance_name = str(data.get("instance", ""))
    library_name = str(data.get("library", ""))
    show_key = str(data.get("show_rating_key", ""))
    _instance, _library, plex = _mark_watched_library(instance_name, library_name)
    if plex is None or not show_key.isdigit():
        return jsonify({"ok": False, "error": "Unknown Plex show"}), 404
    if data.get("scope") == "show" and isinstance(data.get("enabled"), bool):
        runtime.mark_watched_rules.set_show(
            instance_name, library_name, show_key, data["enabled"],
        )
    elif data.get("scope") == "season" and (
        isinstance(data.get("enabled"), bool) or data.get("enabled") is None
    ):
        try:
            season_index = int(data["season_index"])
        except (KeyError, TypeError, ValueError):
            return jsonify({"ok": False, "error": "Valid season_index required"}), 400
        runtime.mark_watched_rules.set_season(
            instance_name, library_name, show_key,
            season_index, data.get("enabled"),
        )
    else:
        return jsonify({"ok": False, "error": "Invalid rule update"}), 400
    return jsonify({"ok": True})


@bp.route("/api/mark-watched/apply", methods=["POST"])
@require_auth
@requires_feature("mark_watched")
def api_mark_watched_apply():
    """Queue an explicit one-time update to existing Plex watch history."""
    data = request.get_json(silent=True) or {}
    scope = str(data.get("scope", ""))
    if scope not in {"show", "season"}:
        return jsonify({"ok": False, "error": "Scope must be show or season"}), 400
    if data.get("confirm") != "MARK WATCHED NOW":
        return jsonify({
            "ok": False,
            "error": "Confirmation must be MARK WATCHED NOW",
        }), 400
    instance_name = str(data.get("instance", ""))
    library_name = str(data.get("library", ""))
    show_key = str(data.get("show_rating_key", ""))
    _instance, _library, plex = _mark_watched_library(instance_name, library_name)
    if plex is None or not show_key.isdigit():
        return jsonify({"ok": False, "error": "Unknown Plex show"}), 404
    manual = {
        "scope": scope,
        "instance": instance_name,
        "library": library_name,
        "show_rating_key": show_key,
    }
    if scope == "season":
        try:
            manual["season_index"] = int(data["season_index"])
        except (KeyError, TypeError, ValueError):
            return jsonify({
                "ok": False, "error": "Valid season_index required",
            }), 400
    title = str(data.get("show_title", "")).strip()[:200] or "Plex show"
    record = runtime.mark_watched.enqueue_manual({
        "series": {"title": title},
        "manual": manual,
        "rule_user": runtime._current_username(),
    })
    return jsonify({
        "ok": True,
        "queued": True,
        "job_id": record["id"],
        "status": record["status"],
        "message": record["message"],
    }), 202


@bp.route("/api/mark-watched/all", methods=["POST"])
@require_auth
@requires_feature("mark_watched")
def api_mark_watched_all():
    data = request.get_json(silent=True) or {}
    enabled = data.get("enabled")
    expected = "ALL ON" if enabled is True else "ALL OFF" if enabled is False else ""
    if data.get("confirm") != expected:
        return jsonify({"ok": False, "error": f"Confirmation must be {expected or 'valid'}"}), 400
    show_keys = []
    configured_visibility = runtime.config.mark_watched.visible_libraries
    visible = set(configured_visibility or [])
    for instance in runtime.config.instances:
        plex = runtime.plex_clients.get(instance.name)
        if plex is None:
            continue
        for library in instance.libraries:
            key = f"{instance.name}::{library.name}"
            if configured_visibility is not None and key not in visible:
                continue
            section_id = library.section_id or plex.find_section_id(library.name)
            if not section_id or plex.get_section_type(str(section_id)) != "show":
                continue
            for show in plex.list_tv_shows(str(section_id)):
                show_keys.append((instance.name, library.name, show["rating_key"]))
    runtime.mark_watched_rules.set_all(show_keys, enabled)
    return jsonify({"ok": True, "enabled": enabled, "shows": len(show_keys),
                    "message": "Future automatic rules updated; Plex history was not changed"})


@bp.route("/api/mark-watched/poster", methods=["GET"])
@require_auth
@requires_feature("mark_watched")
def api_mark_watched_poster():
    instance_name = request.args.get("instance_name", "")
    artwork_key = request.args.get("key", "")
    plex = runtime.plex_clients.get(instance_name)
    if plex is None:
        return "", 404
    try:
        artwork = plex.get_artwork(artwork_key)
        return Response(
            artwork.content,
            content_type=artwork.headers.get("Content-Type", "image/jpeg"),
            headers={"Cache-Control": "private, max-age=3600"},
        )
    except Exception:
        return "", 404
