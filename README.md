# DiskGuard

DiskGuard is a Docker sidecar for qBittorrent that prevents disk exhaustion.
It uses a tag-truth model (`diskguard_paused`, `soft_allowed`) and never keeps local torrent state files.

## What DiskGuard does

- Pauses newly added torrents quickly when free space is low (`POST /on-add`).
- Enforces SOFT and HARD disk safety modes from a polling loop.
- Resumes only torrents tagged as paused by DiskGuard when free space has been regained.
- Uses projection math (`amount_left`, active remaining, floor, buffer) before resuming.
- Verifies qBittorrent URL/auth/connectivity and API compatibility during startup before serving traffic.

## What DiskGuard does not do

- No torrent/file deletion.
- No category rewrites.
- No forcedDL override.
- No external/public API exposure.
- No local persistence store.

## Requirements

- Docker and docker-compose (or equivalent).
- qBittorrent Web API reachable from DiskGuard container.
- qBittorrent `>= 5.1.0` and Web API `>= 2.3.0`.
- DiskGuard container must mount the same filesystem qBittorrent writes downloads to.

## Installation

1. Build the image.

```bash
docker build -t diskguard:latest .
```

2. Create a local config directory at `./diskguard` (DiskGuard creates `./diskguard/config.toml` on first start if missing).
3. Add the qBittorrent on-add hook script at `./qbittorrent/config/scripts/diskguard_on_add.sh`.
4. Start services with compose.

## Example docker-compose.yml

```yaml
services:
  qbittorrent:
    image: lscr.io/linuxserver/qbittorrent:latest
    container_name: qbittorrent
    networks: [media]
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=UTC
      - WEBUI_PORT=8080
      - DISKGUARD_SERVER_PORT=${DISKGUARD_SERVER_PORT:-7070}
    volumes:
      - ./qbittorrent/config:/config
      - /mnt/storage/downloads:/downloads
    ports:
      - "8080:8080"
    restart: unless-stopped

  diskguard:
    image: ghcr.io/alexkahler/qbittorrent-diskguard:latest
    container_name: diskguard
    networks: [media]
    depends_on:
      - qbittorrent
    user: "${PUID:-1000}:${PGID:-1000}"
    environment:
      - DISKGUARD_SERVER_PORT=${DISKGUARD_SERVER_PORT:-7070}
      #- DISKGUARD_QBITTORRENT_URL=http://qbittorrent:8080              # Required if using non-persistent volume
      #- DISKGUARD_QBITTORRENT_USERNAME=${QBITTORRENT_USERNAME:-admin}  # Required if using non-persistent volume
      #- DISKGUARD_QBITTORRENT_PASSWORD=${QBITTORRENT_PASSWORD:-adminadmin} # Required if using non-persistent volume
    volumes:
      - /mnt/storage/downloads:/downloads:ro # qBittorrent download folder
      - ./diskguard:/config
    restart: unless-stopped

networks:
  media:
    driver: bridge
```

`/config` mapping guidance:
- Recommended: bind mount a folder (`./diskguard:/config`) so first-run bootstrap writes `./diskguard/config.toml`.
- Also supported: named volume (example: `diskguard_config:/config`) for persistence across restarts.
- If no `/config` volume is mapped, DiskGuard still starts and creates `/config/config.toml`, but it logs a warning because config is not persistent when the container is removed.

Why set `user` on `diskguard`:
- Bind mounts keep host file ownership/permissions.
- If `/mnt/storage/downloads` is not world-readable, DiskGuard may fail to read disk stats when container UID/GID do not match host ownership.
- `user: "${PUID}:${PGID}"` makes DiskGuard process run with host-equivalent IDs for reliable read access.

## Example config.toml

```toml
[qbittorrent]
url = "http://qbittorrent:8080" # Required
username = "admin"              # Required
password = "password"           # Required

[disk]
watch_path = "/downloads"
soft_pause_below_pct = 10
hard_pause_below_pct = 5
resume_floor_pct = 10
safety_buffer_gb = 10
downloading_states = ["downloading", "metaDL", "queuedDL", "stalledDL", "checkingDL", "allocating"]

[polling]
interval_seconds = 30
on_add_quick_poll_interval_seconds = 1.0
on_add_quick_poll_max_attempts = 10
on_add_quick_poll_max_concurrency = 32

[resume]
policy = "priority_fifo"
strict_fifo = true

[tagging]
paused_tag = "diskguard_paused"
soft_allowed_tag = "soft_allowed"

[logging]
level = "INFO"

[server]
host = "0.0.0.0"
port = 7070
```

> Find a fully commented `config.toml` file in the [examples folder](/examples/config.example.toml).

If you want a different API port, set one variable in your compose `.env`:

```dotenv
PUID=1000
PGID=1000
DISKGUARD_SERVER_PORT=7171
```


## qBittorrent on-add hook

Create `./qbittorrent/config/scripts/diskguard_on_add.sh`:

```sh
#!/bin/sh
# Usage: diskguard_on_add.sh "<hash>"

HASH="$1"
DISKGUARD_SERVER_PORT="${DISKGUARD_SERVER_PORT:-7070}"

curl -fsS -m 2 \
  -X POST "http://diskguard:${DISKGUARD_SERVER_PORT}/on-add" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "hash=$HASH" \
  >/dev/null 2>&1 &

exit 0
```

> Find a copy-paste ready `diskguard_on_add.sh` shell script in the [examples folder](/examples/diskguard_on_add.sh).

How this resolves the port:
- The script runs inside the qBittorrent container.
- `DISKGUARD_SERVER_PORT` is read from qBittorrent container env (`environment:` in compose).
- If missing, it falls back to `7070`.

Alternative (simpler, fixed port):
- If you always use port `7070`, hardcode the URL in the script:
  - `http://diskguard:7070/on-add`
- If you change DiskGuard API port, update this URL to match.

Hook URL requirements:
- Host must be Docker service name `diskguard` (same Docker network as qBittorrent).
- Port must match DiskGuard effective listen port:
  - `server.port` in `config.toml`, or
  - `DISKGUARD_SERVER_PORT` env override in DiskGuard container.
- Path must be `/on-add`.

Recommended single-source setup:
1. Set `DISKGUARD_SERVER_PORT` once in compose `.env`.
2. Pass it to both `diskguard` and `qbittorrent` services (as shown above).
3. Keep the script using `${DISKGUARD_SERVER_PORT:-7070}`.

Make it executable:

```bash
chmod +x ./qbittorrent/config/scripts/diskguard_on_add.sh
```

In qBittorrent settings, set:

`/config/scripts/diskguard_on_add.sh "%I"`

## Configuration reference

DiskGuard reads `/config/config.toml` and supports flat env var overrides.
On startup it creates `/config` and `/config/config.toml` automatically when missing.

Config path override:
- `DISKGUARD_CONFIG` can override the file path, but it must still be inside `/config`.

### Required keys

- `qbittorrent.url`
- `qbittorrent.username`
- `qbittorrent.password`

### Defaults

- `disk.watch_path = "/downloads"`
- `disk.soft_pause_below_pct = 10`
- `disk.hard_pause_below_pct = 5`
- `disk.resume_floor_pct = 10`
- `disk.safety_buffer_gb = 10`
- `polling.interval_seconds = 30`
- `polling.on_add_quick_poll_interval_seconds = 1.0`
- `polling.on_add_quick_poll_max_attempts = 10`
- `polling.on_add_quick_poll_max_concurrency = 32`
- `resume.policy = "priority_fifo"`
- `resume.strict_fifo = true`
- `tagging.paused_tag = "diskguard_paused"`
- `tagging.soft_allowed_tag = "soft_allowed"`
- `logging.level = "INFO"`
- `server.host = "0.0.0.0"`
- `server.port = 7070`

### Env override examples

- `DISKGUARD_CONFIG=/config/config.toml`
- `DISKGUARD_QBITTORRENT_URL=http://qbittorrent:8080`
- `DISKGUARD_DISK_WATCH_PATH=/downloads`
- `DISKGUARD_DISK_SOFT_PAUSE_BELOW_PCT=10`
- `DISKGUARD_SERVER_PORT=7070`
- `DISKGUARD_ON_ADD_QUICK_POLL_INTERVAL_SECONDS=1.0`
- `DISKGUARD_ON_ADD_QUICK_POLL_MAX_ATTEMPTS=10`
- `DISKGUARD_ON_ADD_QUICK_POLL_MAX_CONCURRENCY=32`
- `DISKGUARD_RESUME_POLICY=priority_fifo`
- `DISKGUARD_RESUME_STRICT_FIFO=true`
- `DISKGUARD_LOGGING_LEVEL=DEBUG`

### Server host/port behavior

- `server.host` is the socket bind address inside the DiskGuard container.
- In Docker, keep `server.host = "0.0.0.0"` so other containers can reach DiskGuard.
- `server.host` cannot be auto-derived from Docker service name; service names (`diskguard`) are DNS endpoints, not bind interfaces.
- `server.port` is the listen port and must match what the qBittorrent hook calls.

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=src python -m diskguard
```

## Testing

```bash
PYTHONPATH=src pytest
```

## Troubleshooting

### `watch_path` incorrect

- Symptom: ERROR logs about disk probe failure, no pause/resume actions.
- Check that DiskGuard mounts the same downloads filesystem as qBittorrent.

### `/config` not writable

- Symptom: startup fails with `/config is not writable`.
- Fix by mounting a writable config directory, for example `./diskguard:/config`.
- Avoid read-only `/config` mounts, because DiskGuard writes/maintains `/config/config.toml`.

### Config not persistent warning

- Symptom: startup WARNING says `/config` is not backed by a Docker volume.
- DiskGuard is running without a mapped config volume.
- Mount `./diskguard:/config` (recommended) or `diskguard_config:/config` to persist config.

### qBittorrent auth failure

- Symptom: startup retries followed by ERROR preflight failure, or WARNING logs during runtime ticks.
- Verify `qbittorrent.url`, username, password in `/config/config.toml`.

### qBittorrent version incompatibility

- Symptom: startup fails immediately with an incompatible version ERROR message.
- Required minimum: qBittorrent `>= 5.1.0` and Web API `>= 2.3.0`.
- Upgrade qBittorrent, then restart DiskGuard.

### Network failure between containers

- Symptom: WARNING logs for unreachable qB API, delayed enforcement until recovery.
- Verify both services share the same Docker network and service name resolution works.

### Tags not applied

- Symptom: torrents not resuming or not protected as expected.
- Check tag names in `[tagging]` config.
- Verify qBittorrent account has permission to pause/resume and edit tags.
- Ensure hook script path in qBittorrent is correct and executable.
