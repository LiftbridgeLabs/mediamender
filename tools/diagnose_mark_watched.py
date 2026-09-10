#!/usr/bin/env python3
"""Explain why Mark-it-Watched is or is not marking episodes watched.

Reads only the files mediaMender already writes, so it is safe to run against
a live install and changes nothing:

    docker exec -it mediaMender python tools/diagnose_mark_watched.py

Add --plex to also ask each configured Plex server whether the shows your
rules name still exist under the same ratingKey.

To settle one episode - what the rule says, what Plex holds, and what the job
that handled the import decided:

    docker exec -it mediaMender python tools/diagnose_mark_watched.py         --explain "STAT" --season 2 --episode 67

To explain a whole library at once - every show whose rule is on that still
holds unwatched episodes, and whether an import job ever covered them:

    docker exec -it mediaMender python tools/diagnose_mark_watched.py --audit
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
    # The delays are the ramp, not the deadline: the last one repeats until the
    # give-up window closes. Reporting their sum as "the match window" said 8.7
    # minutes for a job that in fact keeps checking for days.
    give_up = mark.get("give_up_after_hours", 120)
    print(f"  Retry ramp               : {', '.join(str(int(d)) + 's' for d in delays)}, "
          f"then every {int(delays[-1])}s")
    print(f"  Gives up after           : "
          f"{f'{give_up:g} hours' if give_up else 'never'}")
    scanning = mark.get("scan_on_import", True) is not False
    print(f"  Scan on import           : {scanning}")
    if not scanning:
        print("    -> mediaMender waits for Plex to notice the file on its own.")
        print("       On a debrid or webdav library that can take far longer")
        print("       than the ramp above. Turn it on under Mark-it-Watched >")
        print("       Configure to ask Plex to scan the imported folder.")

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
    # A rule keyed by ratingKey is orphaned the moment Plex re-adds the item.
    fragile = {key for key in on if not key.split("::")[-1].startswith("tvdb-")}
    print(f"  Keyed by TVDB id         : {len(on) - len(fragile)}")
    if fragile:
        print(f"  Keyed by Plex ratingKey  : {len(fragile)}")
        print("    -> These break whenever Plex re-adds the item, which a")
        print("       debrid library does routinely. Switching the show off")
        print("       and on again from the rules page stores it by TVDB id.")
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
        # The store writes "connected", so this printed a blank line for every
        # healthy connection.
        if status != "connected" and entry.get("error"):
            print(f"      {entry['error']}")
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


def _plex_clients(raw: dict):
    """Yield (instance name, library dict, client) for every visible TV library."""
    from src.plex_client import PlexClient
    mark = raw.get("mark_watched", {}) or {}
    visible = mark.get("visible_libraries")
    for instance in raw.get("plex_instances", []) or []:
        name = instance.get("name", "?")
        token = instance.get("token") or os.environ.get(
            f"PLEX_TOKEN_{name.upper().replace(' ', '_').replace('-', '_')}", ""
        )
        if not token:
            continue
        plex = PlexClient(instance.get("url", ""), token)
        for library in instance.get("libraries", []) or []:
            key = f"{name}::{library.get('name')}"
            if visible is not None and key not in visible:
                continue
            yield name, library, plex


def explain_episode(raw: dict, rules: dict, jobs: dict, title: str,
                    season: int, episode: int) -> None:
    """Answer "why is this one episode not marked watched?" with evidence.

    Every question so far has needed a screenshot and a guess. The install
    already holds the answer: what the rule says, what Plex has, and what the
    job that handled the import decided.
    """
    heading(f"Rules for {title!r}")
    shows = rules.get("shows", {}) or {}
    matches = {
        key: value for key, value in shows.items()
        if title.strip().casefold() in key.casefold()
    }
    if not matches:
        print("  No rule key mentions this show by name. Rules are keyed by")
        print("  TVDB id or ratingKey, not by title, so this is expected -")
        print("  read the Plex section below for the key, then look for it.")
    for key, value in sorted(matches.items()):
        print(f"  {key}: {'ON' if value else 'OFF'}")

    heading(f"Plex: {title} S{season:02d}E{episode:02d}")
    try:
        clients = list(_plex_clients(raw))
    except Exception as exc:
        print(f"  Could not build Plex clients ({exc})")
        clients = []
    for name, library, plex in clients:
        key = f"{name}::{library.get('name')}"
        try:
            section = library.get("section_id") or plex.find_section_id(library.get("name"))
            if not section or plex.get_section_type(str(section)) != "show":
                continue
            described = plex.describe_show(str(section), title)
            print(f"  {key}: {described or 'no answer'}")
            if not described or described == "does not have this show":
                continue
            shows = plex.list_tv_shows_page(str(section), 0, 50, query=title)["shows"]
            same = [item for item in shows
                    if item.get("tvdb_id") and item["tvdb_id"] == next(
                        (s.get("tvdb_id") for s in shows if s.get("tvdb_id")), "")]
            if len(same) > 1:
                print(f"      NOTE: this library holds {len(same)} entries for "
                      f"this show: {', '.join(item['rating_key'] for item in same)}")
                print("      A new season landing under the second entry is why")
                print("      a job can report every episode watched while one")
                print("      sits unwatched beside it.")
            found = plex.find_episode(str(section), title, season, episode)
            if not found:
                print(f"      no S{season:02d}E{episode:02d} under that numbering")
                continue
            rule_key = f"{key}::{found['show_rating_key']}"
            print(f"      episode ratingKey {found['rating_key']}, "
                  f"show ratingKey {found['show_rating_key']}")
            print(f"      a ratingKey-keyed rule would be {rule_key}")
            for candidate, value in shows.items():
                if candidate.endswith(f"::{found['show_rating_key']}"):
                    print(f"      rule found: {candidate} = {'ON' if value else 'OFF'}")
            episodes = plex.list_show_episodes(found["show_rating_key"])
            this = next(
                (item for item in episodes
                 if item["rating_key"] == found["rating_key"]), None,
            )
            if this:
                print(f"      Plex has it as S{this['season_index']:02d}"
                      f"E{this['episode_index']:02d} {this['title']!r}")
                print(f"      viewCount {this['view_count']}, "
                      f"resume offset {this.get('view_offset', 0)}")
                if this["view_count"] >= 1 and this.get("view_offset", 0) > 0:
                    print("      -> Watched, but a resume point keeps it in")
                    print("         Continue Watching. Mark show watched now")
                    print("         clears it.")
                elif this["view_count"] < 1:
                    print("      -> Plex counts this UNWATCHED.")
        except Exception as exc:
            print(f"  {key}: Plex lookup failed ({type(exc).__name__}: {exc})")

    heading(f"Jobs mentioning {title!r}")
    hits = [
        job for job in jobs.values()
        if title.strip().casefold()
        in str((job.get("event", {}).get("series", {}) or {}).get("title", "")).casefold()
    ]
    if not hits:
        print("  None. No Sonarr import for this show has ever been queued,")
        print("  so nothing was ever going to mark it automatically.")
    for job in sorted(hits, key=lambda item: item.get("updated_at", ""), reverse=True)[:8]:
        coords = ", ".join(
            f"S{int(item.get('season', 0)):02d}E{int(item.get('episode', 0)):02d}"
            for item in (job.get("event", {}).get("episodes") or [])
        )
        print(f"\n  [{job.get('status')}] {coords or 'no episodes'} - "
              f"{job.get('updated_at', '')[:19]}")
        print(f"      {job.get('message', '')}")
        for entry in (job.get("log") or [])[-4:]:
            print(f"      | {entry.get('message', '')}")


def _job_index(jobs: dict) -> dict:
    """Every (show title, season, episode) an import job has ever covered."""
    index: dict = {}
    for job in jobs.values():
        event = job.get("event", {}) or {}
        if event.get("source") == "manual":
            continue
        title = str((event.get("series") or {}).get("title", "")).strip().casefold()
        for episode in event.get("episodes") or []:
            try:
                key = (title, int(episode["season"]), int(episode["episode"]))
            except (KeyError, TypeError, ValueError):
                continue
            index.setdefault(key, []).append(job)
    return index


def audit_library(raw: dict, rules: dict, jobs: dict, limit: int) -> None:
    """Explain a library's worth of unwatched episodes in one pass.

    Asking about one episode at a time cannot show a pattern, and a pattern is
    what "a hundred shows each with exactly one unwatched episode" is. This
    reads every show whose rule is on, finds what Plex still counts unwatched,
    and looks for the import job that should have marked it.
    """
    shows_rules = rules.get("shows", {}) or {}
    index = _job_index(jobs)
    for name, library, plex in _plex_clients(raw):
        key = f"{name}::{library.get('name')}"
        heading(f"Audit: {key}")
        try:
            section = library.get("section_id") or plex.find_section_id(library.get("name"))
            if not section or plex.get_section_type(str(section)) != "show":
                print("  Not a TV library; skipped.")
                continue
            shows = plex.list_tv_shows(str(section))
        except Exception as exc:
            print(f"  Could not read Plex ({type(exc).__name__}: {exc})")
            continue

        enabled, outstanding = [], []
        for show in shows:
            tvdb = show.get("tvdb_id", "")
            rule_key = (f"{key}::tvdb-{tvdb}" if tvdb
                        else f"{key}::{show['rating_key']}")
            legacy_key = f"{key}::{show['rating_key']}"
            on = bool(shows_rules.get(rule_key, shows_rules.get(legacy_key, False)))
            if not on:
                continue
            enabled.append(show)
            missing = int(show.get("leaf_count", 0)) - int(show.get("viewed_leaf_count", 0))
            if missing > 0:
                outstanding.append((missing, show))
        print(f"  Shows in library         : {len(shows)}")
        print(f"  With auto-watch on       : {len(enabled)}")
        print(f"  ...still holding unwatched: {len(outstanding)}")
        if not outstanding:
            print("  -> Nothing outstanding. Anything Plex still shows as")
            print("     unwatched belongs to a library or a show without a rule.")
            continue
        spread = collections.Counter(count for count, _ in outstanding)
        print("  Unwatched per show       : " + ", ".join(
            f"{count} unwatched x{shows}" for count, shows in sorted(spread.items())
        ))

        print(f"\n  Checking the first {min(limit, len(outstanding))} of them "
              f"against the job history:")
        verdicts = collections.Counter()
        outstanding.sort(key=lambda item: item[1]["title"].casefold())
        for _missing, show in outstanding[:limit]:
            try:
                episodes = plex.list_show_episodes(show["rating_key"])
            except Exception as exc:
                print(f"    {show['title']}: could not read episodes ({exc})")
                continue
            unwatched = [item for item in episodes if item["view_count"] < 1]
            for item in unwatched[:3]:
                coord = f"S{item['season_index']:02d}E{item['episode_index']:02d}"
                found = index.get(
                    (show["title"].strip().casefold(),
                     item["season_index"], item["episode_index"]), [],
                )
                if not found:
                    verdicts["no import job ever covered it"] += 1
                    print(f"    {show['title']} {coord}: no import job")
                else:
                    last = found[-1]
                    verdict = f"{last.get('status')}: {last.get('message', '')[:70]}"
                    verdicts[verdict] += 1
                    print(f"    {show['title']} {coord}: {verdict}")
        print("\n  Summary of what was found:")
        for verdict, count in verdicts.most_common():
            print(f"    {count:4d}  {verdict}")
        if verdicts.get("no import job ever covered it"):
            print("\n  -> An episode with no import job was never announced by")
            print("     Sonarr to this install: imported before mediaMender, or")
            print("     imported while the webhook was not reaching it. Nothing")
            print("     automatic will ever mark those; Catch up now is the")
            print("     control that works from the rules rather than the log.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default=os.environ.get("DATA_DIR", "data"))
    parser.add_argument("--config", default=os.environ.get("CONFIG_PATH", ""))
    parser.add_argument("--plex", action="store_true",
                        help="also verify each rule's ratingKey still exists")
    parser.add_argument("--explain", metavar="SHOW",
                        help="explain one episode: why it is or is not marked")
    parser.add_argument("--audit", action="store_true",
                        help="explain a whole library's unwatched episodes at once")
    parser.add_argument("--limit", type=int, default=25,
                        help="how many shows --audit reads episodes for (default 25)")
    parser.add_argument("--season", type=int, default=1)
    parser.add_argument("--episode", type=int, default=1)
    args = parser.parse_args()

    data = Path(args.data)
    config_path = Path(args.config) if args.config else data / "config.yml"

    print(f"config : {config_path}")
    print(f"data   : {data.resolve()}")

    raw = load(config_path, {}) or {}
    if not raw:
        print("\nCould not read the config file. Pass --config /app/data/config.yml")
        return

    if args.audit:
        audit_library(
            raw,
            load(data / "mark-watched-rules.json", {}) or {},
            load(data / "mark-watched-jobs.json", {}) or {},
            max(1, args.limit),
        )
        print()
        return

    if args.explain:
        explain_episode(
            raw,
            load(data / "mark-watched-rules.json", {}) or {},
            load(data / "mark-watched-jobs.json", {}) or {},
            args.explain, args.season, args.episode,
        )
        print()
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
