# Design for issues #89 and #108: stop the `flight` view growing with history

Revised 11-10-2026 (first version the same day). DESIGN ONLY: no code, no migration, nothing run on the live VM or any database. Cost figures are the measurements already in `docs/FLIGHT_VIEW_SCALING.md` (10-10-2026, synthetic data, capped 1 CPU container) and `docs/INDEX_TEST_89.md` (branch test-89-index, 11-10-2026, faster box); anything else below is labelled as reasoning.

Why this revision: the owner asked why a new table is needed when `flight_summary` already exists. The first version never costed extending `flight_summary`. It is costed here (option B), together with a third option found on the way (C).

## Recommendation (read this first)

**Option A still holds, in its smallest form: a separate derived table `flight_merged`, `flight` becomes a thin view over it, and `flight_summary` is not touched.** The reason is not line count. Option B (merged columns on `flight_summary`) saves only about a fifth of the SQL, because the hard parts are identical in both (chain computation, per-pilot recompute, tie-break, live fields). What B adds is a second row grain in the one table that gates the deletion of raw positions. A separate table can be dropped and rebuilt at any time from `flight_summary` without affecting retention; B cannot.

Smallest version: one migration (about 200 lines of SQL with the down step), one test file (about 150 lines), no Python (the collector is at 1790 of 1800 lines), no panel SQL change. Do two cheap local checks first (section 6).

## 1. What is slow and why (facts)

- `flight_summary` (ADR-016) has one row per part, key `(session_id, gap_segment, span_part)`. It is maintained incrementally every 5 minutes by `refresh_flight_summary` for sessions seen in the last 12 hours, marked `final` and finalised before a raw-position partition drops, so flights survive retention. Its refresh cost does not grow with history: ADR-016 exists because the earlier live view was too slow.
- `flight` (migration 110, last redefined by 130) was added later (ADR-023, reconnect merge) on top of `flight_part`. It runs two window passes and about twenty ordered `array_agg` calls over every part, with a sort on four text keys. A time filter cannot be pushed under the running-sum chain id. ADR-023 itself said "revisit if the table passes a few hundred thousand rows": that is the #89 trigger.
- Measured (FLIGHT_VIEW_SCALING): 1 year, 185,018 merged flights, 1.5 to 2.2 s per panel, worst 7 to 12 s. 5 years, 978,357 flights, 8 to 13 s per panel, four panels cancelled at the 30 s timeout. Linear, about 2 s per panel per year on the 1 CPU container; first timeouts around year 3. Live data is about 1,100 sessions, so nothing shows yet.
- Plain indexes were tested and ruled out (INDEX_TEST_89): the planner ignored them, forcing them gave about 20 percent, growth stayed linear (about 5 to 6 s a year on the worst panel on the faster box, 29.7 s at 5 years), and the indexes cost 562 MB. A stored table gave 0.23 to 0.30 s at 5 years.

## 2. Options compared

| | A. New table `flight_merged` | B. Extend `flight_summary` | C. Cheaper merge in the view, nothing stored |
|---|---|---|---|
| Idea | Derived table, one row per merged flight, kept current per touched pilot inside the 5-minute refresh; `flight` = thin view over it plus live joins | Add `is_head`, a pointer to the head part and about ten `merged_*` columns (start, end, airborne, samples, maxima, last session, session id array) to `flight_summary`; chain head rows hold the totals; `flight` reads head rows only | Rewrite `flight` to avoid the window passes (for example a stored narrow chain key, or a fast path for single-session groups) |
| SQL lines (est.) | About 200 (incl. down) | About 160 to 180, plus larger edits to the existing refresh function | About 60, if it worked |
| Test lines (est.) | About 150 | About 200: every existing flight_summary, partition-drop and integrity test re-run against two grains | About 80 |
| Python change | None | None | None |
| Migration risk | Medium: replaces `flight` (35 panels, 3 views) and adds a write path | Medium to high: same `flight` replacement, plus changes to the table that the partition-drop safety checks read | Low to medium |
| Rollback | Down step restores the `flight` body from `flight_chain` and drops the table; `flight_summary` untouched, so nothing can be lost | Down step must restore the view and drop columns on the retention-critical table; any head-row damage is in the source of truth | Trivial (view only) |
| Effect on the 35 panels | None: columns, order and types of `flight` unchanged | None | None |
| Cost at 5 years | Measured floor of a plain stored table: 0.23 to 0.30 s (faster box), 0.4 to 1.6 s (1 CPU container), against over 30 s now. NOT measured: the join to `pilot_session` for the live plan fields | Reasoned: same read cost as A with a partial index on head rows (about 9 in 10 flights are single-part, so head rows are nearly all rows); the table is wider for every other reader (`flight_part`, `monday_attendance`) | Not removed. Evidence against: the 10-10-2026 fast-path prototype was slower (3 to 27 s against 1.5 to 12 s) and not identical on ties; indexes did not help; the sort on text keys and the ordered aggregates remain |
| Removes growth with history? | Merge cost yes; all-time panels still scan the table (about 1 s at 5 years, linear but cheap); time-filtered panels can use an index on `end_time` | Same as A | No |
| Backup and restore | New table in the nightly `pg_dump -Fc`, about 267 MB at 5 years; restore test gets one `EXCEPT ALL` check | No new object; `flight_summary` grows by the new columns | None |
| Retention (ADR-026) | Kept forever, like sessions and summaries; independent of position drops | Same rows, no extra rule | None |

### Why B is not the smaller option

1. **The hard part is the same.** Both need the chain computation (the existing SQL), recompute for the pilots touched in the refresh window, a defined tie-break (the "latest part" pick is arbitrary on equal `end_time`; 3 of 185,018 rows differed in the prototype) and the live joins to `pilot_session` and `online_now`. B saves one `CREATE TABLE`, its indexes and a grants line, nothing else.
2. **Two grains in one table.** `flight_summary` is documented, tested and read as one row per `(session, gap_segment, span_part)` (DATA_DICTIONARY 1.4, ADR-016, `flight_part`, `monday_attendance`, `ops/audit/invariants.sql`, the watchdog integrity checks). B gives the same rows a second meaning: the head row also carries the flight totals while its part columns must stay part-grain for `flight_part`. That is about ten columns meaningful on head rows only, and a pointer from non-head rows. Postgres allows it, but every reader must know which grain it is reading.
3. **Reconnect chains that change later.** Normally a chain only grows at the tail, so the head is stable. The head changes when a plan edit moves a part between groups, or a data-fix migration rewrites sessions (precedent: migration 100, ADR-021). The existing upsert leaves unlisted columns alone (good), but the refresh's DELETE of vanished segments would delete a head row and silently drop the totals of the whole chain. Every such path needs new code in B; in A the same events are handled by "delete this pilot's rows and insert them again".
4. **Rows already final.** `refresh_flight_summary_sessions` skips final sessions and returns early when all are final, so merged totals on a final head row would never be refreshed by it, even if a sibling part is still live. B needs a second pass over final heads. In A the recompute reads `flight_summary` whatever its `final` flag says, and the new table has no final concept.
5. **Finalisation and retention (the decisive point).** `drop_old_pilot_position_partitions` finalises sessions in `flight_summary`, re-checks `final` and `max(end_time)` there, and only then drops raw positions, which cannot be recovered (ADR-026: dumps cover 14 days). A bug in new head-row maintenance would sit in the table that authorises that irreversible step. In A the worst case is a wrong or stale `flight_merged`, repaired by a rebuild.
6. **Churn.** B rewrites a wider existing row every 5 minutes for live pilots (new row versions in `flight_summary` and its two indexes, autovacuum on the retention-critical table). A churns only the small derived table.

If the owner nevertheless prefers B, it is feasible and not much larger. It should then get the Opus second pass in section 6 on the refresh function and the partition-drop interaction.

### Option C in detail (nothing found that works)

- A stored narrow chain key per part (a "B-lite": only the chain id stored) would remove the two window passes and the text sort, leaving a hash group by an integer plus the ordered aggregates. That is reasoning only, not measured. Likely a constant-factor gain, still linear in history, still about a million rows grouped and sorted per panel, and it keeps A's maintenance problem (the chain id must be kept current per pilot). Not supported by evidence; not recommended. If the owner wants it checked, it is one local measurement.
- Indexes and the single-session fast path are already ruled out by measurement (INDEX_TEST_89, FLIGHT_VIEW_SCALING candidate 1). A time-bounded pre-filter is impossible (the chain id is a running sum, and all-time panels would change).
- A materialised view refreshed by the existing `refresh_materialized_views()` is about 40 lines and makes panels fast, but each refresh rebuilds everything (about 10 s at 5 years, growing, on 1 CPU), so growth moves from the panels to the collector job. Acceptable only as a stop-gap, refreshed hourly, if a panel slows before A is built.

## 3. The recommended design (A, smallest version)

1. Rename today's `flight` body to a view `flight_chain` (same SQL, same ordering, so equivalence with today is by construction). A `cid` predicate should push below the window functions because `cid` is a PARTITION BY column; confirm with EXPLAIN first.
2. New table `flight_merged`: one row per merged flight, key `(session_id, gap_segment, span_part)` of the first part, plus `cid`, `session_ids bigint[]`, `last_session_id` and the numeric aggregates. Indexes: primary key, `(cid)`, `(end_time)`.
3. Hook at the end of `refresh_flight_summary_sessions` (already serialised by its advisory lock, and called by both the 5-minute path and the partition finalisation): for the CIDs of the sessions it just processed, delete their `flight_merged` rows and insert them again from `flight_chain WHERE cid = ANY(cids)`, in the same transaction. Per CID, not per group, so a changed plan needs no bookkeeping. The table is written only by the DEFINER functions (only the collector role triggers them; it gets no table rights).
4. `flight` = `flight_merged` joined to `pilot_session` on `last_session_id` for the plan and pilot fields (live, as today), `touches_home_region` computed from stored departure and arrival and the live `home_icao_prefixes` setting, and `is_in_progress` as `session_ids` overlapping the pilot sessions in `online_now`. Same 32 columns, order and types, so `flight_ext`, `movements`, `cohorts` and `monday_attendance` are untouched. `grafana_ro` still reads views only (a view runs with its owner's rights, so no grant on the new table).
5. Tie-break, for the owner to decide: `end_time DESC, session_id DESC, gap_segment DESC, span_part DESC`. A tiny visible change under the "identical rows" rule.
6. Backfill: one `INSERT ... SELECT` from `flight_chain` in the migration; live data is about 1,100 sessions, so seconds. A rebuild function over all CIDs ships with it; any later data-fix migration must call it.

Size against pain: about 350 lines in total, to take every flight panel from 8 to 13 s (or cancelled) at 5 years to well under 2 s.

## 4. Migration, rollback, backup, retention

- **Deploy**: manual and rare, so one migration, tried first on a restore of a live dump, pre-deploy dump taken, then the normal deploy script. Medium tier in the working agreement (independent review, behaviour-preserved section).
- **Rollback**: the down step restores the `flight` body from `flight_chain` and drops `flight_merged`. A view cannot lose columns, so the column list stays identical (as with migrations 130 and 140). Nothing in `flight_summary` or `pilot_session` changes, so rollback loses no data.
- **Backup and restore**: an ordinary table in the nightly dump (about 267 MB at 5 years, against about 0.65 GB a year in total, ADR-026). Add to the restore test an `EXCEPT ALL` of `flight_merged` against `flight_chain`.
- **Retention (ADR-026, #21)**: kept forever; position drops never touch it. A future purge of `pilot_session` must delete by CID and rebuild (no foreign key is possible on the array column).
- **Documents tied to tests**: `docs/DATA_DICTIONARY.md` must gain `flight_merged`, `flight_chain` and the changed function text (its test fails on a table or view missing from it, and the header must name the latest migration). `docs/CHART_DEFINITIONS.md` and `alerts.md` need no change (no panel or alert changes). Add a short ADR (flight_merged is derived; `flight_summary` stays the source) and a line in ADR-026.

## 5. Tests needed

1. Permanent equivalence: `flight_chain` against the new `flight`, `EXCEPT ALL` both ways, also on `flight_ext`, `movements`, `cohorts`; column names and types compared; run on `tests/scale/gen_synth.sql` at 1 and 5 years once, and on a small fixture in CI.
2. Incremental correctness: reconnect within 120 minutes, at exactly 120, after 120; changed plan; a session ending; refresh run twice (idempotent); the stored table equals a full rebuild each time.
3. Live fields: change a plan and the `home_icao_prefixes` setting, `flight` follows with no refresh; `is_in_progress` tracks `online_now`.
4. Finalisation: run the partition-drop path and confirm `flight_merged` and `flight` are unchanged after raw positions go.
5. Refresh cost does not grow: the touched-pilot recompute takes similar time on 1-year and 5-year data.
6. Migration up and down with data; `test_every_panel_query_runs_as_grafana_ro` unchanged; ten slowest panels under 3 s at 5 years; a panel read during a refresh never sees a missing flight.

## 6. Database-risky parts (for the owner's decision on an Opus second pass)

| Part | Why risky | Suggested pass |
|---|---|---|
| Redefining `flight` (base of about 35 panels, three views, Monday attendance) | Any column, type or row change shows on every dashboard | Opus |
| Delete-and-insert hook inside `refresh_flight_summary_sessions` (lock, transaction, per-CID scope; a missed CID leaves a stale flight) | Silent staleness | Opus |
| Tie-break rule | Visible behaviour change | Owner decision |
| Cost of the join to `pilot_session`, and whether the `cid` predicate pushes below the windows | Unmeasured; either could shrink the gain | Sonnet: two local checks before building |
| Backfill, indexes, restore check, documents, ADR | Routine | Sonnet |

Only if B were chosen: the edits to the refresh function's upsert and delete, and the finalisation re-checks, become Opus-grade as well.
