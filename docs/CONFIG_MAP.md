# Configuration map

Last updated 07-10-2026. Part A lists every environment variable v3 reads. Part B maps every variable in v1's `docker-compose.yml` to KEPT, CHANGED or DROPPED.

Sources: `collector/src/vatpac/settings.py`, `ops/watchdog/watchdog.py`, `docker-compose.yml`, `.env.example`. Secrets live only in `.env` (git-ignored); copy `.env.example` to `.env` and replace every `change-me` value. Passwords are placed inside connection URLs, so use URL-safe values (letters and digits, for example `openssl rand -hex 24`).

## Part A: v3 variables

### A1. Set in `.env` (read by `docker-compose.yml`)

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `POSTGRES_DB` | `vatpac` | db, migrate, collector, grafana, watchdog, backup | Database name. |
| `POSTGRES_USER` | `vatpac` | db, migrate, collector, backup | Owner role. |
| `POSTGRES_PASSWORD` | none, **required** | db, migrate, collector, backup | Owner password. Compose refuses to start without it. |
| `GRAFANA_RO_PASSWORD` | none, **required** | migrate, grafana | Password of the read-only role `grafana_ro` (set on every `migrate` run). |
| `WATCHDOG_DB_PASSWORD` | none, **required** | migrate, watchdog | Password of the read-only role `watchdog_ro`. |
| `GRAFANA_ADMIN_USER` | `admin` | grafana | Admin login. |
| `GRAFANA_ADMIN_PASSWORD` | none, **required** | grafana | Admin password. |
| `GRAFANA_PORT` | `3300` | grafana | Host port mapped to Grafana's 3000. |
| `GRAFANA_ROOT_URL` | `http://localhost:3300/` | grafana | External URL; set it when served through a proxy or tunnel. |
| `VATSIM_FEED_URL` | `https://data.vatsim.net/v3/vatsim-data.json` | collector | Fallback feed URL, used if discovery fails. |
| `VATSIM_STATUS_URL` | `https://status.vatsim.net/status.json` | collector | Discovery document (`data.v3[0]`). |
| `POLL_INTERVAL_SECONDS` | `15` | collector | Poll cadence. Valid 5 to 300. |
| `HTTP_TIMEOUT_SECONDS` | `10` | collector | Request timeout (connect timeout is the lower of 5 s and this). Valid 1 to 60. |
| `POSITION_RETENTION_DAYS` | `90` | collector | Raw positions kept this long (whole daily partitions). Valid 2 to 3650. |
| `AIRBORNE_GAP_CAP_SECONDS` | `40` | collector | Cap per sample gap in airborne minutes. Valid 15 to 600. Published to `app_setting` for the views. |
| `HOME_ICAO_PREFIXES` | `Y` | collector | Comma-separated letter prefixes defining the home region (for example `Y` or `Y,NZ`). Published to `app_setting`. |
| `LOG_LEVEL` | `INFO` | collector | `DEBUG`, `INFO`, `WARNING` or `ERROR` (case-insensitive). Also read by the watchdog. |
| `HEALTH_PORT` | `8080` | collector, watchdog | Port of the collector's `/health` and `/metrics` inside the container. |
| `VATPAC_VERSION` | `dev` | collector build arg | Version label baked into the image (the release workflow sets it from the git tag). |
| `BACKUP_RETENTION_DAYS` | `14` | backup | Days of dumps kept. |
| `BACKUP_TIME_UTC` | `02:00` | backup | Nightly backup time (HH:MM, UTC). |
| `BACKUP_RESTORE_TEST_DAY` | `7` | backup | ISO weekday (1 Monday to 7 Sunday) on which the backup is restore-tested; `0` disables. |
| `TELEGRAM_BOT_TOKEN` | empty | watchdog | Optional. If empty, alerts are logged only (as at the 07-10-2026 verification). Delivery to Telegram is not yet verified. |
| `TELEGRAM_CHAT_ID` | empty | watchdog | Optional, as above. |
| `UPTIME_KUMA_PUSH_URL` | empty | watchdog | Optional heartbeat push URL. |
| `WATCHDOG_INTERVAL_SECONDS` | `60` | watchdog | Check cadence. |
| `WATCHDOG_WARN_SECONDS` | `60` | watchdog | Age of newest ok poll or snapshot that raises a warning. |
| `WATCHDOG_PAGE_SECONDS` | `300` | watchdog | Age that raises a critical alert. |
| `WATCHDOG_CONFIRM_CYCLES` | `2` | watchdog | A state change must persist this many cycles before it is alerted. |
| `WATCHDOG_DISK_PCT` | `80` | watchdog | Disk usage percentage that alerts (critical 10 points higher, per `.env.example`). |
| `WATCHDOG_DB_MAX_GB` | `20` | watchdog | Database size that raises a warning. |
| `WATCHDOG_BACKUP_MAX_HOURS` | `26` | watchdog | Newest-backup age that alerts. |

### A2. Set by `docker-compose.yml` itself (not intended for `.env`)

| Variable | Value | Meaning |
|---|---|---|
| `DATABASE_URL` (collector) | built from the `POSTGRES_*` values, host `db` | Overrides the five `POSTGRES_*` connection settings inside the collector. |
| `GF_*` (grafana) | anonymous access off, sign-up off, analytics and update checks off, default timezone UTC | Grafana hardening. |
| `GRAFANA_RO_DB_PASSWORD` (grafana) | `${GRAFANA_RO_PASSWORD}` | Referenced by the provisioned datasource. |
| `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` (backup) | from the `POSTGRES_*` values | Connection for `pg_dump`. |
| `DOCKER_HOST_URL` (watchdog) | empty | Empty means no Docker access: the watchdog only alerts. Set it only to an endpoint provided outside this repository (ADR-012). |
| `COMPOSE_PROJECT` (watchdog) | `vatpac` | Compose project name used when Docker access exists. |
| `COLLECTOR_HEALTH_URL`, `GRAFANA_HEALTH_URL` (watchdog) | `http://collector:${HEALTH_PORT}/health`, `http://grafana:3000/api/health` | Health endpoints it polls. |
| `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` (watchdog) | `db`, `${POSTGRES_DB}`, `watchdog_ro`, `${WATCHDOG_DB_PASSWORD}` | Watchdog's database login. |
| `BACKUP_DIR`, `STATUS_FILE` (watchdog) | `/backups`, `/var/watchdog/watchdog_status.json` | Volumes the watchdog reads and writes. |

### A3. Read by the code but not wired through compose

These have defaults and work as documented, but changing them needs a compose edit (an `environment:` line) or a different deployment.

| Variable | Default | Read by | Meaning |
|---|---|---|---|
| `POLYGON_PATH` | `/app/config/australian_airspace_polygon.json` | collector | Polygon file. |
| `WHITELIST_PATH` | `/app/config/controller_callsigns_list.txt` | collector | Controller whitelist. |
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | collector | Consecutive failures that open the circuit breaker (1 to 1000). |
| `CIRCUIT_SLOW_INTERVAL_SECONDS` | `60` | collector | Poll cadence while the breaker is open (5 to 3600). |
| `POSTGRES_HOST` / `POSTGRES_PORT` | `db` / `5432` | collector | Used only if `DATABASE_URL` is not set. |
| `WATCHDOG_RESTART_CAP_PER_HOUR` | `3` | watchdog | Restart cap per container per hour; only relevant with `DOCKER_HOST_URL`. |
| `WATCHDOG_RESTART_AFTER_CYCLES` | `3` | watchdog | Unhealthy cycles before a restart; only with `DOCKER_HOST_URL`. |
| `WATCHDOG_RESTARTABLE` | `db,collector,grafana,backup` | watchdog | Containers it may restart; only with `DOCKER_HOST_URL`. |
| `WATCHDOG_EXPECTED` | `db,collector,grafana,backup` | watchdog | Containers it expects to be running; only with `DOCKER_HOST_URL`. |

The watchdog's Docker-related variables were read from `ops/watchdog/watchdog.py`; the restart path has not been exercised because no Docker endpoint is provided.

### A4. Validation

The collector validates its settings at start-up and exits with code 2 and an "invalid configuration" message if any value is out of range (for example `POLL_INTERVAL_SECONDS=2`, or `HOME_ICAO_PREFIXES` containing non-letters). Reference files (polygon, whitelist) that are missing or invalid also stop start-up (exit 2).

### A5. Reference files (not environment variables)

| File | Purpose | Reload |
|---|---|---|
| `config/australian_airspace_polygon.json` | Pilot boundary (GeoJSON Polygon, lon,lat). | SIGHUP, or restart. |
| `config/controller_callsigns_list.txt` | Controller whitelist (`CALLSIGN, FREQ` or bare `CALLSIGN`; `#` comments). | SIGHUP, or restart. |

`config/australian_airspace_sectors.geojson` also exists in the repository folder but is not read by v3 (sector matching is out of scope, ADR-002).

## Part B: v1 `docker-compose.yml` variables

Source: `C:\Users\mdora\Claude\VATPAC-Stats V2\docker-compose.yml`. Several v1 variables are marked in the file itself as unused or stale; that is noted where it applies.

### B1. Database and application

| v1 variable | v1 value | Status | v3 equivalent and reason |
|---|---|---|---|
| `POSTGRES_DB` | `vatsim_data` | CHANGED | `POSTGRES_DB` default `vatpac`. New schema, new name. |
| `POSTGRES_USER` | `vatsim_user` | CHANGED | `POSTGRES_USER` default `vatpac`. |
| `POSTGRES_PASSWORD` | a hard-coded default password committed in the compose file | CHANGED | Now required from `.env`, no default, never in git. |
| `POSTGRES_SHARED_BUFFERS`, `POSTGRES_EFFECTIVE_CACHE_SIZE`, `POSTGRES_WORK_MEM`, `POSTGRES_MAINTENANCE_WORK_MEM` | 4GB / 16GB / 128MB / 1GB | CHANGED | These are not standard settings of the official postgres image, so they were probably not applied in v1 (not verified). v3 sets tuning in `ops/postgres/postgresql.conf` (512MB shared buffers, 1GB cache, 16MB work_mem) sized for a 2 GB container. |
| `DATABASE_URL` | hard-coded URL | CHANGED | Built in compose from the `POSTGRES_*` values. |
| `LOG_LEVEL` | `INFO` | KEPT | Same name. v3 emits JSON logs. |
| `API_WORKERS`, `API_DEBUG`, `API_RELOAD`, `CORS_ORIGINS` | 4 / false / false / `*` | DROPPED | v3 has no REST API (ADR-010); only `/health` and `/metrics`. |

### B2. Polling and filters

| v1 variable | v1 value | Status | v3 equivalent and reason |
|---|---|---|---|
| `VATSIM_POLLING_INTERVAL` | 90 | CHANGED | `POLL_INTERVAL_SECONDS` = 15, aligned to the feed's own refresh (ADR-005). |
| `VATSIM_TRANSCEIVERS_POLLING_INTERVAL` | 120 | DROPPED | The transceivers feed is not fetched (SPEC section 3). |
| `ENABLE_BOUNDARY_FILTER` | true | CHANGED | Always on; no switch. The polygon filter is one of the three fixed filters (SPEC section 4). Polygon path is `POLYGON_PATH`. |
| `EXCLUDED_CALLSIGN_PATTERNS` | `ATIS,_OBS` | DROPPED | The whitelist keeps ATIS and `_OBS` out, and ATIS is a separate feed array (ADR-007). |
| `EXCLUDED_FREQUENCIES_MHZ` | 122.800 | DROPPED | Frequency filter removed (ADR-007). |
| (file mount) controller callsign list | read-only mount | CHANGED | Now fail-closed: a missing or empty file stops start-up. Path via `WHITELIST_PATH`. |

### B3. Flight and controller summary system

| v1 variable | v1 value | Status | v3 equivalent and reason |
|---|---|---|---|
| `FLIGHT_SUMMARY_ENABLED` | true | DROPPED | No summary tables; flights are a view over sessions. |
| `FLIGHT_COMPLETION_HOURS` | 8 | CHANGED | Fixed 8-hour span split inside the `flight` view (not configurable). |
| `FLIGHT_RETENTION_HOURS` | 168000000 | DROPPED | Effectively unlimited in v1. v3: raw positions 90 days (`POSITION_RETENTION_DAYS`), sessions kept forever (ADR-008). |
| `FLIGHT_DAYS_BEFORE_ARCHIVE` | 90 | CHANGED | No archive tables; replaced by dropping daily partitions after `POSITION_RETENTION_DAYS` (90). |
| `FLIGHT_SUMMARY_POLL_INTERVAL_SHORT`, `FLIGHT_SUMMARY_POLL_INTERVAL_LONG` | 60 / 900 | DROPPED | No summary scheduler. |
| `CONTROLLER_SUMMARY_ENABLED` | true | DROPPED | No summariser; controller sessions are written directly and merged in a view. |
| `CONTROLLER_COMPLETION_MINUTES` | 15 | DROPPED | "Closed" is derived at query time (`is_open`). |
| `CONTROLLER_RETENTION_HOURS` | 1680000000 | DROPPED | Sessions are kept forever. |
| `CONTROLLER_SUMMARY_INTERVAL` | 30 | DROPPED | No scheduler. |
| `CONTROLLER_RECONNECTION_THRESHOLD_MINUTES` | 5 | CHANGED | Same 5-minute rule, now fixed in the `controller_session_merged` view rather than configurable. |
| `CONTROLLER_DAYS_BEFORE_ARCHIVE` | 120 | DROPPED | No archive tables. |
| `CLEANUP_FLIGHT_TIMEOUT` | 300 | DROPPED | It closed sector entries; sector tracking is out of scope. |

### B4. Sector tracking, detection and enrichment

| v1 variable | Status | Reason |
|---|---|---|
| `SECTOR_TRACKING_ENABLED`, `SECTOR_UPDATE_INTERVAL` (v1 comment: "i dont thjink this is used anymore") | DROPPED | Sector tracking out of scope (ADR-002). |
| `FLIGHT_DETECTION_TIME_WINDOW_SECONDS` | DROPPED | Frequency-matching detection out of scope (ADR-002). |
| `CONTROLLER_PROXIMITY_GROUND_NM`, `_TOWER_NM`, `_APPROACH_NM`, `_CENTER_NM`, `_FSS_NM`, `_DEFAULT_NM` | DROPPED | Proximity ranges for ATC detection; out of scope. |
| `ENABLE_CONTROLLER_SPECIFIC_RANGES` | DROPPED | Same. |
| `ENABLE_ENRICHMENT`, `MAX_ENRICHMENT_RETRIES` | DROPPED | No enrichment workers. |

### B5. Services and mounts

| v1 item | Status | v3 equivalent |
|---|---|---|
| `postgres` service, 5432 published to the host | CHANGED | `db`; not published (dev overlay: `127.0.0.1:55432` only). Named volume instead of `./database/vatsim`. |
| `app` service (API, port 8001, 5 minute healthcheck) | CHANGED | `collector` (no public port; health on 8080 inside the network; 30 s healthcheck). |
| `integrity` service (daily SQL integrity scripts) | DROPPED | Constraints (PK, FK, UNIQUE, CHECK) enforce integrity in the schema; the integration tests cover it. |
| `monitor` service (commented out; pushed to an Uptime Kuma address) | CHANGED | Replaced by the `watchdog` service. Optional heartbeat via `UPTIME_KUMA_PUSH_URL`. |
| `./logs` bind mount | DROPPED | Logs go to Docker's `json-file` driver (10 MB x 5). |
| `./pytest.ini` and `./tests` mounts ("remove for production") | DROPPED | Tests run outside the container. |
| `config/init.sql` initdb mount | CHANGED | Replaced by forward-only migrations applied by the `migrate` container. |
| Sector GeoJSON mount | DROPPED | Not read by v3. |

### B6. New in v3 (no v1 counterpart)

`GRAFANA_*`, `WATCHDOG_*`, `BACKUP_*`, `TELEGRAM_*`, `UPTIME_KUMA_PUSH_URL`, `HTTP_TIMEOUT_SECONDS`, `POSITION_RETENTION_DAYS`, `AIRBORNE_GAP_CAP_SECONDS`, `HOME_ICAO_PREFIXES`, `CIRCUIT_*`, `HEALTH_PORT`, `VATPAC_VERSION`.
