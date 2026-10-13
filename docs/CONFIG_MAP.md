# Configuration map

Last reviewed 13-10-2026 (issue 89 phases 1b and 3 marked live; the `flight_quiet_hours` row checked) against `docker-compose.yml`, `.env.example` and the code (`python ops/check_config_drift.py` enforces the collector part). Part A lists every environment variable v3 reads. Part B maps every variable in v1's `docker-compose.yml` to KEPT, CHANGED or DROPPED.

Sources: `collector/src/vatpac/settings.py`, `ops/watchdog/watchdog.py`, `docker-compose.yml`, `.env.example`. Secrets live only in `.env` (git-ignored). `.env.example` has blank secrets on purpose: run `sh ops/gen_env.sh` to write a `.env` with random values, then `sh ops/check_env.sh` to check it (blank, `change-me*`, under 16 characters, repeated, or non letter/digit values are rejected). Passwords are placed inside connection URLs, so they must be letters and digits only (for example `openssl rand -hex 24`).

## Part A: v3 variables

### A1. Set in `.env` (read by `docker-compose.yml`)

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `POSTGRES_DB` | `vatpac` | db, migrate, collector, grafana, watchdog, backup | Database name. |
| `POSTGRES_USER` | `vatpac` | db, migrate, backup (restore only) | Owner role. The collector does not use it: it connects as the role `collector`. |
| `POSTGRES_PASSWORD` | none, **required** | db, migrate, backup (restore only) | Owner password. Compose refuses to start without it. Read by Postgres only at first initialisation. |
| `GRAFANA_RO_PASSWORD` | none, **required** | migrate, grafana | Password of the read-only role `grafana_ro` (set on every `migrate` run). |
| `COLLECTOR_DB_PASSWORD` | none, **required** | migrate, collector | Password of the role `collector` (DML on its tables only). |
| `BACKUP_DB_PASSWORD` | none, **required** | migrate, backup | Password of the read-only role `backup_ro` used by `pg_dump`. |
| `WATCHDOG_DB_PASSWORD` | none, **required** | migrate, watchdog | Password of the read-only role `watchdog_ro`. |
| `GRAFANA_ADMIN_USER` | `admin` | grafana | Admin login. |
| `GRAFANA_ADMIN_PASSWORD` | none, **required** | grafana | Admin password. |
| `GRAFANA_PORT` | `3300` | grafana | Host port mapped to Grafana's 3000. |
| `GRAFANA_BIND` | `127.0.0.1` | grafana | Interface Grafana is published on. Loopback by default; set the VM's LAN address (preferred) or `0.0.0.0` for LAN access, then firewall it. |
| `GRAFANA_COOKIE_SECURE` | `false` | grafana | Set `true` only when users reach Grafana over https; with plain http a secure cookie stops logins working. |
| `GRAFANA_ROOT_URL` | `http://localhost:3300/` | grafana | External URL; set it when served through a proxy or tunnel. Production on 11-10-2026: `http://192.168.1.222:3300/` (see RUNBOOKS/public-access.md). It is still the LAN http address because it only matters for links that Grafana builds itself (the admin login, redirects, shared links). The public dashboard pages are served by path (`/public-dashboards/<token>`) through the proxy and do not need it. Set it to `https://vpac.mpengor.com/` (with `GRAFANA_COOKIE_SECURE=true`) only if the admin should log in through the public name. |
| `VATSIM_FEED_URL` | `https://data.vatsim.net/v3/vatsim-data.json` | collector | Fallback feed URL, used if discovery fails. |
| `VATSIM_STATUS_URL` | `https://status.vatsim.net/status.json` | collector | Discovery document (`data.v3[0]`). |
| `POLL_INTERVAL_SECONDS` | `30` | collector | Poll cadence (ADR-024). The one place the interval is held: the airborne gap cap, the archive gap threshold, the online-now windows (2 x this), the soak report and the Snapshots-missed panel derive from it. Valid 5 to 300. |
| `HTTP_TIMEOUT_SECONDS` | `10` | collector | Request timeout (connect timeout is the lower of 5 s and this). Valid 1 to 60. |
| `TEXT_MAX_LENGTH` | `2000` | collector | Free-text fields of a pilot or controller (name, route, remarks, server and so on) are cut to this many characters at ingest; each cut field counts once in `poll_log.rejected_values`. Valid 100 to 100000. |
| `FEED_MAX_FUTURE_SECONDS` | `120` | collector | A feed snapshot whose `update_timestamp` is more than this many seconds ahead of the collector's clock is rejected as a feed error (`error` poll, nothing stored, `last seen` unchanged), so one future-dated snapshot cannot make every later real update look unchanged. Valid 15 to 3600. The collector host's clock must be correct to well within this bound. |
| `POSITION_RETENTION_DAYS` | `90` | collector | Raw positions kept this long (whole daily partitions). Valid 2 to 3650. |
| `AIRBORNE_GAP_CAP_SECONDS` | blank (derived: 2.5 x `POLL_INTERVAL_SECONDS`, 75 s at 30 s) | collector | Cap per sample gap in airborne minutes. Set it only to override. Valid 15 to 600. Published to `app_setting` for the views. |
| `HOME_ICAO_PREFIXES` | `Y` | collector | Comma-separated letter prefixes defining the home region (for example `Y` or `Y,NZ`). Published to `app_setting`. |
| `LOG_LEVEL` | `INFO` | collector | `DEBUG`, `INFO`, `WARNING` or `ERROR` (case-insensitive). The watchdog also reads it, but compose does not pass it on, so the watchdog stays at `INFO`. |
| `CIRCUIT_FAILURE_THRESHOLD` | `5` | collector | Consecutive upstream failures that open the circuit breaker (1 to 1000). Polling never stops. |
| `CIRCUIT_SLOW_INTERVAL_SECONDS` | `60` | collector | Poll cadence in seconds while the breaker is open (5 to 3600). |
| `ARCHIVE_ENABLED` | `true` | collector | Keep the rolling raw-feed archive (docs/RUNBOOKS/feed-archive.md). A failure of the archive never stops polling. |
| `ARCHIVE_DIR` | `/archive` | collector | Archive directory inside the container: the named volume `archive` (the root filesystem is read-only). |
| `ARCHIVE_COMPACT_EVERY_S` | `300` | collector | A compact gzipped record (all pilots and controllers, audit fields) is written when the feed timestamp enters a new slot of this many seconds (1 to 86400). |
| `ARCHIVE_COMPACT_KEEP_DAYS` | `30` | collector | Days the compact records are kept. |
| `ARCHIVE_FULL_EVERY_S` | `3600` | collector | The full raw payload, gzipped, is written once per slot of this many seconds. |
| `ARCHIVE_FULL_KEEP_DAYS` | `7` | collector | Days the full payloads are kept. |
| `ARCHIVE_ANOMALY_KEEP_DAYS` | `90` | collector | Days the anomaly payloads are kept (poll error, timestamp gap, older or future timestamp, `logon_time` precision change). |
| `ARCHIVE_GAP_S` | blank (derived: 2 x `POLL_INTERVAL_SECONDS` + 5, 65 s at 30 s) | collector | A gap between two feed timestamps above this is an anomaly. A single missed tick (60 s at 30 s polling) is normal (audit F10), so the derived value sits above it (16 to 3600). |
| `ARCHIVE_MAX_MB` | `1024` | collector | Hard cap on the archive; the oldest files are deleted first when it is exceeded. |
| `HEALTH_PORT` | `8080` | collector, watchdog | Port of the collector's `/health` and `/metrics` inside the container. |
| `VATPAC_VERSION` | `dev` | collector build arg | Version label baked into the image (the release workflow sets it from the git tag). |
| `BACKUP_RETENTION_DAYS` | `14` | backup | Days of dumps kept. |
| `BACKUP_MIN_SIZE_PERCENT` | `50` | backup | A new dump smaller than this percent of the previous good dump is rejected (moved aside as `*.rejected`). |
| `BACKUP_MIN_FIRST_BYTES` | `1048576` | backup | Minimum size of the first-ever dump (no previous dump to compare with). |
| `BACKUP_MIN_KEEP` | `3` | backup | Rotation never deletes below this many good dumps. |
| `BACKUP_TIME_UTC` | `02:00` | backup | Nightly backup time (HH:MM, UTC). |
| `BACKUP_RESTORE_TEST_DAY` | `7` | backup | ISO weekday (1 Monday to 7 Sunday) on which the backup is restore-tested; `0` disables (then set `WATCHDOG_RESTORE_TEST_MAX_DAYS=0` too). |
| `BACKUP_REMOTE` | empty | backup | Optional off-box copy: a shell command run after every successful nightly dump with `$BACKUP_FILE` set. See [restore runbook](RUNBOOKS/restore.md), "Off-box copies". |
| `BACKUP_EXTRA_APT_PACKAGES` | empty | backup (build argument) | Debian packages added to the backup image for the `BACKUP_REMOTE` command (for example `rclone`); rebuild to apply. |
| `TELEGRAM_BOT_TOKEN` | empty | watchdog | Optional. If empty, alerts are logged only. Delivery to Telegram from the VM was verified on 07-10-2026 (docs/TRACKER.md, T4). Secret: keep out of git, chat and logs. |
| `TELEGRAM_CHAT_ID` | empty | watchdog | Optional, as above. |
| `UPTIME_KUMA_PUSH_URL` | empty | watchdog | Optional heartbeat push URL. |
| `WATCHDOG_INTERVAL_SECONDS` | `60` | watchdog | Check cadence. |
| `WATCHDOG_WARN_SECONDS` | `60` | watchdog | Age of newest ok poll or snapshot that raises a warning. |
| `WATCHDOG_PAGE_SECONDS` | `300` | watchdog | Age that raises a critical alert. |
| `WATCHDOG_CONFIRM_CYCLES` | `2` | watchdog | A state change must persist this many cycles before it is alerted. |
| `WATCHDOG_DISK_PCT` | `80` | watchdog | Disk usage percentage that warns (critical 10 points higher). |
| `WATCHDOG_DISK_PATHS` | `/backups,/` | watchdog | Comma-separated paths (inside the watchdog container) whose filesystems are checked. |
| `WATCHDOG_DB_MAX_GB` | `20` | watchdog | Database size that raises a warning. |
| `WATCHDOG_BACKUP_MAX_HOURS` | `26` | watchdog | Newest-backup age that alerts (critical). |
| `WATCHDOG_RESTORE_TEST_MAX_DAYS` | `9` | watchdog | Warn if the last successful weekly restore test is older than this many days; `0` disables the age check. |
| `WATCHDOG_INTEGRITY_INTERVAL_HOURS` | `72` | watchdog | How often the 18-invariant SQL sweep (`ops/audit/invariants.sql`) runs, in hours; `0` disables. A sweep with violations repeats every 6 hours. The last run is kept in the status file, so a restart does not re-run early or skip a due run. Do not raise it above 72 without widening the 3-day windows in the SQL (docs/RUNBOOKS/integrity-checks.md). |
| `WATCHDOG_CROSSCHECK_INTERVAL_DAYS` | `14` | watchdog | How often the raw-feed archive cross-check runs, in days; `0` disables. |
| `WATCHDOG_CROSSCHECK_SAMPLES` | `24` | watchdog | Archived snapshots compared per cross-check, evenly spread over the interval (about 1 s of CPU each). |

### A2. Set by `docker-compose.yml` itself (not intended for `.env`)

| Variable | Value | Meaning |
|---|---|---|
| `DATABASE_URL` (collector) | built from `COLLECTOR_DB_PASSWORD` and `POSTGRES_DB`, role `collector`, host `db` | Overrides the five `POSTGRES_*` connection settings inside the collector. |
| `GF_*` (grafana) | anonymous access off, sign-up off, analytics and update checks off, default timezone UTC | Grafana hardening. Public dashboards are on (Grafana default) and are the only way pages are seen without a login; anonymous access must stay off (ADR-029). Image `grafana/grafana:12.4.12`. |
| `GRAFANA_RO_DB_PASSWORD` (grafana) | `${GRAFANA_RO_PASSWORD}` | Referenced by the provisioned datasource. |
| `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE` (backup) | `db`, `5432`, `backup_ro`, `BACKUP_DB_PASSWORD`, `POSTGRES_DB` | Connection for `pg_dump`. |
| `RESTORE_PGUSER`, `RESTORE_PGPASSWORD` (backup) | `POSTGRES_USER`, `POSTGRES_PASSWORD` | Owner login used only by `restore.sh` and `restore-test.sh`, which need to create, drop and rename databases (ADR-017). |
| `DOCKER_HOST_URL` (watchdog) | empty | Empty means no Docker access: the watchdog only alerts. Set it only to an endpoint provided outside this repository (ADR-012). |
| `COMPOSE_PROJECT` (watchdog) | `vatpac` | Compose project name used when Docker access exists. |
| `COLLECTOR_HEALTH_URL`, `GRAFANA_HEALTH_URL` (watchdog) | `http://collector:${HEALTH_PORT}/health`, `http://grafana:3000/api/health` | Health endpoints it polls. |
| `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` (watchdog) | `db`, `${POSTGRES_DB}`, `watchdog_ro`, `${WATCHDOG_DB_PASSWORD}` | Watchdog's database login. |
| `ARCHIVE_DIR` (watchdog) | `/archive` | Set by compose: the `archive` volume mounted read-only into the watchdog for the cross-check. |
| `REFERENCE_CONFIG_DIR` (watchdog) | `/config` | Set by compose: `./config` mounted read-only (polygon and whitelist the cross-check recomputes with). |
| `BACKUP_DIR`, `STATUS_FILE` (watchdog) | `/backups`, `/var/watchdog/watchdog_status.json` | Volumes the watchdog reads and writes. |

### A3. Read by the code but not wired through compose

These have defaults and work as documented, but changing them needs a compose edit (an `environment:` line) or a different deployment. `ops/check_config_drift.py` records the two path variables as intentionally fixed.

| Variable | Default | Read by | Meaning |
|---|---|---|---|
| `POLYGON_PATH` | `/app/config/australian_airspace_polygon.json` | collector | Polygon file. |
| `WHITELIST_PATH` | `/app/config/controller_callsigns_list.txt` | collector | Controller whitelist. |
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

`config/australian_airspace_sectors.geojson` also exists in the repository folder but is not read by v3 (sector matching is out of scope, ADR-002). A rejected SIGHUP reload (invalid file, or a whitelist smaller than 70% of the old one) leaves the old data in use; see [runbook: update reference files](RUNBOOKS/update-reference-files.md).

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
| `VATSIM_POLLING_INTERVAL` | 90 | CHANGED | `POLL_INTERVAL_SECONDS` = 15 when written, aligned to the feed's own refresh (ADR-005); 30 since 09-10-2026 (ADR-024). |
| `VATSIM_TRANSCEIVERS_POLLING_INTERVAL` | 120 | DROPPED | The transceivers feed is not fetched (SPEC section 3). |
| `ENABLE_BOUNDARY_FILTER` | true | CHANGED | Always on; no switch. The polygon filter is one of the three fixed filters (SPEC section 4). Polygon path is `POLYGON_PATH`. |
| `EXCLUDED_CALLSIGN_PATTERNS` | `ATIS,_OBS` | DROPPED | The whitelist keeps ATIS and `_OBS` out, and ATIS is a separate feed array (ADR-007). |
| `EXCLUDED_FREQUENCIES_MHZ` | 122.800 | DROPPED | Frequency filter removed (ADR-007). |
| (file mount) controller callsign list | read-only mount | CHANGED | Now fail-closed: a missing or empty file stops start-up. Path via `WHITELIST_PATH`. |

### B3. Flight and controller summary system

| v1 variable | v1 value | Status | v3 equivalent and reason |
|---|---|---|---|
| `FLIGHT_SUMMARY_ENABLED` | true | CHANGED | v1's summariser jobs are gone. One table, `flight_summary`, is recomputed every 5 minutes by a database function the collector calls (ADR-016); the `flight` view reads it. Not configurable. |
| `FLIGHT_COMPLETION_HOURS` | 8 | CHANGED | Fixed 8-hour span split in the `flight_summary` refresh function (not configurable). |
| `FLIGHT_QUIET_HOURS` (new, issue 89 phase 1) | 8 | NEW | Not an environment variable: `app_setting.flight_quiet_hours`, seeded 8 by migration 20261012000190. A FLIGHT is written to `flight_record` once the flight itself has had no position for this many hours of stored data (measured against the newest snapshot, not the clock; owner decision 12-10-2026: quiet per flight, not per pilot, so a pilot who is flying again does not hold back an earlier finished flight; migration 20261012000200, live since 13-10-2026 08:01:09 UTC, marker 3d4b5b6). A flight is a session or the sessions joined by v1's rule: same pilot, callsign, departure and arrival, new connection logged on within 120 minutes of the last position. See DEFINITIONS.md section 1b. Change with `UPDATE app_setting SET value = '6' WHERE key = 'flight_quiet_hours';` as the owner role; minimum 2.5 (the writer refuses less); the watchdog check "flights quiet longer than the quiet period plus 1 hour" assumes 8 (9 hours literal) and must follow. Replaces v1's `FLIGHT_COMPLETION_HOURS`; the charts have read it since phase 3 went live on 13-10-2026. |
| `FLIGHT_RETENTION_HOURS` | 168000000 | DROPPED | Effectively unlimited in v1. v3: raw positions 90 days (`POSITION_RETENTION_DAYS`), sessions kept forever (ADR-008). |
| `FLIGHT_DAYS_BEFORE_ARCHIVE` | 90 | CHANGED | No archive tables; replaced by dropping daily partitions after `POSITION_RETENTION_DAYS` (90). |
| `FLIGHT_SUMMARY_POLL_INTERVAL_SHORT`, `FLIGHT_SUMMARY_POLL_INTERVAL_LONG` | 60 / 900 | DROPPED | Replaced by one fixed 5-minute refresh loop in the collector. |
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

`GRAFANA_*`, `WATCHDOG_*`, `BACKUP_*`, `COLLECTOR_DB_PASSWORD`, `TELEGRAM_*`, `UPTIME_KUMA_PUSH_URL`, `HTTP_TIMEOUT_SECONDS`, `POSITION_RETENTION_DAYS`, `AIRBORNE_GAP_CAP_SECONDS`, `HOME_ICAO_PREFIXES`, `CIRCUIT_*`, `ARCHIVE_*`, `HEALTH_PORT`, `VATPAC_VERSION`.
