# DiskGuard 🛡️

**DiskGuard** is a lightweight Docker sidecar for qBittorrent that prevents disk exhaustion by automatically pausing and safely resuming downloading torrents based on available free space.
---

### What DiskGuard Does

When free space drops below defined levels:

* It pauses new torrents immediately (`POST /on-add`).
* It enforces SOFT and HARD protection modes via a polling loop.
* It pauses additional torrents as required to prevent disk exhaustion.

When free space is restored:

* It resumes **only torrents it previously paused** per defined resume policy (`priority_fifo`, `smallest_first`, `largest_first`).
* It uses projected disk usage (`amount_left`, active remaining, floor, buffer) to ensure resuming does not immediately re-trigger protection.

Additional guarantees:

* Respects manual force-start (`forcedDL`).
* All actions are idempotent and safe across restarts.
* State is derived entirely from qBittorrent tags (`diskguard_paused`, `soft_allowed` with no local state file).

### What DiskGuard Does Not Do

DiskGuard is intentionally minimal. It does **not**:

* Delete torrents or files.
* Modify categories.
* Override `forcedDL`.
* Expose any public API.
* Maintain any local persistence database.

> [!IMPORTANT]
> **DiskGuard** never deletes data. It only pauses and resumes torrents.

### Use Cases

* **Limited disk environments** - Ideal for NVMe or small SSD setups where space is constrained, especially when private tracker minimum seed times delay cleanup.
* **Automated request systems (e.g., Seerr)** - Prevent large bursts of requests from consuming all available disk space.
* **System stability protection** - Enforce a hard free-space floor to avoid disk exhaustion and service disruption.

### Requirements

- Docker and docker-compose (or equivalent).
- qBittorrent Web API reachable from DiskGuard container.
- qBittorrent `>= 5.1.0` and Web API `>= 2.3.0`.
- DiskGuard container must mount the same filesystem qBittorrent writes downloads to. 

---

## 🚀 Quick Start (Docker)

DiskGuard is designed to run as a Docker sidecar alongside qBittorrent.

### Option A — Docker Compose (Recommended)

1. Add the `diskguard` service to your `docker-compose.yml` (see example below).
2. Mount:
   * Your qBittorrent downloads folder → `/downloads`
   * A config folder → `/config`
3. Create and edit the `config.toml` inside your mounted config directory (see example below).
4. Add the qBittorrent on-add hook script:

   ```
   /config/scripts/diskguard_on_add.sh "%I"
   ```
5. Start or restart your stack:

    ```bash
    docker compose up -d
    ```


### Option B — Docker CLI

If you are not using Compose:

1. Build the image (if not pulling from GHCR)
    ```bash
    docker build -t diskguard:latest .
    ```

    Or pull the published image:

    ```bash
    docker pull ghcr.io/alexkahler/qbittorrent-diskguard:latest
    ```

2. Run **DiskGuard**

    ```bash
    docker run -d \
      --name diskguard \
      --network media \
      --user 1000:1000 \
      -v /path/to/downloads:/downloads:ro \
      -v /path/to/diskguard:/config \
      --restart unless-stopped \
      ghcr.io/alexkahler/qbittorrent-diskguard:latest
    ```

3. Edit the created `config.toml`

4. Add the qBittorrent on-add hook

    Create:

    ```
    /path/to/qbittorrent/config/scripts/diskguard_on_add.sh
    ```

    Then configure qBittorrent:

    ```
    /config/scripts/diskguard_on_add.sh "%I"
    ```

5. Restart DiskGuard after adding the hook.


> [!IMPORTANT]
> DiskGuard **must mount the same underlying filesystem** that qBittorrent writes downloads to.
>
> Mounting a different path or an overlay filesystem will result in incorrect disk measurements and protection will not work correctly.

---

## Example docker-compose.yml - Standard

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
      - /path/to/qbittorrent:/config
      - /path/to/downloads:/downloads
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
      - DISKGUARD_ON_ADD_AUTH_TOKEN=${DISKGUARD_ON_ADD_AUTH_TOKEN:-}
      #- DISKGUARD_QBITTORRENT_URL=http://qbittorrent:8080              # Required if not using a persistent volume
      #- DISKGUARD_QBITTORRENT_USERNAME=${QBITTORRENT_USERNAME:-admin}  # Required if not using a persistent volume
      #- DISKGUARD_QBITTORRENT_PASSWORD=${QBITTORRENT_PASSWORD:-} # Required if not using a persistent volume
    volumes:
      - /path/to/downloads:/downloads:ro # qBittorrent download folder
      - /path/to/diskguard:/config
    restart: unless-stopped

networks:
  media:
    driver: bridge
```

> [!SECURITY]
> Do not publish the DiskGuard API port externally. Keep it internal to the Docker network.
> Optional hardening for the `diskguard` service: `read_only: true`,
> `cap_drop: ["ALL"]`, and `security_opt: ["no-new-privileges:true"]`.

---

## Example docker-compose.yml - Gluetun

```yaml

services:
  gluetun:
    container_name: gluetun
    image: ghcr.io/qdm12/gluetun:latest
    cap_add:
      - NET_ADMIN
    volumes:
      - ./gluetun:/config
    environment:
      - VPN_SERVICE_PROVIDER=
      - VPN_TYPE=wireguard
      - PORT_FORWARD_ONLY=on
      - WIREGUARD_PRIVATE_KEY=
      - VPN_PORT_FORWARDING=on
      - VPN_PORT_FORWARDING_PROVIDER=protonvpn
      - FIREWALL_OUTBOUND_SUBNETS=
      - UPDATER_PERIOD=24h
      - TZ=${TZ}
      - VPN_PORT_FORWARDING_UP_COMMAND=/bin/sh -c 'wget -O- --retry-connrefused --post-data "json={\"listen_port\":{{PORTS}}}" http://127.0.0.1:8080/api/v2/app/setPreferences 2>&1'
    ports:
      - ${LAN_IP}:8080:8080/tcp # qBittorrent
    restart: always
    networks:
      - "vpn-net"
 
  qbittorrent:
    container_name: qbittorrent
    image: lscr.io/linuxserver/qbittorrent:latest
    environment:
      - PUID=${PUID}
      - PGID=${PGID}
      - TZ=${TZ}
      - WEBUI_PORT=${WEBUI_PORT}
    volumes:
      - /path/to/qbittorrent:/config
      - /path/to/downloads:/downloads
    restart: unless-stopped
    network_mode: service:gluetun
    stop_grace_period: 60s
    healthcheck: # https://github.com/qdm12/gluetun/issues/641#issuecomment-933856220
      test: "curl -sf ifconfig.me  || exit 1"
      interval: 1m
      timeout: 10s
      retries: 5

  diskguard:
    image: ghcr.io/alexkahler/qbittorrent-diskguard:latest
    container_name: diskguard
    user: "${PUID}:${PGID}"
    depends_on:
      qbittorrent:
        condition: service_healthy
    environment:
      - DISKGUARD_SERVER_PORT=${DISKGUARD_SERVER_PORT:-7070}
    volumes:
      - /path/to/downloads:/downloads:ro
      - /path/to/diskguard:/config
    network_mode: service:gluetun
    restart: unless-stopped 

```

---

## Example Docker CLI

```bash

docker run -d \
  --name diskguard \
  --network media \
  --user 1000:1000 \
  -e DISKGUARD_SERVER_PORT=7070 \
  -e DISKGUARD_ON_ADD_AUTH_TOKEN=your-static-token \
  -v /path/to/downloads:/downloads:ro \
  -v /path/to/diskguard:/config \
  --restart unless-stopped \
  ghcr.io/alexkahler/qbittorrent-diskguard:latest

```

`/config` mapping guidance:
- Recommended: bind mount a folder (`/path/to/diskguard:/config`) so first-run bootstrap writes `/path/to/diskguard/config.toml`.
- Also supported: named volume (example: `diskguard_config:/config`) for persistence across restarts.

> [!WARNING]
> If no `/config` volume is mounted, DiskGuard will start and create `/config/config.toml`,
> but configuration will be lost when the container is removed.
> 
> It is recommended to always mount `/config` to persist settings.

Why set `user` on `diskguard`:
- Bind mounts keep host file ownership/permissions.
- If `/path/to/downloads` is not world-readable, DiskGuard may fail to read disk stats when container UID/GID do not match host ownership.
- `user: "${PUID}:${PGID}"` makes DiskGuard process run with host-equivalent IDs for reliable read access.

---

## Example config.toml

```toml
[qbittorrent]
url = "http://qbittorrent:8080" # Required
username = "admin"              # Required
password = ""                   # Required (must be non-empty)

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
on_add_max_pending_tasks = 64

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
on_add_auth_token = ""          # Required (must be non-empty)
on_add_max_body_bytes = 8192
```

> [!TIP]
> Find a fully commented `config.toml` file in the [examples folder](/examples/config.example.toml).

If you want a different webhook port, set one variable in ``environment`` section of your docker compose:

```dotenv
DISKGUARD_SERVER_PORT=7171
```

---

## qBittorrent on-add hook

> [!NOTE]
> To enable quick stopping of torrents when they are added by your *arr applications, it is recommended to set up a shell script so that DiskGuard can be notified whenever a new torrent is added.

Generate a static shared secret once:

```bash
openssl rand -hex 32
```

Set the same token value in both:
- DiskGuard `server.on_add_auth_token` (or `DISKGUARD_ON_ADD_AUTH_TOKEN`)
- qBittorrent hook script variable `DISKGUARD_ON_ADD_AUTH_TOKEN`

Create `/path/to/qbittorrent/config/scripts/diskguard_on_add.sh` (or another path which qBittorrent has access to):

```sh
#!/bin/sh
# Usage: diskguard_on_add.sh "<hash>"

HASH="$1"
DISKGUARD_URL=diskguard # Change this to match the service name, or localhost if using Gluetun
DISKGUARD_SERVER_PORT=7070 # Remember to update this if you have changed the default DiskGuard server port in the environment settings.
DISKGUARD_ON_ADD_AUTH_TOKEN=your-secret-token # Set this to the same value as [server].on_add_auth_token in DiskGuard config.

curl -fsS -m 2 \
  -X POST "http://${DISKGUARD_URL}:${DISKGUARD_SERVER_PORT}/on-add" \
  -H "X-DiskGuard-Token: ${DISKGUARD_ON_ADD_AUTH_TOKEN}" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "hash=${HASH}" \
  >/dev/null 2>&1 &

exit 0
```

> [!TIP]
> Find a copy-paste ready `diskguard_on_add.sh` shell script in the [examples folder](/examples/diskguard_on_add.sh).

> [!IMPORTANT]
> Hook URL requirements:
> - Host must be Docker service name `diskguard` (same Docker network as qBittorrent) or `localhost` if *DiskGuard* is in `network_mode: service:<some_service>`.
> - Port must match DiskGuard effective listen port:
>   - `server.port` in `config.toml`, or
>   - `DISKGUARD_SERVER_PORT` env override in DiskGuard container.
> - Path must be `/on-add`.
> - Header `X-DiskGuard-Token` must match DiskGuard `server.on_add_auth_token`.

> [!CAUTION]
> If the host or port is incorrect, torrents will not be paused on add.
> SOFT mode polling will eventually correct this, but protection will be delayed.

Make it executable:

```bash
chmod +x ./qbittorrent/config/scripts/diskguard_on_add.sh
```

Finally, in **qBittorrent**, go to:
1. `Options` (Gear icon)
2. Click on `Downloads` tab
3. Scroll down to `Run external program` section
4. Enable checkbox on `Run on torrent added:`
5. Fill in the path to the script with: `/config/scripts/diskguard_on_add.sh "%I"`

---

## Configuration reference

DiskGuard reads `/config/config.toml` and supports flat environment variable overrides.
On startup it creates `/config` and `/config/config.toml` automatically when missing.

> [!IMPORTANT]
> Bootstrapped config initializes `qbittorrent.password` and `server.on_add_auth_token`
> as empty values. DiskGuard exits until both are set to non-empty secrets.

Config path override:
- `DISKGUARD_CONFIG` can override the file path, but it must still be inside `/config`.

### Required keys

- `qbittorrent.url`
- `qbittorrent.username`
- `qbittorrent.password`
- `server.on_add_auth_token`

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
- `polling.on_add_max_pending_tasks = 64`
- `resume.policy = "priority_fifo"`
- `resume.strict_fifo = true`
- `tagging.paused_tag = "diskguard_paused"`
- `tagging.soft_allowed_tag = "soft_allowed"`
- `logging.level = "INFO"`
- `server.host = "0.0.0.0"`
- `server.port = 7070`
- `server.on_add_max_body_bytes = 8192`

### Env override examples

- `DISKGUARD_CONFIG=/config/config.toml`
- `DISKGUARD_CONFIG_PATH=/config/config.toml` (legacy fallback)
- `DISKGUARD_QBITTORRENT_URL=http://qbittorrent:8080`
- `DISKGUARD_QBITTORRENT_USERNAME=admin`
- `DISKGUARD_QBITTORRENT_PASSWORD=your-qb-password`
- `DISKGUARD_QBITTORRENT_CONNECT_TIMEOUT_SECONDS=2.0`
- `DISKGUARD_QBITTORRENT_READ_TIMEOUT_SECONDS=8.0`
- `DISKGUARD_QBITTORRENT_TOTAL_TIMEOUT_SECONDS=12.0`
- `DISKGUARD_DISK_WATCH_PATH=/downloads`
- `DISKGUARD_DISK_SOFT_PAUSE_BELOW_PCT=10`
- `DISKGUARD_DISK_HARD_PAUSE_BELOW_PCT=5`
- `DISKGUARD_DISK_RESUME_FLOOR_PCT=10`
- `DISKGUARD_DISK_SAFETY_BUFFER_GB=10`
- `DISKGUARD_DISK_DOWNLOADING_STATES=downloading,metaDL,queuedDL,stalledDL,checkingDL,allocating`
- `DISKGUARD_POLLING_INTERVAL_SECONDS=30`
- `DISKGUARD_SERVER_PORT=7070`
- `DISKGUARD_ON_ADD_QUICK_POLL_INTERVAL_SECONDS=1.0`
- `DISKGUARD_ON_ADD_QUICK_POLL_MAX_ATTEMPTS=10`
- `DISKGUARD_ON_ADD_QUICK_POLL_MAX_CONCURRENCY=32`
- `DISKGUARD_ON_ADD_MAX_PENDING_TASKS=64`
- `DISKGUARD_RESUME_POLICY=priority_fifo`
- `DISKGUARD_RESUME_STRICT_FIFO=true`
- `DISKGUARD_TAGGING_PAUSED_TAG=diskguard_paused`
- `DISKGUARD_TAGGING_SOFT_ALLOWED_TAG=soft_allowed`
- `DISKGUARD_LOGGING_LEVEL=DEBUG`
- `DISKGUARD_SERVER_HOST=0.0.0.0`
- `DISKGUARD_ON_ADD_AUTH_TOKEN=your-secret-token`
- `DISKGUARD_SERVER_ON_ADD_MAX_BODY_BYTES=8192`

> [!IMPORTANT]
> Environment variables always override values in `config.toml`.
> If both are set, the environment variable takes precedence.

### Server host/port behavior

- `server.host` is the socket bind address inside the DiskGuard container.
- In Docker, keep `server.host = "0.0.0.0"` so other containers can reach DiskGuard. Only change this if your setup is unique.
- `server.host` cannot be auto-derived from Docker service name; service names (`diskguard`) are DNS endpoints, not bind interfaces.
- `server.port` is the listen port and must match what the qBittorrent hook calls.
- `server.on_add_auth_token` is required and must be non-empty.
- `/on-add` rejects requests missing `X-DiskGuard-Token` with HTTP `401`.
- `server.on_add_max_body_bytes` bounds accepted payload size for `/on-add` (default `8192`).
- Do not publish DiskGuard port externally (no `ports:` mapping on the DiskGuard service).

---

## Resume Policies

When disk space becomes available again (NORMAL mode), DiskGuard resumes only torrents tagged `diskguard_paused`.
The order in which they are resumed is controlled by the `resume.policy` setting.

### 1️⃣ `priority_fifo` (default)

Resumes torrents by:

1. Highest qBittorrent priority first
2. Oldest first within the same priority

This respects manual priority settings and keeps queue behavior predictable.

If `strict_fifo = true`:

* Stops at the first torrent that does not fit the disk budget.

If `strict_fifo = false`:

* Skips torrents that do not fit and continues checking the next one.

Best for: predictable queue behavior that aligns with qBittorrent priorities.

### 2️⃣ `smallest_first`

Resumes torrents with the smallest `amount_left` first.

This maximizes the number of torrents that can resume within the available disk budget.

Best for: finishing many small downloads quickly.

### 3️⃣ `largest_first`

Resumes torrents with the largest `amount_left` first.

This favors completing large downloads earlier.

Best for: prioritizing big releases or long-running downloads.

> [!TIP]
> If unsure, keep the default `priority_fifo`. It aligns with qBittorrent’s built-in priority system and works well for most users.

### How the Budget Works

Before resuming any torrent, DiskGuard calculates projected disk usage.
A torrent is resumed only if doing so will not drop free space below:

* `resume_floor_pct`
* plus `safety_buffer_gb`

---

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=src python -m diskguard
```

## Testing

```bash
pip install -r requirements-dev.txt
PYTHONPATH=src pytest
```

### Dependency lockfile

- Runtime container installs from `requirements.lock` using `--require-hashes`.
- Regenerate lockfile after dependency updates:

```bash
pip install -r requirements-dev.txt
python -m piptools compile --generate-hashes --output-file requirements.lock requirements.in
```

## Troubleshooting

### `watch_path` incorrect

- **Symptom**: ERROR logs about disk probe failure, no pause/resume actions.
- Check that DiskGuard mounts the same downloads filesystem as qBittorrent.

### `/config` not writable

- **Symptom**: startup fails with `/config is not writable`.
- Fix by mounting a writable config directory, for example `./diskguard:/config`.
- Avoid read-only `/config` mounts, because DiskGuard creates `/config/config.toml` on first run.

### Config not persistent warning

- **Symptom**: startup WARNING says `/config` is not backed by a Docker volume.
- DiskGuard is running without a mapped config volume.
- Mount `./diskguard:/config` (recommended) or `diskguard_config:/config` to persist config.

### qBittorrent auth failure

- **Symptom**: startup retries followed by ERROR preflight failure, or WARNING logs during runtime ticks.
- Verify `qbittorrent.url`, username, password in `/config/config.toml`.

### Required secrets missing

- **Symptom**: startup exits with `cannot be empty` for `qbittorrent.password` or `server.on_add_auth_token`.
- Set both `qbittorrent.password` and `server.on_add_auth_token` to non-empty values in `/config/config.toml`.

### `/on-add` unauthorized (`401`)

- **Symptom**: qBittorrent hook runs, but DiskGuard returns HTTP `401`.
- Verify `X-DiskGuard-Token` in the hook script matches `server.on_add_auth_token` exactly.
- Ensure `server.on_add_auth_token` is set to a non-empty secret value.

### qBittorrent version incompatibility

- **Symptom**: startup fails immediately with an incompatible version ERROR message.
- Required minimum: qBittorrent `>= 5.1.0` and Web API `>= 2.3.0`.
- Upgrade qBittorrent, then restart DiskGuard.

### Network failure between containers

- **Symptom**: WARNING logs for unreachable qB API, delayed enforcement until recovery.
- Verify both services share the same Docker network and service name resolution works.

### Tags not applied

- **Symptom**: torrents not resuming or not protected as expected.
- Check tag names in `[tagging]` config.
- Verify qBittorrent account has permission to pause/resume and edit tags.
- Ensure hook script path in qBittorrent is correct and executable.
