"""Single definition of the optional features mediaMender exposes.

Feature identity used to be restated in the config dataclass, the permission
prefix table, ad-hoc route guards, and several places in the template. Each
copy could drift on its own, and one of them did: every Mark-it-Watched route
shipped without a feature guard, so turning the feature off hid it from the
navigation while the Sonarr webhook kept marking episodes watched.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Feature:
    """One optional feature, named once for every layer that needs it."""

    key: str
    """Attribute on FeatureConfig, and the permission name."""

    label: str
    """User-facing name, used in navigation and in API error messages."""

    page: str
    """Front-end page id, without the ``page-`` prefix."""

    description: str
    """One-line summary shown beside the toggle in Settings."""

    route_prefixes: tuple[str, ...]
    """API path prefixes this feature owns."""


FEATURES: tuple[Feature, ...] = (
    Feature(
        key="trash_removal",
        label="Trash Removal",
        page="trash-removal",
        description="Protected Plex Empty Trash scheduling and manual runs.",
        route_prefixes=(
            "/api/run", "/api/dryrun", "/api/checks", "/api/scheduling",
        ),
    ),
    Feature(
        key="library_refresh",
        label="Library Refresh",
        page="library-refresh",
        description="Scheduled Plex scan requests for libraries without an external trigger.",
        route_prefixes=("/api/library-refresh",),
    ),
    Feature(
        key="mark_watched",
        label="Mark-it-Watched",
        page="mark-watched",
        description="Automatic Plex watch rules applied to finalized Sonarr imports.",
        route_prefixes=("/api/mark-watched", "/api/webhooks/sonarr"),
    ),
    Feature(
        key="metadata_health",
        label="Metadata Health",
        page="metadata-audit",
        description="Find Plex items that never matched a real metadata source.",
        route_prefixes=("/api/metadata-audit",),
    ),
    Feature(
        key="timestamp_repair",
        label="Timestamp Repair",
        page="timestamp-repair",
        description="Review and repair invalid Plex part timestamps one folder at a time.",
        route_prefixes=("/api/timestamp-repair",),
    ),
)

FEATURES_BY_KEY: dict[str, Feature] = {feature.key: feature for feature in FEATURES}

# Permissions that gate a route prefix without being a toggleable feature.
NON_FEATURE_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("/api/status", "dashboard"),
    ("/api/history", "dashboard"),
    ("/api/config", "settings"),
    ("/api/providers", "settings"),
    ("/api/notifications", "settings"),
    ("/api/plex", "settings"),
    ("/api/wizard", "settings"),
    ("/api/settings", "settings"),
    ("/api/logs", "settings"),
    ("/api/users", "settings"),
    ("/api/auth", "settings"),
)


def feature_label(key: str) -> str:
    feature = FEATURES_BY_KEY.get(key)
    return feature.label if feature else key.replace("_", " ").title()


def permission_prefixes() -> tuple[tuple[str, str], ...]:
    """Return every (route prefix, required permission) pair, longest first.

    Longest-first ordering matters because ``/api/mark-watched`` and
    ``/api/metadata-audit`` would otherwise be shadowed by a shorter prefix
    that happens to be registered before them.
    """
    pairs = [
        (prefix, feature.key)
        for feature in FEATURES
        for prefix in feature.route_prefixes
    ]
    pairs.extend(NON_FEATURE_PERMISSIONS)
    return tuple(sorted(pairs, key=lambda pair: len(pair[0]), reverse=True))
