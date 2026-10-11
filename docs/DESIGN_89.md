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

---

# Addendum 11-10-2026: ranking with the owner's new facts

New facts from the owner: (1) finished flights may reach the panels 3 hours or more after they end; (2) existing data may be lost, so no backfill, conversion, legacy copy or data rollback is needed; (3) simplest code with the logic right comes first; (4) the current code (`flight_summary`, the refresh, the flight views, finalisation and their tests) is deployed, tested and working, and counts as a large bonus. Written before reading any other review. Still design only; nothing was run.

## Plain English verdict

**Recommend (b): write once when final, built as a new table from today's pieces, with the proven code left alone.** (c) is the tidiest end state on paper but buys about 150 fewer lines at the price of rewriting the code that guards the irreversible drop of raw positions, before that code has ever run live (first real drop is due about 06-01-2027). (a) is the worst on every count: it keeps everything and adds the most delicate new code.

Why the new facts make (b) small: a flight is final once no session of its chain has been seen for 3 hours (the merge gap is 120 minutes, so nothing can join it after that). A final flight never changes, so there is no delete-and-reinsert, no live join to `pilot_session`, no `online_now` test (a final flight is never in progress) and no staleness question. The plan fields are copied once, from the latest part, at write time.

## The three options

| | (a) DESIGN_89: stored table, delete and reinsert every 5 minutes | (b) write once when final, from today's pieces | (c) one finished-flight table straight from sessions and positions |
|---|---|---|---|
| Idea | `flight_merged` kept current per touched pilot; `flight` joins live fields back | Same table, but a flight is written once, 3 hours after its chain went quiet, by one INSERT from today's chain SQL | Replace `flight_summary` and the parts layer; one writer reads `pilot_session` and `pilot_position` and writes the flight; no pieces |
| Proven code kept unchanged | All of it (about 700 lines of SQL and ~25 test files) | All of it, except the body of `flight` | The parts and chain rules are copied into the writer (the logic is proven, the wrapper is not); the finalisation, refresh, flag and `flight_part` go |
| Proven code rewritten | `refresh_flight_summary_sessions` gets a hook; `flight` rewritten | `flight` becomes a thin view (about 15 lines) | `drop_old_pilot_position_partitions` (finalisation block and three re-checks, about 45 lines), `monday_attendance`, helpers and about 20 test files that call `refresh_flight_summary` or read `flight_summary`/`flight_part` (about 110 references) |
| New untested code | About 200 lines (hook, live joins, tie-break, array overlap for in-progress) | About 60 lines: one table, one writer function, one call | About 180 lines (writer plus the new drop gate) |
| Tables / views / functions removed | none | none | `flight_summary`, `flight_part`, `refresh_flight_summary`, `refresh_flight_summary_sessions`, the `final` column, placeholder rows |
| Tables / views / functions added | table, `flight_chain` view, 1 to 2 functions | table, `flight_chain` view (today's `flight` body renamed), 1 function | table, 1 writer function |
| SQL lines (net) | about +200 | about +120 | about +300 written, about 290 of old logic deleted, net about -150 |
| Tests to change | about 4 | about 2, plus one new file (about 80 lines) | about 20 files |
| `is_in_progress` | Kept, live | Kept as a constant `false` column (one line), so the 11 panels that filter on it and `flight_ext` stay untouched | Removed everywhere (ADR-025 is then true by construction) but 11 panels and the view need editing |
| Cost at 5 years | 0.2 to 1.6 s (reasoned from the stored-table floor) | Same | Same |

Logic correctness: (b) and (c) carry the same rules (120 minute gap, 8 hour span, reconnect merge, flight gate) with one fewer moving part than (a), because nothing is recomputed after the flight is final. (a) alone has the live-join tie-break and the stale-edit problems.

By simplicity of the final shape: (c) first, (b) second, (a) third. By reuse of proven code: (b) first, (a) second, (c) third. Combined with "proven code is a large bonus", (b) wins; (c) can still follow later as a pure clean-up if the owner wants the smaller end state.

## What the simplest correct final shape is, and what to delete

For (c), the tidy end state, delete: `flight_summary`, the `final` column and its checks, `flight_part`, `refresh_flight_summary` and `refresh_flight_summary_sessions` (replace with one function, for example `write_final_flights()`, called by the same 5-minute maintenance call, so no Python line is added; only the SQL function name in `maintenance.py` changes), the placeholder rows, `is_in_progress`, and the finalisation block in the partition drop. Keep a one-line gate there instead: do not drop a partition that holds a session which has a flight-gate plan and no written flight. Raw positions then simply roll off at the retention limit, because a flight is written hours after it ends and retention is 90 to 180 days.

For (b), nothing is deleted. That is the point of recommending it: the part layer still does its job (finalising before a partition drop), the new table sits on top, and the old view body survives as `flight_chain` for the equivalence test and for the writer.

## Does a clean start allow a much smaller migration?

Not much. The migration is small because of the design, not because of the data. With (b) or (c), the first run of the writer finds every quiet session not yet written and writes it, so there is no special backfill code either way; a wiped database simply makes that first run empty. What a clean start does allow: no conversion of old rows, no `flight_old` copy, no data rollback (the down step only restores the view), and no need to prove that old and new rows are identical. It does not remove the work of rewriting dependants for (c): `monday_attendance` reads `flight_summary` today and has to move to the finished-flight table.

## What remains risky

- The quiet test must be done per chain, not per session: an early part can have been silent for 3 hours while a later reconnect in the same chain is still connected (a long second session). Writing the early part alone would split a flight. The writer must only write chains whose latest end is older than the quiet period.
- Tie-break for the "latest part" pick: still an owner decision (b and c write once, so it is a small rule but still a visible one).
- Monday attendance during the event (07:00 to 12:00 Z): with (c) it reads finished flights, so it would lag 3 hours exactly when it is watched. (b) leaves it on `flight_summary`, unchanged.
- First catch-up run: one INSERT over all unwritten sessions; bound it by a time window or batch it if history is large (not at today's size).
- A collector outage longer than the quiet period: flights are written late, never lost, because the rule is "quiet and not yet written", not a time window.
- For (c) only: the rewritten partition-drop gate guards irreversible deletion and its 11 lock tests would change; this is the one database-risky piece that warrants an Opus second pass. For (b) the Opus pass is only worthwhile on the writer function and the `flight` view swap.

---

# Final recommendation 11-10-2026: balanced, with all of the owner's guidance

Written before reading docs/OPUS_REVIEW_89.md or docs/DESIGN_89_FINAL.md. Design only; nothing run.

## Verdict

**Recommend (c), done as one migration: `flight_summary` becomes the final record of the merged flight, written once, 3 hours after the flight went quiet, straight from `pilot_session` and `pilot_position`.** This reverses my addendum above, which preferred (b). The new guidance changes the test: the owner wants one clean end state, not a stepping stone. (b) would leave two tables for ever with the wrong names (`flight_summary` holding pieces, a second table holding flights), which breaks guidance 1 and 9. (a) is the worst: it keeps everything and adds the most delicate code.

## Ranking

1. **(c)** One table, one writer, nothing recomputed, nothing live. Net about -150 lines of logic.
2. **(b)** Cheapest to build, least risk now, but permanent two-layer duplication and a later migration if the owner wants the tidy shape.
3. **(a)** Most new code (live joins, tie-break, delete and reinsert, in-progress overlap), no simplification.

## Simplest correct end state

- Table `flight_summary`: one row per finished flight (all 31 `flight` columns, plus the list of session ids). Written once, never updated. The old `flight_summary` is dropped and the name reused in the same migration, so there is no overlap period.
- One writer function (about 100 lines), called by the existing 5-minute maintenance call (rename the SQL function name only; no Python growth). Per guidance 8, it looks only at sessions not yet in a flight, groups them by CID, callsign, departure and arrival, applies today's rules (gap, 8 hour span, 120 minute merge, flight gate), and writes a chain only when its latest end is older than 3 hours.
- `flight_ext`, `movements`, `cohorts`, `monday_attendance` read it. `is_in_progress` and the `online_now` dependency go (guidance 7). Delete: old `flight_summary`, `final`, `flight_part`, `flight`'s window view, `refresh_flight_summary(_sessions)`, placeholder rows, and the finalisation and three re-checks in the partition drop. The drop keeps one gate: do not drop a partition holding a gated session with no written flight.

## Cost and risk

One migration, about 300 lines written, about 290 deleted. Tests: about 20 files touch the old names, but most go through `tests/integration/helpers.py` (7 references), so one helper change carries most of them; new tests for the writer and the gate. Proven code kept: ingest, tables, the rules themselves (copied), `flight_ext`, `movements`, `cohorts`. New untested: about 130 lines. Prove it by running old and new pipelines on the same synthetic data and comparing `flight_ext` with `EXCEPT ALL`. High-tier pieces: the writer, and the drop gate (never run live, so little proven value is lost by rewriting it). Also changed: 11 panels if `flight` is dropped.

## Lost versus today

5-minute freshness (now 3 hours), live in-progress rows in `flight`, per-part rows, and the unproven finalisation block. If the owner wipes data, the history too; if not, the first writer run writes existing sessions with no conversion code.

## Owner must decide

1. Keep a `flight` view or edit the 11 panels to read `flight_summary` (I recommend editing: one name per thing).
2. Quiet period: 3 hours or v1's 8.
3. Tie-break for equal end times.
4. Keep the 8 hour span split as separate flights.
5. Monday attendance lags 3 hours.
