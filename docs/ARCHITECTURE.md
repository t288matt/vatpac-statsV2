# Architecture

Last updated 07-10-2026. Authoritative sources: [SPEC.md](../SPEC.md) and [DECISIONS.md](DECISIONS.md). This page describes what the code in the repository does.

## 1. What it is

VATPAC Stats v3 polls the public VATSIM data feed every 15 seconds, keeps the pilots inside the Australian airspace polygon and the controllers on a callsign whitelist, stores them in PostgreSQL 16, and shows them in Grafana dashboards. Everything runs from one `docker compose up -d`.

## 2. Data flow

```
 VATSIM feed (JSON, refreshes every 15 s)
        |
        v
 collector / feed.py     conditional GET, gzip, retries, 429 handling, circuit breaker
        |
        v
 collector / model.py    parse pilots + controllers; plausibility caps; frequency to Hz
        |
        v
 collector / filters.py  pilots: inside polygon?   controllers: on whitelist? (near-miss check)
        |
        v
 collector / store.py    ONE transaction per snapshot (idempotent)
        |
        v
 PostgreSQL tables       pilot_session, pilot_position (daily partitions), controller_session,
        |                events, network_snapshot, poll_log, ...
        v
 SQL views               flight, controller_session_merged, online_now, movements, ... (see DATA_DICTIONARY.md)
        |
        v
 Grafana (role grafana_ro, SELECT on views and lookups only)
```

Step by step, per poll (`collector/src/vatpac/main.py`, `Collector.poll_once`):

1. Fetch the feed. The feed URL is discovered from `https://status.vatsim.net/status.json` (`data.v3[0]`) at start-up, with `VATSIM_FEED_URL` as the fallback.
2. Read `general.update_timestamp`. If it is not newer than the last processed snapshot, the poll is recorded as `skipped_unchanged` and retried in about 2 seconds (never less than 2 s, at most 5 s, depending on `Cache-Control`).
3. Otherwise parse the snapshot. A snapshot with no pilots and no controllers at all is treated as an outage (an error), not as data.
4. `apply_snapshot` runs in one database transaction: pilot upserts and position inserts, controller upserts and rating/frequency events, near-miss controller sightings, and the `network_snapshot` row. Every row uses the snapshot's own timestamp as `observed_at`.
5. A `poll_log` row is written for every attempt, including failures. This write is separate from the snapshot transaction, so a failed snapshot still leaves its error row.
6. The in-memory statistics behind `/health` and `/metrics` are updated.

Replaying a snapshot inserts zero rows: sessions are upserted on `(cid, callsign, logon_time)`, positions on `(session_id, observed_at)` with `ON CONFLICT DO NOTHING`, and `network_snapshot` on `observed_at`.

## 3. Containers

Defined in `docker-compose.yml` (project name `vatpac`, one internal network, `json-file` logs capped at 10 MB x 5, memory limits on every service).

| Service | Image / build | Role | Notes |
|---|---|---|---|
| `db` | `postgres:16.4-bookworm` | The database. | Named volume `pgdata`; `--data-checksums`; tuning in `ops/postgres/postgresql.conf`; healthcheck `pg_isready`; port not published (the dev overlay publishes `127.0.0.1:55432` only). |
| `migrate` | `ops/migrate` (dbmate) | One-shot: applies `db/migrations/*.sql`, then `db/roles.sql` (`grafana_ro`) and creates `watchdog_ro`. | `restart: "no"`. Safe to re-run on every `up`. Other services wait for `service_completed_successfully`. |
| `collector` | `collector/Dockerfile` | The only writer. Poll loop, daily maintenance, `/health` and `/metrics`. | Non-root, read-only filesystem, all capabilities dropped, `config/` mounted read-only at `/app/config`, 512 MB. |
| `grafana` | `grafana/grafana-oss:11.2.2` | Dashboards, provisioned from `grafana/provisioning` and `grafana/dashboards` (read-only mounts). | Login required, anonymous access off, published on host port `GRAFANA_PORT` (default 3300). |
| `backup` | `ops/backup` | Nightly `pg_dump` (custom format), rotation, restore test. | Volume `backups`; see [runbook: restore](RUNBOOKS/restore.md). |
| `watchdog` | `ops/watchdog` | Checks health and the database every 60 s and alerts. | Alert-only; see section 6. Writes a status file on volume `watchdog_status`. |

All services except `migrate` use `restart: unless-stopped` and have healthchecks (the collector's calls its own `/health`).

Start-up order: `db` healthy, then `migrate` completes, then `collector`, `grafana`, `backup` and `watchdog` start.

## 4. Collector internals

Package `collector/src/vatpac/` (Python 3.12, asyncio):

| Module | Responsibility |
|---|---|
| `settings.py` | Environment variables, validated by pydantic-settings. See [CONFIG_MAP.md](CONFIG_MAP.md). |
| `feed.py` | Fetching: `If-Modified-Since`, gzip, `Cache-Control`, retries with jittered backoff, 429 `Retry-After`, URL discovery. |
| `model.py` | Typed `Pilot`, `Controller`, `Snapshot`; parsing; plausibility caps; frequency conversion; `home_icao`. |
| `filters.py` | The polygon filter, the controller whitelist, near-miss detection, reference-file loading and SHA-256 hashing. |
| `store.py` | One transaction per snapshot; `poll_log` and `reference_data_change` writers. |
| `maintenance.py` | Daily: publish settings to `app_setting`, create partitions, drop old partitions, trim `poll_log` (400 days), refresh any materialized views. |
| `health.py` | aiohttp `/health` (200 when the last good poll is under 60 s old, else 503) and `/metrics` (Prometheus text). |
| `main.py` | Supervisor: poll loop, maintenance loop, SIGHUP reload, signal handling. |

Behaviours worth knowing:

- **Scheduling.** A monotonic scheduler: the next poll is due at the previous due time plus the interval, so there is no drift. If a poll overruns, missed ticks are skipped rather than fired in a burst.
- **Reference files.** The polygon (`config/australian_airspace_polygon.json`) and whitelist (`config/controller_callsigns_list.txt`) are loaded once at start-up. A missing, unreadable or invalid file stops start-up with a non-zero exit (fail closed). On SIGHUP they are reloaded; an invalid new file is rejected, the previous one stays in use, and an error is logged. Their SHA-256 values are written to every `poll_log` row, and a change is recorded in `reference_data_change`.
- **Maintenance** runs once at start-up (partitions must exist before the first insert) and then every 24 hours; if a run has a failing task it is retried after 60 seconds. One failing task does not stop the others. If an insert is rejected because no partition covers the snapshot time, the collector runs maintenance immediately.
- **Database connection** is a small pool (1 to 4 connections) that keeps retrying in the background if the database is not yet up.

## 5. Database layout in one paragraph

Sessions (`pilot_session`, `controller_session`) are the identity tables, keyed `(cid, callsign, logon_time)`. Raw 15-second positions live in `pilot_position`, partitioned by day and kept 90 days. Controller rating and frequency changes are events. `network_snapshot` holds one row of headline counts per snapshot. `poll_log` holds one row per attempt. Views derive everything the dashboards need. Full detail in [DATA_DICTIONARY.md](DATA_DICTIONARY.md), definitions in [DEFINITIONS.md](DEFINITIONS.md).

Grafana connects as `grafana_ro`: login role, `default_transaction_read_only = on`, 30 s statement timeout, SELECT on every view and the lookup tables, no access to base tables. Views run with their owner's privileges, which is why the role does not need table access.

## 6. Failure behaviour

| Event | What happens | Verified? |
|---|---|---|
| Feed unreachable, slow, malformed or empty | The poll is logged as `error` (with the HTTP status and message). The collector stays up. After `CIRCUIT_FAILURE_THRESHOLD` (default 5) consecutive failures the breaker opens: polling continues at `CIRCUIT_SLOW_INTERVAL_SECONDS` (default 60 s), the feed URL is re-discovered, and `/health` reports `breaker_open`. One success closes it. | Yes, on 07-10-2026 (outage left the collector up, recorded error rows and opened the breaker). |
| Feed returns the same snapshot | Logged as `skipped_unchanged`; nothing written. | Yes (normal operation). |
| Feed returns 429 | `Retry-After` is honoured. | Code path only; not exercised live. |
| A bad poll (any exception) | Caught, logged, recorded in `poll_log`; the process never exits because of a poll. | By design (code); not separately demonstrated. |
| Collector process crashes (for example an out-of-memory kill) | Docker restarts it (`unless-stopped`). | Yes, an OOM kill was restarted on 07-10-2026. Note: `docker kill` is treated by Docker as a manual stop and is not restarted; a genuine crash is. |
| Collector running but unhealthy | Not restarted automatically. The watchdog alerts. Restarts need the external Docker endpoint (ADR-012). | Alerting verified; restart not in scope. |
| Database down | Polls fail and are retried; the pool reconnects in the background; the `poll_log` row for those attempts cannot be written either (a failed write is logged, not raised). | Not separately verified. |
| Reference file replaced with an invalid one | SIGHUP reload rejected, old file stays, error logged. A bad file at start-up stops the collector. | Loading and validation are unit tested; the SIGHUP path itself was not demonstrated. |
| Near-miss controller callsign appears | Recorded in `controller_unlisted_sighting`, logged once at WARNING, watchdog alerts on the first sighting. | Detection logic is unit tested; the alert path was not demonstrated. |

The watchdog (`ops/watchdog/watchdog.py`) checks the collector `/health`, the age of the newest `ok` poll and snapshot, database size, disk, newest backup age, Grafana `/api/health` and near-miss sightings, and alerts by Telegram on state change. It is **alert-only by default**: it has no Docker access, so container-state checks and capped restarts stay off unless `DOCKER_HOST_URL` points at an endpoint provided outside this repository (ADR-012). On 07-10-2026 it alerted and recovered correctly; Telegram was not configured, so alerts were logged only. Telegram delivery is **not yet verified**.

## 7. Deliberately out of scope

- Frequency matching, sector tracking and occupancy, ATC-interaction detection (ADR-002). Controller frequency is stored so a stand-in can be added later without a schema change.
- Enrichment workers, the transceivers feed, a REST data API (ADR-010), backfill from v1 (ADR-011).
- The Docker socket proxy and any Cloudflare tunnel configuration: set up externally by the owner (ADR-012). See [runbook: deploy to the VM](RUNBOOKS/deploy-to-vm.md).
- TimescaleDB, compression, and any raw-position retention beyond 90 days (ADR-003, ADR-008).

## 8. Size and quality gates

The collector package has a hard size budget of 1300 physical lines with no module over 300 (ADR-014), checked in CI by `ops/check_size_budget.py`. CI also runs ruff, mypy, the unit tests, `docker compose config`, image builds and a fixture PII check (`.github/workflows/ci.yml`).

## 9. Related documents

[DATA_DICTIONARY.md](DATA_DICTIONARY.md), [DEFINITIONS.md](DEFINITIONS.md), [CONFIG_MAP.md](CONFIG_MAP.md), [CHART_DEFINITIONS.md](CHART_DEFINITIONS.md), [DASHBOARDS_SIMPLIFIED.md](DASHBOARDS_SIMPLIFIED.md), [DECISIONS.md](DECISIONS.md), [RUNBOOKS/](RUNBOOKS/).
