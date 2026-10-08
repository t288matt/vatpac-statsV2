# VATPAC Stats v3: Specification

Authoritative spec. Agents build from this file; if it is wrong, change it here first (and record the change in `docs/DECISIONS.md`).

## 1. Goal
Reproduce the VATPAC pilot/controller graphs, live and available 24/7, fully dockerised, rebuilt from git with `docker compose up -d`. Small, maintainable, supportable, watched by a watchdog and a support agent. Existing geography filtering kept. Frequency matching and sector matching are NOT part of v3.

## 2. Decisions (see docs/DECISIONS.md for reasons)
- Database: **plain PostgreSQL 16** (no TimescaleDB). Native declarative partitioning for raw positions.
- Dashboards: **Grafana OSS**, provisioned from files in git. Login required. LAN only for now; any Cloudflare tunnel is set up externally by the owner (no config in this repo).
- Collector: **Python 3.12** (httpx, psycopg 3 + psycopg_pool, shapely, pydantic-settings, aiohttp for the health endpoint).
- Migrations: plain SQL files in `db/migrations/`, applied by a one-shot `migrate` container (dbmate).
- Positions: every 15 s sample is stored; raw positions kept 90 days (drop old daily partitions); sessions kept forever.
- Names and CIDs are stored and shown (as v1). Repo is private but written public-safe: no secrets, no real personal data in fixtures.
- No backfill. Charts start empty at go-live.
- Dev/test on Windows (Docker Desktop), deploy to an always-on Linux VM. Compose must be portable.
- The watchdog alerts and never changes data or config. It is alert-only (ADR-012); capped container restarts exist in its code but only activate if `DOCKER_HOST_URL` points at a Docker endpoint provided outside this repo.

## 3. Feed and polling
- URL: `https://data.vatsim.net/v3/vatsim-data.json` (discover through `https://status.vatsim.net/status.json`, key `data.v3[0]`; fall back to the hard-coded URL if discovery fails).
- The feed refreshes every 15 s; `general.update_timestamp` (ISO-8601 UTC) is the **snapshot time** and is used as `observed_at` for every row from that snapshot.
- Poll loop (monotonic scheduler, no drift): fetch; if `update_timestamp` equals the last processed one, skip and retry in about 2 s; else process. Use `If-Modified-Since`, `Accept-Encoding: gzip`, honour `Cache-Control`, User-Agent `vatpac-stats/<version>`, timeout 10 s (connect 5 s), 3 retries with jittered exponential backoff, honour 429/`Retry-After`, circuit breaker after N consecutive failures (poll continues at a slower cadence, never exits).
- Only `pilots` and `controllers` are read. `atis`, `prefiles`, `servers`, `facilities`, `ratings`, `pilot_ratings` (the lookup arrays) are ignored; the lookup tables are seeded by migrations 3 and 11, not from the feed.
- The transceivers feed is NOT fetched.
- One database transaction per snapshot. Idempotent: replaying a snapshot inserts zero rows and changes no state.
- Every attempt writes a `poll_log` row, including failures.
- A bad poll never exits the process.

## 4. Filters (the only three)
1. **Pilots: boundary polygon.** `config/australian_airspace_polygon.json` (GeoJSON Polygon, lon,lat order). Point-in-polygon on the pilot's current latitude/longitude using shapely `contains` (edge excluded). A pilot with no position (latitude or longitude absent or null) passes (v1 behaviour); a latitude or longitude that is present but unparsable makes the record malformed (skipped and counted), not "no position". Pilots outside the polygon are not stored. A pilot is stored from the first snapshot in which they are inside. Filter object built once; prepared geometry.
2. **Controllers: callsign whitelist.** `config/controller_callsigns_list.txt`: lines `CALLSIGN, FREQ` or bare `CALLSIGN`; blank lines and `#` comments skipped; exact, case-sensitive match on the callsign. **Fail closed**: missing, unreadable or empty file stops startup (exit non-zero with a clear message). No geographic filter for controllers.
3. **Flight gate (view only).** A pilot session is a "flight" only when departure and arrival are non-empty after UPPER/TRIM. This is applied in the `flight` view, not at ingest, so every in-region session is stored.
- Removed from v1: callsign-pattern filter, frequency filter. ATIS and `_OBS` never enter because they are not on the whitelist (and ATIS is a separate feed array). A test with a fixture must prove this.
- Reference files are loaded once at startup, validated (polygon is a closed ring with lon -180..180, lat -90..90; whitelist well-formed, duplicates reported), reloaded on SIGHUP (an invalid new file is rejected, the old one stays, an alert is raised). The SHA-256 of each file is stored in `poll_log` (`polygon_sha`, `whitelist_sha`), and changes are recorded in `reference_data_change`.
- **Near-miss controllers:** a controller callsign that is not whitelisted but whose prefix (text before the first `_`) matches the prefix of any whitelisted callsign is recorded in `controller_unlisted_sighting` and logged once at WARNING (the watchdog alerts on the first sighting). `_OBS` and `_SUP` (supervisor) callsigns are never near-misses.

## 5. Definitions
- **Pilot session (flight)**: `(cid, callsign, logon_time)` where `logon_time` is kept to WHOLE SECONDS (truncated at parse, never rounded; the feed sometimes sends a fraction and sometimes not, ADR-021; a CHECK constraint enforces it). A new whole-second `logon_time` for the same cid+callsign is a new session. Within one logon, a gap of more than 120 minutes between consecutive samples or a span of more than 8 hours starts a new session (handled in the `flight` view by splitting, not by the collector). Plan fields (departure, arrival, aircraft, route, remarks, flight rules, planned altitude, deptime) are attributes updated in place to the latest non-null values; `plan_changed_at` is updated when departure or arrival changes.
- **Flight start** = first stored sample; **end** = last sample; **duration** = end - start. **Airborne minutes** = sum over consecutive sample pairs of the gap where groundspeed >= 60 kt at the earlier sample, each gap capped at `AIRBORNE_GAP_CAP_SECONDS` (default 40, about 2.5 x the 15 s interval).
- **Controller session**: `(cid, callsign, logon_time)` with `logon_time` in whole seconds as for pilots (ADR-021). Start = `logon_time`, end = last sighting. Reconnect of the same cid+callsign within 5 minutes merges (in the `controller_session_merged` view). "Closed" is derived (last seen older than 2 polls), never written.
- **Online now** = seen within the last 2 snapshots.
- **Plausibility caps** (ingest): groundspeed 0..700 kt, altitude -1500..65000 ft; values outside are stored as NULL and counted in `poll_log.rejected_values`. The same counter also takes the other values parsing drops (an integer outside int32, an unparsable number, a heading outside 0..360) for stored pilots and controllers, and every malformed feed entry that was skipped.
- Frequency is stored as integer Hz (`121.700` -> 121700000). `199.998` MHz means "no frequency" and is stored as NULL.

## 6. Database (PostgreSQL 16)
All timestamps `timestamptz` UTC. Ids BIGINT. Real PK/FK/UNIQUE/CHECK. No JSONB links.

Tables (final DDL lives in `db/migrations/`; this is the contract):
- `pilot_session(id bigserial PK, cid bigint NOT NULL, callsign text NOT NULL, logon_time timestamptz NOT NULL, UNIQUE(cid, callsign, logon_time), name text, home_icao text, server text, first_seen timestamptz NOT NULL, last_seen timestamptz NOT NULL CHECK(last_seen >= first_seen), departure text, arrival text, aircraft_icao text, aircraft_faa text, flight_rules text, route text, remarks text, planned_alt text, deptime text, plan_changed_at timestamptz, max_gs_kt int, max_alt_ft int, pilot_rating int, military_rating int)`. `home_icao` = the trailing 4-letter uppercase ICAO token of `name` if it matches `[A-Z]{4}$` after a space, else NULL.
- `pilot_position(observed_at timestamptz NOT NULL, session_id bigint NOT NULL REFERENCES pilot_session, lat double precision, lon double precision, alt_ft int, gs_kt int, hdg int CHECK (hdg BETWEEN 0 AND 360), PRIMARY KEY (session_id, observed_at)) PARTITION BY RANGE (observed_at)` with daily partitions `pilot_position_YYYYMMDD`. Maintenance (run by the collector daily and at startup): create partitions for today..today+7, drop partitions older than `POSITION_RETENTION_DAYS` (default 90) that contain no open session. No compression.
- `controller_session(id bigserial PK, cid bigint, callsign text, logon_time timestamptz, UNIQUE(cid, callsign, logon_time), name text, rating int, facility int, server text, frequency_hz bigint, visual_range int, first_seen, last_seen CHECK(last_seen >= first_seen))`; `controller_rating_event(session_id FK, observed_at, rating, PRIMARY KEY(session_id, observed_at))`; `controller_frequency_event(session_id FK, observed_at, frequency_hz, PRIMARY KEY(session_id, observed_at))` (insert only on change).
- `network_snapshot(observed_at timestamptz PRIMARY KEY, pilots_in_region int, controllers_in_region int, pilots_global int, controllers_global int)`.
- `poll_log(id bigserial PK, polled_at timestamptz default now(), status text CHECK (status IN ('ok','skipped_unchanged','error')), snapshot_ts timestamptz, duration_ms int, http_status int, pilots_seen int, pilots_inside int, pilots_with_plan int, pilots_kept int, controllers_seen int, controllers_kept int, rejected_values int, polygon_sha text, whitelist_sha text, error text)`. Retention 400 days.
- `controller_unlisted_sighting(callsign text PRIMARY KEY, cid bigint, first_seen, last_seen, polls_seen bigint)`; `reference_data_change(id, changed_at, file text, old_sha text, new_sha text)`.
- Lookups: `controller_rating(id int PK, short text, long text)`, `facility(id int PK, short text, long text)`, `pilot_rating(id int PK, short text, long text)`, `airline(prefix text PK, name text)`, `aircraft_type(icao text PK, category text, is_military boolean default false)`, `region_map(prefix text PK, region text)` (home prefixes come from config `HOME_ICAO_PREFIXES`, default `Y`).
- Views (Grafana reads views only; read-only role `grafana_ro` has SELECT on views and lookups, nothing else): `flight` (sessions after gap/span split, with start/end/duration/airborne_minutes, normalised dep/arr, flags `is_circuit`, `is_zzzz`, `touches_home_region`, `sample_count`; only rows where the flight gate passes), `controller_session_merged`, `controller_activity_daily`, `movements`, `cohorts`, `online_now`, `data_freshness`. `flight` is a thin view over the incrementally maintained table `flight_summary` (ADR-016), which the collector refreshes every 5 minutes; any materialized view would be refreshed by the daily maintenance task. The complete, current list of tables and views is `docs/DATA_DICTIONARY.md`; the dashboards also read `controller_summaries` (a v1-compatible view over `controller_session_ranked`).
- Indexes only for real chart queries; created CONCURRENTLY in later migrations.
- Migrations are forward-only SQL; each file idempotent where possible.

## 7. Collector layout (`collector/src/vatpac/`)
`settings.py` (pydantic-settings; every variable documented, defaults, validation), `feed.py` (fetch, retry, conditional, discovery), `filters.py` (the three filters + reference loading + near-miss), `model.py` (typed dataclasses and parsing/plausibility), `store.py` (one transaction per snapshot, upserts, events, network_snapshot, poll_log), `maintenance.py` (settings sync, partitions, retention, flight summary refresh, view refresh, poll_log trimming), `health.py` (aiohttp `/health` and `/metrics`), `main.py` (supervisor, signals). Budget: whole package under 1400 physical lines (ADR-014), no module over 300. One DB driver. Type-annotated, ruff + mypy clean.

### Public interfaces (contract so QA can test independently of the builder)
- `vatpac.errors.ReferenceDataError(Exception)`.
- `vatpac.filters.load_polygon(path: str | Path) -> shapely.geometry.Polygon` (raises `ReferenceDataError` if missing, not a Polygon, ring not closed, or coordinates outside lon -180..180 / lat -90..90).
- `vatpac.filters.inside_polygon(polygon, lat: float | None, lon: float | None) -> bool` (None for either coordinate returns True; edge points return False; shapely `contains`).
- `vatpac.filters.load_whitelist(path: str | Path) -> frozenset[str]` (both line formats, `#` comments and blanks skipped, exact case-sensitive callsigns; raises `ReferenceDataError` if the file is missing, unreadable or yields zero callsigns).
- `vatpac.filters.is_near_miss(callsign: str, whitelist: frozenset[str]) -> bool` (False if listed or if it ends with `_OBS` or `_SUP`; True when the text before the first `_` equals the prefix of any whitelisted callsign).
- `vatpac.model.parse_frequency_hz(text: str | None) -> int | None` ("121.700" -> 121700000; "199.998" -> None; None/invalid -> None).
- `vatpac.model.home_icao(name: str | None) -> str | None` (trailing space-separated token of exactly 4 uppercase letters, else None).
- `vatpac.model.clamp_pilot(gs: int | None, alt: int | None) -> tuple[int | None, int | None, int]` (out-of-range values become None; third item = number of rejected values).
- `vatpac.model.parse_snapshot(payload: dict) -> Snapshot` where `Snapshot` has `observed_at: datetime` (from `general.update_timestamp`, UTC), `pilots: list[Pilot]`, `controllers: list[Controller]`, `pilots_global: int`, `controllers_global: int`; `Pilot` and `Controller` are frozen dataclasses with the feed fields in snake_case (`cid, name, callsign, server, logon_time, latitude, longitude, altitude, groundspeed, heading, departure, arrival, aircraft_icao, aircraft_faa, flight_rules, route, remarks, planned_alt, deptime, pilot_rating, military_rating`; controllers: `cid, name, callsign, frequency_text, facility, rating, server, visual_range, logon_time`).
- `vatpac.settings.Settings` (pydantic-settings; env names as in `.env.example`).
- `vatpac.store.apply_snapshot(conn, snapshot, polygon, whitelist, *, polygon_sha, whitelist_sha) -> ApplyResult` (single transaction; `ApplyResult` carries the counts written to `poll_log`).

Upsert rules per snapshot (store.py):
- Pilot inside polygon: upsert `pilot_session` on `(cid, callsign, logon_time)`: set `last_seen = observed_at`, update plan fields to latest non-null, `max_gs_kt/max_alt_ft = GREATEST`, then `INSERT INTO pilot_position ... ON CONFLICT DO NOTHING`.
- Controller on whitelist: upsert `controller_session`; if `rating` changed insert `controller_rating_event`; if `frequency_hz` changed insert `controller_frequency_event` (first sighting also inserts both).
- Always insert `network_snapshot` (`ON CONFLICT DO NOTHING`).

## 8. Health, metrics, watchdog
- Collector `/health`: 200 when the last successful poll is under 60 s old and the newest stored snapshot is under 120 s old, 503 otherwise (an unchanged feed does not count as a successful poll); JSON with last_ok, last_snapshot_ts, consecutive_failures, breaker_open, reload_rejected, last_reload_error and the SHA-256 of the reference files. `/metrics`: Prometheus text (polls total/ok/error/skipped, last snapshot age, rows written, filter counts).
- Watchdog (`ops/watchdog/`, Python): every 60 s checks collector `/health`, newest `poll_log.ok` age (warn 60 s, page 300 s), `network_snapshot` freshness, DB size and disk (alert above 80%), newest backup age (alert above 26 h), the result and age of the weekly restore test, Grafana `/api/health`, near-miss sightings. **Alert-only**: it has no Docker access (the socket proxy is out of this repo, ADR-012); container state checks and capped restarts only activate if `DOCKER_HOST_URL` is set to an endpoint provided externally. It sends Telegram alerts (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` from `.env`; plain text, no emoji; send on state change only). Writes `watchdog_status` (JSON file on a volume).

## 9. Docker (compose)
Services: `db` (postgres:16, named volume, `POSTGRES_INITDB_ARGS=--data-checksums`, config via mounted `postgresql.conf` fragment, healthcheck `pg_isready`, port not published except by the `docker-compose.dev.yml` overlay, on 127.0.0.1:55432), `migrate` (dbmate, one-shot, `restart: "no"`, runs on every `up`: applies new migrations, then `db/roles.sql` for `grafana_ro`, `collector` and `backup_ro`, and creates `watchdog_ro`), `collector` (built from `collector/Dockerfile`: multi-stage python:3.12-slim, non-root, healthcheck), `grafana` (grafana-oss pinned, provisioning mounted read-only, login required, published on host port `GRAFANA_PORT` default 3300, on the interface `GRAFANA_BIND`, default loopback only), `backup` (pg_dump custom format nightly + rotation + restore script), `watchdog` (alert-only). There is no `docker-socket-proxy` and no `cloudflared` service: the owner will handle those externally. All with `restart: unless-stopped` (except migrate), healthchecks, log rotation (`json-file` max-size 10m max-file 5), memory limits, one internal network. Secrets only from `.env` (git-ignored); `.env.example` committed. `config/` mounted read-only into the collector at `/app/config`.

## 10. Dashboards (Grafana, provisioned)
Folders (named after the directories under `grafana/dashboards/`): pilots, atc, events, demographics, military, monitoring. Queries read views only, use UTC explicitly, and a Grafana variable for the time range. In scope (from the v1 cards): online pilots/controllers (now, over time, weekday/weekend hours), Monday event attendance (0700-1200Z; distinct CIDs from sessions), popular routes (international/domestic/VFR), diverse routes, airport movements, airlines, callsigns, aircraft (flights and pilots), highest groundspeed, busiest days, cohorts, home region of pilots, servers, controllers logged on (30 d / 3 mo) by rating, CTR/FSS active, MRM locals %, top 50 service providers (score = hours online, 30 d), departure board, SELCAL %, military aircraft (3 cards), monitoring/freshness from `poll_log`. Controller retention (active, lapsed, retention rate, reactivated, "who might need help reactivating" and related cards) is built from the owner's Metabase SQL export of 08-10-2026 over the `controller_summaries` view. Dropped: anything needing frequency or sector matching, Milk Run Monday fastest/slowest, v1 pipeline monitors. Every chart has an entry in `docs/CHART_DEFINITIONS.md` (v1 card id, definition, source view, test).
Dashboards show "data since <go-live>" because there is no backfill.

## 11. Tests and quality gates
- Unit tests (no database, no network): filters (polygon: YPDN inside, NZAA outside, point on edge; whitelist formats, malformed, missing -> fail closed), parsing and plausibility caps, frequency conversion, `home_icao`, session split logic helpers, settings validation.
- Fixture: an **anonymised** recorded feed snapshot (names/CIDs scrambled deterministically; callsigns kept) in `tests/fixtures/`. Golden test: expected kept pilots/controllers counts for the fixture; `_OBS` and ATIS never stored.
- Integration tests (need Docker): compose up, migrate, ingest the fixture twice (second run inserts zero rows), views return expected rows, partition create/drop, `grafana_ro` cannot write, every provisioned panel query runs.
- CI (GitHub Actions, `.github/workflows/ci.yml`): ruff, mypy, unit tests, integration tests against a Postgres service, `docker compose config`, dashboard validation, image build, gitleaks + a PII check on fixtures, config-reference drift check.
- Size budget check in CI (collector package under 1400 lines, no module over 300).

## 12. Acceptance (the goal)
1. Clean clone + `.env` from `.env.example` + `docker compose up -d`: all services healthy.
2. `poll_log` shows a snapshot about every 15 s; under 1% of expected snapshots skipped over 24 h.
3. Replaying a snapshot inserts zero rows.
4. Every provisioned panel returns data (once data exists) with no query errors; each in-scope v1 chart has a panel.
5. Kill any container: Docker/watchdog restores it; watchdog alerts once.
6. Backup restores into a fresh volume.
7. Runs unchanged on the Linux VM for 24 h.

## 13. Out of scope
Frequency matching, sector tracking/occupancy, ATC-interaction detection, enrichment workers, transceivers, the v1 REST data API, backfill from v1, anything about v1 itself.
