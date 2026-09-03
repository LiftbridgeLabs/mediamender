"""Late-bound access to the application's mutable runtime state.

Blueprints must not capture these at import time. ``app.config`` is rebound
whenever configuration is reloaded, and the tests replace it with
``patch.object(app, "config", ...)``; a name captured during import would keep
pointing at the object that existed when the module was first loaded.

Attribute access here resolves against the ``app`` module on every read, so a
blueprint sees the same object the rest of the application does.
"""

from __future__ import annotations

import importlib
from typing import Any


class _Runtime:
    """Read-through view onto the ``app`` module's globals."""

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(importlib.import_module("app"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(importlib.import_module("app"), name, value)


runtime = _Runtime()


# ── Shared route decorators ───────────────────────────────────────────────────
# These live here rather than in app.py so blueprints can apply them without
# importing the module that registers them, which would be circular.

import threading  # noqa: E402
from functools import wraps  # noqa: E402

from flask import jsonify  # noqa: E402

from src.features import feature_label  # noqa: E402

# One writer at a time for config.yml, across every route that rewrites it.
config_file_lock = threading.RLock()


def serialized_config_write(function):
    """Serialise handlers that rewrite config.yml."""
    @wraps(function)
    def decorated(*args, **kwargs):
        with config_file_lock:
            return function(*args, **kwargs)
    return decorated


def requires_feature(*keys: str):
    """Refuse a request when every feature owning the route is switched off.

    Feature checks used to be written inline in individual handlers, which
    meant a new route was unguarded by default. Applying this decorator keeps
    the check next to the route it protects and impossible to forget silently.
    """
    def decorate(view):
        @wraps(view)
        def guarded(*args, **kwargs):
            for key in keys:
                if getattr(runtime.config.features, key, True):
                    return view(*args, **kwargs)
            labels = " or ".join(feature_label(key) for key in keys)
            return jsonify({"error": f"{labels} is disabled"}), 409
        guarded._required_features = keys
        return guarded
    return decorate
