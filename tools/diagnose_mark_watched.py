#!/usr/bin/env python3
"""Explain why Mark-it-Watched is or is not marking episodes watched.

Reads only the files mediaMender already writes, so it is safe to run against
a live install and changes nothing:

    docker exec -it mediaMender python tools/diagnose_mark_watched.py

Add --plex to also ask each configured Plex server whether the shows your
rules name still exist under the same ratingKey.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402


def load(path: Path, default):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return default
    try:
        return yaml.safe_load(text) if path.suffix in {".yml", ".yaml"} else json.loads(text)
    except ValueError:
        return default


def heading(text: str) -> None:
    print(f"\n{text}\n{'-' * len(text)}")


def report_config(raw: dict) -> dict:
    heading("Configuration")
    features = raw.get("features", {}) or {}
    enabled = features.get("mark_watched", True) is not False
    print(f"  Feature enabled          : {enabled}")
    if not enabled:
        print("    -> Mark-it-Watched is switched off; nothing will be marked.")

    mark = raw.get("mark_watched", {}) or {}
    secret = bool(mark.get("webhook_secret")) or bool(
        os.environ.get("MEDIAMENDER_SONARR_WEBHOOK_SECRET")
    )
    print(f"  Webhook secret configured: {secret}")
    if not secret:
        print("    -> Sonarr cannot authenticate; every webhook is rejected 401.")

    visible = mark.get("visible_libraries")
    if visible is None:
        print("  Visible libraries        : all (unset)")
    elif not visible:
        print("  Visible libraries        : NONE - every library is hidden")
        print("    -> Nothing can match. Turn libraries back on under")
        print("       Mark-it-Watched > Configure, or delete the key.")
    else:
        print(f"  Visible libraries        : {len(visible)} listed")
        for key in visible:
            print(f"      {key}")

    delays = mark.get("retry_delays") or [15, 30, 60, 120, 300, 600, 900, 1200]
    print(f"  Plex match window        : {round(sum(delays) / 60, 1)} minutes "
          f"over {len(delays) + 1} attempts")
    print(f"  Scan on import           : {mark.get('scan_on_import', True) is not False}")

    tv = []
    for instance in raw.get("plex_instances", []) or []:
        for library in instance.get("libraries", []) or []:
            tv.append(f"{instance.get('name', '?')}::{library.get('name', '?')}")
    print(f"  Configured libraries     : {len(tv)}")
    for key in tv:
        hidden = visible is not None and key not in visible
        print(f"      {key}{'   [hidden]' if hidden else ''}")
    return mark


def report_rules(rules: dict) -> set:
    heading("Rules")
    if "users" in rules:
        print("  Stored in the old per-user format; it migrates on next start.")
        shows, seasons = {}, {}
        for user in (rules.get("users") or {}).values():
            shows.update(user.get("shows", {}) or {})
            seasons.update(user.get("seasons", {}) or {})
    else:
        shows = rules.get("shows", {}) or {}
        seasons = rules.get("seasons", {}) or {}
    on = {key for key, value in shows.items() if value}
    print(f"  Shows with auto-watch ON : {len(on)} of {len(shows)}")
    print(f"  Season overrides         : {len(seasons)}")
    if not on:
        print("    -> No show has a rule enabled, so imports match and stop there.")
    libraries = collections.Counter(
        "::".join(key.split("::")[:2]) for key in on
    )
    for key, count in libraries.most_common():
        print(f"      {key}: {count} shows")
    return on


def report_jobs(jobs: dict) -> None:
    heading("Recent jobs")
    if not jobs:
        print("  No jobs recorded at all.")
        print("    -> No Sonarr webhook has ever reached mediaMender. Check the")
        print("       connection under Mark-it-Watched > Configure, and that")
        print("       Sonarr's Connect entry points at a URL its container can")
        print("       reach.")
        return
    counts = collections.Counter(job.get("status", "?") for job in jobs.values())
    print("  " + ", ".join(f"{status}: {count}" for status, count in counts.most_common()))
    sources = collections.Counter(
        "manual" if (job.get("event", {}) or {}).get("source") == "manual"
        else "sonarr import" for job in jobs.values()
    )
    print("  " + ", ".join(f"{name}: {count}" for name, count in sources.most_common()))
    if not sources.get("sonarr import"):
        print("    -> Every job here was started by hand. No Sonarr import has")
        print("       ever been queued.")
    recent = sorted(jobs.values(), key=lambda job: job.get("updated_at", ""), reverse=True)
    for job in recent[:5]:
        title = (job.get("event", {}).get("series", {}) or {}).get("title", "?")
        print(f"\n  [{job.get('status')}] {title} - {job.get('updated_at', '')[:19]}")
        print(f"      {job.get('message', '')}")
        for entry in (job.get("log") or [])[-6:]:
            print(f"      | {entry.get('message', '')}")


def report_webhooks(log: dict) -> None:
    heading("Sonarr webhook requests")
    attempts = (log or {}).get("attempts") or []
    if not attempts:
        print("  None recorded. Sonarr has never called this endpoint.")
        print("    -> Automatic rules only run when Sonarr calls the webhook.")
        print("       A 'connected' status means the Test event worked, which")
        print("       proves the URL is reachable but not that real imports")
        print("       are being sent. Check that the connection in Sonarr has")
        print("       'On File Import' (onDownload) enabled.")
        return
    counts = collections.Counter(entry.get("outcome", "?") for entry in attempts)
    print("  " + ", ".join(f"{name}: {count}" for name, count in counts.most_common()))
    for entry in attempts[:8]:
        print(f"  {entry.get('at', '')[:19]}  {entry.get('outcome', ''):9s} "
              f"{entry.get('event_type', '-'):16s} {entry.get('series', '')}")
        if entry.get("detail"):
            print(f"      {entry['detail']}")


def report_sonarr(state: dict) -> None:
    heading("Sonarr connections")
    connections = (state or {}).get("connections", {}) or {}
    if not connections:
        print("  None recorded. mediaMender has not provisioned a webhook.")
        return
    for url, entry in connections.items():
        status = entry.get("status", "?")
        print(f"  {url}: {status}")
        if status != "success":
            print(f"      {entry.get('error', '')}")
        print(f"      callback: {entry.get('callback_url', '')}")


def check_plex(raw: dict, enabled_rules: set) -> None:
    heading("Plex ratingKey check")
    from src.plex_client import PlexClient
    for instance in raw.get("plex_instances", []) or []:
        name = instance.get("name", "?")
        token = instance.get("token") or os.environ.get(
            f"PLEX_TOKEN_{name.upper().replace(' ', '_').replace('-', '_')}", ""
        )
        if not token:
            print(f"  {name}: no token available here; skipped")
            continue
        plex = PlexClient(instance.get("url", ""), token)
        for library in instance.get("libraries", []) or []:
            key_prefix = f"{name}::{library.get('name')}::"
            wanted = {k.split('::')[2] for k in enabled_rules if k.startswith(key_prefix)}
            if not wanted:
                continue
            try:
                section = library.get("section_id") or plex.find_section_id(library.get("name"))
                live = {show["rating_key"] for show in plex.list_tv_shows(str(section))}
            except Exception as exc:
                print(f"  {name}::{library.get('name')}: could not read Plex ({exc})")
                continue
            missing = wanted - live
            print(f"  {name}::{library.get('name')}: {len(wanted)} rules, "
                  f"{len(missing)} pointing at a show Plex no longer has")
            if missing:
                print("    -> Those rules are orphaned. Plex reassigns a ratingKey")
                print("       when an item is removed and re-added, which a debrid")
                print("       library does routinely. Re-enable them on the page.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default=os.environ.get("DATA_DIR", "data"))
    parser.add_argument("--config", default=os.environ.get("CONFIG_PATH", ""))
    parser.add_argument("--plex", action="store_true",
                        help="also verify each rule's ratingKey still exists")
    args = parser.parse_args()

    data = Path(args.data)
    config_path = Path(args.config) if args.config else data / "config.yml"

    print(f"config : {config_path}")
    print(f"data   : {data.resolve()}")

    raw = load(config_path, {}) or {}
    if not raw:
        print("\nCould not read the config file. Pass --config /app/data/config.yml")
        return

    report_config(raw)
    enabled_rules = report_rules(load(data / "mark-watched-rules.json", {}) or {})
    report_webhooks(load(data / "sonarr-webhook-log.json", {}) or {})
    report_jobs(load(data / "mark-watched-jobs.json", {}) or {})
    report_sonarr(load(data / "sonarr-webhook.json", {}) or {})
    if args.plex:
        check_plex(raw, enabled_rules)
    print()


if __name__ == "__main__":
    main()
