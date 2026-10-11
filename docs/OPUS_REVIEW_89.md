# Independent review of issue #89 (and #108): the `flight` view and history growth

**In plain English (five lines):**
1. **Problem:** every time a chart opens, the database rebuilds every flight ever recorded from its pieces, so charts get slower each year (about 25 s for the worst chart with 5 years of data, against a 30 s limit).
2. **Recommendation:** keep a stored list of finished-and-merged flights, and every 5 minutes re-check only the flights of pilots who had a session in the last 12 hours.
3. **Why:** in a test with 5 years of made-up data the worst chart fell from 24.5 s to 0.3 s, and the results matched today's on all but 15 of nearly a million flights.
4. **Risk:** this changes the base of about 35 charts, so it needs a careful test against a copy of the live data before it goes live; if it goes wrong, the fix is to restore the backup taken just before.
5. **You decide:** (we do not recommend option (c), the v1-faithful rewrite, section 1.8) one question: may a finished flight reach the charts up to 3 hours after it ends? If yes, the simpler "write once when final" design (section 1.6) is as good, and arguably closer to v1; if no, build the recommended one. Smaller choices: the tie-break (we recommend the later connection's details) and table names (we recommend no rename now).

Written 11-10-2026 by an independent reviewer (Opus 5.5), branch `opus-89`, from `origin/main` at e575996. Design only: no migration, no product code, nothing on the live VM. Part 1 was written and committed BEFORE reading `docs/DESIGN_89.md` (branch design-89). Part 2 reviews that design. Part 3 is the verdict. Part 4 answers the owner's naming question.

All measurements: a private PostgreSQL 17.5 cluster created with `initdb` on this PC (port 55433, `shared_buffers` 512 MB, `work_mem` 16 MB, no parallel workers, JIT off), all migrations from main applied, the 5-year synthetic set from `tests/scale/gen_synth.sql` (925000 flights over 1825 days: 1,029,076 sessions, 1,059,703 `flight_summary` rows, 978,357 merged flights, the same counts as `docs/FLIGHT_VIEW_SCALING.md`). Production runs PostgreSQL 16 on a slower 1 CPU VM, so compare the numbers here with each other, not with the VM. Times are single warm runs as the owner role (not `grafana_ro`). The cluster was deleted afterwards.

**Words used in this document.** A *pilot* is one person, identified by a CID. A *session* is one connection to VATSIM, identified by (CID, callsign, logon time); a disconnect and reconnect makes a new session. A *flight* is what the charts count: one or more sessions of the same pilot, callsign, departure and arrival, joined when the gap is 120 minutes or less. One pilot has many flights; one flight has one or more sessions. A session can also be split into *pieces*: a new piece starts after a 120-minute silence, and every 8 hours. "Grouped by pilot" below only means "which flights to re-check": all flights of the pilots (CIDs) who had a session in the last 12 hours.

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
| Maintenance: recompute all flights of the 477 pilots (CIDs) with a session in the last 12 h and write only what changed | 160 to 250 ms per run, 0 rows written when nothing changed (the synthetic data is static; on live data each run also rewrites the flights whose sessions are still connected or just ended, about one per connected pilot), 1 row written after a plan change; recomputed result equal to the old logic for those pilots | cheap enough for the 5-minute job |
| Same, but delete every stored flight of those pilots and re-insert | 306 ms, and 9,318 rows rewritten every 5 minutes (about 2.7 million dead row versions a day at 5 years) | delete-and-reinsert works but churns the table; write-only-what-changed avoids that |
| Heaviest single pilot (2,226 flights) recomputed | 29 ms | skew is not a problem |
| Narrow alternative: store only a per-piece "flight id" and keep grouping at query time | domestic routes 2.7 s without the home-airport test, 16.9 s with it | still linear in history and still exposed to the function push-down; does not fix #89 |

### 1.3 My recommended solution

**Store the merged flights in a table, keep the old merge logic as the single source of truth, and re-check only the flights of pilots (CIDs) who had a session in the refresh window, writing only rows that changed.**

1. **Keep today's merge logic, unchanged, as a reference view** (for example `flight_merge_reference`, see Part 4). It is the current body of `flight` minus the live plan fields, plus two extra outputs: `last_session_id` (the session of the piece that ended last, ties broken by the higher session id) and `session_ids` (every session in the flight). Nobody reads it for dashboards; it is the definition, the backfill source and the comparison target for tests and the integrity check.
2. **New table** of merged flights: key = first piece's (session_id, gap_segment, span_part) (exactly the key `flight` already promises to be unique on); columns cid, callsign, departure, arrival (the merge group, normalised), last_session_id, session_ids, logon_time, start_time, end_time, airborne_minutes, max_gs_kt, max_alt_ft, sample_count. Indexes: cid, start_time, end_time, logon_time. No foreign key (see 1.5).
3. **`flight` becomes a thin view**: the table joined to `pilot_session` on `last_session_id` for the plan and pilot fields (so they stay live, as today), `is_circuit`, `is_zzzz` and `touches_home_region` computed from the stored departure and arrival (they are functions of the group key, so this is identical), and `is_in_progress` = the flight's `session_ids` overlap the online-now session ids (one small lookup per query, identical to today's `bool_or`). Same 32 columns, same order and types, so `CREATE OR REPLACE VIEW` keeps `flight_ext`, `movements`, `cohorts` and every panel untouched.
4. **Maintenance grouped by pilot (CID), not by merge group.** At the end of `refresh_flight_summary_sessions` (so that every path that changes `flight_summary`, including finalisation before a partition drop, also updates the merged table) recompute every flight of the pilots (cids) whose sessions were just refreshed, from the reference view, and upsert only rows whose values differ, deleting rows that no longer exist. Why per pilot: a pilot's cid never changes, merges never cross pilots, and recomputing all of a pilot's history makes plan changes, callsign case, gaining or losing the flight gate and "the part moved to another group" correct with no tracking of old keys. It costs about 0.2 s at 5 years (measured above) and does not grow with total history, only with the number of active pilots.
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
- **Plan change** (different airports): only possible while the session is online, which puts it in the 12 hour refresh window, so the pilot is recomputed. Moving a piece between groups is handled because all of that pilot's flights are recomputed.
- **In-progress flights**: `is_in_progress` stays live in the view; the numbers lag by up to 5 minutes exactly as `flight_summary` already does (ADR-016).
- **Plan fields** stay live through `last_session_id`. Departure and arrival are stored (they are the merge key), so for a session that changes its plan in the last 5 minutes the merged row shows the old airports until the next refresh. Today the airports change instantly but the numbers do not; the flight is in progress in that window, so no counting panel sees it (ADR-025). This is a lag of at most one refresh, and is the only behaviour difference besides ties.
- **Settings**: `home_icao_prefixes` stays live (computed in the view). A change of `airborne_gap_cap_seconds` already only applies to flights recomputed afterwards (ADR-016); unchanged.
- **Finalisation before a partition drop** calls the same worker, so the merged rows are updated in the same transaction; the drop rules and the `final` flag are unchanged.
- **Session deletes** (none exist; ADR-026 keeps sessions forever): a deleted session cascades its `flight_summary` rows but not the merged row. The thin view's inner join on `last_session_id` hides a row whose last session is gone, but a deleted first or middle session would leave stale numbers. Rule to write into ADR-026: any session delete must be followed by a recompute of that pilot (one function call). No foreign key on purpose: `ON DELETE CASCADE` would delete a whole merged flight when one of its pieces goes.
- **Data-fix migrations** (like migration 100's twin merge) must call the rebuild for the affected pilots. A full rebuild function should exist for repairs and restores.
- **Backup and restore**: the table is in every dump (derived data, about 160 MB more per dump at 5 years, under 1 MB today). After a restore it is already consistent with the restored `flight_summary` (one dump snapshot). The restore test should add one check: the table equals a recomputation for a sample of pilots.
- **Tests**: every test that inserts `flight_summary` rows directly (the helper in `tests/integration/helpers.py` and seven test files do) will see no flights until the merged table is refreshed, so the helper must call the rebuild. This is a known, mechanical change, not a design problem.

### 1.6 The "write once when final" option (re-assessed 11-10-2026 after the owner's correction)

#### In plain English

**A correction first.** My first version of this section was wrong on one point. Flights that are still connected are never shown in any chart. All 35 flight charts count finished flights only (owner decision, issue #38; CHART_DEFINITIONS.md "In-progress flights"). The charts that show who is online now read other views and never touch flights. So charts do NOT need "stored old flights stitched to freshly worked-out recent ones". That stitching was my main objection, and it was not needed. I have redone the comparison below.

**The idea.** As in v1: once a flight is over and the pilot has been quiet for a set time (the *quiet period*), write the flight down once, as one row, and never change it. The charts read only these written rows.

**Two words used below.**
- A *piece* is one stretch of one session (one connection). A disconnect and reconnect makes two sessions, so at least two pieces.
- *Reference merge* is today's merge logic, kept unchanged as the definition of a correct flight. We compare stored rows against it.

**A worked example** (a 3-hour quiet period). A pilot flies Sydney to Melbourne and loses the connection halfway.

| Time (UTC) | What happens | Pieces table (`flight_summary`) | Stored flights (charts read this) | Chart counts |
|---|---|---|---|---|
| 09:00 | Connects (connection A), takes off | piece A from the next 5-minute update | nothing | not counted (in the air) |
| 10:10 | Connection drops | A: 09:00 to 10:10 | nothing | not counted yet |
| 10:40 | Reconnects (connection B), same callsign and route | A, and B from 10:40 | nothing | not counted |
| 11:30 | Lands, disconnects | A, B: 10:40 to 11:30 | nothing | not counted yet (today's design: counted from about 11:31) |
| 14:30 | 3 quiet hours since 11:30 | unchanged | **one row: 09:00 to 11:30, made of A and B; never changed again** | counted |

If the quiet period were only 20 minutes, A would be written down at 10:30 as its own flight. B would then become a second flight, and the reconnect would be counted twice. That is why the quiet period must be longer than the 120-minute reconnect rule.

**What quiet period is safe?** More than 120 minutes. The write-down happens in the same 5-minute update that brings the pieces up to date, so any reconnect seen before the write-down is already in the pieces table. **3 hours** gives a safe margin. The 8-hour split of long connections does not matter: a flight is only written down once every connection in it has gone quiet, however long it lasted. v1 waited 8 hours, but that adds nothing to safety. It only delays the charts.

**What it fixes.** Charts stop rebuilding years of flights; they read finished rows, as fast as the recommended design (same stored table, same indexes). Old flights can never be changed by a later update, so a bug in the update can only affect new flights.

**What it costs.**
- **Every flight appears in the charts up to 3 hours after it ends** (today: about 1 to 6 minutes). This undoes part of ADR-025 ("counted once the pilot has been gone 60 s"), so it needs your decision and a new ADR. The panels that notice (from the real SQL):
  - Servers used by home-region pilots, last 24 hours: the newest 3 hours are missing (about an eighth of the window).
  - Highest groundspeeds, RAAFv flights and "most time online", all last 7 days: the newest 3 hours are missing.
  - Monday event share of YMML-YSSY airborne time (07:00 to 12:00Z): this Monday's figure appears from about 15:00Z instead of about 12:05Z.
  - Busiest days, monthly management and onboarding panels: today's day or month reads slightly low until 3 hours have passed.
  - 30-day route, airport and aircraft panels: the last 3 hours of 30 days, under 0.5 percent.
  - Not affected: Monday attendance and the watchdog's integrity checks (they read the pieces table, not flights), the restore test, and every live "online now" panel.
- About the same amount of SQL as the recommended design (about 200 lines), but more test changes: every test that makes a flight and checks a chart straight away must either make it older than the quiet period or set the quiet period to zero.

**What could go wrong, side by side with the recommended design.** Both keep a stored table that could disagree with the reference merge. Both are checked the same way and repaired the same way. The question is how each can go wrong.

| | Write once when final (charts read only stored rows) | Re-check the flights of active pilots every 5 minutes (DESIGN_89 / my Part 1) |
|---|---|---|
| How it could be wrong | (1) A flight is never written down: the write-down step missed it, or the updates were stopped for longer than it looks back. The charts then undercount, silently. (2) A flight is written down too early, if the quiet period is set or coded wrong: a reconnect is counted twice. (3) A later data fix (like the 08-10-2026 duplicate-session repair) does not reach rows already written. | (1) A pilot is missed by an update (a code bug): that pilot's flights go stale until their next update. (2) The updates stop for more than 12 hours: flights from that gap stay stale until finalisation or a rebuild. A half-done update cannot happen: each update is all-or-nothing. (3) A bug in the update code rewrites the whole history of every active pilot, every 5 minutes. |
| How far damage can spread | Only new flights. Old rows are never touched. | Every flight of every pilot active that day, old ones included. |
| How it is detected | A nightly comparison with the reference merge, limited to flights older than the quiet period, plus "every piece older than 4 hours belongs to a stored flight". There are a few expected differences (a pilot who comes back on the same connection with a new plan, see below), so the check must allow for them. | A nightly comparison with the reference merge over all flights. Zero differences expected (exact ties excepted), so any difference is a bug. A simpler, stricter check. |
| How it is repaired | Delete that pilot's rows and write them again from the pieces table (one function call); or a full rebuild. | The same. |
| Effect after restoring a backup | None: the dump is one consistent moment. | The same. |

**On the boundary risk I raised before.** With no stitching, the "counted twice or missed at the join" risk is gone. What remains is the same kind of risk in both designs: the stored table drifting from the reference merge. Write-once can only drift on new flights. The 5-minute design can drift on any flight of an active pilot, but its drift is easier to spot, because the expected difference is exactly zero.

**Pilot comes back on the same connection hours later with a new plan.** For example, a pilot leaves Australian airspace and comes back with a different destination. Today's view moves the whole connection, old part included, to the new airports. Write-once keeps the old part under the old airports and makes the new part a new flight. The docs say "a changed plan is a different flight, as in v1" (ADR-023), and v1 grouped each sample by the plan it had at that moment. So **write-once is closer to v1 and to the written rule.** Today's behaviour here is a side effect of storing the plan per connection. It is rare.

**Collector outage.** If the collector stops, nothing is in progress and today every flight counts at once. With write-once, flights that ended before the outage are written down when the collector restarts and its first update runs (as long as the look-back covers the outage; the look-back should be days, not hours, to make that safe).

**Can the "final" flag be shared?** No. `flight_summary.final` means "this connection's raw positions may now be deleted" and is set 90 (soon 180) days later, just before positions are dropped. The update skips final connections, so setting it after 3 hours would stop a returning connection's new pieces from ever being summarised. Write-once needs its own record of what was written down; the stored rows themselves can be that record.

**Combining the two** (stored rows when final, today's live merge for the last 3 hours, joined together) would give speed with no lag. But it brings back the joining step, and the 5-minute design already gives no lag with one code path. Not worth it.

**Plain verdict.** Both designs are equally fast and about the same size, and both are safe if built carefully. Write-once is simpler to reason about, cannot damage old flights, and matches v1 and the written plan-change rule; its price is that flights reach the charts up to 3 hours after they end, plus more test changes. The 5-minute design keeps today's behaviour exactly, with a stricter check. The choice is yours: it turns on whether that 3-hour delay is acceptable.

#### Technical detail

- **Premise correction.** Every panel reading `flight` filters `NOT is_in_progress`, and all the others read `flight_ext`, `movements` or `cohorts`, which filter it. A script over `grafana/dashboards` found no panel that needs in-progress rows from `flight`. LIVE panels read `pilot_online_now`, `pilot_latest` and `network_history`. So `flight` can be "stored rows only" with `is_in_progress` false for them, and no union is needed.
- **Write-down step** (inside the refresh, after the pieces update, under the same advisory lock):
  - For pilots with a session whose `last_seen` is between now minus (Q plus a look-back L) and now minus Q, run the reference merge over pieces not yet inside a stored flight. A piece is stored when its session is in a stored row's `session_ids` and the piece lies inside that row's time range.
  - Insert, with `ON CONFLICT DO NOTHING`, the flights all of whose sessions have `last_seen` older than now minus Q.
  - Append-only. About 60 lines. L should be several days, so a refresh outage cannot leave flights unwritten, with a nightly "unrecorded piece older than Q plus 1 h" invariant as the backstop.
- **Safety of Q.** A new piece B can join a flight F only if B.start - F.end <= 120 min. When the write-down runs in the same transaction as the pieces refresh, any session first seen before that transaction is already in `flight_summary`, and any later one starts after the write-down time, which is at least F.end + Q. So any Q > 120 min is safe. 3 h leaves margin for clock and poll jitter. Parts of one session are never joined (the lag-row rule), so a session that comes back after Q starts a new flight in both the reference merge and write-once. The only difference is which airports the old part keeps, if the plan changed.
- **Stored columns.** Plan and pilot fields can be frozen at write-down time (v1 style). That removes the join to `pilot_session` and makes the rows fully immutable. Or they stay live via `last_session_id` (identical to today). Freezing is simpler; it differs from today only for the returning-session case.
- **Measured costs carry over.** The table size (273 MB with indexes at 5 years), the panel times (0.2 to 0.4 s), the pushdown and the index needs are the same as in 1.2 and Part 2, because it is the same stored table. The write cost is lower: about 700 inserts a day, against an upsert read of about 9,300 flights every 5 minutes (about 0.2 s).
- **Tests.** `tests/integration/helpers.py` and the panel tests make flights that end "now"; they need Q set to 0 through an `app_setting` key, or older fixtures. Reconnect tests still work with Q = 0 because both pieces exist before the refresh runs.
- **Rollback and restore** are the same as the 5-minute design: pre-deploy dump; forward migration restoring the old body; the table is in the dump.

### 1.7 Can only a pilot's latest flight change? (owner's point, checked against the rule)

#### In plain English

The owner's point: a pilot can only be on one flight at a time. So when a new session arrives, only that pilot's latest flight can change, and all earlier flights are final. If that is true, each 5-minute update would only need to look at one flight per active pilot.

**Checked against the real merge rule, it is nearly true but not exactly.** The rule does not join "the pilot's previous session". It joins sessions with the **same pilot, callsign, departure and arrival** when the new one starts within 120 minutes of the end of that group's earlier sessions. Another flight in between does not stop the join. Example:

| Time (UTC) | Session | Plan | Flight it belongs to |
|---|---|---|---|
| 09:00 to 10:00 | A | QFA1, Sydney to Melbourne | flight 1 |
| 10:10 to 10:20 | B | QFA1, Sydney to Brisbane (a wrong plan) | flight 2 (a different route, so a different flight) |
| 10:30 to 11:30 | C | QFA1, Sydney to Melbourne again | joins **flight 1** (30 minutes after A), not flight 2 |

So session C changes flight 1, which was no longer the pilot's latest flight. A plan change can do the same. When a session's plan changes, the whole session moves to the new route, and it can then join an earlier flight on that route.

**What is exactly true:** a new or changed session can only change two things: its own flight, and flights of the same pilot that ended **within 120 minutes before that session started**. In practice that is the latest one or two flights. Everything older is final.

**Two connections at once.** VATSIM allows one connection per CID. v3 has seen overlapping sessions only once: the duplicate sessions of 08-10-2026 (ADR-021), which are now prevented at source. If two sessions with the same pilot, callsign and route did overlap, the rule would join them into one flight, because an overlap counts as a gap under 120 minutes. If they had different callsigns or routes, they would stay two flights at the same time. I could not check the live data for overlaps in this task; `ops/check_identity_flap.sql` is the existing check.

**Does this change the design?** A narrower update is possible. For each active pilot, re-check only the flights that ended after a cut-off: the earliest start of that pilot's new or changed sessions, minus 120 minutes.
- That reads one or two flights per active pilot instead of all of that pilot's flights. At 5 years, that is about 500 to 1,000 flights per update instead of about 9,300.
- It saves at most about 0.2 s every 5 minutes.
- It is exact only if a pilot's sessions never overlap in time.

So I keep the simpler "all flights of active pilots" for the first version: it is exact in every case and measured at 0.2 s. The narrower version is a ten-line change for later, if update time ever matters.

It also does not change how many rows are written. With "write only rows that changed", an update writes only flights whose numbers or sessions changed. That is the same set either way: about one flight per connected pilot.

**Effect on "write once when final".** The owner's point is the reason write-once works. A flight is final once no new session with the same pilot, callsign and route can start within 120 minutes of its end, and the 3-hour quiet period guarantees exactly that. It does not make write-once simpler than described in 1.6, which already looks only at the recent flights of recently active pilots.

#### Technical detail

- The rule (migration 130, `flight` view): the window is `PARTITION BY cid, callsign, departure, arrival ORDER BY start_time, session_id, gap_segment, span_part`. `is_new = 0` when all three hold: `prev_end` (the running max of `end_time` over earlier pieces in the partition) exists, the lag piece belongs to a different session, and `start_time - prev_end <= 120 min`. A different callsign or route is a different partition, so pieces from other groups never block a join.
- Bounded recompute: delete and recompute the pilot's stored flights with `end_time >= T0`, where T0 = min(start of touched pieces) - 120 min, using the pieces of those flights plus the touched pieces.
  - It equals the full computation when pieces within each group do not overlap in time, because the running max then equals the previous piece's end.
  - With overlapping sessions, an earlier and longer piece could carry a later `prev_end` than the subset sees, so the result could differ. The whole-pilot recompute has no such condition.

### 1.8 Option (c), "v1-faithful": finished flights written straight from the raw data

**Note on 1.6.** Option (b) in section 1.6 keeps today's pieces table (`flight_summary`), so its stored flights can be rebuilt from the pieces and compared with the old merge logic. That is not true of the option below. Everything said about rebuilding from `flight_summary` applies to (a) and (b) only.

#### In plain English

**The idea.** This is what v1 did. There is no pieces layer. When a flight is complete (after the quiet period), one process reads the sessions and the raw positions and writes ONE finished row for that real flight. That row is the permanent record: the "flight summary" in v1's sense.

**1. It becomes the only record.** Raw positions are deleted after 90 days (180 once ADR-026 is applied). After that, a finished row cannot be recreated from anything. It stops being a copy and becomes the original.
- **Backup.** It is as precious as `flight_summary` is today. It is in the nightly dump already, but the restore test must now check it properly: row count, no overlapping flights for one pilot, totals per month.
- **Retention (ADR-026, #21).** Keep forever, like sessions.
- **A bug in the writing process.** Flights still inside the raw window can be rewritten. Older ones are wrong for good.
- **What we lose for good.** Today, if the reconnect rule ever changed (say 120 minutes became 60), every flight in history could be re-merged from the pieces. With (c), flights older than the raw window keep the old rule for ever.

**2. What can be checked each night.** Only flights inside the raw window can be recomputed from raw positions and compared. Older flights get sanity checks only: no gaps or overlaps for one pilot, and monthly totals that do not move. During the changeover, the old view can run beside the new process for a few weeks, and every difference can be listed. After the pieces are gone, that comparison is gone too.

**3. What happens to today's parts.**
- **The pieces table and the old view.** The migration converts once: every flight whose sessions are all quiet past the quiet period is written from the old merge logic. This is exact, because the pieces still exist for all history. Then the pieces table is either kept read-only as a legacy table for the changeover, or dropped. Dropping is irreversible and loses re-merging.
- **The `final` flag and the partition drop.** Writing the finished row would replace finalisation. But the drop needs a new rule, because sessions with no flight plan never become flights, and today's rule ("every session in the partition has a final summary") covers them too. That needs a new marker on the session, for example "finished at". This touches `drop_old_pilot_position_partitions`, the function that deletes raw data for ever. That is High tier.
- **The 5-minute update.** `refresh_flight_summary` becomes a "finish flights" job. It can keep its name and its 5-minute timing, so the collector's Python does not change (it is at 1,790 of 1,800 lines). Its proven code for airborne time and the 120-minute and 8-hour splits would be rewritten in a new form. Those rules would now live inside the one process that also merges sessions.
- **The views.**
  - `flight_part` goes, or is redefined.
  - `flight` becomes a thin view over the finished rows, with nothing ever in progress.
  - `flight_ext`, `movements` and `cohorts` are unchanged.
  - **Monday attendance** reads pieces today, so it must be rewritten. It would then count a pilot as present across a reconnect gap, and it would appear only after the quiet period. That is a visible change.
- **Checks and grants.** The two watchdog integrity checks on `flight_summary` must be rewritten. So must the `watchdog_ro` grant in `migrate.sh`, and the audit scripts.
- **Tests.** The helper and its 7 test files must insert finished rows instead of pieces. About 13 further test files that name `flight_summary` (summary, partition drop, integrity, migration, roles and others) must be rewritten as well.

**4. Names.** In this option the finished table really is "the flight summary" in v1's sense, and nearly all 187 references in 56 files have to be revisited anyway. So the cost argument against renaming mostly disappears. The safety argument does not. During the changeover, and in the 14 days of kept dumps, an unchanged script reading `flight_summary` could silently read the wrong kind of row: the two share column names such as `session_id`, `start_time`, `end_time` and `sample_count`. My advice for (c):
- name the new table `flight_record`;
- rename the old one to `flight_summary_legacy` for the changeover;
- only once the legacy table is gone and every reference has been changed, rename `flight_record` to `flight_summary` in a separate migration, if the owner still wants that name.

**5. Compared with (a) and (b).**

| | (a) 5-minute update of a stored copy | (b) write once, pieces kept | (c) v1-faithful, from raw |
|---|---|---|---|
| Chart speed at 5 years | 0.2 to 0.4 s | same | same |
| Delay before a flight is counted | about 1 to 6 minutes (as today) | up to 3 hours | up to 3 hours |
| Size of the change | about 220 SQL + 170 test lines | about the same, more test edits | largest: about 400 or more SQL lines, about 20 test files, watchdog, grants, Monday attendance, partition drop |
| Touches the raw-data drop | no | no | yes (High tier) |
| Can old flights be rebuilt or re-merged? | yes, from the pieces | yes, from the pieces | only inside the raw window |
| Nightly check | exact, all history | exact for finished flights | raw window only |
| Rollback | old view back, nothing lost | same | easy only while the legacy table is kept; impossible after it and the raw data are gone |
| Storage at 5 years | pieces + copy (about 270 MB more) | same | one table (saves about that) |

**Plain verdict.** (c) is the most faithful to v1 and stores the least. But it is the biggest and riskiest change: it rewrites proven code, touches the function that deletes raw data for ever, changes Monday attendance, and gives up the ability to re-check or re-merge old flights. Its only gain over (b) is one table instead of two and about 270 MB at 5 years. Not recommended.

---

## Part 2. Review of docs/DESIGN_89.md (branch design-89, read after Part 1 was committed)

### 2.1 Overall

DESIGN_89 (option A) and my own answer reached the same architecture independently: a stored table of merged flights, the old merge body kept as a reference view, recompute grouped by pilot (CID) inside `refresh_flight_summary_sessions` under its advisory lock, `flight` as a thin view with live plan fields via `last_session_id` and live `is_in_progress` via `session_ids`, highest session id as the tie-break, no Python and no panel change. I **agree** with its core and with its reasons for rejecting B (merged columns on `flight_summary`) and C. Below: where I add evidence, and seven points where I disagree or find a gap.

### 2.2 Where I agree, with evidence the design did not have

| Design claim | My evidence |
|---|---|
| "A cid predicate should push below the window functions; confirm with EXPLAIN" (its open check 1) | Confirmed: the filter reaches an index scan on `pilot_session_identity` through all three window passes; 477 active pilots, full history, 130 ms at 5 years. |
| "NOT measured: the join to `pilot_session` for the live plan fields" (open check 2) | Measured with the join, the online-now lookup and the full `flight_ext` / `movements` stack: 0.2 to 0.4 s for the 30-day and 6-month panels at 5 years, 1.8 s for an all-time monthly panel. The join does not shrink the gain. |
| Per CID, not per group | Agree; it also makes plan changes, gate changes and part moves correct with no old-key tracking. Heaviest pilot (2,226 flights): 29 ms. |
| Equivalence by construction | Confirmed: all 32 columns identical on 978,342 of 978,357 flights; the 15 others are exact ties on `end_time` where the old pick is arbitrary. `is_in_progress` identical for all 211 online flights; `movements` identical. |
| Option C "stored narrow chain key": "not measured" | Measured: 2.7 s at 5 years (16.9 s with the home-airport test), still linear. Rejection confirmed. |
| B is the riskier place (retention-critical table) | Agree; the strongest argument in the design. |

### 2.3 Where I disagree, or the design has a gap

1. **Delete-and-reinsert churns the table; write only what changed (DISAGREE, small).** At 5 years the pilots seen in 12 hours own about 9,300 flights; delete-and-reinsert rewrites all of them every 5 minutes, about 2.7 million dead row versions a day across a ~1M-row table and its indexes, when only a few change (measured on static synthetic data: 0 after an idle run, 1 after a plan change; on live data, about one flight per connected pilot, the ones whose sessions are still growing). Use the pattern `refresh_flight_summary_sessions` already uses: upsert with `WHERE row IS DISTINCT FROM EXCLUDED`, then delete keys that no longer exist. Same correctness and lock, about 15 more SQL lines, measured 160 to 250 ms.
2. **Indexes on (cid) and (end_time) are not enough (GAP).** Panels also filter on `start_time` (the departure half of `movements`, busiest days by flights) and `logon_time` (RAAFv flights, busiest days by pilots, onboarding and management panels). Measured at 5 years without those two indexes: busiest airports 1.3 s instead of 0.34 s, RAAFv flights (7 days) 905 ms instead of 11 ms, both linear again. Add `(start_time)` and `(logon_time)`.
3. **Existing tests will break, and the design does not say so (GAP).** `tests/integration/helpers.py` (used by seven panel test files: events, military, online hours, panel rules, pilots people, time zones, watchdog flap) INSERTs `flight_summary` rows directly and never calls the refresh. With a stored table those flights do not appear in `flight` until it is rebuilt. The helper must call the rebuild (one line). It belongs in the plan and the size estimate.
4. **The migration's locking is not specified (GAP).** (a) The backfill must take the refresh advisory lock (`pg_advisory_xact_lock(hashtext('vatpac.flight_summary_refresh'))`), or a 5-minute refresh during the deploy can interleave with it. (b) `CREATE OR REPLACE VIEW flight` needs an exclusive lock on the view: it waits for running panel queries (up to 30 s each; the dashboards are now public, ADR-029) and every new panel query queues behind it. Set a `lock_timeout` (for example 5 s) in the migration so a busy moment fails the deploy cleanly instead of stalling the dashboards; `migrate.sh` sets none.
5. **The `is_in_progress` expression has a trap (GAP, caught by existing tests).** `session_ids && (SELECT array_agg(...) FROM online_now ...)` is NULL when `online_now` is empty (collector stale); `NOT is_in_progress` is then NULL and `flight_ext` would hide EVERY flight. It must be wrapped in `COALESCE(..., false)`. `test_stale_collector_does_not_hide_flights` and `test_nothing_is_in_progress_when_the_collector_is_stale` would catch a mistake; a builder warning, not a design change.
6. **"Live, as today" is slightly overstated.** Name and plan fields are live, but departure and arrival are **stored** (they are the merge key), so they lag a plan change by up to one refresh. The flight is in progress then, so no counting panel sees it; the design and the data dictionary should say so.
7. **Stop-gap materialised view "refreshed hourly" (DISAGREE, minor)** — `refresh_materialized_views()` runs only in the daily maintenance; hourly needs a collector (Python) change. The only no-code emergency lever is raising `grafana_ro`'s `statement_timeout`. And on **rollback**: dbmate down steps are not run in deploys; the real rollback is the pre-deploy dump or a forward migration restoring the old body (possible because the columns are unchanged). The deploy plan should name the dump.

### 2.4 The other review topics

- **Reconnect chains (within, exactly at, after 120 minutes)**: inherited unchanged from the reference body; the design's test list covers them. Agree.
- **Plan changes**: correct with the recompute grouped by pilot (with the wording in 2.3 item 6).
- **In-progress flights**: agree (live via `session_ids`, with the COALESCE).
- **Flights spanning the position drop and finalisation**: the hook sits in the worker that finalisation calls, so merged rows are recomputed in the same transaction; drops never touch the new table; no drop safety check changes. About 0.2 s more per finalised partition, inside the lock window already noted in #112 item 5. Agree.
- **Session deletes and ADR-026 / #21**: agree with "delete by CID and rebuild"; write it into ADR-026 item 2 as a rule; no foreign key (a cascade would delete a whole merged flight when one piece goes).
- **Duplicates**: the key is the first piece's key, unique by construction; ADR-021 twins are merged at source; the upsert cannot create duplicates.
- **Race with the 5-minute refresh and the collector**: one writer (advisory lock); readers see before or after (one transaction); a plan change committed during a refresh is picked up by the next, because the session stays in the 12-hour window. Agree.
- **Migration and backfill on the live database**: milliseconds today (about 1,100 sessions), about 7 s at 5 years on this PC; lock points in 2.3 item 4.
- **Backup and restore**: an ordinary table in the dump, consistent with `flight_summary` because a dump is one snapshot. The design's restore check (`EXCEPT ALL` against the reference view) is right; at 5 years several seconds on the VM, acceptable weekly, or limit it to a sample of pilots.
- **Role grants**: no `roles.sql` change. Note that `roles.sql` grants SELECT on every view, so the reference view becomes readable by `grafana_ro`; harmless (public dashboards cannot run free SQL), but it is the slow query, so `validate_dashboards.py` should refuse it in a panel.
- **Effect on the 35 panels**: no SQL change; at 5 years from 8 to 30 s (or cancelled) to well under 1 s, all-time panels about 2 s. The 15-in-a-million tie rows may show a different aircraft or name.
- **Smallest sufficient change?** Yes, once items 1 and 2 are added (a few lines each).
- **Test adequacy**: the design's six test groups are right. Add: the helper change (item 3); a session gaining or losing the flight gate (plan filled in later) moves into and out of `flight` after one refresh; and "a second refresh with nothing changed writes 0 rows", which catches churn regressions.

---

## Part 3. Verdict

**Build a stored table of merged flights read by a thin `flight` view. Two designs qualify, equally fast and about the same size, and the choice is one owner question (3.3 item 1): (a) DESIGN_89 option A with the amendments above (write only changed rows, two more indexes, test helper, migration locking, COALESCE), which keeps today's behaviour exactly; or (b) write once when final with a 3-hour quiet period (section 1.6, revised), which cannot damage old flights and matches v1, at the price of a 3-hour delay before a flight is counted. Only flights that ended within 120 minutes before a new session can change (1.7), so either design touches only recent flights; for (a) the first version still re-checks all flights of active pilots, because that stays exact even if sessions overlap (0.2 s per update). My default if the owner has no preference: (a), because it changes nothing users see and its nightly check expects exactly zero differences. Do not extend `flight_summary`, do not use a materialised view, and do not build (c), the v1-faithful version that writes finished flights straight from raw positions and retires the pieces table (section 1.8): same speed, but the biggest change, it touches the raw-data drop and loses the ability to rebuild or re-merge old flights.** (Revised 11-10-2026: the first version rejected write-once on a wrong premise, see 1.6.)

### 3.1 Smallest first version (one migration, no Python, no roles.sql, no panel change)

1. Reference view = today's `flight` body plus `last_session_id` (latest piece, ties by highest session id) and `session_ids`.
2. Table of merged flights, key = first piece; indexes on `cid`, `start_time`, `end_time`, `logon_time`.
3. A function that recomputes all flights of the given pilots (CIDs) from the reference view and writes only rows that changed, deleting vanished keys; a full-rebuild wrapper for repairs, tests and restores.
4. One added call at the end of `refresh_flight_summary_sessions` for the cids it processed.
5. `flight` replaced by the thin view (same 32 columns), `is_in_progress` wrapped in COALESCE.
6. Backfill under the refresh advisory lock, with `lock_timeout` set.
7. Tests: helper calls the rebuild; equivalence both ways on `flight`, `flight_ext`, `movements`, `cohorts`; incremental cases; finalisation; idempotence with zero writes. Data dictionary, a short ADR, a line in ADR-026.

About 220 SQL lines and 170 test lines. Medium tier by the table (existing columns identical), but deploy it like High: rehearse on a restore of a live dump, fresh dump first.

### 3.2 Checks before building

1. Repeat the pushdown EXPLAIN on **PostgreSQL 16** (I ran 17.5; production and CI are 16). The recompute grouped by pilot depends on it; without it the maintenance would read all history every 5 minutes.
2. Run the equivalence comparison on a restore of a recent live dump (real reconnects, real ties), on a private local database.
3. Time the slowest panels at 5 years as `grafana_ro` with its limits, ideally under a 1 CPU cap.

### 3.3 Owner decisions

1. **The main question: may a finished flight appear in the charts up to 3 hours after it ends (today about 1 to 6 minutes; v1 8 hours)?** Yes: build write-once when final (1.6). No: build the 5-minute design (3.1). If (b) is chosen, 3.1 changes to: append-only write-down step instead of the upsert, quiet period as a setting, a nightly "no unrecorded piece older than 4 hours" check, test fixtures older than the quiet period; the indexes, locking and helper changes stay.
1a. **Tie-break**: when two pieces of one flight end in the same second, take the plan and name fields from the session with the higher id (the later one created). Today the answer is arbitrary; about 15 flights per million. Recommend yes.
2. **Names** (Part 4): name the new objects clearly now; do not rename `flight_summary` in this change.
3. **When to build**: no timeouts for about three years, but the dashboards are public (ADR-029), so each visitor's page load runs about ten of these queries on one CPU, and at one year of history (mid-2027) the airport and route panels take 7 to 12 s each. Recommend building in the next few months, while the backfill is trivial.
4. Folded into item 1 above (revised 11-10-2026).

### 3.4 What I could not verify

- PostgreSQL 16 behaviour (tested 17.5), the 1 CPU VM's speed, and timings as `grafana_ro` with its limits (I ran as the owner role).
- Real data: synthetic only; no live dump used.
- Concurrency under load (dashboards, collector and refresh together) and the actual lock wait of `CREATE OR REPLACE VIEW` on the live system.
- The integration suite: nothing was built, so no test was run.

---

## Part 4. Naming (owner's question)

(For option (c), where the finished table replaces the pieces table, the naming advice is in section 1.8 item 4.)

### 4.1 Proposed names for every object in the recommended solution

| Object | Name today | Proposed | What it is, in plain words |
|---|---|---|---|
| Per-piece table | `flight_summary` | keep now; `flight_part_summary` later if wanted | one row per connection piece (session, gap segment, 8 hour part); the source of truth that gates raw-position drops |
| Per-piece view | `flight_part` | keep | the pieces with plan fields and the flight gate |
| Old merge logic | body of `flight` | `flight_merge_reference` (design: `flight_chain`) | the definition of how pieces become flights; used to build and check the stored table, never by a panel |
| New stored table | new (design: `flight_merged`) | `flight_record` | one stored row per real flight, after merging reconnects |
| Public view | `flight` | keep | what every panel reads |
| New functions | new | `refresh_flight_record(cids)`, `rebuild_flight_record()` | keep the stored table current; full rebuild for repairs |

### 4.2 Why the new table should not be called `flight_summary`

Reusing a name for a different meaning is the riskiest kind of rename. The old and new tables share column names (`session_id`, `start_time`, `end_time`, `sample_count`), so any script, runbook, watchdog check, audit query or person working from an older document would still run against the reused name and get **wrong answers without any error** (for example the watchdog's "sample_count differs from stored positions" check would compare a whole flight with one piece). The 14 days of kept dumps hold `flight_summary` with the old meaning. A new, unused name cannot be confused with anything.

### 4.3 What renaming today's `flight_summary` to `flight_part_summary` would cost

`flight_summary` appears 187 times in 56 files. To change:

- **Database code**: the bodies of `refresh_flight_summary_sessions`, `refresh_flight_summary` and `drop_old_pilot_position_partitions` (function bodies name tables as text, so they fail at their next call unless replaced in the same migration), and the views `flight_part` and `monday_attendance` (views follow a rename automatically, but their text and the docs change). The drop function decides when raw positions are deleted for ever, so any edit to it is High-tier work.
- **Grants**: `ops/migrate/migrate.sh` grants `watchdog_ro` SELECT on `flight_summary` by name on every deploy; it must change in the same deploy or the next deploy fails. Grants make the rename High tier.
- **Operations**: `ops/audit/invariants.sql` and four other audit scripts, the watchdog check names and messages (`ops/watchdog/messages.py`), the scale generator, the restore drill notes.
- **Tests**: about 20 test files, plus the data dictionary test that ties table names to the document.
- **Documents**: DATA_DICTIONARY, ARCHITECTURE, SPEC, CHART_DEFINITIONS, runbooks; old ADRs keep the old name (they are history) with a note.
- **Live database**: `ALTER TABLE ... RENAME` is instant but takes a brief exclusive lock on a table the refresh uses every 5 minutes; index and constraint names (`flight_summary_pkey`, the foreign key) should follow. Restoring a dump from before the rename still works, because the migration is re-applied.

Rough size: a migration of about 150 lines (mostly re-creating three function bodies with only the name changed), about 60 file edits, High tier, a rehearsal.

### 4.4 Recommendation

**Keep today's names for existing objects and give the new objects clear names now. Do the `flight_summary` rename later as its own change, or not at all.** Putting it into the #89 migration would roughly double the change, move it from Medium to High tier and touch the irreversible partition-drop function for a cosmetic gain, and it would make the behaviour-preserved proof for #89 harder to read. If the owner still wants it, do it as a separate migration after #89 has been live and stable for a few weeks, and never reuse `flight_summary` for the merged table. Meanwhile the database comment and the data dictionary entry for `flight_summary` should say plainly "one row per piece of a flight, not per flight".
