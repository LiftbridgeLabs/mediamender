# mediaWarden build test inventory

This repository-only document describes the validations that run before every
Docker image publication. Markdown files are excluded by `.dockerignore`, so
this file is not included in the published Docker image.

The authoritative workflow is `.github/workflows/docker-publish.yml`. A failed
step stops the build before Docker Hub login and publication.

## Pre-commit checks

Command:

```shell
pre-commit run --all-files --show-diff-on-failure
```

The build installs the pinned `pre-commit` version declared in the workflow and
runs every hook configured in `.pre-commit-config.yaml` against the repository.
This currently includes secret scanning with Gitleaks plus the configured file
cleanliness and syntax checks. A hook failure stops the build before tests,
Docker Hub login, or image publication.

## Unit and safety suite

Command:

```shell
python -m unittest discover -s tests -v
```

### Configuration precedence

- `test_empty_environment_values_do_not_override_file_settings` — empty
  environment variables must not erase saved settings.
- `test_session_key_is_generated_once_and_persisted` — the Flask session key
  must remain stable across reloads.

### Live configuration

- `test_invalid_global_cron_is_rejected_before_apply` — an invalid global
  schedule never reaches the live scheduler.
- `test_global_schedule_is_inherited_without_library_override` — libraries
  without an override inherit the configured global default.
- `test_library_schedule_override_wins_over_global_default` — an explicit
  per-library schedule takes precedence.
- `test_next_run_is_available_before_first_library_run` — the dashboard receives
  the first scheduled time before any run history exists.
- `test_invalid_log_storage_policy_is_rejected` — invalid file-size, total
  storage, and retention combinations never reach the live logger.
- `test_duplicate_plex_machine_identifier_is_rejected` — the same Plex server
  cannot be imported twice under different names.
- `test_invalid_cron_is_rejected_before_apply` — invalid schedules never reach
  the live scheduler.
- `test_library_without_safety_path_is_rejected` — every monitored library
  requires at least one filesystem safety path.
- `test_live_apply_reconciles_jobs_and_removed_libraries` — live settings
  changes update jobs and remove stale dashboard state without a restart.

### Plex authentication and discovery

- `test_connections_prefer_local_non_relay_and_parse_string_booleans` — Plex
  discovery prefers direct local connections and correctly handles Plex boolean
  strings.

### Plex inventory behavior

- `test_tv_count_uses_episode_type` — TV safety ratios compare disk files with
  Plex episode counts rather than show counts.
- `test_count_failure_is_not_reported_as_zero` — a failed Plex count is unknown,
  not an apparently safe empty library.
- `test_trash_inventory_failure_is_explicit` — incomplete trash inventory
  fails closed.
- `test_trash_inventory_keeps_same_title_with_distinct_plex_ids` — separate Plex
  items with the same title remain distinct in safety snapshots.
- `test_unmatched_items_use_one_bulk_request_and_local_guid` — match audits use
  one bulk request and report only top-level items with a `local://` GUID.

### Read-only status and match audit

- `test_readonly_status_preload_never_reads_or_empties_trash` — startup status
  checks populate dashboard state without reading trash, creating history, or
  calling Empty Trash.
- `test_startup_status_refresh_runs_in_background` — the preload is launched in
  a daemon worker and covers each configured library.
- `test_audit_returns_direct_links_without_touching_trash` — Metadata Health returns
  rating keys and encoded Plex detail links without entering the destructive
  workflow.
- `test_match_audit_ui_is_explicitly_manual_and_read_only` — the UI identifies
  the audit as manual, read-only, and separate from the Empty Trash safety gate.
- `test_ignored_metadata_library_is_not_requested` — per-server exclusions are
  applied before Plex library item requests.
- `test_metadata_health_ignores_round_trip_through_config_builder` — Metadata
  Health exclusions survive parsing and UI configuration saves.
- `test_navigation_and_startup_progress_match_feature_layout` — navigation,
  rollup pages, and visible startup progress remain wired to their intended
  feature areas.

### Trash protection and destructive workflow

- `test_missing_plex_count_fails_closed` — an unavailable Plex count blocks
  emptying.
- `test_debrid_mount_passes_when_discovered_mount_is_populated` — a populated
  discovered debrid mount passes.
- `test_debrid_mount_fails_when_discovered_mount_is_empty` — an empty underlying
  debrid mount fails.
- `test_provider_checks_receive_live_config` — provider checks use current
  settings.
- `test_overlapping_library_run_is_skipped` — scheduled and manual runs cannot
  overlap for the same library.
- `test_failed_health_check_never_empties_trash` — a filesystem check failure
  prevents the destructive call.
- `test_unreachable_plex_never_empties_trash` — Plex reachability failure
  prevents inventory and deletion.
- `test_missing_count_never_empties_trash` — runner orchestration preserves the
  fail-closed count policy.
- `test_failed_provider_check_never_empties_trash` — configured provider failure
  prevents deletion.
- `test_missing_section_never_empties_trash` — an unresolved Plex library never
  reaches deletion.
- `test_failed_initial_inventory_never_empties_trash` — the first inventory must
  succeed.
- `test_dry_run_never_empties_trash` — dry runs never call Plex Empty Trash.
- `test_clean_bundles_failure_never_empties_trash` — an enabled Clean Bundles
  failure stops the run.
- `test_paused_scheduling_never_empties_trash` — paused scheduled work remains
  paused.
- `test_manual_run_can_bypass_paused_scheduler` — explicitly requested manual
  work remains available while scheduling is paused.
- `test_empty_snapshot_does_not_call_empty_trash` — empty trash does not issue a
  needless destructive request.
- `test_failed_final_preflight_never_empties_trash` — safety checks are repeated
  immediately before deletion.
- `test_changed_trash_snapshot_never_empties_trash` — trash added or removed
  after the initial inventory cancels the run.
- `test_deletion_limit_never_empties_oversized_snapshot` — the absolute item
  limit is enforced.
- `test_percentage_limit_never_empties_oversized_snapshot` — the active-library
  percentage limit is enforced.
- `test_successful_run_has_one_destructive_call` — a fully valid run contains
  exactly one Plex Empty Trash request.

### Manual timestamp repair

- Read-only SQLite detection filters negative timestamps to explicit path
  allowlists and deduplicates media-part rows by file.
- Path containment rejects sibling-prefix escapes, and temporary names preserve
  the final media extension.
- A durable manifest exists before the first symlink rename.
- Ambiguous recovery leaves the manifest in `recovery_required` for review.
- Any active repair manifest blocks Empty Trash before its health checks begin.
- Enabled configuration requires both a read-only database path and at least
  one writable symlink prefix.
- The repair API rejects folders not present in the latest server-side audit.
- Repair workers authenticate timestamped requests with HMAC signatures and
  reject tampering and replay.
- A worker constrains controller-supplied database and media paths to roots
  independently configured on that worker container.
- Workers never receive Plex tokens; scan callbacks are signed and limited to
  the exact section and folder approved by the controller transaction.
- An unreachable configured worker fails the shared maintenance gate closed.

### Web and API security

- `test_ui_renders_with_security_headers` — the UI returns the expected CSP and
  clickjacking protections.
- `test_state_change_requires_csrf_for_browser_session` — browser mutations
  require the session CSRF token.
- `test_invalid_api_token_does_not_bypass_csrf` — merely supplying an API token
  header does not bypass CSRF.
- `test_valid_api_token_authenticates_without_csrf` — a verified independent
  bearer token supports non-browser automation.
- `test_password_hash_is_not_an_api_token` — login password hashes are not API
  credentials.
- `test_generated_api_token_is_revealed_once` — generated tokens are returned
  only at creation.
- `test_generated_api_token_persists_only_its_hash` — plaintext API tokens never
  reach `config.yml`.
- `test_metadata_address_is_rejected` — known cloud metadata addresses are
  rejected as Plex targets.
- `test_browse_opens_at_allowed_roots_and_stays_inside_them` — filesystem
  browsing cannot escape configured roots.

### Log storage and viewer API

- `test_rotation_uses_readable_log_filenames` — rotations use names such as
  `mediawarden.1.log`.
- `test_retention_removes_expired_rotated_logs` — files beyond the configured
  retention duration are removed.
- `test_total_storage_removes_oldest_rotated_logs` — the total MB cap removes
  oldest rotations first.
- `test_log_api_lists_reads_and_rejects_unknown_files` — authenticated log
  listing, viewing, and downloads work while arbitrary filenames are rejected.

### Notification destinations

- `test_apprise_destinations_are_parsed_with_routing` — saved destination
  presets and event routes load into the live configuration.
- `test_validation_rejects_duplicate_names` — destination names remain
  unambiguous.
- `test_validation_requires_at_least_one_routed_event` — a destination cannot
  be saved without an event route.
- `test_fanout_only_starts_enabled_matching_destinations` — delivery targets
  only enabled destinations subscribed to the current event.
- `test_dispatch_preserves_native_discord_and_apprise` — native Discord and
  Apprise can receive the same enabled event.

## Static and rendered-code validation

- `python -m compileall -q app.py worker.py src tests` compiles every Python source file.
- The Flask index is rendered through the test client, every inline script is
  extracted, and Node.js parses the resulting JavaScript with `new Function`.
  This catches errors that only appear after Jinja template rendering.

## Configuration validation

- PyYAML parses `data/config.yml.example`.
- Python's XML parser loads `unraid/mediawarden.xml` and the legacy `unraid/emptyarr.xml`.
- `docker compose config --quiet` validates the Compose model.

## Container validation and publication

After all tests pass, the build uses the production `Dockerfile` and explicit
allowlisted `COPY` instructions. The workflow publishes:

- `liftbridgelabs/mediawarden:latest` (primary)
- `liftbridgelabs/mediawarden:<full-git-commit-sha>` (primary)
- `liftbridgelabs/emptyarr:latest` (compatibility alias)
- `liftbridgelabs/emptyarr:<full-git-commit-sha>` (compatibility alias)

The Docker build context excludes repository metadata, tests, local
configuration, runtime data, logs, editor files, and Markdown documentation.
