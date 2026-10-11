# Independent review of issue #89 (and #108): the `flight` view and history growth

Written 11-10-2026 by an independent reviewer (Opus 5.5), branch `opus-89`, from `origin/main` at e575996. Design only: no migration, no product code, nothing on the live VM. Part 1 was written and committed BEFORE reading `docs/DESIGN_89.md` (branch design-89). Part 2 reviews that design. Part 3 is the verdict. Part 4 answers the owner's naming question.

All measurements: a private PostgreSQL 17.5 cluster created with `initdb` on this PC (port 55433, `shared_buffers` 512 MB, `work_mem` 16 MB, no parallel workers, JIT off), all migrations from main applied, the 5-year synthetic set from `tests/scale/gen_synth.sql` (925000 flights over 1825 days: 1,029,076 sessions, 1,059,703 `flight_summary` rows, 978,357 merged flights, the same counts as `docs/FLIGHT_VIEW_SCALING.md`). Production runs PostgreSQL 16 on a slower 1 CPU VM, so compare the numbers here with each other, not with the VM. Times are single warm runs as the owner role (not `grafana_ro`). The cluster was deleted afterwards.

---

## Part 1. My own analysis (written before reading DESIGN_89)

### 1.1 Root cause, in plain words

A "flight" is assembled at the moment a dashboard asks for it. The `flight` view (migrations 110 and 130) takes every flight piece ever stored (`flight_part`, one row per session, gap segment and 8 hour part), sorts all of them by pilot, callsign, departure, arrival and start time, walks them in order to decide which pieces belong together (the 120 minute reconnect rule), and then glues each group together with about twenty ordered `array_agg` calls. A dashboard panel asks for "the last 30 days", but that filter is on the glued result (`max(end_time)`), so Postgres has to glue the whole of history first and throw almost all of it away. History is kept forever (ADR-026), so every panel gets slower every day.

So the root cause is not a missing index and not the window functions as such: it is that the merge result is **recomputed on read, over all history, on every query**, although for any flight older than a few hours it can never change again.

### 1.2 What I measured (5 years of synthetic history)

| Test | Result | What it shows |
|---|---|---|
| `SELECT count(*) FROM flight` | 4.6 s | the fixed cost every panel pays before its own work |
| Busiest home-region airports (movements, 30 days), today's views | 24.5 s | near the 30 s limit even on this fast PC |
| Most popular domestic routes (30 days), today's views | 15.3 s | |
| Plan of the domestic routes panel | 15.5 of 19.2 s is a sequential scan of all 1M `pilot_session` rows evaluating `icao_is_home()` | a second, hidden cost: the planner pushes the `dep_is_home`/`arr_is_home` function calls BELOW the merge, so the function runs on all history before the time filter |
| `flight` filtered by `cid = ANY(477 pilots seen in the last 12 h)` | 130 ms, 9,318 flights; the plan shows the filter pushed down through all three window functions to an index scan of `pilot_session_identity` | a per-pilot recompute of the old logic is cheap and needs no new code for the merge rule |
| Stored merged table `flight_merged` built from the old logic | 7.0 s to build; 273 MB with 5 indexes (162 MB heap) | the one-off backfill cost at 5 years (today: milliseconds) |
| Same panels over a thin view on the stored table | busiest airports 0.34 s, domestic routes 0.21 s, international routes 0.21 s, aircraft categories (6 months) 0.33 s, busiest days by pilots (6 months) 0.38 s, an all-time monthly panel 1.8 s | flat: no longer depends on history length, except all-time panels, which are linear but cheap |
| Row-for-row comparison, all 32 columns, old view vs thin view (`EXCEPT ALL` both ways) | 15 rows of 978,357 differ, in each direction | all 15 are flights where two pieces END AT THE SAME INSTANT and the old view picks the plan fields of one of them arbitrarily (checked: every one has two pieces at the final `end_time`). `is_in_progress` (211 flights online) and every number agreed exactly |
| `movements` over the last 30 days, old vs new | 0 rows differ either way | |
| Maintenance: recompute all flights of the 477 pilots seen in 12 h and write only what changed | 160 to 250 ms per run, 0 rows written when nothing changed, 1 row written after a plan change; recomputed result equal to the old logic for those pilots | cheap enough for the 5-minute job |
| Same, but delete every row of those pilots and re-insert | 306 ms, and 9,318 rows rewritten every 5 minutes (about 2.7 million dead row versions a day at 5 years) | delete-and-reinsert works but churns the table; write-only-what-changed avoids that |
| Heaviest single pilot (2,226 flights) recomputed | 29 ms | skew is not a problem |
| Narrow alternative: store only a per-piece "flight id" and keep grouping at query time | domestic routes 2.7 s without the home-airport test, 16.9 s with it | still linear in history and still exposed to the function push-down; does not fix #89 |

### 1.3 My recommended solution

**Store the merged flights in a table, keep the old merge logic as the single source of truth, and refresh only the pilots who were active, writing only rows that changed.**

1. **Keep today's merge logic, unchanged, as a reference view** (for example `flight_merge_reference`, see Part 4). It is the current body of `flight` minus the live plan fields, plus two extra outputs: `last_session_id` (the session of the piece that ended last, ties broken by the higher session id) and `session_ids` (every session in the flight). Nobody reads it for dashboards; it is the definition, the backfill source and the comparison target for tests and the integrity check.
2. **New table** of merged flights: key = first piece's (session_id, gap_segment, span_part) (exactly the key `flight` already promises to be unique on); columns cid, callsign, departure, arrival (the merge group, normalised), last_session_id, session_ids, logon_time, start_time, end_time, airborne_minutes, max_gs_kt, max_alt_ft, sample_count. Indexes: cid, start_time, end_time, logon_time. No foreign key (see 1.5).
3. **`flight` becomes a thin view**: the table joined to `pilot_session` on `last_session_id` for the plan and pilot fields (so they stay live, as today), `is_circuit`, `is_zzzz` and `touches_home_region` computed from the stored departure and arrival (they are functions of the group key, so this is identical), and `is_in_progress` = the flight's `session_ids` overlap the online-now session ids (one small lookup per query, identical to today's `bool_or`). Same 32 columns, same order and types, so `CREATE OR REPLACE VIEW` keeps `flight_ext`, `movements`, `cohorts` and every panel untouched.
4. **Maintenance per pilot, not per merge group.** At the end of `refresh_flight_summary_sessions` (so that every path that changes `flight_summary`, including finalisation before a partition drop, also updates the merged table) recompute every flight of the pilots (cids) whose sessions were just refreshed, from the reference view, and upsert only rows whose values differ, deleting rows that no longer exist. Why per pilot: a pilot's cid never changes, merges never cross pilots, and recomputing all of a pilot's history makes plan changes, callsign case, gaining or losing the flight gate and "the part moved to another group" correct with no tracking of old keys. It costs about 0.2 s at 5 years (measured above) and does not grow with total history, only with the number of active pilots.
5. **Concurrency**: the refresh already holds the `vatpac.flight_summary_refresh` advisory lock; the new step runs inside it, in the same transaction, so a dashboard sees either the old or the new state, never half. The migration takes the same lock before its backfill. No triggers.
6. **Tie-break** (two pieces ending at the same instant): highest session id wins. Today's behaviour on ties is not defined (it can differ between two runs of the same query), so any fixed rule is a strict improvement; it should still be shown to the owner as a one-line decision, with the evidence that it touches about 15 in a million flights.

Expected effect: every panel on `flight`, `flight_ext`, `movements` and `cohorts` stops growing with history (0.2 to 0.4 s at 5 years for the 30-day and 6-month panels here; all-time panels about 2 s at 5 years, from about 10 s). Cost: one table (about 270 MB at 5 years with indexes, under 1 MB today), about 0.2 s more work every 5 minutes, one migration, no collector (Python) change, no `roles.sql` change.

### 1.4 Alternatives I rejected, and why

| Option | Verdict | Evidence or reason |
|---|---|---|
| Plain indexes | Rejected | Already measured (INDEX_TEST_89): ignored by the planner, 15 to 20 percent when forced, still linear. My plan reading agrees: the sort key spans two tables. |
| Store only a per-piece flight id (narrow chain key) | Rejected | Measured 2.7 s (16.9 s with the home-airport test) at 5 years and still linear: the grouping still reads all history because the panel filters on the grouped end_time. |
| Plain materialised view, refreshed every 5 minutes | Rejected | A full rebuild is about 7 s at 5 years and grows; `REFRESH` without CONCURRENTLY blocks every reader for that time, with CONCURRENTLY it computes the whole thing plus a diff; it would rewrite or diff 1M rows 288 times a day. It also cannot hold live plan fields or `is_in_progress`, so a thin view on top is needed anyway. Refreshing it only daily would make counts lag a day. |
| Time-bounded pre-filter in the view | Not possible | The chain id is a running sum; already shown in FLIGHT_VIEW_SCALING. |
| Single-session fast path | Rejected | Already prototyped, slower. |
| Raise `statement_timeout` | Stop-gap only | Does not reduce latency; still the right emergency lever. |
| Fix only the `icao_is_home` push-down | Rejected as a fix, worth knowing | It is the bigger part of the airport and route panels today, but the merge alone is still 4.6 s at 5 years and linear. The stored table removes both, because the time filter is applied first by index. |
| Bounded recompute (only pieces after a safe cut point per pilot) | Rejected | Correct cut points are subtle: the old rule only looks at the previous piece's session, so overlapping sessions (ADR-021 twins) can make a "fresh start" depend on older pieces. Whole-pilot recompute costs 29 ms for the heaviest pilot; the extra cleverness buys nothing. |
| Extend `flight_summary` with merged columns | Rejected | Mixes two row grains (piece and flight) in the table whose `final` flag authorises dropping raw positions. |

### 1.5 Edge cases I checked in my own design

- **Reconnect within 120 minutes**: the new session's cid is refreshed, so the pilot's flights are recomputed and the two pieces merge; the old row (keyed by the first piece) is updated, nothing is double counted.
- **Plan change** (different airports): only possible while the session is online, which puts it in the 12 hour refresh window, so the pilot is recomputed. Moving a piece between groups is handled because the whole pilot is recomputed.
- **In-progress flights**: `is_in_progress` stays live in the view; the numbers lag by up to 5 minutes exactly as `flight_summary` already does (ADR-016).
- **Plan fields** stay live through `last_session_id`. Departure and arrival are stored (they are the merge key), so for a session that changes its plan in the last 5 minutes the merged row shows the old airports until the next refresh. Today the airports change instantly but the numbers do not; the flight is in progress in that window, so no counting panel sees it (ADR-025). This is a lag of at most one refresh, and is the only behaviour difference besides ties.
- **Settings**: `home_icao_prefixes` stays live (computed in the view). A change of `airborne_gap_cap_seconds` already only applies to flights recomputed afterwards (ADR-016); unchanged.
- **Finalisation before a partition drop** calls the same worker, so the merged rows are updated in the same transaction; the drop rules and the `final` flag are unchanged.
- **Session deletes** (none exist; ADR-026 keeps sessions forever): a deleted session cascades its `flight_summary` rows but not the merged row. The thin view's inner join on `last_session_id` hides a row whose last session is gone, but a deleted first or middle session would leave stale numbers. Rule to write into ADR-026: any session delete must be followed by a recompute of that pilot (one function call). No foreign key on purpose: `ON DELETE CASCADE` would delete a whole merged flight when one of its pieces goes.
- **Data-fix migrations** (like migration 100's twin merge) must call the rebuild for the affected pilots. A full rebuild function should exist for repairs and restores.
- **Backup and restore**: the table is in every dump (derived data, about 160 MB more per dump at 5 years, under 1 MB today). After a restore it is already consistent with the restored `flight_summary` (one dump snapshot). The restore test should add one check: the table equals a recomputation for a sample of pilots.
- **Tests**: every test that inserts `flight_summary` rows directly (the helper in `tests/integration/helpers.py` and seven test files do) will see no flights until the merged table is refreshed, so the helper must call the rebuild. This is a known, mechanical change, not a design problem.

### 1.6 The "write once when final" option (owner's question, assessed independently)

The idea, as in v1: once every session of a flight has been gone for a quiet period, write one final row for the flight and never touch it again; recent and in-progress flights are computed live by the old logic; `flight` = stored finished flights UNION live recent ones.

**What it gets right.** It names the real root cause (re-merging settled history), it is immutable (good for audit and backups, no churn) and it matches v1's mental model.

**Where it is weaker than per-pilot maintenance:**

1. **The union boundary must be exact.** "Live" must mean "every piece not already inside a stored flight", decided by stored membership, not by a time test, or a flight straddling the boundary is counted twice or not at all. That needs a per-piece marker (a column on `flight_summary` or a membership table), which is more stored state than my design, plus an anti-join in every panel query.
2. **Two code paths for every panel.** Each query runs the union: an index read of the stored part plus a live window computation over the recent pieces (a few thousand rows, tens of milliseconds). Correct, but every panel now depends on the planner handling a UNION ALL with a window branch, and there are two places where the merge rule could drift.
3. **It is not identical to today on revived sessions.** A session can come back after a long silence with the same logon (a pilot who leaves the polygon and re-enters, or a collector outage). If its plan has changed when it comes back, today's view moves ALL its pieces, including the old ones, to the new airports; a frozen row keeps the old airports. Arguably the frozen answer is more correct, but it is a behaviour change the owner has not decided, and `is_in_progress` for the frozen row must still be computed live (the revived session is online).
4. **Late reconnects are safe only with a long enough quiet period.** A new piece appears in `flight_summary` within one refresh (5 minutes, about 13 minutes if a refresh fails during a partition drop, issue #112 item 5, and much longer if the refresh job is broken while ingest continues). A stored flight can still be extended by any piece that starts within 120 minutes of its end. So the quiet period must be more than 120 minutes plus the worst refresh lag, and freezing must happen in the same transaction as the summary refresh. The 8 hour part rule does not shorten it: parts of one session are never joined, and a live session's flight is not final whatever its age.
5. **It is not cheaper.** The stored part is the same size as mine; the live part adds per-query work; the write path is similar (a freeze step instead of an upsert of active pilots).

**The quiet period, if the owner prefers this model anyway:** 12 hours. That is exactly when a session leaves the 12 hour refresh window (`FLIGHT_REFRESH_WINDOW`), after which `flight_summary` stops changing for it anyway, it is far above 120 minutes plus any realistic refresh lag, and it is what v1 did. Anything under about 3 hours is unsafe.

**Plainly: it is not better for v3.** It solves the same problem with more moving parts (union, membership marker, two code paths) and a behaviour change on revived sessions. The per-pilot maintained table gives the same speed with one code path and rows identical to today's view except on exact ties. The good idea to keep from it: settled flights should not be recomputed on read, and the maintenance should touch only active pilots, which is what 1.3 does.
