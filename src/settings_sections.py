"""Decide which part of config.yml each Settings section may write.

The Settings UI used to POST the whole configuration on every save, with a
hint naming the section the user was actually on. Anything the browser failed
to render - a collapsed section, a list that had not loaded - was submitted as
absent and overwrote what was on disk, which is why the save handler grew
salvage heuristics for the Plex instance list.

Ownership is declared here instead, and the server takes only the fields the
named section owns. A field nobody owns, such as ``clean_bundles_before_empty``
which has no control in the UI, is preserved untouched.
"""

from __future__ import annotations

import copy

# Top-level config.yml keys each section is allowed to replace.
SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "plex": (),
    "trash-removal": (
        "max_trash_items", "max_trash_percent", "schedule",
    ),
    "library-refresh": (),
    "mark-watched": ("mark_watched",),
    "metadata-health": (),
    "timestamp-repair": ("timestamp_repair_workers",),
    "features": ("features",),
    "providers": ("providers",),
    "notifications": ("discord_webhook", "notify", "notifications"),
    "general": ("log_level", "logging"),
    "security": ("auth",),
    "about": (),
}

# Per-instance keys each section owns inside plex_instances[].
SECTION_INSTANCE_KEYS: dict[str, tuple[str, ...]] = {
    "plex": ("name", "url", "token", "machine_id"),
    "metadata-health": ("metadata_health",),
    "timestamp-repair": ("timestamp_repair",),
}

# Per-library keys each section owns inside plex_instances[].libraries[].
SECTION_LIBRARY_KEYS: dict[str, tuple[str, ...]] = {
    "trash-removal": ("name", "type", "paths", "cron", "section_id"),
    "library-refresh": (
        "refresh_enabled", "refresh_cron", "refresh_guard_minutes",
    ),
}

# Sections whose submitted list is authoritative, so entries may be added and
# removed. Every other section may only update entries that already exist.
SECTIONS_OWNING_INSTANCE_LIST = frozenset({"plex"})
SECTIONS_OWNING_LIBRARY_LIST = frozenset({"plex", "trash-removal"})


def known_section(section: str) -> bool:
    return section in SECTION_KEYS


def _by_name(entries) -> dict[str, dict]:
    return {
        str(entry.get("name", "")): entry
        for entry in entries if isinstance(entry, dict)
    }


def _overlay(target: dict, source: dict, keys: tuple[str, ...]) -> dict:
    """Copy only ``keys`` that the request actually supplied."""
    merged = dict(target)
    for key in keys:
        if key in source:
            merged[key] = copy.deepcopy(source[key])
    return merged


def _merge_libraries(section: str, existing: list, submitted: list) -> list:
    library_keys = SECTION_LIBRARY_KEYS.get(section)
    if library_keys is None:
        return existing
    submitted_by_name = _by_name(submitted)
    if section in SECTIONS_OWNING_LIBRARY_LIST:
        # The submitted list decides which libraries exist, but unowned keys
        # on a surviving library still come from disk.
        existing_by_name = _by_name(existing)
        return [
            _overlay(
                existing_by_name.get(str(item.get("name", "")), {}),
                item, library_keys,
            )
            for item in submitted if isinstance(item, dict)
        ]
    return [
        _overlay(item, submitted_by_name.get(str(item.get("name", "")), {}), library_keys)
        if isinstance(item, dict) else item
        for item in existing
    ]


def _merge_instances(section: str, existing: list, submitted: list) -> list:
    instance_keys = SECTION_INSTANCE_KEYS.get(section, ())
    touches_libraries = section in SECTION_LIBRARY_KEYS
    if not instance_keys and not touches_libraries:
        return existing

    def merge_one(current: dict, incoming: dict) -> dict:
        merged = _overlay(current, incoming, instance_keys)
        if touches_libraries:
            merged["libraries"] = _merge_libraries(
                section,
                current.get("libraries", []) if isinstance(current.get("libraries"), list) else [],
                incoming.get("libraries", []) if isinstance(incoming.get("libraries"), list) else [],
            )
        return merged

    if section in SECTIONS_OWNING_INSTANCE_LIST:
        existing_by_name = _by_name(existing)
        return [
            merge_one(existing_by_name.get(str(item.get("name", "")), {}), item)
            for item in submitted if isinstance(item, dict)
        ]
    submitted_by_name = _by_name(submitted)
    return [
        merge_one(item, submitted_by_name.get(str(item.get("name", "")), {}))
        if isinstance(item, dict) else item
        for item in existing
    ]


def apply_section(existing: dict, section: str, data: dict) -> dict:
    """Return ``existing`` with only ``section``'s owned fields taken from ``data``.

    ``existing`` is not modified. A key the section does not own is carried
    through untouched even when the request supplies a value for it.
    """
    if not known_section(section):
        raise ValueError(f"Unknown settings section: {section}")
    merged = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    for key in SECTION_KEYS[section]:
        if key in data:
            merged[key] = copy.deepcopy(data[key])
    submitted_instances = data.get("plex_instances", data.get("instances", []))
    if not isinstance(submitted_instances, list):
        submitted_instances = []
    current_instances = merged.get("plex_instances", [])
    if not isinstance(current_instances, list):
        current_instances = []
    merged["plex_instances"] = _merge_instances(
        section, current_instances, submitted_instances,
    )
    return merged
