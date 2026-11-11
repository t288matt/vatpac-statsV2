# Architecture

Last reviewed 08-10-2026 against the repository; the Grafana version and the public-access part were updated 11-10-2026; sections 4 and 5 (flights, the drop gate) were updated on 13-10-2026 against the code and the live database (issue 89 phases 4 and 5). Authoritative sources: [SPEC.md](../SPEC.md) and [DECISIONS.md](DECISIONS.md). This page describes what the code in the repository does.

## 1. What it is

VATPAC Stats v3 polls the public VATSIM data feed every 30 seconds (`POLL_INTERVAL_SECONDS`, ADR-024; the feed itself refreshes every 15 s), keeps the pilots inside the Australian airspace polygon and the controllers on a callsign whitelist, stores them in PostgreSQL 16, and shows them in Grafana 12.4 dashboards, which can be published read-only on the internet (section 3). Everything runs from one `docker compose up -d`.

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
2. Read `general.update_timestamp`. If it is not newer than the last processed snapshot (after a restart: the newest stored one, read from the database at start-up), the poll is recorded as `skipped_unchanged` and retried in about 2 seconds (never less than 2 s, at most 5 s, depending on `Cache-Control`). A timestamp more than `FEED_MAX_FUTURE_SECONDS` (default 120) ahead of the collector clock is rejected first, as an `error` poll (audit F2, issue 34); this needs the VM clock to be right (NTP).
3. Otherwise parse the snapshot. A snapshot with no pilots and no controllers at all is treated as an outage (an error), not as data.
4. `apply_snapshot` runs in one database transaction: pilot upserts and position inserts, controller upserts and rating/frequency events, near-miss controller sightings, and the `network_snapshot` row. Every row uses the snapshot's own timestamp as `observed_at`.
5. A `poll_log` row is written for every attempt, including failures. This write is separate from the snapshot transaction, so a failed snapshot still leaves its error row.
6. The in-memory statistics behind `/health` and `/metrics` are updated.
7. The raw payload and the poll outcome are queued for the archive thread (`archive.py`), which keeps a compact record every 5 minutes, the full payload hourly and anomaly payloads (poll error, timestamp gap, older or future timestamp, `logon_time` precision change). This runs after the snapshot is stored, off the poll path, and can never fail or slow a poll ([runbook: feed archive](RUNBOOKS/feed-archive.md)).

Replaying a snapshot inserts zero rows: sessions are upserted on `(cid, callsign, logon_time)`, positions on `(session_id, observed_at)` with `ON CONFLICT DO NOTHING`, and `network_snapshot` on `observed_at`.

## 3. Containers

Defined in `docker-compose.yml` (project name `vatpac`, one internal network, `json-file` logs capped at 10 MB x 5, memory limits on every service).

| Service | Image / build | Role | Notes |
|---|---|---|---|
| `db` | `postgres:16-bookworm` | The database. | Named volume `pgdata`; `--data-checksums`; tuning in `ops/postgres/postgresql.conf`; healthcheck `pg_isready`; port not published (the dev overlay publishes `127.0.0.1:55432` only). |
| `migrate` | `ops/migrate` (dbmate 2 and psql) | One-shot: applies new `db/migrations/*.sql`, then `db/roles.sql` (roles `grafana_ro`, `collector`, `backup_ro`) and creates `watchdog_ro`. | `restart: "no"`. Safe to re-run on every `up`. `./db` is mounted read-only, so new migration files need no image rebuild. Other services wait for `service_completed_successfully`. |
| `collector` | `collector/Dockerfile` | The only writer. Poll loop, daily maintenance, `/health` and `/metrics`. | Connects as the least-privilege role `collector`. Non-root, read-only filesystem, all capabilities dropped, `config/` mounted read-only at `/app/config`, 512 MB. The only writable place is the named volume `archive` at `/archive` (the raw-feed archive, owned by uid 10001). |
| `grafana` | `grafana/grafana:12.4` | Dashboards, provisioned from `grafana/provisioning` and `grafana/dashboards` (read-only mounts). | Login required, anonymous access off, published on `GRAFANA_BIND`:`GRAFANA_PORT` (default `127.0.0.1:3300`, loopback only; production uses the VM's LAN address). Reads the database as `grafana_ro`. Image `grafana/grafana` (OSS edition), because `grafana-oss` stops at 11.6.5. |
| `backup` | `ops/backup` | Nightly `pg_dump` (custom format) as the read-only role `backup_ro`, rotation, weekly restore test, optional off-box hook (`BACKUP_REMOTE`). | Volume `backups`; see [runbook: restore](RUNBOOKS/restore.md). |
| `watchdog` | `ops/watchdog` | Checks health and the database every 60 s and alerts. | Alert-only; see section 6. Writes a status file on volume `watchdog_status`. |

All services except `migrate` use `restart: unless-stopped` and have healthchecks (the collector's calls its own `/health`).

Start-up order: `db` healthy, then `migrate` completes, then `collector`, `grafana`, `backup` and `watchdog` start.

### Public read-only access (ADR-029)

The 14 dashboards are also published read-only on the internet at `https://vpac.mpengor.com` (the owner confirmed the hostname). The mechanism is Grafana public dashboards: one public link per dashboard (a token in the address), a fixed time range, no annotations. The token of each link is fixed (first 32 hex characters of sha256 of `vatpac-public:` plus the dashboard uid), and 14 short addresses (`/traffic`, `/new-pilots`, `/origins`, `/atc`, `/atc-retention`, `/atc-hours`, `/pilots`, `/routes`, `/aircraft`, `/atc-positions`, `/atc-ranks`, `/monday`, `/military`, `/health`) redirect to the links through Traefik labels on the helper container; the address bar then shows the long address. The path:

```
visitor -> Cloudflare (rate limit rule, Bot Fight Mode) -> Cloudflare Tunnel (named Home; connector on the Traefik machine)
        -> Traefik (owner's docker 145 machine, <TRAEFIK_HOST_IP>) -> helper container vatpac-proxy (alpine/socat, ~/vatpac-proxy there)
        -> http://<VM_LAN_IP>:3300 (Grafana on this VM)
```

| Part | Where | In this repository? |
|---|---|---|
| Grafana with public dashboards enabled (the default); anonymous access off | `docker-compose.yml`, the VM | Yes |
| `ops/grafana_public.py` (create, repair, `--list`, `--remove`, `--traefik-labels --host <host>`; reads the admin login from `.env`, never prints it; shares the Grafana client `ops/grafana_api.py` with `ops/check_panels.py`: the login goes in clear text only to an address of this machine, https otherwise, and redirects are never followed) | run on the VM | Yes |
| `.env` readers: `ops/env_key.sh` (shell scripts) and `ops/env_file.py` (Python tools), one set of rules; `ops/release_common.sh` (release-marker reader and `gh_bin`, shared by `ops/deploy.sh` and `ops/stale_issues.sh`) | ops scripts | Yes |
| Cloudflare (rate limit, Bot Fight Mode), the tunnel, DNS | Cloudflare account | No (described in the runbook) |
| Traefik and the `vatpac-proxy` helper (Traefik reads Docker labels only on its own machine and its file provider is off, so a forwarder with labels carries the route; the forwarder is deployed on the same host as Traefik, docker 145, for that reason) | owner's docker 145 machine (runs Traefik) | No (compose file shown in the runbook) |

Neither Cloudflare nor the Traefik machine can be checked from this repository or the VM. A visitor can open the public pages and see every panel; they cannot run their own SQL, change the time range, edit, or use Explore. Panels run only the queries saved on the dashboard, through the read-only role `grafana_ro`; the page does expose the SQL text of each panel. Verified 11-10-2026: without a login `/api/search`, `/api/datasources`, `/api/user` and `/api/ds/query` answer 401, SQL sent inside a public panel request is ignored, and 84 of 84 data panels work as public dashboards (none over 5 s). Anonymous access stays off because an anonymous Viewer could send any SQL to `grafana_ro` through `/api/ds/query`. The Grafana login page is still reachable from the internet (the owner chose not to add a Cloudflare rule against it). Public dashboards do not fill text-box variables, so no dashboard has a text-box input. Runbook: [RUNBOOKS/public-access.md](RUNBOOKS/public-access.md); the work on the Traefik machine is documented in its section "The Traefik machine (<TRAEFIK_HOST_IP>)". The owner keeps copies of that machine's compose file and README privately on his PC.

## 4. Collector internals

Package `collector/src/vatpac/` (Python 3.14, asyncio):

| Module | Responsibility |
|---|---|
| `settings.py` | Environment variables, validated by pydantic-settings. See [CONFIG_MAP.md](CONFIG_MAP.md). |
| `feed.py` | Fetching: `If-Modified-Since`, gzip, `Cache-Control`, retries with jittered backoff, 429 `Retry-After`, URL discovery. |
| `model.py` | Typed `Pilot`, `Controller`, `Snapshot`; parsing; plausibility caps; frequency conversion; `home_icao`. |
| `filters.py` | The polygon filter, the controller whitelist, near-miss detection, reference-file loading and SHA-256 hashing. |
| `store.py` | One transaction per snapshot; `poll_log` and `reference_data_change` writers. |
| `maintenance.py` | At start-up and then daily: publish settings to `app_setting`, create partitions, write finished flights (`write_finished_flights`, 50 pilots a call), drop old partitions (only days whose positions are all in written flights, see DATA_DICTIONARY section 1.5), trim `poll_log` (400 days), refresh any materialized views. Finished flights are also written every 5 minutes by their own loop in `main.py`, which calls the writer directly (`SELECT write_finished_flights(50)`; a statement timeout is logged and the next run continues; live since 13-10-2026, marker 7a71d30). The drop loop drops one partition per database call and pauses 0.5 seconds between calls. |
| `archive.py` | The rolling raw-feed archive: gzipped compact (5 min, 30 days), full (hourly, 7 days) and anomaly (90 days) files on the `archive` volume, a hard size cap, one worker thread, errors counted and swallowed. See [runbook: feed archive](RUNBOOKS/feed-archive.md). |
| `health.py` | aiohttp `/health` (200 only when the last `ok` poll is under 60 s old and the newest stored snapshot is under 120 s old, else 503; an unchanged feed does not count as progress) and `/metrics` (Prometheus text). |
| `main.py` | Supervisor: poll loop, daily maintenance loop, 5-minute flight-writer loop (`flight_summary_loop`), SIGHUP reload, signal handling. |

Behaviours worth knowing:

- **Scheduling.** A monotonic scheduler: the next poll is due one interval after the previous due time or one interval after the feed client's last real send, whichever is later (since issue 261), so a poll never starts before the 30 s request floor has elapsed and the cadence is about 30.003 s rather than a drift-free grid. If a poll overruns, missed ticks are skipped rather than fired in a burst.
- **Reference files.** The polygon (`config/australian_airspace_polygon.json`) and whitelist (`config/controller_callsigns_list.txt`) are loaded once at start-up. A missing, unreadable or invalid file stops start-up with a non-zero exit (fail closed). On SIGHUP they are reloaded; an invalid new file, or a whitelist that shrinks to under 70% of its previous size, is rejected: the previous files stay in use, an error is logged and `/health` reports `reload_rejected` (the watchdog does not alert on this yet). Their SHA-256 values are written to every `poll_log` row, and a change is recorded in `reference_data_change`.
- **Maintenance** runs once at start-up (partitions must exist before the first insert) and then every 24 hours; if a run has a failing task it is retried after 60 seconds. One failing task does not stop the others. If an insert is rejected because no partition covers the snapshot time, the collector runs maintenance immediately.
- **Database connection** is a small pool (1 to 4 connections) that keeps retrying in the background if the database is not yet up. Statement, lock and idle-in-transaction timeouts apply, and applying one snapshot is abandoned after 25 seconds.
- **Bad records.** A single malformed or poisoned feed record is bisected out inside savepoints and counted; it never blocks the snapshot. A snapshot with more than 20% malformed entries is treated as an error.

## 5. Database layout in one paragraph

Sessions (`pilot_session`, `controller_session`) are the identity tables, keyed `(cid, callsign, logon_time)`. Raw 15-second positions live in `pilot_position`, partitioned by day and kept 180 days (`POSITION_RETENTION_DAYS`, a literal in `docker-compose.yml` since 19-10-2026, ADR-034; the VM has run 180 since 13-10-2026, issue 142; the collector reports it). Since 13-10-2026 (issue 89 phases 1, 1b, 3, 4 and 5 live; marker 7a71d30) the `flight` view and everything built on it read `flight_summary`, one row per finished flight written once by `write_finished_flights()` when the flight has had no position for 8 hours (DEFINITIONS section 1b). It is the only flight store: the per-session pieces table of ADR-016 and its refresh functions were removed by phase 5 (ADR-030). A raw-position partition is dropped only when every position in it lies inside a written flight (the phase 4 drop gate, DATA_DICTIONARY section 1.5); at 180 days retention the first partition can be dropped about 05-04-2027. Controller rating and frequency changes are events. `network_snapshot` holds one row of headline counts per snapshot. `poll_log` holds one row per attempt. Views derive everything the dashboards need. Full detail in [DATA_DICTIONARY.md](DATA_DICTIONARY.md), definitions in [DEFINITIONS.md](DEFINITIONS.md).

Grafana connects as `grafana_ro`: login role, connection limit 10, `default_transaction_read_only = on`, 30 s statement timeout, SELECT on every view and the lookup tables, no access to base tables. Views run with their owner's privileges, which is why the role does not need table access. A session can switch its own read-only default off, so the real protection is the lack of write privileges, which `ops/smoke.sh` proves.

## 6. Failure behaviour

| Event | What happens | Verified? |
|---|---|---|
| Feed unreachable, slow, malformed or empty | The poll is logged as `error` (with the HTTP status and message). The collector stays up. After `CIRCUIT_FAILURE_THRESHOLD` (default 5) consecutive failures the breaker opens: polling continues at `CIRCUIT_SLOW_INTERVAL_SECONDS` (default 60 s), the feed URL is re-discovered, and `/health` reports `breaker_open`. One success closes it. | Yes, on 07-10-2026 (outage left the collector up, recorded error rows and opened the breaker). |
| Feed returns the same snapshot | Logged as `skipped_unchanged`; nothing written. | Yes (normal operation). |
| Feed returns 429 | `Retry-After` is honoured. | Code path only; not exercised live. |
| A bad poll (any exception) | Caught, logged, recorded in `poll_log`; the process never exits because of a poll. | By design (code); not separately demonstrated. |
| Collector process crashes (for example an out-of-memory kill) | Docker restarts it (`unless-stopped`). | Yes, an OOM kill was restarted on 07-10-2026. Note: `docker kill` is treated by Docker as a manual stop and is not restarted; a genuine crash is. |
| The VM reboots | All containers come back by themselves (`unless-stopped`). | Yes, reboot test 07-10-2026 21:48 UTC: all five long-running containers returned unaided. |
| Collector running but unhealthy | Not restarted automatically. The watchdog alerts. Restarts need the external Docker endpoint (ADR-012). | Alerting verified (see below); restart not in scope. |
| Database down | Polls fail and are retried; the pool reconnects in the background; the `poll_log` row for those attempts cannot be written either (a failed write is logged, not raised). | Not separately verified. |
| Reference file replaced with an invalid one | SIGHUP reload rejected, old file stays, error logged. A bad file at start-up stops the collector. | Loading and validation are unit tested; the SIGHUP path itself was not demonstrated. |
| The archive volume is full or unwritable | Archive writes fail; each failure is counted (`vatpac_archive_errors_total`) and the first is logged; polling is unaffected. The watchdog warns after twice the compact interval without a write. | Yes, 08-10-2026 on a throwaway stack: an unwritable `ARCHIVE_DIR` left 8 of 8 polls `ok`, logged one error and counted 8. |
| Near-miss controller callsign appears | Recorded in `controller_unlisted_sighting`, logged once at WARNING, watchdog alerts on the first sighting. | Detection logic is unit tested; the alert path was not demonstrated. |

The watchdog (`ops/watchdog/watchdog.py`) checks the collector `/health`, the age of the newest `ok` poll and snapshot, database size, disk, newest backup age, Grafana `/api/health` and near-miss sightings, and alerts by Telegram on state change. It is **alert-only by default**: it has no Docker access, so container-state checks and capped restarts stay off unless `DOCKER_HOST_URL` points at an endpoint provided outside this repository (ADR-012). On 07-10-2026 it alerted and recovered correctly while testing locally, and a Telegram test alert from the VM was delivered and confirmed by the owner. It also reads the weekly restore-test result, runs the identity-flap check (`identity_flap`, sessions of one `cid` and callsign with `logon_time` within 2 s, or a fractional `logon_time`, seen in the last 24 hours; WARN once, RECOVERED when clear) and warns when the collector's archive has written nothing for twice its compact interval (`feed_archive`). What each alert means and what to do is in [runbook: alerts](RUNBOOKS/alerts.md). It does not alert on a rejected reference-file reload (`/health` shows `reload_rejected`); that gap is known.

## 6b. How it is deployed and where the images come from (issue 173; live since 14-10-2026)

Until 14-10-2026 the production VM was deployed by copying the tree with `git archive` over ssh and building the four project images (collector, watchdog, backup, migrate) on the VM. Since the evening of 14-10-2026 (first registry deploy, commit 67c1ca0, verified): the VM folder is a read-only git checkout (deploy key); the release workflow (`.github/workflows/release.yml`, started by hand for a commit that is on main with a green CI run) builds and pushes those four images to a private registry as `ghcr.io/t288matt/vatpac-stats-<name>:sha-<7>`; `docker-compose.yml` uses them when `VATPAC_IMAGE_PREFIX` and `VATPAC_IMAGE_TAG` are set, and builds locally when both are unset (one compose file, two modes). A second registry deploy and a rollback to a previous tag have not been run yet. Postgres and Grafana images come from Docker Hub as before. Dashboards, provisioning, `config/`, `db/` and `ops/postgres/postgresql.conf` stay bind mounts from the tree, so they follow a git checkout without an image build. Migrations are baked into the migrate image as well as mounted. Decision and reasons: ADR-031 in [DECISIONS.md](DECISIONS.md). Procedure: [RUNBOOKS/deploy-to-vm.md](RUNBOOKS/deploy-to-vm.md).

## 7. Deliberately out of scope

- Frequency matching, sector tracking and occupancy, ATC-interaction detection (ADR-002). Controller frequency is stored so a stand-in can be added later without a schema change.
- Enrichment workers, the transceivers feed, a REST data API (ADR-010), backfill from v1 (ADR-011).
- The Docker socket proxy and the Cloudflare and Traefik configuration for public access: set up externally by the owner (ADR-012, ADR-029). See [runbook: deploy to the VM](RUNBOOKS/deploy-to-vm.md).
- TimescaleDB, compression, and any raw-position retention beyond the configured retention (180 days; ADR-003, ADR-008, ADR-026).

## 8. Size and quality gates

The collector package has a hard size budget of 1800 physical lines with no module over 300 (ADR-014, ADR-022), checked in CI by `ops/check_size_budget.py`. CI also runs ruff, mypy, the unit tests (with a check on the number of skipped tests), the integration tests once, in UTC, against a Postgres service, the config-drift check, the dashboard validator (which parses every query with sqlglot) and navigation check, workflow linting (actionlint), `docker compose config`, image builds, a fixture PII check, a secret scan and an attribution check (`.github/workflows/ci.yml`). The small checks are the steps of one `quick-checks` job. A pull request that changes only dashboard JSON runs a lighter set, and one that changes only prose runs the secret scan and the attribution check only; everything else, and every push to `main`, runs the full suite ([run-tests.md](RUNBOOKS/run-tests.md) section 1c).

## 9. Related documents

[DATA_DICTIONARY.md](DATA_DICTIONARY.md), [DEFINITIONS.md](DEFINITIONS.md), [CONFIG_MAP.md](CONFIG_MAP.md), [CHART_DEFINITIONS.md](CHART_DEFINITIONS.md), [DECISIONS.md](DECISIONS.md), [RUNBOOKS/](RUNBOOKS/).

## 10. Goal evidence

This table records the goal gate evidence, each item produced in the session that recorded it (clock read with `ssh vatpac-v3 date -u`). The owner changed the soak standard on 08-10-2026 from 24 h to 12 h; both readings are given.

| Gate | Status on 08-10-2026 | Evidence |
|---|---|---|
| Soak on the VM (verdict PASS) | Met at the owner's 12 h standard (decision 08-10-2026); the 24 h run was still INCOMPLETE | `sh ops/soak_report.sh 12` at 12:23 UTC on 08-10-2026 (and again at 12:36 and 12:39 UTC, before and after the deploy): window 00:22 to 12:22 UTC, 12.00 h covered, 2868 of 2881 snapshots (99.55%), 0 gaps over 45 s (longest 38 s), p95 cadence 15.0 s, 0 error polls, verdict PASS. The same minute the 24 h report read 14.74 h covered of 24 h, 99.52%, one 45 s gap (the reboot test at 21:48 UTC on 07-10), verdict INCOMPLETE. The 12 h window includes the 02:00 UTC backup and the midnight partition rollover, but no weekday evening peak and not the reboot-test gap. |
| Reboot test | Met | 07-10-2026 21:48 UTC, all five long-running containers returned unaided, smoke passed, 0 panel errors. |
| CI green on GitHub | Met | Owner confirmed all jobs green for commit fec82a4 (08-10-2026), the deployed head, after earlier confirmations for 7ec487e, 653b2ed and e5816af. The run for 483cce5 failed one new integration test (case of the TimeZone setting in the test, root cause reproduced on a throwaway cluster and fixed in fec82a4). |
| Telegram alert delivered | Met | Test alert accepted by Telegram and confirmed by the owner, 07-10-2026. |
| Deploy of fec82a4 to the VM | Done 08-10-2026 12:37 UTC | Pre-deploy dump (1.8 MB) and a tree copy taken at 12:36 UTC; code copied by git archive; `docker compose up -d --build` recreated the collector only (started 12:37:21 UTC). Migrations 20261008000030, 31, 80 and 90 applied, roles.sql re-applied (grafana_ro has TimeZone=UTC). `sh ops/smoke.sh`: SMOKE PASS. `python3 ops/check_panels.py`: 84 panel queries, 75 with rows, 9 empty (young data), 0 errors. All five containers healthy, restart count 0. The soak report (12 h) after the deploy still showed 0 gaps over 45 s. EXPLAIN (ANALYZE) on live data: card 156 execution 0.76 ms, card 186 execution 2.16 ms (tables still small: 106,371 positions, 45 controller sessions, so these timings say little; re-measure when the data has grown). |
| Deploy of c071955 (duplicate-session fix, T36) | Done 08-10-2026 17:10 UTC | Pre-deploy dump 2.1 MB and a tree copy at 17:10 UTC; collector stopped, `docker compose up -d --build`; migration 20261008000100 applied (1.13 s). The identity-flap check: 0, 0, 0 with the new collector polling. `sh ops/smoke.sh`: SMOKE PASS. `ops/check_panels.py`: 84 queries, 74 with rows, 10 empty, 0 errors. All five containers healthy, restart count 0; longest snapshot gap 30 s, so the 12 h soak report still reads PASS. |
| Full 24 h soak on the VM (verdict PASS) | Met 09-10-2026 07:23 UTC | `sh ops/soak_report.sh 24`: 24.00 h from 08-10-2026 07:22:57 to 09-10-2026 07:22:42 UTC, 5,733 of 5,760 snapshots (99.53%), 1 gap over 45 s (90 s, the test-container memory incident, tracker T39), p95 15.0 s, 0 error polls, PASS. Containers up with 0 restarts and no OOM kills; the window includes the 08-10 evening peak and the 17:10 deploy restart. |
