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

## ADR-018 Airport movements stay the same as v1 (07-10-2026)
Owner decision: the movements charts keep v1's definition. Every flight-gate session counts as one departure at its departure airport and one arrival at its arrival airport, whether or not the aircraft ever became airborne. Evidence at the time: in v1's last full month (12-07 to 12-08-2026) 12% of home-region departures had zero airborne time and 6% were circuits, so the charts include parked connections. The `movements` view carries a SQL comment saying so. A later change (an airborne-only panel or definition) needs a new decision and an ADR.

## ADR-019 Names and CIDs are retained indefinitely (07-10-2026)
Owner decision, confirming ADR-009: no retention or anonymisation rule. Names and CIDs stay in the database and in backups for as long as the data is kept. The option of anonymising sessions older than 12 months (stable hash, so unique-person counts still work) was offered and declined. Consequence: backups hold real names and CIDs and must stay private; before any public exposure (Cloudflare), check the VATSIM data-use policy and put the dashboards behind login (see ADR-009). A later change needs a new ADR and a new numbered migration.

## ADR-020 Chart rebuild decisions of 08-10-2026 (owner, in chat)
Recorded here because SPEC.md says decisions belong in this log. Source: the owner's statements during the 08-10-2026 session, as written down in docs/RESUME.md section 2.
- Cards 94 and 95 (when people are online) are rebuilt to match v1 counting; card 94 uses Monday to Friday (UTC), where v1 used Sunday, Monday and Saturday as a data workaround.
- Cards 90 and 135 (most popular aircraft) keep the `aircraft_type` lookup categories, not v1's `LIKE` ladder.
- The retention panels copy v1's rules exactly, including the quirk that "reactivated" needs no earlier session, so every controller counts as reactivated until 60 days of history exist.
- Accepted risk (owner, 07-10-2026): the Telegram bot token appeared once in a session transcript and was not rotated.
Consequence: the corresponding rows in CHART_DEFINITIONS.md describe these rules. A later change needs a new ADR.

## ADR-021 Session identity uses whole-second logon_time (08-10-2026, owner decision)
Problem (T36, GitHub issue 15): the feed sometimes sends `logon_time` with fractional seconds and sometimes without. After a feed restart at about 04:08 UTC on 08-10-2026 every online connection lost its fraction, and because a session is identified by `(cid, callsign, logon_time)` with no tolerance, 31 pilot and 3 controller sessions were stored twice (flights about 7 to 8 percent too high).
Decision: `logon_time` is truncated (floor, not round) to whole seconds when the feed is parsed, for pilots and controllers, and the fraction is dropped on purpose. One callsign cannot log on twice in the same second, so nothing real is lost. Rounding was rejected because it would move a logon into the next second for some fractions and could still differ between two sightings that straddle .5. Truncation is stable: every form of the same second maps to the same value.
Rejected alternatives: matching with a tolerance (for example within 2 s) in the upsert, which needs a range lookup on every row of every poll, can chain, and leaves the table able to hold near-duplicates; storing the fraction as an attribute but keying on whole seconds (extra column nobody uses); merging twins only at report time in the views (every pilot view would need it, and the stored data would stay wrong).
Enforcement: migration 20261008000100 merges existing twins (survivor = lowest id, children re-pointed, flight_summary rebuilt), truncates every stored `logon_time` and adds CHECK constraints `pilot_session_logon_whole_second` and `controller_session_logon_whole_second`, so a regression fails loudly instead of duplicating sessions quietly. The fraction in already stored rows cannot be restored (the migration's down half only drops the constraints). A permanent check is `ops/check_identity_flap.sql` (T16).

## OPEN decisions for the owner
- The backup container holds the owner password for the restore scripts (ADR-017); a dedicated restore-only role would be tighter.
- Backfill of v1 history (ADR-011 says no backfill and stands until changed): the owner has not decided whether to load v1 sessions so the 30 to 90 day charts are meaningful immediately (docs/TRACKER.md, T22).
- Data retention for every data set other than names and CIDs (docs/TRACKER.md, T12, low priority).
- The chart questions listed in docs/RESUME.md section 4 (cards 66, 131, 134, 65, 41/43/98, 61, 93, 55 and others); they are definitions of individual charts and are answered there and in the tracker, not here.
