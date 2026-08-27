# mediaMender

mediaMender is a maintenance and repair suite for Plex media libraries.

Plex doesn't automatically clean up its library trash when you're using symlinked debrid or usenet media. When a file gets replaced or removed, Plex marks it unavailable — but unless you have "empty trash automatically after every scan" turned on (which you probably don't, because that's risky), those entries just pile up.

mediaMender runs on a schedule, checks that your mounts are actually healthy, and then calls Plex's emptyTrash API. If anything looks wrong — mount missing, symlinks broken, file count dropped — it skips the empty and can notify you through Discord or Apprise.

---

## How it works

Before emptying trash on any library, mediaMender runs:

1. **Mount check** — walks up the path tree to find the nearest mount point and verifies it's accessible
2. **Debrid mount check** — for debrid/usenet paths, reads symlink targets via `os.readlink()` (without resolving them), finds the underlying FUSE mount point, and verifies it is accessible and non-empty. This detects a dead mount even when symlinks point into trash and would otherwise appear broken
3. **File threshold** — compares the count of files on disk to your Plex library count. If the ratio drops below your configured threshold (default 90%), something's wrong and it bails
4. **Combined check** — for mixed libraries (physical + debrid), sums all paths and checks the combined ratio

All checks pass → trash gets emptied. Any check fails → skip, log it, notify if configured.

---

## Installation

### Unraid WebUI (recommended)

mediaMender is distributed as a prebuilt Docker image. A normal Unraid installation
does not require the terminal, a Git checkout, or a local image build.

If a mediaMender template is available in your Apps feed, install it there.
Otherwise, open **Docker → Add Container** and configure:

| Setting | Value |
|---|---|
| Name | `mediaMender` |
| Repository | `liftbridgelabs/mediamender:latest` |
| Network | `bridge` or the custom Docker network used by your media applications |
| Container port | `8222` |
| Host port | `8222` or another available port |
| WebUI | `http://[IP]:[PORT:8222]/` |

Add these path mappings in the container editor:

**Path mappings:**

| Host | Container | Mode |
|---|---|---|
| `/mnt/cache/appdata/mediamender/data` | `/app/data` | Read/Write |
| `/mnt/symlink_media` | `/mnt/symlink_media` | Read Only - Slave |
| `/mnt/user/media` | `/mnt/user/media` | Read Only |

The host paths are examples; select the paths used by your own Unraid setup.
Container paths are what mediaMender displays and saves in its library settings.

> The container path for symlink media must match what the symlinks actually
> point to. For example, if their targets begin with `/symlink_media/`, use
> `/symlink_media` as the container path instead of `/mnt/symlink_media`.

> **Slave propagation required for FUSE mounts:** The symlink media volume must use `slave` propagation (`:ro,slave`) so that FUSE mounts created by tools like Decypharr or zurg after the container starts are visible inside the container. Without `slave`, the container sees a stale snapshot of the host mount namespace and the FUSE filesystem will appear empty or missing.

Add the following variables under **Add another Path, Port, Variable, Label or
Device → Variable**:

| Variable | Default | Description |
|---|---|---|
| `PUID` | — | User ID for file permissions. `99` on Unraid (nobody) |
| `PGID` | — | Group ID for file permissions. `100` on Unraid (users) |
| `TZ` | — | Timezone, e.g. `America/New_York` |
| `CONFIG_PATH` | `data/config.yml` | Path to the config file |
| `LOG_DIR` | `data/logs` | Directory where log files are written |
| `BROWSE_ROOTS` | `/mnt,/media,/data,/home` | Comma-separated list of root paths the file browser is allowed to enter |
| `SESSION_COOKIE_SECURE` | `false` | Set to `true` when serving over HTTPS — marks the session cookie as Secure so it's never sent over plain HTTP |

`PUID=99`, `PGID=100`, and your local `TZ` are the only variables most Unraid
installations need. After selecting **Apply**, open the WebUI from the Docker
page.

Plex tokens, provider keys, Discord notifications, web authentication, schedules,
and logging are all configurable in the UI and persist in `data/config.yml`.
The session key is generated once and persisted automatically in the data
directory.

Non-empty environment variables such as `PLEX_TOKEN_<NAME>`, `RD_API_KEY`,
`DISCORD_WEBHOOK`, or `MEDIAMENDER_USERNAME` remain supported as optional
deployment-managed overrides.
A Compose `.env` file only supplies those environment overrides; it is not
mediaMender's primary configuration file.

### Docker Compose

For Unraid Compose Manager or any other Compose installation, create a Compose
file using the published image:

```yaml
services:
  mediamender:
    image: liftbridgelabs/mediamender:latest
    container_name: mediaMender
    restart: unless-stopped
    ports:
      - "8222:8222"
    environment:
      PUID: "99"
      PGID: "100"
      TZ: America/Denver
    volumes:
      - /mnt/cache/appdata/mediamender/data:/app/data
      - /mnt/symlink_media:/mnt/symlink_media:ro,slave
      - /mnt/user/media:/mnt/user/media:ro
```

Change the timezone, port, and host paths for your system. If mediaMender must
reach Plex by container name, attach it to the same custom Docker network as
Plex.

### First run

Open `http://YOUR_IP:8222` and run through the setup wizard. You can connect
your Plex account in the browser to discover servers and libraries automatically;
mediaMender never receives your Plex password. Manual URL/token setup remains
available as a fallback.

---

## Building from source

Local builds are intended for development or testing changes that are not yet
in the published image:

```bash
git clone https://github.com/LiftbridgeLabs/mediamender.git
cd mediamender
docker build -t mediamender:local .
```

Normal Unraid and Compose installations should use
`liftbridgelabs/mediamender:latest` instead.

---

## Configuration

Config lives at `/app/data/config.yml` (your host's data directory). The Settings
page can validate and apply changes immediately, including additions, removals,
schedules, paths, credentials, notifications, authentication, and log level.
Restart only after changing container runtime variables or volume mappings.

### Library types

- **physical** — standard files on disk
- **debrid** — symlinked content (Real-Debrid, AllDebrid, etc.)
- **usenet** — usenet downloads with symlinks
- **mixed** — combination of physical and debrid in the same Plex library

For mixed libraries the file threshold check combines all paths before comparing to your Plex count, so individual paths don't need to hold the full library.

### Threshold

`min_threshold` is the percentage of your Plex library count that must exist on disk. Default is 90. If you have 1000 movies in Plex and only 850 files on disk, that's 85% — below 90%, so the empty gets skipped.

### Cron schedules

The Settings page provides a global default plus optional per-library
overrides. Libraries without a `cron` value inherit `schedule.default_cron`.
`0 * * * *` runs every hour on the hour and `*/30 * * * *` runs on the next
half-hour boundary. A daily time selected in the UI is evaluated in the
container timezone (`TZ`).

There is no automatic Empty Trash run immediately at startup. mediaMender does run
the non-destructive safety checks in the background so the dashboard can show
current Plex, mount, provider, and file-threshold health before the first
scheduled protection run. This preload does not inspect trash, write history,
send notifications, or call Plex's Empty Trash endpoint. **Run now** remains
available when an immediate full safety run is wanted.

### Metadata Health

Open **Metadata Health** to run an explicit, read-only scan for top-level movie and
show items whose primary Plex GUID starts with `local://`. mediaMender makes one
bulk Plex request per configured movie or TV library, saves the latest result,
and displays the title, rating key, metadata key, and a direct Plex details
link. This scan is never scheduled and does not affect the Empty Trash safety
gate. It uses the existing Plex connection and requires no additional Docker
variables or volume mappings. Per-server settings can exclude libraries that
intentionally use local-only metadata, such as YouTube libraries.

### Library Refresh

Library Refresh can request a normal full Plex scan for any configured library,
manually or on an independent per-library schedule. This is useful for sources
such as YouTube or Sportarr when no AutoPulse/OmniScan-style trigger is
available. It uses the existing Plex URL, token, and section ID and requires no
additional Docker mounts.

Plex accepts refresh requests asynchronously, so mediaMender reports the request
as accepted rather than claiming the scan is complete. After an accepted
request, mediaMender applies the configured library-specific Empty Trash safety
hold (15 minutes by default). Existing trash inventory stabilization and all
other destructive safety checks still run afterward.

```yaml
libraries:
  - name: Sports
    type: physical
    refresh_enabled: true
    refresh_cron: "0 * * * *"       # every hour
    refresh_guard_minutes: 15
    paths:
      - path: /mnt/user/media/sports
        type: physical
```

### Manual Plex part timestamp repair

mediaMender can optionally audit Plex's database for media parts whose stored
timestamp is negative and repair one explicitly reviewed movie or TV season
folder at a time. This is backend-neutral: it can protect symlink trees created
by NZBDAV, Decypharr, AltMount, Ultimate Usenet, or another tool when the
filesystem exposed an invalid modification time while Plex scanned it.

The feature is disabled by default and is never scheduled. Open **Timestamp
Repair**, run an audit, inspect the exact affected filenames on a folder card,
then explicitly approve that folder. mediaMender durably records the transaction,
temporarily renames only the affected symlinks, requests two path-limited Plex
HTTP scans, restores the original names, and verifies positive timestamps.
Empty Trash and timestamp repair share one maintenance lock.

```yaml
plex_instances:
  - name: My Plex
    url: http://192.168.1.100:32400
    token: ''
    timestamp_repair:
      enabled: false
      worker: local
      database_path: /plex-db/com.plexapp.plugins.library.db
      allowed_prefixes:
        - /mnt/symlink_media/symlinks/nzbdav
      max_files_per_folder: 5
      scan_timeout_seconds: 1800
      poll_interval_seconds: 5
      heartbeat_seconds: 30
```

Least-privilege container mounts are required. Retain the broad symlink root as
read-only, overlay only the repair prefix as read/write, and expose only Plex's
database directory read-only:

```yaml
- /mnt/symlink_media:/mnt/symlink_media:ro,slave
- /mnt/symlink_media/symlinks/nzbdav:/mnt/symlink_media/symlinks/nzbdav:rw,slave
- /mnt/cache/appdata/plex/Library/Application Support/Plex Media Server/Plug-in Support/Databases:/plex-db:ro
```

The writable path must exactly match the path stored by Plex. mediaMender never
writes to Plex's database or symlink targets and does not require Docker-socket
access. Its PUID/PGID must have rename permission on the allowed symlink tree.

Configure this from **Settings → Timestamp Repair**. After the database
directory is mounted, **Discover** locates
`com.plexapp.plugins.library.db`; the UI saves the per-server configuration and
shows whether each instance is ready. Docker isolation means mediaMender cannot
create host mounts or infer a host appdata path from a Plex API token.

#### Unraid container fields

The shipped Unraid template exposes two optional, read-only Plex database
directory mappings at `/plex-db/server-1` and `/plex-db/server-2`. Use one for
each Plex server hosted on the same Unraid machine. Then use **Settings →
Timestamp Repair → Discover** and select the database found for that instance.

For each enabled local repair instance, add one additional Unraid **Path**
manually for every allowed repair prefix:

- Host path: the narrow symlink folder mediaMender may repair.
- Container path: the exact same absolute path stored in the Plex database.
- Access mode: read/write with slave propagation (`rw,slave`).

There is deliberately no generic `/repairable` directory and the template does
not create one: the correct path is specific to each media layout. Do not make
the broad media-root mapping writable. Neither Metadata Health nor the startup
safety preload needs these repair-only mappings.

#### Plex on another machine

Run one repair-only sidecar on the remote Plex machine. This is not another
mediaMender controller: it has no dashboard, scheduler, Empty Trash operation,
notifications, or Plex token. The main mediaMender remains the only UI and sends
signed, replay-protected filesystem requests. When Plex must scan a folder, the
sidecar calls a transaction-limited signed controller endpoint and the main
mediaMender performs the Plex HTTP request.

In **Settings → Timestamp Repair**, add a worker, generate its pairing secret,
assign the remote Plex instance, and use **Copy worker Compose**. Replace each
`HOST_*` placeholder with the real VM path before deploying it. The sidecar
requires only:

- its persistent `/app/data` directory;
- the remote Plex database directory mounted read-only; and
- explicitly allowed symlink prefixes mounted read/write at the exact paths
  stored in Plex.

If a configured worker is unreachable, mediaMender fails closed for maintenance
because it cannot prove that a rename transaction is not awaiting recovery.

### Example config

```yaml
log_level: INFO
discord_webhook: https://discord.com/api/webhooks/...
notify:
  on_emptied: true
  on_health_fail: true
  on_error: true
  on_clean: false
  on_skip: false

# Optional. Plex Clean Bundles is server-wide, so it is disabled by default.
# Enable only if you intentionally want it before each library trash operation.
clean_bundles_before_empty: false

# Abort unusually large empty-trash runs (0 disables a limit)
max_trash_items: 1000
max_trash_percent: 25

schedule:
  default_cron: "0 * * * *"

plex_instances:
  - name: My Plex
    url: http://192.168.1.100:32400
    token: ''
    libraries:
      - name: Movies
        type: physical
        paths:
          - path: /mnt/user/media/movies
            type: physical
            min_threshold: 90
      - name: TV Shows
        type: physical
        cron: "*/30 * * * *"  # optional per-library override
        paths:
          - path: /mnt/user/media/tv
            type: physical
            min_threshold: 90

  - name: My Plex Unlimited
    url: http://192.168.1.100:32410
    token: ''
    libraries:
      - name: Movies
        type: mixed
        cron: "0 * * * *"
        paths:
          - path: /mnt/user/media/movies
            type: physical
            min_threshold: 90
          - path: /mnt/symlink_media/symlinks/radarr
            type: debrid
            min_threshold: 90
      - name: TV Shows
        type: debrid
        cron: "0 * * * *"
        paths:
          - path: /mnt/symlink_media/symlinks/sonarr
            type: debrid
            min_threshold: 90
```

---

## Mark-it-Watched

Mark-it-Watched applies per-user show defaults and explicit season overrides to
future finalized Sonarr imports. An administrator can open Settings â†’
Mark-it-Watched, enter the Sonarr URL and API key, verify the callback URL, and
select **Connect Sonarr**. mediaMender uses Sonarr's advertised Webhook schema,
runs Sonarr's Test event, and creates or updates the managed connection. The
Sonarr API key is used for that request only and is never saved.

Repeat Connect Sonarr for every Sonarr instance. mediaMender keeps a non-secret
per-instance connection list and gives each automatically managed webhook a
unique connection identifier. All connected Sonarr instances use the same
Mark-it-Watched rules. Plex watched state is written through the matching
server's globally configured Plex token and therefore belongs to that Plex
account, not every Plex Home profile.

Each saved Sonarr row has a Remove control. Failed setup records can be
forgotten immediately. Removing a successfully managed connection asks for that
Sonarr instance's API key once, deletes mediaMender's webhook from Sonarr, and
then removes the local status record. Sonarr validation and callback-test errors
are displayed without exposing API keys or webhook secrets.

If no dedicated webhook secret exists, Connect Sonarr generates and saves one
automatically. `MEDIAMENDER_SONARR_WEBHOOK_SECRET` remains the preferred
environment override. Manual setup is still supported: use
`http://MEDIAMENDER:8222/api/webhooks/sonarr` and send the secret in
`X-Sonarr-Webhook-Secret` or as a Bearer token.

Only Sonarr `Download` events containing an imported `episodeFile` are queued.
The HTTP request returns immediately; persistent background work retries until
Plex has scanned and matched every episode in the import, then applies the
current show/season rules. Duplicate payloads reuse the original durable job.
Recent success, retry, and failure state is shown on the Mark-it-Watched page.
The non-secret Sonarr connection status is stored in `data/sonarr-webhook.json`.
The rule browser loads one selected Plex server and TV library at a time. Shows
use Plex-native pagination (12, 24, 36, or 48 per page); movie libraries are not
shown.

Settings controls which shared Plex TV libraries are visible. All On and All Off
update the single future rule set only; they never rewrite existing Plex watch
history.

## Auth

mediaMender uses one application login and the globally configured Plex
identity. It does not require separate application-user or per-user Plex-token
setup.

Settings → Security. Enter username and password, save. Takes effect immediately, no restart needed. Stored as a bcrypt hash in config.yml — never plaintext.

You can also set `MEDIAMENDER_USERNAME` and `MEDIAMENDER_PASSWORD` env vars instead.

API access uses a separate random token; the login password hash is never an
API credential. Generate or rotate the token under Settings â†’ Security and
copy it when shownâ€”mediaMender stores only its hash and cannot display it again.
You may alternatively set `MEDIAMENDER_API_TOKEN` as an environment override.

The API token is useful for Home Assistant, scripts, health monitors, and
external dashboards. Send it in the `X-API-Token` header to read endpoints such
as `/api/status`, `/api/history`, and `/api/logs`, or to trigger an authorized
run without storing the UI password:

```bash
curl -H "X-API-Token: ${MEDIAMENDER_API_TOKEN}" http://MEDIAMENDER:8222/api/status
```

## Logs

Settings → General → Logging contains the running log viewer and all rotated
log files. Select a prior file to view it or download it. The active file is
`mediamender.log`; rotations use names such as `mediamender.1.log` and
`mediamender.2.log`.

Retention is configured in understandable storage and time units:

- **Rotate each file at** controls the size of an individual file in MB.
- **Maximum total log storage** caps all log files combined in MB.
- **Keep rotated logs for** removes old files after the selected number of days.

The oldest rotated files are removed when either the storage or age limit is
reached. Defaults are 5 MB per file, 50 MB total, and 14 days. Logs remain under
the persistent `LOG_DIR` (`data/logs` by default) and are also written to the
container console for Docker/Unraid.

Logs record scheduled/manual runs, safety checks, skipped operations, Plex
actions and results, configuration changes, provider failures, and operational
errors. mediaMender does not intentionally log passwords, Plex tokens, provider
keys, or API tokens.

---

## Notifications

mediaMender supports native Discord embeds plus named Apprise destinations. Friendly
presets in Settings cover Telegram, ntfy, Gotify, email/SMTP, Pushover, and
generic webhooks; the custom preset accepts any
[Apprise service URL](https://appriseit.com/services/).

Each Apprise destination can be enabled independently, tested before saving, and
routed to its own selection of events. The global event controls are master
switches for both Discord and Apprise:

- **Trash emptied** — something was actually removed
- **Health check failed** — checks didn't pass, empty was skipped
- **Error** — the emptyTrash API call failed
- **Already clean** — ran fine, nothing to remove (off by default — gets noisy)
- **Skipped** — scheduling paused, config error, section not found (off by default)

Notification delivery runs outside the library operation so a slow or unavailable
notification provider cannot block trash-protection work. Destination URLs often
contain credentials and are stored in `config.yml`; keep the file private.

Quiet hours, failure/recovery notifications, daily summaries, and digest routing
are planned after the first destination release is proven stable.

---

## Updating

### Unraid WebUI

From the **Docker** page, use **Check for Updates**, then apply the update for
mediaMender. Unraid pulls the current `liftbridgelabs/mediamender:latest` image and recreates
the container while preserving everything mapped to `/app/data`.

### Docker Compose

```bash
docker compose pull mediamender
docker compose up -d mediamender
```

---

## Privacy

mediaMender talks to your Plex server, Plex's authorization/discovery service when
you choose account linking, configured debrid provider APIs, and notification
services you configure. It sends no telemetry or analytics. See
[PRIVACY.md](PRIVACY.md).

---

## License

MIT
