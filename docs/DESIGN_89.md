# Design for issue #89 / #108: remove the growth of the `flight` view with history

Written 11-10-2026 by the data engineer. DESIGN ONLY: no code, no migration, nothing run on the live VM. Builds on `docs/FLIGHT_VIEW_SCALING.md` (10-10-2026 measurements); no new measurement was made for this document.

## Recommendation (read this first)

Option A below: a stored table `flight_merged`, kept up to date inside the existing 5-minute `refresh_flight_summary` for the pilots touched in the refresh window, with `flight` turned into a thin view over it. One new migration of about 200 lines of SQL (including the down step), one integration test file of about 150 lines, no Python change (the collector is at 1790 of 1800 lines, ADR-014, so this matters). It removes the sort and the twenty ordered aggregates over all of history from every panel read. It is database-risky (it changes the definition behind about 35 panels and adds a write path), so an Opus second pass on the migration and the equivalence test is advisable; the rest is routine.

## 1. Cost of the current view as history grows

`flight` runs two window functions and about twenty ordered `array_agg` calls over every row of `flight_part` (sort on cid, callsign, departure, arrival, start_time). A time filter cannot be pushed under the running-sum chain id, so every panel pays for all of history. History is kept forever (ADR-026 item 2).

Measured on 10-10-2026 (synthetic data, capped 1 CPU container, warm, one query at a time; from FLIGHT_VIEW_SCALING.md):

| History | Merged flights | Typical panel | Worst panels |
|---|---|---|---|
| 1 year | 185,018 | 1.5 to 2.2 s | 7 to 12 s (movements: view read twice) |
| 5 years | 978,357 | 8 to 13 s | 4 panels cancelled at the 30 s timeout |

Cost is linear, about 2 s a year per panel; a dashboard opens about ten panels at once, so real latency is a multiple. First timeouts at roughly 3 years. The live database holds about 1,100 sessions today, so nothing is visible yet and there is time to do this properly. These figures are estimates from synthetic data, not live measurements.

## 2. Options, smallest first

| Option | Size | Removes growth? | Verdict |
|---|---|---|---|
| Bounded window (only recent history) | Small | Only for panels with a window; all-time panels (monthly totals, cohorts, Monday attendance, per-pilot hours) would be wrong or unchanged. The chain id cannot be windowed without changing chains across the edge. | Rejected. |
| Materialised view of `flight`, refreshed by the existing `refresh_materialized_views()` | About 40 lines | No: each refresh is a full rebuild (about 10 s at 5 years, growing, on 1 CPU). Panels become fast but the load grows with history. `is_in_progress` needs a join to `online_now` on top. | Acceptable stop-gap only, refreshed hourly. |
| **A. Stored `flight_merged`, maintained per touched pilot (recommended)** | About 200 SQL lines + 150 test lines, 0 Python | Yes for the merge cost: maintenance depends on the pilots active in the window, not on history. Panel reads become a scan or index range of a plain table (floor measured 0.4 to 1.6 s at 5 years, against over 30 s). | Recommended. |
| B. Incremental maintenance by the collector job instead of SQL | Python change, breaks the size budget | Same as A | Rejected: A already runs from the same job through SQL. |

Honest limit of A: all-time panels still read every merged row, so they grow linearly, but with a cheap constant (a table scan, about 1 s at 5 years) instead of a sort plus aggregates. Panels with a time filter can use an index on `end_time` or `start_time` and stop growing at all.

### How A works

1. Rename the current body of `flight` to a view `flight_chain` (same SQL, same ordering, so equivalence is by construction). A `cid` predicate should push below the window functions because `cid` is a PARTITION BY column, so `SELECT ... FROM flight_chain WHERE cid = ANY(...)` costs only the history of those pilots. This must be confirmed with EXPLAIN on the test database before build.
2. New table `flight_merged`: one row per merged flight, key (session_id, gap_segment, span_part) of the first part, plus `cid`, group key, `session_ids bigint[]` (every session in the flight), `last_session_id` (the part chosen as "latest") and the numeric aggregates. Indexes: primary key, (cid), (end_time), (start_time).
3. At the end of `refresh_flight_summary_sessions` (already serialised by its advisory lock): take the CIDs of the sessions just refreshed, DELETE their rows from `flight_merged`, INSERT from `flight_chain WHERE cid = ANY(cids)`, in the same transaction. Recomputing per CID (not per group) handles a changed plan moving a part between groups with no bookkeeping.
4. `flight` becomes `flight_merged` joined to `pilot_session` on `last_session_id` for the live plan and pilot fields, `touches_home_region` computed live from stored departure and arrival and the `home_icao_prefixes` setting, and `is_in_progress` as `session_ids` overlapping the pilot sessions in `online_now`. Plan fields, the home-region setting and in-progress therefore stay live exactly as today. Columns, order and types are unchanged, so `flight_ext`, `movements`, `cohorts` and `monday_attendance` need no change.
5. Tie-break: today, when two parts end at the same instant, the pick is arbitrary (3 of 185,018 rows differed in the prototype). Define it as end_time DESC, then session_id DESC, gap_segment DESC, span_part DESC. This is a visible but tiny behaviour change and needs the owner's yes before build (the "identical rows" rule).

Size versus pain: about 350 lines in total against panels going from 8 to 13 s (or timing out) to roughly 0.4 to 1.6 s at 5 years. The primary-key join to `pilot_session` was NOT in the floor measurement; measure it first. If it costs too much, fall back to storing the plan fields in the table and adding a rebuild hook for data-fix migrations (more staleness risk, cheaper read).

## 3. Migration, rollback, backfill, backup and retention

- **Migration** (one forward-only file): create `flight_chain` from the current `flight` body; create `flight_merged` and indexes; replace the refresh function; backfill with one INSERT from `flight_chain` (live data is about 1,100 sessions, seconds); replace `flight` by `CREATE OR REPLACE VIEW` with identical columns. Deploy through the normal deploy script with a pre-deploy dump taken first.
- **Rollback**: the down step restores the `flight` body from `flight_chain` and drops `flight_merged`. A view cannot lose columns, so the columns stay identical (as with migrations 130 and 140). Past that, restore the pre-deploy dump (docs/RUNBOOKS/restore.md). While rolled back nothing is lost: `flight_summary` and `pilot_session` stay the source of truth, and `flight_merged` is derived and rebuildable at any time. Provide a rebuild function over all CIDs; any later data-fix migration (such as the logon-time one in migration 100) must call it.
- **Backup and restore**: `flight_merged` is an ordinary table in the nightly `pg_dump -Fc`, so it is dumped and restored with the rest. About 267 MB with two indexes at 5 years (floor test), small beside ADR-026's 0.65 GB a year in total. Add an `EXCEPT ALL` check of `flight_merged` against `flight_chain` to the restore test (`ops/backup/restore-test.sh`), so a stale or half-restored table is caught.
- **Retention (#21, ADR-026)**: sessions and summaries are kept forever, so `flight_merged` is kept forever; raw position drops do not touch it (summaries are final before a partition drops). If a purge of `pilot_session` is ever built it must also delete the matching `flight_merged` rows (no foreign key is possible on the array column; delete by CID and rebuild). Add a line to ADR-026 and the data dictionary, and an integrity check (#20) that samples recent CIDs for drift.

## 4. Tests needed

1. Permanent equivalence test: `flight_chain` against the new `flight`, `EXCEPT ALL` in both directions, on `flight`, `flight_ext`, `movements` and `cohorts`; column names and types compared. Run once on the generator (`tests/scale/gen_synth.sql`) at 1 and 5 years, and on a small fixture in CI. Ties covered by the defined tie-break.
2. Incremental correctness: after a reconnect within 120 minutes, after one at exactly 120 and after 120, a changed plan, a session ending, and a second refresh with no change (idempotent), the stored table equals a full rebuild.
3. Live fields: change a plan on `pilot_session` and the `home_icao_prefixes` setting; `flight` reflects it with no refresh. `is_in_progress` tracks `online_now`.
4. Refresh cost does not grow: the touched-pilot recompute takes similar time on a 1-year and a 5-year dataset (EXPLAIN shows the `cid` predicate below the window).
5. Migration up and down on a database with data (as `test_flight_in_progress.py` does); `test_every_panel_query_runs_as_grafana_ro` unchanged; the ten slowest panels under 3 s at 5 years.
6. Concurrency: a panel read during a refresh sees either the old or the new rows, never a missing flight (single transaction).

## 5. Database-risky parts (for the owner's decision on an Opus second pass)

| Part | Risk | Pass |
|---|---|---|
| Rewriting `flight` (base of about 35 panels, three views and Monday attendance) | Any column, type or row change shows on every dashboard | Opus recommended |
| Delete-and-insert maintenance inside `refresh_flight_summary_sessions` (lock, transaction, per-CID scope; a missed CID leaves a stale flight) | Silent staleness | Opus recommended |
| Tie-break rule (behaviour change) | Needs the owner's yes | Owner decision |
| Cost of the primary-key join to `pilot_session`, not yet measured | Smaller gain than the floor figures | Sonnet, measure first |
| Backfill, indexes, restore check, docs (ADR-026 line, data dictionary) | Routine | Sonnet |

Alternatives for the owner: do nothing now (first timeouts about 3 years out) and use the materialised-view stop-gap if a panel slows; or ask for the join measurement before committing to A.
