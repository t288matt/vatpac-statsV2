# Decisions (ADR log)

Each entry: decision, reason, consequence. Dates are DD-MM-YYYY.

## ADR-001 Rebuild instead of refactor (07-10-2026)
v1 (t288matt/vatpac-stats) is over-engineered: a 3.6k-line service class, sector tracking and ATC detection inside the ingest path, enrichment queues, archive copies, no keys or constraints. Rebuilt small, with its own schema. v1 is not modified or depended on.

## ADR-002 Frequency matching and sector matching are out of scope (07-10-2026)
Reason: they caused most of v1's cost and failures (slow queries, corruption, stalled summaries) and are not required. Consequence: the charts that need them are dropped (pilots on same frequency, % time/flights with ATC, sector charts, Milk Run Monday fastest/slowest, aircraft handled). Controller frequency is still stored so a stand-in can be added later without a schema change.

## ADR-003 Plain PostgreSQL 16, no TimescaleDB (07-10-2026)
Reason: volume is small (about 9 to 29 million raw rows at 90 days) and fewer moving parts is more supportable. Consequence: native daily partitions for `pilot_position`, partition create/drop done by the collector's maintenance task, no compression (about 2 to 3 GB), rollups as views or materialized views.

## ADR-004 Grafana as code instead of Metabase (07-10-2026)
Dashboards live in git as JSON and load on start, so the stack rebuilds from a clean clone. Metabase cannot load dashboards from files without a paid feature.

## ADR-005 Poll every 15 s aligned to the feed timestamp (07-10-2026)
The feed refreshes every 15 s (`general.update_timestamp`). v1 polled every ~92 s and saw one snapshot in six. v3 skips unchanged snapshots and records every attempt in `poll_log`.

## ADR-006 Session identity (07-10-2026)
Flight and controller sessions are keyed `(cid, callsign, logon_time)`. Departure, arrival, route and aircraft are attributes, never keys (v1 keyed on them and produced duplicates).

## ADR-007 Filters (07-10-2026)
Kept: pilot boundary polygon, controller callsign whitelist (now fail-closed). Removed: callsign-pattern and frequency filters, which were inactive in v1 (the whitelist keeps ATIS and `_OBS` out). The flight-plan gate moves from ingest to a view so early positions are not lost.

## ADR-008 Retention (07-10-2026)
Raw positions 90 days; sessions and rollups permanent.

## ADR-009 Names and CIDs are stored and shown (07-10-2026)
As v1. When dashboards are exposed publicly (Cloudflare later) real names become visible; recommended: Cloudflare Access (login). Owner to check the VATSIM data-use policy before exposure.

## ADR-010 No REST data API (07-10-2026)
Only health/metrics endpoints. Dashboards read the database through a read-only role.

## ADR-011 No backfill (07-10-2026)
Charts start empty at go-live and show "data since <go-live date>".

## ADR-012 Watchdog authority (07-10-2026, amended)
The watchdog alerts and never changes data or config. Container restarts were originally in scope (through a restricted Docker socket proxy), but the owner chose to leave the socket proxy and any Cloudflare configuration out of this repo and to handle them externally later. So: no `docker-socket-proxy` and no `cloudflared` service in the compose file; the watchdog runs alert-only (HTTP health and database checks) unless `DOCKER_HOST_URL` is set to an endpoint provided outside the repo. Crashes are still restarted by Docker's `restart: unless-stopped`; a container that is running but unhealthy is not restarted automatically until that external piece exists.

## ADR-013 Hosting (07-10-2026)
Develop on Windows with Docker Desktop; deploy to an always-on Linux VM. Compose uses named volumes and no host paths except read-only `config/`.

## ADR-014 Collector size budget: 900, then 1300, now 1400 lines (07-10-2026)
The original budget (about 900 physical lines) was set before the required behaviour was specified in detail (retries and circuit breaker, SIGHUP reload with validation, near-miss tracking, partition maintenance, health endpoint, idempotent single-transaction ingest). The implementation is 1243 physical lines (comments and blanks included) with no module over 300, against v1's 12.8k lines for `app/`. The budget stays a hard gate (CI) so growth is a conscious decision. Raised again to 1400 after the review fixes (database timeouts, poison-record handling, reload alerting, flight-summary refresh loop) took the package to 1325 lines; each addition was a required behaviour, not feature creep.

## ADR-015 Integration tests against a real Postgres found and fixed defects (07-10-2026)
First execution of the SQL and the integration tests (scratch Postgres 16.4): 31 of 95 failed. Fixed: `flight` view used `trim()` (spaces only) so tabs in airport codes slipped past the gate (now `btrim` with an explicit set); `store.py` assumed tuple rows (now explicit `tuple_row`); three test defects (read-only role test now proves the privilege layer as well as the read-only default; partial `network_snapshot` insert; poisoned pilot was outside the polygon). Result: 288 passed, 1 skipped.

## ADR-016 Flights are an incrementally maintained table, not a live view (07-10-2026)
The first design computed `flight` live with window functions over every position row. A reviewer measured 14 s at 8.9M rows (the 30 s dashboard limit is exceeded at the top of the expected size), and `monday_attendance` never finished. Now: `flight_summary` (keyed session, gap segment, span part) is recomputed every 5 minutes only for sessions seen in the last 12 hours (`refresh_flight_summary`, about 0.1 s) and finalised before a raw-position partition is dropped, so flights also survive the 90-day retention. `flight` is a thin view over it with the same columns. Measured on 10.5M synthetic rows: 12.5 s to 11 ms; `monday_attendance` 153 s to 61 ms; results identical (`flight_old EXCEPT flight` empty). Consequences: flight charts can lag live data by up to 5 minutes; changing `AIRBORNE_GAP_CAP_SECONDS` applies only to flights recomputed afterwards.

## ADR-017 Least-privilege database roles (07-10-2026)
The collector connects as `collector` (DML on its tables, EXECUTE on the maintenance functions, no DDL; the partition and refresh functions are SECURITY DEFINER), backups as `backup_ro` (`pg_read_all_data`), Grafana as `grafana_ro` (views and lookups, connection limit 10), the watchdog as `watchdog_ro`. The owner role is used by `migrate` and by restore only. Trade-off: the backup container still holds the owner password for the restore scripts; a dedicated restore role would be tighter.

## OPEN decisions for the owner
- `movements` (airport movements charts) counts every flight-gate session, including people who connect and never move. Changing it to count only flights that became airborne would depart from "same as v1" (decision 11).
- Names and CIDs are kept forever (ADR-009); no retention or anonymisation rule exists for old sessions or backups.
