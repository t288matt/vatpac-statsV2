# Issue #89: every design option, consolidated (PARKED)

> **Note, issue 89 phase 5 (13-10-2026, branch p5-remove-old, NOT YET DEPLOYED).** This is a dated record. It uses the names as they were when it was written: `flight_summary` meant the old pieces table, `flight_record` the table of finished flights. Migration 20261013000300 drops the pieces table, `flight_part`, `flight_legacy`, `refresh_flight_summary` and `refresh_flight_summary_sessions`, and renames `flight_record` to `flight_summary`. Current description: [DATA_DICTIONARY.md](DATA_DICTIONARY.md) and ADR-030.

Written 12-10-2026 by the technical writer. This is a summary of four dated working papers (kept unchanged on the branches `design-89`, `opus-89` and `test-89-index`, not on main). Nothing here was built or run; no code, migration or panel was changed. All times are UTC. Dates are DD-MM-YYYY.

> **Do not delete the branches `design-89`, `opus-89` and `test-89-index`.** They hold the four working papers (about 128,000 characters, with dated sections whose earlier recommendations are superseded) and are deliberately not on `main`. Their tips are also tagged `parked-89-design`, `parked-89-opus` and `parked-89-index`. The issue #89 comments of 12-10-2026 repeat this summary.

## 1. Status, and what to read first

**Status: PARKED by the owner on 12-10-2026.** Issue #89 (the `flight` view recomputes the merge chain over all history on every query) stays open, labelled `deferred`, still P1. No work is scheduled.

**Why parked.** The problem is real but not urgent: the live database holds only a few thousand sessions, nothing is slow yet, and the first panel timeouts are about three years out at today's growth. The owner wants one clean, simple end state rather than a short-term fix, and chose to stop and get it right rather than patch. There is also a deadline that shapes any restart: the first deletion of raw positions on the live system is due about 06-01-2027, and one of the designs must be live before then (section 8).

**What to read first, in this order.**
1. This document (the whole picture, including how the advice changed).
2. `docs/DESIGN_89_FINAL.md` on branch `opus-89` (the build-ready design for option (c), four phases). This is the current recommendation.
3. Only if needed: `docs/OPUS_REVIEW_89.md` on branch `opus-89` (many dated sections; later sections supersede earlier ones), `docs/DESIGN_89.md` on branch `design-89` (the Sonnet designer's three papers), `docs/INDEX_TEST_89.md` on branch `test-89-index` (why indexes do not work) and `docs/FLIGHT_VIEW_SCALING.md` (the first measurements).

**Related issues.** #108 (FUTURE: stored merged flights `flight_merged`, the follow-up that was filed first; superseded in detail by this document), #21 (data retention rules for every data set; any option interacts with it, see section 8).

**Decision context (the owner's guidance of 11-10-2026 and 12-10-2026).** Every recommendation below was judged against these points:
1. Not looking for short-term fixes. Prepared to stop and get it right and simple: one clean end state rather than changing it later.
2. Simplicity of code and clarity of logic matter most.
3. A delay of 3 hours or more before a finished flight reaches the panels is fine (v1 waited 8 hours after the end).
4. Losing existing data and restarting collection is fine.
5. Working, deployed, tested code is a big bonus.
6. No raw positions have been dropped on the live system yet (the first drop is due about 06-01-2027), and the existing finalisation gate has never run live.
7. Panels never show flights that are still connected (issue #38 decision).
8. A pilot can only be on one flight at a time.
9. Pilot, session and flight must never be conflated: a pilot (CID) has many flights; a flight has one or more sessions.
10. Table names must be intuitive: a table called `flight_summary` should be the final record of the merged flight.
11. v1 wrote one summary only after the flight had finished and never touched it again.

The owner also asked three questions that are answered below: would indexes help (no, measured, section 4.1); could `flight_summary` be extended (rejected, section 4.2); does v1's write-once approach fit (yes, it is options (b) and (c)).

## 2. The problem in plain English, with the evidence

**What happens.** A "flight" is what the charts count. The database does not store it. It assembles every flight, from all of history, each time a chart opens. It takes every stored flight piece, sorts them all by pilot, callsign, departure, arrival and start time, walks them in order to decide which pieces belong together (the 120-minute reconnect rule), and glues each group together with about twenty ordered aggregate calls. A chart asking for "the last 30 days" cannot shortcut this, because the time filter is applied to the glued result, so the database glues all of history and throws almost all of it away. History is kept for ever (ADR-026), so every chart gets slower every day. About 35 panels, three views (`flight_ext`, `movements`, `cohorts`) and Monday attendance sit on top of it. ADR-023 itself said to revisit the view if the table passed a few hundred thousand rows; that is the #89 trigger.

The root cause is not a missing index. It is that the merge result is recomputed on read, over all history, on every query, although for any flight older than a few hours it can never change again.

**Measured evidence.** Labels: M = measured on synthetic data (generator `tests/scale/gen_synth.sql`), R = reasoned only.

| Fact | Result | Label and source |
|---|---|---|
| 1 year of history (185,018 merged flights) | nearly every panel 1.5 to 2.2 s; worst 7 to 12 s | M, FLIGHT_VIEW_SCALING, capped 1 CPU / 1 GB container, PostgreSQL 16.15 |
| 5 years (978,357 merged flights) | 8 to 13 s per panel; four panels cancelled by the 30 s timeout | M, same |
| Growth | linear, about 2 s per panel per year on the container; first timeouts around year 3 | M |
| Live database today | about 1,100 sessions (Sonnet, 11-10-2026); Opus's final design says about 4,000 sessions by deploy time. Either way nothing is visible yet | M, but the two figures differ and were taken at different moments |
| Same, on the synthetic PC database (about 2 times faster than the container) | count of `flight`: 4.6 s; busiest home-region airports 24.5 s; domestic routes 15.3 s | M, Opus, PostgreSQL 17.5, private `initdb` cluster |
| Hidden second cost | 15.5 of 19.2 s of the domestic-routes panel is the `icao_is_home()` function evaluated on all 1M sessions, because the planner pushes it below the merge | M, Opus |
| Indexes | planner ignored them; forcing them gave about 20 percent; growth still linear (about 5 to 6 s a year on the worst panel); 562 MB of indexes at 5 years; busiest airports 29.7 s at 5 years unindexed, 30.5 s with indexes, 24.5 s forced | M, INDEX_TEST_89 (PC database) |
| Stored table, 5 years | 0.23 to 0.30 s (PC), 0.4 to 1.6 s (container), against over 30 s now | M, INDEX_TEST_89 and FLIGHT_VIEW_SCALING |
| Per-pilot recompute of the old logic | 130 ms for the 477 pilots seen in 12 hours (9,318 flights) because the pilot filter pushes down through all window passes; heaviest pilot (2,226 flights) 29 ms | M, Opus (PostgreSQL 17.5) |
| Stored table build | 7.0 s and 273 MB with five indexes at 5 years | M, Opus |
| Stored table vs old view, all 32 columns | 978,342 of 978,357 flights identical; 15 differ, all exact ties where two pieces end at the same instant and the old pick is arbitrary | M, Opus (Sonnet's prototype found 3 of 185,018 at 1 year) |
| Narrow stored chain key, panels | 2.7 s at 5 years (16.9 s with the home-airport test), still linear | M, Opus |
| Production database version | PostgreSQL 16; Opus tested 17.5. The 1 CPU VM is slower again | known gap, section 10 |

**A note on the two machines.** The container figures (FLIGHT_VIEW_SCALING) are about twice as slow as the PC figures (INDEX_TEST_89, Opus). Compare columns within one document, not across. The PC database is the synthetic one; no live dump has been used in any test.

## 3. Words used

- **Pilot**: one person, identified by CID. A pilot has many flights over time, and is on only one flight at a time.
- **Session**: one connection to VATSIM, identified by (CID, callsign, logon time). A disconnect and reconnect makes a new session.
- **Flight**: what the charts count. One or more sessions of the same pilot, callsign, departure and arrival, joined when the gap between them is 120 minutes or less. A flight has one or more sessions.
- **Piece** (the `flight_summary` row today): one stretch of a session. A new piece starts after 120 minutes of silence, and every 8 hours. Key `(session_id, gap_segment, span_part)`. Today's `flight_summary` is therefore one row per piece, not per flight; this is the naming problem in owner guidance point 10.
- **Quiet period**: how long after a pilot was last seen before their flight is considered finished and written. Must exceed the 120-minute joining window; 3 hours is proposed (v1 used 8).

## 4. Every option considered

Each entry gives: what it is; how it works; who proposed it; cost at 5 years; lines of SQL and tests; proven code kept, rewritten or new; risks; rollback; what the owner would see; verdict.

### 4.1 Plain indexes

- **What / how.** Add btree indexes so the existing view can find rows faster: `flight_summary (cid, start_time, session_id, gap_segment, span_part)`, a covering `flight_summary` index, `pilot_session (cid, callsign, dep_n, arr_n, id)` on the view's own normalising expressions, and a covering `pilot_session (id)` index.
- **Proposed by.** The owner asked the question; tested on branch `test-89-index` (INDEX_TEST_89).
- **Cost at 5 years.** M: busiest airports 29.7 s without, 30.5 s with (planner free), 24.5 s forced; domestic routes 19.1 s, 18.9 s, 15.5 s. Growth about 5 s a year indexed against 6 s unindexed. Indexes 562 MB (more than the 267 MB stored table), plus write cost on the 5-minute refresh. On the slower VM, roughly double.
- **SQL / tests.** About 4 lines. No tests.
- **Code.** Everything kept; nothing rewritten.
- **Why it fails.** The planner ignored every index (the plan is a hash join of two full tables, a sort, three window passes and a hash aggregate, all linear). Forcing them removed none of the real cost: the sort key spans two tables, so no single btree can deliver the order; the window functions and ordered aggregates still read every row; the time filter cannot go below the running-sum chain id.
- **Risk / rollback.** Low risk; drop the indexes.
- **Owner would see.** No improvement.
- **Verdict: ruled out by measurement.**

### 4.2 Extend `flight_summary` with merged head rows (old option B)

- **What / how.** Add `is_head`, a pointer to the head piece and about ten `merged_*` columns (start, end, airborne, samples, maxima, last session, session id array) to `flight_summary`. The first piece of each chain carries the flight totals; `flight` reads head rows only.
- **Proposed by.** The owner asked why a new table is needed; Sonnet costed it as option B in the revision of 11-10-2026.
- **Cost at 5 years.** R: the same read cost as a stored table (a partial index on head rows; about 9 in 10 flights are single-piece). Not measured.
- **SQL / tests.** About 160 to 180 lines of SQL plus larger edits to the refresh function; about 200 test lines (every existing summary, partition-drop and integrity test re-run against two row meanings).
- **Code.** Existing table and refresh kept but modified; the retention-critical table changes.
- **Why it is rejected (Sonnet and Opus agree).**
  1. The hard parts (chain computation, per-pilot recompute, tie-break, live fields) are identical to a new table; B saves only one `CREATE TABLE`.
  2. Two row meanings in one table that is documented, tested and read as one row per piece (DATA_DICTIONARY, ADR-016, `flight_part`, `monday_attendance`, audit scripts, watchdog checks). Every reader must know which grain it reads.
  3. If a reconnect chain changes later (a plan edit moves a piece between groups; a data-fix migration like migration 100), the refresh's delete of vanished segments could delete a head row and silently lose the whole chain's totals.
  4. Rows already marked `final` are skipped by the refresh, so merged totals on a final head row would never be refreshed.
  5. Decisive: `flight_summary` is the table that authorises dropping raw positions, which cannot be undone (ADR-026: dumps cover 14 days). A bug in head-row maintenance would sit in that table.
  6. Rewrites a wider row every 5 minutes for live pilots, on the retention-critical table.
- **Rollback.** Hard: drop columns on the source-of-truth table; damage to head rows is in the source of truth.
- **Owner would see.** No panel change.
- **Verdict: rejected.** Feasible and not much larger, but it puts the risk in the wrong table.

### 4.3 Cheaper view, nothing stored (old option C)

- **What / how.** Rewrite `flight` to avoid the two window passes, for example a fast path for single-session groups.
- **Proposed by.** Sonnet (option C); the single-session fast path was prototyped on 10-10-2026 (FLIGHT_VIEW_SCALING candidate 1).
- **Cost.** M: the prototype was slower (3 to 27 s against 1.5 to 12 s at 1 year; about double the buffers and temp pages) and not identical on ties (3 rows of 185,018). The sort on four text keys remains.
- **SQL / tests.** About 60 lines of SQL and 80 test lines, if it worked.
- **Code.** Rewrites `flight` only.
- **Risk / rollback.** Low to medium; trivial rollback (view only).
- **Owner would see.** No improvement.
- **Verdict: ruled out by measurement.** Nothing of it is in the repository.

### 4.4 Narrow stored chain key ("B-lite")

- **What / how.** Store only a per-piece "flight id" (the chain id) and keep the grouping at query time, removing the window passes and the text sort.
- **Proposed by.** Sonnet (reasoning only); Opus measured it.
- **Cost at 5 years.** M (Opus): 2.7 s without the home-airport test, 16.9 s with it. Still linear, still grouping about a million rows per panel, still exposed to the `icao_is_home()` push-down. It also keeps the maintenance problem (the chain id must be kept current per pilot).
- **SQL / tests.** Not costed; comparable to a stored table without the benefit.
- **Verdict: rejected by measurement.** Does not fix #89.

### 4.5 Materialised view

- **What / how.** A materialised view of the merged flights, refreshed with the existing `refresh_materialized_views()`; `flight` reads it.
- **Proposed by.** Considered by FLIGHT_VIEW_SCALING (candidate 4), Sonnet (as a stop-gap) and Opus.
- **Cost.** M: a full rebuild is about 7 s at 5 years on the PC (about 10 s on the container) and grows. `REFRESH` without `CONCURRENTLY` blocks every reader; with it, it computes everything plus a diff, about a million rows 288 times a day. It cannot hold the live plan fields or the in-progress flag, so a thin view on top is needed anyway.
- **SQL / tests.** About 40 lines.
- **Frequency problem.** `refresh_materialized_views()` runs only in the daily maintenance; hourly would need a collector (Python) change, and the collector is at 1,790 of 1,800 lines (Opus). Refreshing only daily would make counts lag a day.
- **Verdict: rejected as a fix.** Only a stop-gap if a panel slows before the real fix exists; and the only no-code emergency lever is raising `grafana_ro`'s `statement_timeout`, which does not reduce latency.

### 4.6 Bounded window (time-bounded pre-filter or bounded recompute)

- **What / how.** Two variants: (i) filter the view to recent time before the chain computation; (ii) recompute only the pieces after a "safe cut point" per pilot.
- **Verdict.** (i) is impossible: the chain id is a running sum, so a time window cannot be pushed under it without changing chains that cross the window edge, and all-time panels would change. (ii) rejected by Opus: correct cut points are subtle, because the rule only looks at the previous piece's session, so overlapping sessions (the ADR-021 twins) can make a "fresh start" depend on older pieces. A whole-pilot recompute costs 29 ms for the heaviest pilot, so the extra cleverness buys nothing. Opus's section 1.7 shows that only flights that ended within 120 minutes before a new session can change, so a narrower update would be a ten-line change saving at most 0.2 s every 5 minutes; kept as a later option only.

### 4.7 Collector-side incremental computation

- **What / how.** Have the Python collector compute and maintain merged flights as it ingests.
- **Why it is out.** The collector is at 1,790 of the 1,800-line budget, so it cannot take new logic. Every SQL-only option avoids this by hanging off the existing 5-minute SQL call. (Option (c) changes only the one SQL string the collector calls, with no net Python growth.)
- **Verdict: rejected on the line budget.**

### 4.8 Option (a): stored `flight_merged`, kept current by recompute every 5 minutes

- **What / how.** A derived table with one row per merged flight (key = first piece's `(session_id, gap_segment, span_part)`; also cid, callsign, departure, arrival, `session_ids`, `last_session_id`, the totals). Today's `flight` body is kept as a reference view (`flight_chain` in Sonnet's paper, `flight_merge_reference` in Opus's). At the end of `refresh_flight_summary_sessions` (so finalisation also updates it, in the same transaction, under the existing advisory lock), the flights of the pilots whose sessions were just refreshed are recomputed from the reference view. `flight` becomes a thin view: the table joined to `pilot_session` on `last_session_id` for the live plan and pilot fields, with `touches_home_region` computed live and `is_in_progress` live from `online_now` (wrapped in `COALESCE`, see below). Same 32 columns, so no panel changes.
- **Proposed by.** Sonnet (option A, 11-10-2026); Opus independently (Part 1, section 1.3); Opus recommended it again in section 1.10.
- **Cost at 5 years.** M: 0.2 to 0.4 s for the 30-day and 6-month panels, about 1.8 to 2 s for all-time monthly panels (PC); 7.0 s one-off backfill; 160 to 250 ms per 5-minute recompute. Table 273 MB with indexes (about 160 MB heap).
- **Lines.** About 200 to 220 SQL (including the down step), about 150 to 170 test lines, no Python.
- **Code.** All proven code kept (about 700 lines of SQL, about 25 test files). Rewritten: the `flight` body (moves to the reference view; `flight` becomes about 15 lines), one call added to the refresh. New and untested: about 90 lines of SQL plus about 170 of tests (Opus); about 200 lines new on Sonnet's count.
- **Opus's five changes to Sonnet's version.**
  1. Write only rows that changed (upsert with `IS DISTINCT FROM`, then delete vanished keys) instead of delete-and-reinsert, which rewrites about 9,300 flights every 5 minutes (about 2.7 million dead row versions a day). M: 160 to 250 ms, 0 rows written when idle.
  2. Add indexes on `start_time` and `logon_time` as well as `cid` and `end_time` (M: without them busiest airports 1.3 s instead of 0.34 s, RAAFv flights 905 ms instead of 11 ms).
  3. The test helper `tests/integration/helpers.py` (used by seven panel test files) inserts `flight_summary` rows directly and would see no flights; it must call the rebuild (one line). Add tests for a session gaining or losing the flight gate and for "a second refresh with nothing changed writes 0 rows".
  4. The migration must take the refresh advisory lock for the backfill and set a `lock_timeout` (for example 5 s) before `CREATE OR REPLACE VIEW flight`, which otherwise queues behind 30 s panel queries on public dashboards.
  5. `is_in_progress` must be wrapped in `COALESCE(..., false)`: `session_ids && (SELECT array_agg(...))` is NULL when `online_now` is empty (stale collector), and `NOT NULL` would hide every flight.
  (Opus also noted that "live, as today" is slightly overstated: departure and arrival are stored, so they lag a plan change by one refresh; that flight is in progress then, so no panel counts it.)
- **Risks.** Silent staleness if a pilot is missed; a bug in the hook rewrites many old flights every 5 minutes; changes the base of about 35 panels (Medium tier by the table, deploy like High); the tie-break on equal end times is a visible change (15 in a million). Session deletes need a rule: delete by CID and rebuild (no foreign key, a cascade would delete a whole flight).
- **Rollback.** A forward migration restoring the old `flight` body (columns unchanged) or the pre-deploy dump; the stored table can be dropped without touching `flight_summary` or `pilot_session`. (dbmate down steps are not run in deploys.)
- **Owner would see.** No change at all; flights appear within 1 to 6 minutes as now; panels fast.
- **Verdict.** Technically sound and the smallest change that works, but it keeps both layers for ever and adds a copy that must be kept in step. Sonnet ranked it last under the owner's final guidance; Opus ranked it first under "proven code is a big bonus" (section 1.10), then last again in the final design.

### 4.9 Option (b): write once when final, built from today's pieces

- **What / how.** The same stored table, but a flight is written once, by one INSERT from today's chain SQL, after the whole chain has been quiet for the quiet period (3 hours). Nothing is recomputed afterwards; there is no live join, no `online_now` test and no in-progress. `is_in_progress` stays as a constant `false` column so the 11 panels that filter on it and `flight_ext` stay untouched. Today's `flight_summary`, the refresh, finalisation and `flight_part` are left alone.
- **Proposed by.** Opus (section 1.6, first as an alternative needing the owner's decision); Sonnet (addendum, ranked first at that point: "write once when final, built from today's pieces").
- **Cost at 5 years.** Same stored-table reads as (a): 0.2 to 1.6 s. Lower write cost: about 700 inserts a day against re-checking about 9,300 flights every 5 minutes.
- **Lines.** About 110 to 120 net SQL (about 60 new untested lines on Sonnet's count), about 80 new test lines plus about 10 test files changed so fixtures are older than the quiet period (or the quiet period set to 0 via an `app_setting`).
- **Code.** All proven code kept, except `flight` becomes a thin view. Nothing deleted.
- **How the quiet test works.** It must be per chain, not per piece: an early piece can be silent for 3 hours while a later reconnect in the same chain is still connected. Safety: any quiet period above 120 minutes is safe, because a new session can only join a flight that ended within 120 minutes before it. The look-back should be days, not hours, so a collector outage cannot leave flights unwritten; a nightly invariant ("no piece older than the quiet period plus 1 hour that is outside a stored flight") is the backstop.
- **Risks.** Never-written flights (undercount), too-early writes (a reconnect counted twice), a later data fix not reaching rows already written; the `final` flag cannot be shared (it means "raw positions may be deleted", set 90 or 180 days later); tie-break; the first catch-up run should be bounded. Monday attendance stays on `flight_summary`, so it is unchanged.
- **Rollback.** Pre-deploy dump or a forward migration restoring the old view body.
- **Owner would see.** Flights reach the panels up to 3 hours after the end. Server panel for the last 24 hours misses the newest 3 hours; Monday event share appears about 15:00 instead of 12:05; 30-day panels miss under 0.5 percent.
- **Verdict.** Safe and the cheapest route to write-once, but it leaves two flight stores and two meanings of "final" for ever, and the table holding the name `flight_summary` stays pieces. Meets owner points 3, 5, 11 but not 2, 1 and 10. It is the fallback for (c) (section 8).

### 4.10 Option (c): v1-faithful, one finished-flight table written from raw positions (four phases)

- **What / how.** There is no pieces layer. A single SECURITY DEFINER function `write_finished_flights()` runs every 5 minutes (the collector's one SQL string changes from `refresh_flight_summary(...)` to `write_finished_flights()`; no Python growth). It picks pilots (CIDs) who have sessions with unwritten positions and no session seen for 3 hours, reads their positions after each session's new marker `flown_upto`, applies today's split, airborne, gate and joining SQL (copied word for word from the existing refresh and `flight` view), inserts one row per finished flight (`ON CONFLICT DO NOTHING`) and sets `flown_upto = last_seen` on those sessions (including sessions with no flight plan). One transaction, one advisory lock, capped at 2,000 pilots per call. The table is `flight_record` while the pieces table still exists; in phase 4 it is renamed `flight_summary`. Plan and name fields are stored at write time from the session that ended last (tie-break `end_time DESC, session_id DESC`), so no join to `pilot_session`.
  The drop gate becomes: a day partition of `pilot_position` may be dropped only when every position in it is already in written flights (`observed_at <= flown_upto` for every session).
- **Proposed by.** Opus (sections 1.8, 1.9 and DESIGN_89_FINAL); Sonnet (final recommendation of 11-10-2026), after the owner's guidance. Both now recommend it.
- **Cost at 5 years.** Same stored-table reads as (a) and (b): 0.2 to 0.4 s. Reasoned (not built): the writer's speed from measured pieces of today's SQL (130 ms for the chain over 477 pilots' history; the refresh at about 0.1 s).
- **Lines.** About 380 SQL added across four phases, about 330 deleted (net about -150 to +50 depending on the count); about 250 test lines for the writer alone; about 20 test files change. Phase sizes (DESIGN_89_FINAL): phase 1 about 180 SQL and 250 test lines; phase 2 about 120 SQL, 1 Python line, about 15 test files; phase 3 about 60 changed SQL lines and 2 test files; phase 4 about 20 SQL and about 10 ops and test files plus docs.
- **The four phases** (each its own branch, PR, independent review and green CI):
  1. Writer alongside today's code: `flight_record`, `pilot_session.flown_upto`, a partial index, `write_finished_flights()`. Nothing calls it, nothing is deleted. An equivalence test compares it with today's `flight` view for flights quiet for 3 hours. Medium tier, Opus second pass.
  2. Switch the readers: `flight` re-created over `flight_record` (dropping `gap_segment`, `span_part`, `is_in_progress`), `flight_ext`, `movements`, `cohorts`, `monday_attendance`; the collector's one SQL string; the `roles.sql` EXECUTE grant; the 11 `NOT is_in_progress` panel filters removed; test helper changed. High tier (grant).
  3. New drop gate in `drop_old_pilot_position_partitions` (the lock-friendly mechanism of migration 170 stays; only the condition changes to the `flown_upto` test, and it takes the writer's lock). Tested with synthetic old dates via the existing `p_today` parameter. **Must be live before about 06-01-2027.** High tier (deletes raw data for ever), Opus second pass, rehearsal on a restore of a live dump.
  4. Delete the old machinery (pieces table, both refresh functions, `flight_part`, placeholder rows), then `ALTER TABLE flight_record RENAME TO flight_summary` with everything re-pointed in the same migration; watchdog invariants and messages, the `migrate.sh` grant, ops scripts, `gen_synth`, alerts.md, docs, a new ADR-030 and an ADR-026 amendment. High tier (grant).
- **Proven code.** Kept: ingest, tables, `flight_ext`, `movements`, `cohorts`, and the split, airborne and merge rules (moved, not rewritten). Deleted or rewritten: the refresh pair, finalisation and its three re-checks, the `final` flag, `flight_part`, placeholder rows, `monday_attendance`, two watchdog checks, one grant, about 20 test files. New and untested: about 130 to 180 lines.
- **Risks.** (1) The writer is new code in a new arrangement; mitigated by copying SQL, the phase 1 equivalence test and the Opus pass. (2) The drop gate deletes raw data for ever and has a deadline; it is stricter and simpler than today's, which has never run live. (3) The first run on live data is a backfill (small, capped at 2,000 pilots per call). (4) Counts appear 3 hours after a pilot leaves. (5) Two visible differences: Monday attendance counts a reconnect gap as present; a session that comes back after being written with a changed plan keeps its old airports for the written part (closer to v1 and ADR-023). What (c) loses: re-joining old flights under a changed rule, a full-history cross-check against the merge logic (only the raw window can be recomputed), piece-level detail, and live in-progress rows.
- **Rollback.** Easy only while the old machinery exists (phases 1 to 3 add or switch only). After phase 4 and after raw data is dropped, rollback means restoring the pre-deploy dump. Each phase can deploy alone.
- **Owner would see.** One table where one row is one flight, written once; panels fast; finished flights counted up to 3 hours after the pilot leaves (Monday figure final about 15:00); the 11 panels lose a filter; an unchanged column set otherwise.
- **Verdict.** Current recommendation (section 8). Tidiest end state, matches v1 and the naming wish, mostly deletes code. Biggest single change, and it touches the raw-data drop.

## 5. Cost of renaming `flight_summary` on its own (naming)

Raised by the owner. Sonnet and Opus costed it separately from the other options.

- **Size.** `flight_summary` appears 187 times in 56 files. A rename migration of about 150 lines (mostly re-creating three function bodies with only the name changed: `refresh_flight_summary_sessions`, `refresh_flight_summary`, `drop_old_pilot_position_partitions`, because function bodies name tables as text and would fail at their next call), about 60 file edits, about 20 test files plus the data dictionary test. High tier (grant: `ops/migrate/migrate.sh` grants `watchdog_ro` SELECT on it by name on every deploy, and the next deploy fails if it is not changed in the same release; the drop function is the irreversible one).
- **Other places.** `ops/audit/invariants.sql` and four other audit scripts, the watchdog check names and messages, the scale generator, restore notes, DATA_DICTIONARY, ARCHITECTURE, SPEC, CHART_DEFINITIONS, runbooks. Old ADRs keep the old name with a note.
- **Safety concern.** Reusing the name for a different meaning is the risky kind of rename: old and new tables share column names (`session_id`, `start_time`, `end_time`, `sample_count`), so an old script or document could run against the reused name and give wrong answers without an error, and 14 days of kept dumps hold the old meaning. Opus's rule: new name now (`flight_record`), rename later.
- **How option (c) resolves it.** In (c) nearly all 187 references are touched anyway, so the extra cost is small, and phase 4 drops the pieces table and renames `flight_record` to `flight_summary` in one migration together with the rewritten watchdog checks. Nothing old is left to read the name. Old dumps restore and then migrate in order.
- **For options (a) and (b).** Opus advises not renaming in the same change (it roughly doubles the change and touches the drop function for a cosmetic gain); if wanted, a separate migration weeks later, and never reuse `flight_summary` for the merged table. A database comment and the data dictionary should say plainly "one row per piece of a flight, not per flight". Under owner guidance point 10 this is why (a) and (b) are weaker.

## 6. Ranked comparison

Scores: 1 is best.

| Criterion | Indexes | Extend flight_summary (B) | Cheaper view (C) | Narrow key | Mat. view | (a) stored, 5-min | (b) write once, pieces kept | (c) v1 shape, from raw |
|---|---|---|---|---|---|---|---|---|
| Fixes #89 at 5 years | no | yes | no | no | yes, grows | yes | yes | yes |
| Speed at 5 years | 30 s | 0.3 s (R) | 3 to 27 s | 2.7 to 16.9 s | 0.3 s plus 7 s rebuild | 0.2 to 0.4 s | same | same |
| Simplicity of final code and logic (guidance 2) | n/a | 5 | n/a | n/a | n/a | 3 | 2 | 1 |
| One clean end state (guidance 1) | n/a | 5 | n/a | n/a | n/a | 3 | 3 | 1 |
| Proven code kept (guidance 5) | 1 | 3 | 2 | 2 | 2 | 1 | 1 | 3 |
| Risk to the raw-data drop | none | highest | none | none | none | none | none | highest (but gate never ran live) |
| Naming fit (guidance 10) | n/a | 5 | n/a | n/a | n/a | 3 | 3 | 1 |
| Matches v1 write-once (guidance 11) | no | no | no | no | no | no | yes | yes |
| Effort to build | 1 | 4 | 2 | 3 | 1 | 2 | 2 | 4 |
| Delay before a flight is counted | none | none | none | none | up to refresh interval | 1 to 6 minutes | up to 3 hours | up to 3 hours |
| Rollback | trivial | hard | trivial | easy | easy | easy | easy | easy until phase 4 |
| Verdict | ruled out | rejected | ruled out | rejected | stop-gap only | fallback if no delay wanted | fallback if the gate cannot be ready | recommended |

Ranking by simplicity of final shape: (c), (b), (a). Ranking by reuse of proven code: (a), (b), (c). The owner's guidance weights the first more heavily.

## 7. How the recommendations changed over time

Read this before reading the working papers, because earlier sections of those papers still carry superseded advice.

**Sonnet (designer, `design-89`, DESIGN_89.md), three stages on 11-10-2026:**
1. **A.** First two versions: a separate derived table `flight_merged` kept current every 5 minutes; reason: do not touch `flight_summary` (rejecting B, costed after the owner's question). Written before the owner's new facts.
2. **b (addendum).** After the owner said a delay of 3 hours is fine, data may be lost, simplicity first, and proven code is a bonus: write once when final, built from today's pieces. (c) was judged tidiest on paper but too risky to the drop gate.
3. **c (final).** After the full guidance (one clean end state, gate never run live, naming, v1 write-once): (c), done as one migration. (b) would leave two tables with the wrong names for ever; (a) worst. A trailing note after reading Opus's review agreed with its shape and its "used up to" marker, but disagreed with its (a) pick. (The note says DESIGN_89_FINAL.md did not exist on opus-89 at that moment; it does now.)

**Opus (architect, `opus-89`, OPUS_REVIEW_89.md and DESIGN_89_FINAL.md), four stages:**
1. **a.** Section 1.3 and Part 3: keep all proven code, store merged flights, re-check active pilots every 5 minutes, write only changed rows. Rejected write-once (1.6, originally on a wrong premise that panels needed in-progress rows, corrected the same day) and (c) as a first step (1.8).
2. **c.** Section 1.9, after the owner's facts (3 h delay fine, data loss fine, simplicity first): (c) ranks first, with the observation that no raw positions have ever been dropped, so building before 06-01-2027 loses nothing.
3. **a again.** Section 1.10, after the owner said working deployed code is a big bonus: (a) first, (b) second, (c) only as a later tidy-up. The top summary (five lines) and Part 3 of that paper still state this.
4. **c final.** DESIGN_89_FINAL.md, after the full guidance: (c), four phases, name `flight_record` renamed to `flight_summary` in phase 4, with the fallback of phases 1 and 2 only.

Why they swung: each new fact from the owner changed the test. Reuse of proven code favours (a) and (b); one clean end state, simplicity and naming favour (c). Once the owner said the existing finalisation gate has never run live and no data has been dropped, the reuse advantage of (a) and (b) shrank, and both designers ended at (c).

**Where the later text overrides the earlier text.** Anything in OPUS_REVIEW_89.md saying "build (a) now" (the top summary, Part 3, section 1.10) is superseded by DESIGN_89_FINAL.md. Anything in DESIGN_89.md recommending A or b is superseded by its final section.

## 8. Current recommendation

**Both designers recommend option (c)**, as described in `docs/DESIGN_89_FINAL.md` on branch `opus-89`: one finished-flight table written once, 3 hours after the pilot was last seen, straight from raw positions, in four phases, ending with the table named `flight_summary`. The strongest argument against it is real (it replaces working code at the most dangerous point, the drop gate, with a deadline), and both designers weighed it and still chose (c).

**Fallback.** If phase 3 (the drop gate) cannot be built, reviewed and rehearsed before about December 2026, ship phases 1 and 2 only. That is effectively option (b): the old finalisation stays in charge of drops, and phases 3 and 4 follow later. The raw-position drop must not run on the live system until the gate matches whichever shape is live.

**Why not wipe.** All raw positions since go-live (07-10-2026) still exist, so the writer rebuilds every flight on its first runs. A wipe would also reset the "data since" banner and is not recommended. If the owner prefers it, the owner runs the VM steps (preflight dump, stop collector, truncate sessions and positions, deploy, start); nothing in the repository does it.

**Interaction with #21 (retention).** Flights are kept for ever under every option. In (c), a session delete must delete its flights by `session_ids` (ADR-026 item 2 amendment). The drop gate rule (a day of raw positions may be dropped when every position is in a written flight) interacts directly with retention periods (90 or 180 days).

## 9. Open owner decisions

1. **Monday attendance.** Count a pilot as present across a short reconnect gap? Today's attendance reads pieces and does not count the gap; reading finished flights counts it. Recommended: yes (it is the same flight).
2. **Quiet rule.** Write after 3 hours counted from the pilot's last sighting (any session), not per connection. Recommended: yes.
3. **Keep data or wipe.** Recommended: keep; build before the first drop so nothing is lost.
4. **Deploy timing.** All four phases live before about 06-01-2027, as one deploy or two. Fallback if the gate is late.
5. **Quiet period length.** 3 hours or v1's 8 hours. Anything above 120 minutes is safe; longer only delays panels.
6. **Keep a `flight` view or edit the 11 panels.** Opus's final keeps `flight` as a thin view with `is_in_progress` always false so no panel changes in phase 2 (phase 2 does remove the filter text from 11 panels). Sonnet had recommended editing panels to read `flight_summary` directly (one name per thing). Undecided.
7. **Tie-break for equal end times.** Two pieces of a flight ending at the same instant: take the plan and name fields from the higher session id (about 15 per million flights; today the pick is arbitrary).
8. **Naming.** `flight_record` while the pieces table exists, renamed `flight_summary` in phase 4 (the owner's wish), or the safer Opus alternative of keeping `flight_record` as the permanent name.
9. **Ancillary.** Whether to keep the 8-hour span split as separate flights (current rule: yes).

## 10. What was NOT verified

- **Nothing was built.** No migration, writer function or phase has been run. All (c) cost figures are reasoned from measured pieces of today's SQL.
- **PostgreSQL 16 and the 1 CPU production VM.** Opus tested 17.5 on a PC; INDEX_TEST_89 also 17.5; only FLIGHT_VIEW_SCALING used 16.15 in a capped container. Production runs 16 on a slower box. The per-pilot pushdown (130 ms) must be repeated on 16.
- **Real data.** Everything is synthetic. No live dump was used for equivalence, real reconnects, real ties or overlapping sessions (`ops/check_identity_flap.sql` is the existing check).
- **Timings as `grafana_ro`** with its limits (Opus ran as the owner role), and concurrency under load (dashboards, collector and refresh together). The lock wait of `CREATE OR REPLACE VIEW` on the live system.
- **The integration suite** was not run against any option.
- **Live session count** is stated as about 1,100 (Sonnet) and about 4,000 (Opus); the two were taken at different moments and not re-checked.
- **The drop gate and the Monday attendance change** have no live evidence at all.

## 11. How to resume

1. Re-read this document, then `docs/DESIGN_89_FINAL.md` on branch `opus-89`, then re-check the open owner decisions (section 9) and the date: how long is left before about 06-01-2027?
2. Re-measure anything that matters on PostgreSQL 16: the per-pilot pushdown, and the writer against a restore of a recent live dump (run on a private local database, never on the live VM). Check for overlapping sessions with `ops/check_identity_flap.sql`.
3. Build phase 1 first (a writer beside today's code; nothing deleted). Use a new worktree under `.worktrees/`, one branch and PR per phase, independent review for each, and `Refs #89`, never `Fixes`.
4. Models: Sonnet builds each phase. Use an Opus second pass **only** for the writer function (phase 1) and the drop gate (phase 3), and **only after telling the owner first**. Never use Opus for alerting or docs.
5. Deploy: fresh dump first (`ops/deploy.sh --preflight`), rehearse on a restore of a live dump, then deploy; the owner runs all writes on the VM.
6. Verification before calling it done: equivalence test (`flight_record` against today's `flight` view with `EXCEPT ALL`) on synthetic data at 1 and 5 years and on a live-dump restore; ten slowest panels under 3 s at 5 years; synthetic-date partition-drop tests; `test_every_panel_query_runs_as_grafana_ro` unchanged.
7. Documents tied to tests that will need updating: `docs/DATA_DICTIONARY.md`, `docs/CHART_DEFINITIONS.md`, `docs/alerts.md`, plus a new ADR-030 and an ADR-026 amendment. If panels time out before any work starts, the only no-code lever is raising `grafana_ro`'s `statement_timeout`.

## 12. Glossary

| Term | Meaning |
|---|---|
| Pilot | One person (CID). Has many flights; on one flight at a time. |
| Session | One connection (CID, callsign, logon time). A reconnect is a new session. |
| Flight | What charts count: one or more sessions of the same pilot, callsign, departure and arrival, joined across gaps of 120 minutes or less. |
| Piece | One stretch of a session: split after 120 minutes of silence and every 8 hours. Today's `flight_summary` row, key (session, gap segment, span part). |
| Quiet period | Time since the pilot was last seen before their flight is written (3 hours proposed; must exceed 120 minutes). |
| Reference view | Today's `flight` body kept unchanged as the definition and test target (`flight_chain` or `flight_merge_reference`). |
| `flown_upto` | Proposed marker on a session: positions up to this time are already in written flights. |

## Source papers

- `docs/DESIGN_89.md` on branch `design-89` (branch `design-89`): Sonnet designer, options A to C, addendum, final (c).
- `docs/OPUS_REVIEW_89.md` on branch `opus-89` and `docs/DESIGN_89_FINAL.md` on branch `opus-89` (branch `opus-89`): Opus architect.
- `docs/INDEX_TEST_89.md` on branch `test-89-index` (branch `test-89-index`): index measurements.
- `docs/FLIGHT_VIEW_SCALING.md`: first measurements.
