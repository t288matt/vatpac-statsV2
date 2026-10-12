# Final design for issue #89: flights written once, when finished

Written 11-10-2026 (Opus 5.5, branch `opus-89`). Design only: nothing built, nothing run on the VM. It replaces the "(a) now, (c) later" advice in `docs/OPUS_REVIEW_89.md`, following the owner's decisions of 11-10-2026:
- a delay of 3 hours or more before a flight is counted is fine;
- losing existing data is acceptable;
- the logic must be right and the code simple now, not patched later.

The measurements behind it are in OPUS_REVIEW_89.md Part 1.

## Plain English summary

- **What changes.** Today the system stores small pieces of each connection and glues them into flights every time a chart opens. That is why charts slow down as history grows. In the end state, a flight is **written once, as one row**, three hours after the pilot was last seen, straight from the raw positions. It is never changed again, and charts read those rows directly.
- **What goes.** The pieces table, the two refresh functions, the "final" flag, the piece view and the in-progress machinery are all deleted. What remains is one table of flights, one writer function, one marker on each session saying "positions up to here are already in flights", and a simpler rule for deleting old raw data.
- **No wipe needed.** No raw positions have been deleted on the live system yet. So the new writer can rebuild every flight since go-live by itself on its first runs. Nothing is lost and there is no conversion step. A wipe is possible but not recommended (section 5).
- **Do I still disagree?** Not with the end state. My only reservation was rewriting proven code. This design copies the proven split and merge SQL word for word into the writer, and checks the writer against today's view on the same data before anything is deleted. With that, the owner's choice is sound.
- **Balanced verdict (section 0):** (c), this design, over (b) (Sonnet's preference, keep the pieces) and (a). The fallback, if the drop gate cannot be ready before December 2026, is to ship phases 1 and 2 only.
- **Owner decisions:**
  1. Monday attendance: count a pilot as present through a short reconnect gap? Recommended: yes.
  2. Write flights 3 hours after the pilot was last seen, not after each separate connection. Recommended: yes.
  3. Keep the data, no wipe. Recommended: yes.
  4. The order and timing of the four phases. They must be live before about 06-01-2027.

---

## 0. Balanced recommendation among (a), (b) and (c)

**Words.**
- A *pilot* is one person (CID). A pilot is on one flight at a time and has many flights over time.
- A *session* is one connection (CID, callsign, logon time).
- A *flight* is what the charts count: one or more sessions of the same pilot, callsign and route, joined across reconnects of up to 120 minutes.

**The owner's guidance, as weighed here (11-10-2026).**
1. One clean end state and few migrations, not a short-term fix.
2. Simple code and clear logic first.
3. A 3-hour or longer delay is fine (v1 waited 8 hours).
4. Losing data is acceptable.
5. Working, tested code is a big bonus.
6. No raw positions have been dropped yet, and the finalisation gate has never run live.
7. Panels never show connected flights.
8. Pilot, session and flight are kept distinct.
9. A table called `flight_summary` should be the final record of a merged flight.
10. v1 wrote one summary after the flight finished and never touched it again.

| | (a) stored copy, re-checked every 5 minutes | (b) written once after 3 hours, pieces kept | (c) written once from raw positions, pieces deleted (this design) |
|---|---|---|---|
| End state | pieces table + merge view + stored copy + refresh | pieces table + merge view + finished table + refresh + writer | **one finished-flight table + one writer** |
| Matches v1 (10) and the naming wish (9) | no (rows are rewritten; `flight_summary` stays the pieces) | written once, yes; name only after an expensive rename | **yes, both** |
| Proven code kept (5) | all | all | the split, airborne and merge SQL kept word for word; the refresh wrapper, finalisation step and pieces table deleted |
| New logic | small update function | write-once step on top of the refresh | writer (moved SQL) + simpler drop gate |
| Riskiest part | none new | none new | the drop gate (deletes raw data for ever) |
| Migrations | 1 | 1 | 4 small phases, ending in one clean state |

**Recommendation: (c).** This changes my last report, which recommended (a). That report weighed reuse above everything else. Under guidance points 1, 2, 9 and 10 together, (c) is the only option that ends with one clear rule ("a flight is written once, 3 hours after the pilot was last seen, and never changed"), one flight table, and the name `flight_summary` meaning what the owner expects.

**The strongest argument against (c), and why it does not win.** (c) replaces working, deployed code with new code at the most dangerous point, the gate that deletes raw positions for ever, and it has a deadline (the first drop, about 06-01-2027). (b) gives the same write-once behaviour and the same delay while touching none of that code. Sonnet, the design agent, independently preferred (b) for exactly this reason, and the argument is real. It does not win, for three reasons:
- The reuse advantage is smaller than it looks. The finalisation gate has never run live; its proof is CI only. And the SQL that matters (splitting, airborne time, joining) is reused word for word in (c).
- (b) leaves two flight stores and two kinds of "final" (the pieces' `final` flag and the written flights) for ever. That is the opposite of guidance points 1 and 2.
- (b) still needs the expensive rename to meet point 9, because its pieces table holds the name `flight_summary`.

Nothing in Sonnet's reasoning changes this verdict. It does set the fallback: if phase 3 (the drop gate) cannot be built, reviewed and rehearsed before about December 2026, ship phases 1 and 2 only. That is effectively (b): the old finalisation stays in charge of drops, and phases 3 and 4 follow later.

**Naming (guidance point 9), revised.** The finished table is built as `flight_record` in phases 1 to 3, while the pieces table still holds the name. Phase 4 drops the pieces table and renames `flight_record` to `flight_summary` in the same migration, together with every script, check and document that names it. My earlier objection was that an unchanged script might read the wrong kind of row. That falls away once nothing old is left to read the name, and once the shared-name watchdog checks are rewritten in that same phase. Old dumps restore, then migrate in order. ADRs before ADR-030 keep the old meaning, with a note.

---

## 1. End-state data model

### 1.1 What exists afterwards

| Object | Kind | Why it is kept or added |
|---|---|---|
| `pilot_session` | table, unchanged plus one column `flown_upto timestamptz` | Sessions are the identity of a connection. `flown_upto` = "this session's positions up to this time are already in written flights". It is the only bookkeeping the writer and the drop rule need. |
| `pilot_position` | table, unchanged | The raw data the writer reads. Partitions are dropped by the gate in section 3. |
| `flight_record` | **new table** | One row per finished flight, written once. The only flight store. |
| `write_finished_flights()` | **new function**, SECURITY DEFINER | The writer (section 2). Replaces `refresh_flight_summary` and `refresh_flight_summary_sessions`. |
| `drop_old_pilot_position_partitions()` | function, kept, gate replaced | The lock-friendly drop mechanism (migration 170) stays as it is. Only "finalise and check `final`" becomes "check `flown_upto`" (section 3). |
| `flight` | view, rewritten as a thin view over `flight_record` | Grafana reads views only (ADR-017), and every panel and `flight_ext` reads `flight`. |
| `flight_ext`, `movements`, `cohorts` | views, re-created | They lose the `NOT is_in_progress` filter. Their columns are otherwise unchanged. |
| `monday_attendance` | view, rewritten | Reads `flight_record` instead of pieces (section 4). |
| everything else (controllers, snapshots, poll log, lookups, `online_now`, `pilot_latest`, `pilot_online_now`, archive) | unchanged | Not part of this problem. |

### 1.2 What is deleted

- table `flight_summary` (the pieces), with its `final` flag, its indexes, its foreign key and the placeholder rows of migration 20;
- functions `refresh_flight_summary(interval)` and `refresh_flight_summary_sessions(bigint[], boolean)`;
- view `flight_part`, and the merge body of `flight` (it moves, unchanged, into the writer);
- the columns `gap_segment`, `span_part` and `is_in_progress` of `flight` (no panel reads the first two; the third is always false once only finished flights exist);
- the `NOT is_in_progress` filter in `flight_ext` and in the 11 panels that carry it;
- `tests/test_in_progress_panels.py`, `test_flight_in_progress.py`, `test_exclude_in_progress.py` and `test_flight_summary.py` (replaced by writer tests);
- the "In-progress flights" section of CHART_DEFINITIONS.md (it becomes one sentence: only finished flights exist).

### 1.3 The table

```sql
CREATE TABLE flight_record (
    session_id       bigint      NOT NULL,   -- first session of the flight (kept name: panels use it as the flight id)
    start_time       timestamptz NOT NULL,
    end_time         timestamptz NOT NULL CHECK (end_time >= start_time),
    cid              bigint      NOT NULL,
    callsign         text        NOT NULL,
    departure        text        NOT NULL,   -- upper(btrim), the flight gate passed
    arrival          text        NOT NULL,
    session_ids      bigint[]    NOT NULL,   -- every session in the flight: for repairs and audits
    logon_time       timestamptz NOT NULL,   -- earliest logon of its sessions
    name text, home_icao text, server text, aircraft_icao text, aircraft_faa text, flight_rules text,
    route text, remarks text, planned_alt text, deptime text, pilot_rating int, military_rating int,
    airborne_minutes numeric     NOT NULL,
    max_gs_kt        int,
    max_alt_ft       int,
    sample_count     bigint      NOT NULL,
    written_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, start_time)
);
CREATE INDEX ON flight_record (cid);
CREATE INDEX ON flight_record (start_time);
CREATE INDEX ON flight_record (end_time);
CREATE INDEX ON flight_record (logon_time);
```

**Why each index.** Panels filter on `end_time` (most), `start_time` (`movements` departures, busiest days by flights) and `logon_time` (RAAFv, busiest days by pilots, onboarding). Without the last two, those panels went back to scanning everything (measured: 1.3 s and 905 ms instead of 0.34 s and 11 ms at 5 years). `cid` serves the writer's and repairs' per-pilot lookups.

**Why `(session_id, start_time)` as the key.** The first session plus the start uniquely identify a flight, including a session that comes back later and starts a second flight. That is why the key avoids gap and span numbers, which restart in a later batch.

**Plan and name fields are stored.** They are taken from the session that ended last (tie-break: `end_time DESC, session_id DESC`) at write time. By then the pilot has been quiet for 3 hours, so the values are final. This is v1's behaviour, and it removes the join to `pilot_session` from every query.

### 1.4 The exact definition of one flight

These are today's rules, unchanged (SPEC section 5, ADR-016, ADR-023), applied by the writer:

1. **Pieces.** A session's positions split into pieces where two consecutive positions are more than 120 minutes apart. Each piece also splits into 8-hour buckets counted from its first position.
2. **Airborne time.** Sum over consecutive positions where groundspeed is at least 60 kt at the earlier one, each gap capped at `airborne_gap_cap_seconds`; pairs more than 120 minutes apart count zero.
3. **Flight gate.** The session's departure and arrival must both be non-empty after `upper(btrim(.. , ' \t\r\n'))`.
4. **Joining.** Pieces with the same `cid`, `callsign`, departure and arrival, ordered by start time, are joined into one flight when the next piece starts no more than 120 minutes after everything before it in that group ended, AND it comes from a different session than the piece just before it. Pieces of one session are never joined (the 8-hour and 120-minute splits stay separate flights). A different callsign or route is a different flight.

### 1.5 The quiet period and the "final" rule

**Rule: a pilot's unwritten flights are written once that pilot (CID) has had no session seen for 3 hours.**

I recommend applying it to the pilot, not to each flight separately, because it is simpler and still correct:
- Joins never cross pilots, and a pilot has one connection at a time.
- When the pilot has been quiet 3 hours, every flight of that pilot is complete. Any later session starts more than 3 hours after the last one ended, which is more than the 120-minute joining window, so it can never join a written flight.
- A per-flight rule would write an earlier flight on another callsign a little sooner, but it needs partial bookkeeping per session. The only cost of the pilot rule is that delay, which the owner accepts.

3 hours is safe for any reconnect pattern: it only has to exceed 120 minutes. It is far below the raw retention (90 or 180 days). The 8-hour split does not affect it.

## 2. The writer

**Where it runs.** The collector's existing 5-minute flight job (`maintenance.refresh_flights`, called from `main.flight_summary_loop`) changes its one SQL string from `SELECT refresh_flight_summary(%s)` to `SELECT write_finished_flights()`. That is the same number of Python lines (the collector is at 1,790 of 1,800). The daily maintenance runs it as well. It is a SECURITY DEFINER function; `db/roles.sql` swaps the EXECUTE grant from `refresh_flight_summary(interval)` to `write_finished_flights()` (High tier: grants).

**What it does, in one transaction:**

1. `pg_advisory_xact_lock(hashtext('vatpac.flight_writer'))`, so one writer runs at a time. The partition drop takes the same lock.
2. **Pick pilots:** CIDs that have a session with unwritten positions (`flown_upto IS NULL OR last_seen > flown_upto`) and no session seen in the last 3 hours, oldest first, at most 2,000 per call. The cap keeps the first run on live data (backfill) and any catch-up after an outage in short transactions. A partial index `pilot_session (cid) WHERE flown_upto IS NULL OR last_seen > flown_upto` makes this a small lookup.
3. For those pilots' sessions, read positions with `observed_at > COALESCE(flown_upto, '-infinity')`, and apply the piece, airborne, gate and joining SQL. These are copied word for word from `refresh_flight_summary_sessions` (the sample, gap, span and aggregate CTEs) and from the current `flight` view (the chain window). This is the proven code, moved, not rewritten.
4. `INSERT INTO flight_record ... ON CONFLICT (session_id, start_time) DO NOTHING`.
5. `UPDATE pilot_session SET flown_upto = last_seen` for every selected session, using the `last_seen` read in step 3. That includes sessions with no flight plan, which produce no flight. Positions that arrive later stay unwritten.
6. Return the number of flights written.

**Idempotent.** A second call finds no pilots. A crash rolls back the inserts and the markers together.

**Concurrency.**
- The collector keeps writing `pilot_session` and `pilot_position` while the writer runs. The writer only touches pilots who have been quiet for 3 hours, so it does not compete with the poll for rows.
- If such a pilot reconnects during the call, the new positions are newer than the `flown_upto` that gets written, so they are picked up later as a new flight. A brief row-lock wait is the only interaction.
- Readers see either the state before the call or after it, never half of it.

**Collector down.** The writer only runs when the collector runs, and it runs at start-up.
- A pilot who stayed connected through a 4-hour outage looks quiet at start-up, so the flight before the outage is written. After start-up the same connection continues more than 120 minutes later, which is a new piece and so a new flight. Today's rules give the same split.
- An outage shorter than 3 hours writes nothing early.
- Catch-up is capped at 2,000 pilots per call and repeats every 5 minutes.

**Late data.** Positions are only ever stored with the current snapshot time, and replays insert nothing. So no position can arrive at or before a session's `flown_upto`. A session that comes back later only adds positions after it.

## 3. The raw-data drop gate

**New rule: a day partition of `pilot_position` may be dropped only when every position in it is already in written flights.** In SQL terms: no row in it has `observed_at > COALESCE(session.flown_upto, '-infinity')`.

- This covers sessions without a flight plan, because the writer marks them too.
- It replaces three things: the pinning rule (a pilot still connected has unwritten positions, so the partition is kept anyway), the finalisation step, and the `final` re-checks.
- The notice "partition kept for more than twice the retention" stays, and now fires when unwritten positions are older than twice the retention. That means a pilot connected for months, or a writer that has stopped.

**Changes to `drop_old_pilot_position_partitions`.**
- Phase 1 (finalise) becomes the check above.
- Phase 2 (re-check with no lock) and phase 3 (`LOCK ... NOWAIT`, the in-lock re-check by session id, `DROP` with a 1 s lock timeout, retries) keep their code. Only the condition they test changes, to the same `flown_upto` test.
- The function takes the writer's advisory lock, so a write and a drop never interleave.
- The signature, return value and the collector's call loop are unchanged.

**Testing without waiting for 06-01-2027.** The function already takes `p_today date`.
- The rewritten `tests/integration/test_partition_drop_locks.py` and `test_partitions.py` create partitions for synthetic old dates, insert sessions and positions there, then call the function with `p_today` set 91 days later.
- They cover four cases:
  1. unwritten positions keep the partition;
  2. after `write_finished_flights()` the partition drops;
  3. a no-plan session's positions drop after the writer marks it;
  4. a session that comes back after being written keeps only the partitions holding its new positions.
- The lock and busy-partition tests stay as they are.

## 4. Every consumer

| Consumer | Change |
|---|---|
| `flight` | Thin view: the stored columns plus `duration`, `duration_minutes`, `is_circuit`, `is_zzzz` and `touches_home_region`. The last is computed from the stored airports and the live `home_icao_prefixes` setting, with today's expression. The columns `gap_segment`, `span_part` and `is_in_progress` are dropped, so `DROP VIEW flight CASCADE` and `flight_ext`, `movements`, `cohorts` and `monday_attendance` are re-created in the same migration. |
| `flight_ext` | Same definition without `WHERE NOT f.is_in_progress`. |
| `movements`, `cohorts` | Same definitions. |
| `monday_attendance` | Pilot side: `count(DISTINCT cid)` of `flight_record` rows overlapping 07:00 to 12:00Z. **Recommendation: accept that a pilot who reconnects within the window counts as present across the gap.** They were on the same flight, which is the event. The figure for a Monday becomes final about 15:00Z (written 3 hours after the last pilot leaves). The controller side is unchanged. |
| 11 panels with `NOT is_in_progress` | The filter text is removed (Low tier; `grafana/check_columns.py` and `validate_dashboards.py` will flag any miss). |
| Watchdog invariants (`ops/audit/invariants.sql`, `ops/watchdog/messages.py`) | "flight_summary rows missing for sessions older than 12 h" becomes **"sessions quiet for more than 4 hours with unwritten positions"** (the writer has stopped or is behind). "sample_count differs from stored positions" becomes **"a flight written in the last 3 days whose sample_count differs from its sessions' positions between its start and end"**. Plain-language messages and docs/alerts.md updated; `tests/test_watchdog_checks.py` and `tests/test_integrity_schedule.py` follow. |
| `ops/migrate/migrate.sh` | The `watchdog_ro` grant list swaps `flight_summary` for `flight_record` (High tier: grants). |
| Restore test (`ops/backup/restore-lib.sh`) | Add one check: `flight_record` readable, and its row count reported with the others. |
| Digest | The manager's daily digest is not code in this repository; no change beyond reading the new alert names. |
| Ops scripts | `ops/audit/recompute_flights.py` (an independent Python recompute from raw positions) is re-pointed at `flight_record` and becomes the independent cross-check of the writer. `capture.py`, `panel_recompute.py`, `logon_precision_impact.py` and `ops/audit/throwaway/*.sql`: re-point or delete. `tests/scale/gen_synth.sql` and `measure_partition_drop.py`: generate positions or flights directly. |
| Docs tied to tests | DATA_DICTIONARY.md (tables, columns, functions, views, the drop rule), CHART_DEFINITIONS.md (in-progress section removed; Monday attendance definition), alerts.md (two alerts renamed). Also ARCHITECTURE.md, SPEC.md sections 5 and 6, a new ADR-030 (supersedes ADR-016's piece table and ADR-025's in-progress mechanism; amends ADR-023 to "written 3 hours after the pilot is last seen"), and ADR-026 item 2 (flights kept forever; a session delete must delete its flights by `session_ids`). |

## 5. Clean start: recommended NOT to wipe

**Why not wipe.**
- Every raw position since go-live (07-10-2026) still exists, so the writer rebuilds all flights from them on its first runs (capped at 2,000 pilots a call).
- Wiping would also reset the "data since" banner (`data_since` is the oldest `network_snapshot`) and lose controller history, which this change does not touch.
- So there is no conversion code and no data loss, provided phase 3 is live before the first partition is due to drop, about 06-01-2027 (later if the 180-day setting goes in first).

**If the owner still prefers a wipe.** The owner runs these steps on the VM; nothing in the repo does it.
1. `ops/deploy.sh --preflight`, which takes a fresh dump.
2. `docker compose stop collector`.
3. In `psql` as the owner role: `TRUNCATE pilot_position, pilot_session RESTART IDENTITY CASCADE;`
4. Deploy the new release.
5. `docker compose start collector`.

What is kept: lookups, `app_setting`, controller tables, `network_snapshot`, `poll_log`, the feed archive and Grafana. The pilot charts then start empty, with a misleading "data since" date.

## 6. Phased plan (each phase its own branch, pull request, independent review and green CI)

| Phase | Content | Tier and review | Size (estimate) |
|---|---|---|---|
| 1. Writer alongside today's code | Migration: `flight_record`, `pilot_session.flown_upto`, the partial index, `write_finished_flights()`. Nothing calls it yet, and nothing is deleted. Tests: new `test_flight_writer.py` covers reconnect within, at and after 120 minutes, the 8-hour split, the 120-minute split, plan change before writing, a session that comes back after writing, no-plan sessions, idempotence, the 2,000 cap, the tie-break, and a collector outage. Plus an **equivalence test**: on the same fixtures, `flight_record` equals today's `flight` view (minus the three dropped columns) for flights quiet for 3 hours. | Medium; **Opus second pass** (the writer is the new core) | about 180 SQL, about 250 test lines |
| 2. Switch the readers | Migration: re-create `flight` over `flight_record` without the three columns; re-create `flight_ext`, `movements`, `cohorts`, `monday_attendance`. Collector: one SQL string. `roles.sql`: EXECUTE grant swapped. Panels: 11 filters removed. Test helper inserts `flight_record` rows (or positions plus a writer call); the 8 helper-based test files keep their assertions, with times moved at least 3 hours into the past where needed; in-progress tests deleted. CHART_DEFINITIONS, DATA_DICTIONARY. | High (grant); Sonnet build, independent review | about 120 SQL, 1 Python line, about 15 test files |
| 3. New drop gate | Migration: `drop_old_pilot_position_partitions` with the `flown_upto` gate and the writer's lock. Partition tests rewritten with synthetic dates (section 3). **Must be live before about 06-01-2027.** | High (deletes raw data for ever); **Opus second pass**; rehearsal on a restore of a live dump | about 60 changed SQL lines, 2 test files |
| 4. Delete the old machinery and take the name | Migration: drop the pieces table `flight_summary`, both refresh functions and `flight_part`; then `ALTER TABLE flight_record RENAME TO flight_summary` (and its indexes), with `flight`, the writer and the drop function re-pointed in the same migration. Watchdog invariants and messages, the `migrate.sh` grant, ops scripts, gen_synth, alerts.md, the remaining docs, ADR-030, ADR-026 amendment. | High (grant); Sonnet build, independent review | about 20 SQL, about 10 ops and test files, docs |

**Why this order keeps main green.** Phase 1 only adds things. Phase 2 switches readers while the old machinery still exists and runs harmlessly. Phase 3 changes the gate once the writer is live. Phase 4 deletes only things nothing reads any more. Each phase can deploy on its own, but the owner may also deploy 1 to 4 together after all are merged, with one rehearsal on a restore of a live dump and a fresh dump first.

**Test files that change** (about 20): `helpers.py`, `test_flight_summary.py` (replaced), `test_flight_reconnect_merge.py`, `test_flight_view.py`, `test_flight_in_progress.py` (deleted), `test_exclude_in_progress.py` (deleted), `tests/test_in_progress_panels.py` (deleted), `test_partition_drop_locks.py`, `test_partitions.py`, `test_integrity_checks.py`, `test_audit_t30.py`, `test_end_to_end.py`, `test_roles.py`, `test_logon_whole_seconds_migration.py` (it exercises migration 100 on pieces; keep it running against the migration version it tests, or retire it with a note), `tests/test_watchdog_checks.py`, `tests/test_integrity_schedule.py`, `tests/test_data_dictionary_tests.py`, and the 8 helper users (events, military, online hours, panel rules, pilots people, pilots t14, time zones, watchdog flap), which keep their assertions.

**Net code.** About 380 SQL lines added across the phases. About 330 deleted: the two refresh functions, the merge view, `flight_part`, the finalisation blocks of the drop function, placeholders, and the in-progress filters. The end state has one flight table instead of two and one maintenance function instead of two.

## 7. Risks, what could not be verified, owner decisions

**Risks.**
- **The writer is new code in a new arrangement.** Mitigation: the SQL is copied, not rewritten; phase 1 runs it beside the old view with an equivalence test; Opus second pass.
- **The drop gate deletes raw data for ever.** Mitigation: the gate is stricter and simpler than today's; there are synthetic-date tests; a rehearsal; it must ship before about 06-01-2027.
- **The first run on live data is a backfill.** At about 4,000 sessions now (more by deploy time) it is small, and the 2,000-pilot cap keeps each call short.
- **Counts appear 3 hours after a pilot leaves.** Accepted by the owner. The "last 24 hours" server panel shows about 21 hours of finished flights; Monday figures are final about 15:00Z.
- **Two visible differences from today.** The Monday attendance reconnect gap now counts as present. A session that comes back after being written with a changed plan keeps its old airports for the written part (closer to v1 and ADR-023).

**Not verified.**
- None of this is built. The writer's speed is reasoned from the measured pieces of today's SQL: 130 ms for the chain over 477 pilots' full history at 5 years, and the refresh at about 0.1 s.
- PostgreSQL 16 and the 1 CPU VM were not tested.
- Real data was not used. Overlapping sessions of one pilot on live data were not checked (`ops/check_identity_flap.sql` is the existing check).

**Owner decisions.**
1. Monday attendance: count a pilot as present across a short reconnect gap? Recommended: yes.
2. The quiet rule: "3 hours after the pilot (not each connection) was last seen". Recommended: yes.
3. Keep the data, no wipe. Recommended: keep.
4. The deploy plan: phases 1 to 4 live before about 06-01-2027, as one deploy or two.
5. The table name: `flight_record` while the pieces table exists, renamed to `flight_summary` in phase 4, as the owner asked (section 0). Recommended: yes.
