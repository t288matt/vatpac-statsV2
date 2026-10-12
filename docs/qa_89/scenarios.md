# QA scenarios for the flight writer (issue #138, phase 1 of #89)

**Revised 12-10-2026 for the owner decision of 12-10-2026: the quiet rule is per FLIGHT, not per pilot (sections 1 and 1b; the design text of 1.3 is superseded where it says per pilot).** Written 12-10-2026 by the QA agent from `docs/DESIGN_89_FINAL.md` (branch `opus-89`) sections 1.3 to 1.6, 2, 5 and 7.3 only. The builder's code was not read. Nothing here has been run: these are specifications for tests and for the owner's checks.

## 0. Conventions

- Clock-free: every time is stored data. `T0` = 20-10-2026 00:00:00 UTC. `hh:mm` means `T0` plus that time. The real `now()` must never matter (scenario Q4).
- Cadence 30 s. Airborne cap `C` = 75 s (`airborne_gap_cap_seconds` at the 30 s poll, design 1.5). Quiet period `Q` = 8 h unless a scenario changes the `flight_quiet_hours` setting.
- "Marker" = a `network_snapshot` row at the stated time, inserted by the test helper to move `ref` (= newest `observed_at`). Without a marker `ref` is the newest snapshot of the fixture.
- `Pn` = pilot (CID), `Sn` = session `(cid, callsign, logon_time)`, key `K` = `(cid, callsign, departure, arrival)`. Default plan YSSY to YMML, callsign QFA1.
- "Positions a to b" = one `pilot_position` per 30 s from `a` to `b` inclusive (00:00 to 01:00 = 121 positions), groundspeed 250 kt unless stated. `last_seen` = the last position time unless stated. `flown_upto` starts NULL.
- "Written" = a row in `flight_record`. "Marked" = `pilot_session.flown_upto = last_seen`.
- Expected rows give `(session_id, start, end, session_ids, sample_count, airborne_minutes)`.
- Each scenario names the design clause it proves (section numbers of DESIGN_89_FINAL.md).

## 1. Quiet period (1.3, 2 steps 2 to 4, 7.3)

Rule under test (owner decision 12-10-2026): a flight GROUP (units of one key joined by the v1 rule) is written once its own newest position is 8 h or more behind `ref`, even if the pilot is online in a later, different flight. A group still active is untouched and never blocks an earlier quiet group. A later session of the same key logging on within 120 min of the group's last position joins the group, so the group is not quiet.

**Q1. 7 h 59 min is not quiet.** P1 S1 positions 00:00 to 01:00. Marker 08:59:59 (`ref` minus `last_seen` = 7:59:59). Call writer. Expected: returns 0; no row; `flown_upto` NULL. Proves: quiet test is `>= quiet`, not before (1.3).

**Q2. Exactly 8 h is quiet.** As Q1, marker 09:00:00. Expected: returns 1; row `(S1, 00:00, 01:00, {S1}, 121, 60.00)`; S1 marked 01:00. Proves: boundary inclusive (1.3 formula `>= quiet`).

**Q3. 8 h 01 min.** As Q1, marker 09:01:00. Expected: same row as Q2. Proves: boundary above (7.3 "at 8 h written").

**Q4. Not the clock.** As Q1 (marker 08:59:30) but the test database clock is set (or the fixture is time-shifted) so `now()` is 30 days after `T0`. Expected: returns 0. Reverse: marker 09:00 with `now()` equal to `T0` plus 1 h: returns 1. Proves: `ref` is the newest snapshot, never `now()` (1.3, 2).

**Q5. Other pilots do not delay.** P1 S1 positions 00:00 to 01:00. P2 S2 positions 00:00 to 08:59 (still connected), marker 09:00. Expected: P1 written, P2 not. Proves: no cross-pilot delay (1.3, 7.3).

**Q6. Multi-leg pilot: the earlier leg is written first.** P1 S1 (YSSY to YMML) positions 00:00 to 01:30; S2 (YMML to YSSY, different key, logon 03:00) positions 03:00 to 05:00. Marker 10:00 (S1 8.5 h behind, S2 5 h): returns 1, S1 written, S2 untouched (not marked). Marker 13:00: S2 written. Proves: per-flight rule; a recent or online later flight does not hold back the earlier one (owner decision).

**Q7. A no-plan session does not hold a flight.** P1 S1 (plan) positions 00:00 to 01:00; P1 S2 (no plan, logon 06:00) positions 06:00 to 07:00. Marker 09:00. Expected: S1 written (S2 has no key and joins nothing). S2 is marked and makes no flight once it is itself 8 h behind `ref` (marker 15:00); whether a no-plan session is marked earlier is open (section 8). Proves: 1.4 item 2 with the new rule.

**Q8. The setting.**
- `flight_quiet_hours` = 6, S1 last_seen 01:00, marker 07:00: written. Marker 06:59:59: not written.
- Row absent: default 8 applies (Q2 result).
- Row = 2.5: accepted, works. Row = 2.4 and 2.49: the call raises (error), writes and marks nothing, even when pilots are quiet.
- Row changed from 8 to 6 after a flight is written: the written row is unchanged (7.3 "quiet setting changed").
Proves: 1.3 "The setting".

**Q9. Cold start.** `network_snapshot` empty, sessions and positions present. Expected: returns 0, nothing marked, no error. Proves: `ref` NULL writes nothing (1.3, 2 step 2).

**Q10. Collector outage.** P1 S1 positions 00:00 to 01:00; no snapshot after 01:00 (outage); the test clock jumps to 10:00. Writer: 0. Poll at 10:00 stores a snapshot and P1 S1 position (same logon), `last_seen` 10:00. Writer: 0. P1 leaves 10:30 (positions 10:00 to 10:30 every 30 s), marker 18:30: returns 1. Expected row `(S1, 00:00, 10:30, {S1}, 121 + 61, 60.00 + gap)`: ONE flight (no cut by silence or span), airborne pair 01:00 to 10:00 counts 0 (gap over 120 min). Proves: 2 "Collector or feed down", 1.4 item 1, 1.5.

**Q11. Feed outage (collector running, nothing stored).** Same data as Q10 but the gap is `poll_log` `skipped_unchanged` rows only. Same expected result. Proves: same clause; feed variant (7.3).

**Q12. A flight that ended during the outage is written after the first stored poll.** P1 (connected, as Q10) and P2 (S2 positions 00:00 to 01:00, left). Snapshots stop 01:00. Poll at 10:00 stores P1 only. Writer: P2's flight written (9 h behind the new `ref`), P1's not. Proves: 2 "Collector or feed down" last bullet.

**Q13. Re-check under lock.** Two connections. Connection A starts the writer after choosing P1's group (hold with a test-only delay). Connection B logs a new session of the same key for P1 within 120 min of the group's last position (or advances the group's `last_seen`). Expected: A, after taking the row lock, drops the group (no longer quiet); nothing written or marked for it. Proves: 2 step 4. Run both orders; no deadlock; each call under the collector's 5 s lock timeout.

**Q14. Batch cap and order.** Three quiet single-flight pilots with newest `last_seen` 01:00 (P1), 02:00 (P2), 03:00 (P3). `write_finished_flights(2)` writes the two oldest-quiet (P1, P2); a second call writes P3; a third returns 0. Whether a pilot with two quiet flights counts once against the cap is open (section 8). `p_max_pilots` of 0 or negative: silent. Proves: 2 step 3, 5.1, 7.3.

**Q15. One writer at a time.** Two connections call the writer together on the same quiet pilots. Expected: no duplicate rows, no error, total rows equal one call's rows; the second returns 0 once the first commits. Proves: 2 step 1 (advisory lock), idempotence.

## 1b. Per-flight quiet rule (owner decision 12-10-2026)

Key `K` = P1, QFA1, YSSY to YMML. `L` = the group's newest position. The marker is set to make the stated flights quiet.

**F1. S quiet, T same key within 120 min is still active.** S positions 00:00 to 01:00 (L = 01:00). T logs on 02:00 (60 min after L), positions 02:00 to 09:30, marker 09:30. Expected: nothing written, nothing marked (S+T is one group whose newest position is 09:30). Marker 17:30: ONE flight `{S,T}`. S is never written alone. Proves: a joinable later session keeps the group active.

**F2. S quiet, T different key.** As F1 but T is YMML to YSSY. Marker 09:30: S written (8.5 h behind), T untouched. Proves: per flight; an online later flight of another key does not block.

**F3. Boundary 119 / 120 / 121.** S ends 01:00. T same key logs on at 02:59 / 03:00 / 03:01 (119 / 120 / 121 min), positions to 03:30, marker 09:00 (S exactly 8 h behind). Expected for 119 and 120: T joins, group is 5.5 h behind, nothing written. For 121: S is its own group, written; T untouched. Proves: the join boundary is applied before the quiet test (">120 starts a new flight").

**F4. T logs on long after S went quiet.** S ends 01:00, marker 09:00: S written. T same key logs on 10:00 (540 min later). Expected: T is a new flight; S's row unchanged. Proves: written flights never change; a join is impossible after 120 min.

**F5. Amended plan.** S1 positions 00:00 to 01:00 whose session row holds the amended arrival YBBN (original YMML). Quiet at 09:00. Expected: one flight YSSY to YBBN. A reconnect S2 (YSSY to YMML, original plan) 30 min later is a separate group and flight on its own clock. Proves: key uses the stored (final) plan; per-group clocks.

**F6. Resume.** Three groups: G1 quiet, G2 quiet, G3 active (two of them for one pilot). Call with cap 1, re-run, and once with a rolled-back call between. Expected: G1 and G2 exactly once each, G3 never; no duplicate keys; `flown_upto` set only on written groups' sessions. Proves: idempotent resume under the new rule.

**F7. Unprocessed session in the middle.** Key K: S1 (00:00 to 01:00) already written (row R1); S2 (logon 01:30, positions 01:30 to 02:00) and S3 (logon 02:50, positions to 03:30) unprocessed. Expected: S2+S3 form one new group, written when quiet; R1 untouched. Variant: S1, S2, S3 all unprocessed and quiet: one group `{S1,S2,S3}`. Proves: groups are built from unprocessed units; written flights never gain members. How an unprocessed unit that fits the 120-minute window of a written flight is handled is open (section 8).

**F8. Earlier flight independent of a later online flight.** S ends 01:00, written at 09:00 while the pilot is online in a different-key flight U (to 12:00). Expected: S's row exists, no change when U is written at 20:00. Proves: earlier flight independent of later.

**F9. Two groups of one key.** S1 ends 01:00; S2 logs on 05:00 (240 min later: a new group), positions to 06:00. Marker 09:00: S1's group written, S2's not. Marker 14:00: S2's written. Proves: groups of one key have separate clocks.

## 2. v1 join rule and flights (1.4, 7.3)

Common fixture for J1 to J7: P1, callsign QFA1, YSSY to YMML throughout, S1 positions 00:00 to 01:00 (`L1` = 01:00 = S1's last position), S2 logon `L1 + d`, first position logon + 30 s, positions to `L1 + d` + 60 min. Marker far later (everything quiet).

**J1. d = 119 min.** Expected: ONE flight `(S1, 00:00, S2 last, {S1,S2}, sum, ...)`, `logon_time` = S1's logon. Proves: join (1.4 item 3, 7.3).

**J2. d = 120 min exactly.** Expected: one flight (">120" starts a new flight, so 120 joins). Proves: strict inequality (1.4 "exactly 120 joins").

**J3. d = 121 min.** Expected: TWO flights: `(S1, ...)` and `(S2, ...)`. Proves: 120-minute limit.

**J4. Logon rule, not first position.** d = 120 min but S2's first position is 150 s after its logon (gap by first position 122.5 min). Expected: one flight (join decided by `logon_time`). Proves: 1.4 item 3 and the "logon within 120 minutes but first position later (joins, v1)" test in 7.3. The first-position rule would give two: the comparison A reason E3 (5.2 item 3).

**J5. Late first position, early logon.** d = 119 min, first position 10 min after logon. Expected: one flight. Proves: as J4.

**J6. Chain.** S2 as J1 (d = 119 from S1's last), S3 logon 119 min after S2's last position (238 min after S1's). Expected: ONE flight `{S1,S2,S3}`. Proves: comparison against the latest earlier end, not the first unit (1.4 "previous last point").

**J7. Running maximum, not the previous unit.** S1 positions 00:00 to 05:00. S2 (same key, logon 01:00, overlaps S1) positions 01:00 to 02:00, so the latest earlier end is 05:00 although the unit before S3 in order (S2, by first position) ends 02:00. Case (a) S3 logon 06:00: 60 min after 05:00 but 240 min after 02:00; expected: joins, one flight `{S1,S2,S3}`. Case (b) S3 logon 07:01: 121 min after 05:00; expected: S1+S2 one flight, S3 a second flight. Proves: "latest end of all earlier units" (1.4 last paragraph).

**J8. Overlap joins; negative gap.** S1 positions 00:00 to 02:00; S2 same key, logon 01:00, positions 01:00 to 03:00. Expected: one flight `{S1,S2}`, start 00:00, end 03:00, sample_count 241 + 241 = 482 (positions of different sessions are separate rows). Proves: 1.4 item 3 negative gap, 7.3 "overlapping sessions join".

**J9. Key components all matter.** Four extra sessions, each logging on 5 min after S1's last position, differing in one thing: callsign QFA2; CID P2; departure YBBN; arrival YPPH. Expected: each its own flight, nothing joined to S1. Proves: key = cid, callsign, departure, arrival (1.4 item 3).

**J10. Normalisation joins.** S1 arrival `YMML`, S2 arrival `' ymml\t'`. Expected: one flight; stored arrival `YMML`. Same for departure with CR and LF padding. Proves: `upper(btrim(.., E' \t\r\n'))` (1.4 item 2, 1.6).

**J11. A 10 h session is one flight.** One session, positions 00:00 to 10:00 (1,201). Expected: ONE row, sample_count 1201. Today's code gives two (8 h part). Proves: no 8-hour parts (1.4 item 1, 7.3).

**J12. Silence inside a connection never cuts.** One session, positions 00:00 to 01:00 and 04:00 to 05:00 (3 h silence). Expected: one flight, sample_count 242, airborne = 3600 s from the first block + 3600 s from the second, zero for the 01:00 to 04:00 pair, = 120.00. Repeat with silences of 119, 120 and 121 min: always one flight. Proves: 1.4 item 1, 7.3 "3-hour silence".

**J13. Flight id is the first session by first position, not the lowest id.** Insert S9 (id 9) with positions 00:00 to 01:00 and S4 (id 4) logon 01:30 positions 01:30 to 02:30, same key. Expected: one flight with `session_id` 9, `session_ids` containing both, `logon_time` = S9's (earliest logon). Proves: ordering by first position then id; `session_id` = first session (1.4 item 3, 1.6).

**J14. Earliest logon even when a later session logged on earlier.** S1 logon 00:00 first position 00:20; S2 logon 00:10 (earlier than S1's first position) first position 00:05 (S2 first by position). Expected: flight `session_id` S2; `logon_time` = 00:00 (S1's, the minimum). Proves: columns rule (1.4 item 4). (Overlapping sessions: acceptable, no live case.)

**J15. Gate.** Sessions with departure NULL; `''`; `'  '`; `E'\t\n'`; arrival likewise; both missing. Expected: no row; each session marked `flown_upto = last_seen`; pilot not re-picked on the next call. `ZZZZ` departure: row written (the gate is non-empty only). Departure = arrival (circuit): row written. Proves: 1.4 item 2, 2 step 9.

**J16. No-plan session between two planned sessions.** S1 plan, S2 no plan (logon 30 min after S1's last), S3 plan same key as S1 logon 100 min after S1's last. Expected: S1 and S3 one flight; S2 marked, no flight. Proves: gate-failing sessions take no part in joining (1.4 items 2 and 3).

**J17. Returning unit forms its own flight.** S1 written (quiet, row R1). Then insert 40 new positions for S1 after its `flown_upto` (as a replay would, the test does it directly), raise `last_seen`, advance `ref` by 9 h. Call writer. Expected: R1 unchanged (compare all columns and `written_at`); a second row `(S1, new start, new end, {S1}, 40, ...)`; NOT joined to R1 even if within 120 min; S1 marked again. Proves: 1.4 item 3 last sentence, 2 "Late positions" and the key `(session_id, start_time)` (1.6).

**J18. Tie in end times.** Two overlapping sessions of one key (S1 id 1, S2 id 2) both ending 02:00:00 with different `aircraft_icao`. Expected: plan fields from S2 (higher id). Proves: `ORDER BY end_time DESC, session_id DESC` (1.4 "Equal end times").

## 3. Plan fields (1.4 table, 1.6)

**P1. Plan filed late.** S1 positions 00:00 to 01:00, plan filed (set on the session) at 00:20. Expected: flight start 00:00 (first stored position), not 00:20; all 121 positions counted. Proves: plan is a session attribute (1.4 table "Storage").

**P2. Amended plan inside one connection.** S1 plan YSSY to YMML at the start; arrival changed to YBBN at 00:30 (the session row holds YBBN). Expected: ONE flight YSSY to YBBN, 121 samples, none under YMML. Proves: the source of the 23 v1-only flights (1.4 "Amended plan").

**P3. Amended plan across a reconnect.** S1 YSSY to YMML positions 00:00 to 00:30; S2 (new logon 45 min later) YSSY to YBBN. Expected: TWO flights (key differs), as v1. Proves: key includes arrival.

**P4. Frozen at write.** After a flight is written, UPDATE the session's route, arrival and name; run the writer again with newer markers. Expected: `flight_record` row unchanged (all columns, `written_at`). Proves: "plan frozen at write time" (1.4 table).

**P5. Field rules over two sessions.** S1 route `R1`, `deptime` `0900`, aircraft B738, name `A`, server `X`, flight rules `I`, remarks `rA`, planned alt `F350`, ratings 1 and 0. S2 (joins; ends later) route `R2`, `deptime` `0100`, aircraft A320, name `B`, server `Y`, rules `V`, remarks NULL, planned alt NULL. Expected: route `R2` (latest non-null); `deptime` `0900` (maximum, not the latest); aircraft, name, server, rules, remarks, planned alt, ratings all from S2, so remarks NULL and planned alt NULL (S2's own values; no fallback to S1). Variant: S2 route NULL gives `R1`. Variant: S1 ends last gives S1's values. Proves: 1.4 table rows "Route", "deptime", "Aircraft...". Open question about NULL fallback: section 8 item 6.

**P6. `logon_time` and `session_ids`.** Three joined sessions: `session_ids` lists all three (order not specified, compare as sets); `logon_time` = earliest; `cid`, `callsign` equal the key. Proves: 1.6.

**P7. Constraints.** `end_time >= start_time` holds (one-position flight: start = end, `sample_count` 1, airborne 0.00). `departure`, `arrival` stored normalised. Proves: 1.6.

## 4. Airborne time, maxima (1.5, 2 step 5 and 8)

Single session, YSSY to YMML, written as in Q2.

**A1. Steady flight.** 121 positions 00:00 to 01:00, gs 250. Expected airborne 60.00 (120 pairs x 30 s = 3600 s; the last position counts 0), `sample_count` 121.

**A2. Threshold.** gs 60 at the earlier sample counts; gs 59 does not; gs NULL does not (design silent on NULL, see section 8). Pair rule uses the EARLIER sample: positions gs 80, 0, 0 give one 30 s gap counted; gs 0, 80, 0 gives one 30 s.

**A3. Missed polls and the cap.** Earlier-sample gs 100 and gap to next: 30 s counts 30; 60 s counts 60; 75 s counts 75; 100 s counts 75 (capped); 119 min counts 75; exactly 120 min counts 75; 120 min 30 s and 121 min count 0. Proves: "each gap capped"; "more than 120 minutes apart counts zero".

**A4. No pair across sessions.** Joined S1 (airborne 30 min) and S2 (airborne 20 min) with a 60-minute gap between them. Expected flight airborne 50.00, not 50.00 plus the gap. Proves: pairs are within a session (1.5, 2 step 5).

**A5. Rounded once per flight.** Two joined sessions, each with exactly 100 s airborne (gaps 30, 30, 30, 10 s at gs 200). Expected 3.33 (round(200/60, 2)), not 3.34 (sum of two rounded values). Proves: 2 step 8, 5.2 expected difference 5.

**A6. Maxima.** Positions gs 120, 480, NULL; altitude 3,000, 36,000, NULL. Expected `max_gs_kt` 480, `max_alt_ft` 36000. All NULL gives NULL. Maxima over all sessions of the flight. Proves: 1.6, 1.5 "stay v3 additions".

**A7. sample_count counts every position**, airborne or not. Proves: 1.6.

## 5. Resume, idempotency, atomicity (2, 7.3)

**R1. Idempotent.** Run the writer twice after Q2. Expected: second returns 0; `flight_record` identical, `written_at` unchanged, no `flown_upto` change.

**R2. Marking.** After a call: every locked session of a written pilot has `flown_upto = last_seen`, including sessions with no plan and a session with no positions at all. Sessions of pilots not written (not quiet, capped out, conflicted) are untouched. Proves: 2 step 9.

**R3. Atomic.** A test-only trigger on `flight_record` raises on the second insert of a call that would write 3 flights (two pilots). Expected: the call errors; no `flight_record` rows, no `flown_upto` changes; remove the trigger and re-run: all written once. Proves: "A crash rolls back inserts and markers together" (2).

**R4. Resume after partial work.** Cap 1 with three quiet pilots. Run once (P1 written), then run with a failing trigger (rolls back), then run twice more without. Expected: each pilot has exactly one flight; no duplicates; no missed pilot. Proves: idempotent resume.

**R5. Position at exactly `flown_upto` is not recounted.** After J17 setup, plant a position at exactly the old `flown_upto`. Expected: not in the returning unit (`observed_at >` strict). Only newer positions counted.

**R6. Positions beyond the locked `last_seen` are left.** Session S1 `last_seen` 01:00 but a stray position at 01:00:30 (fixture). Expected: unit and marker stop at 01:00; the stray is neither counted nor marked. Proves: 2 step 5 (`observed_at <= last_seen`). Design is silent on what happens later (section 8 item 5).

**R7. Conflict check.** Pre-insert a `flight_record` row with the key `(S1, 00:00)` for P1 (a fabricated "already written" row), leave S1 unmarked. P2 quiet and clean. Call. Expected: P1 gets no insert and no marker, a WARNING names P1's CID; P2 written; the call does not error. The row count returned: design silent (section 8 item 4). Proves: 2 step 7.

**R8. Delete a session cleans its flights** (ADR-026 item 2): `WHERE cid = s.cid AND s.id = ANY(session_ids)` finds exactly the joined flights of that session. Proves: 1.6 index note. Test the lookup, not a purge job.

## 6. Backfill (5.1, 5.3)

**B1. Batch independence.** Build the same fixture three times: 300 pilots over 5 days, mixed legs, joins, no-plan sessions, 10 pilots still connected at the end. Run `write_finished_flights(p)` until it returns 0 with `p` = 1, 7 and 500. Expected: `flight_record` identical across the three (`EXCEPT ALL` both ways empty, ignoring `written_at`); the 10 connected pilots absent; every other gate-passing session in exactly one flight. Proves: batch size does not change results (5.1).

**B2. Order and progress.** With `p` = 100 the first call writes the 100 oldest-quiet pilots. Each call returns between 0 and the cap. Calls terminate (no pilot picked twice without progress). Proves: 2 step 3 order.

**B3. Coverage.** After B1: `sum(sample_count)` of the flight table equals the count of `pilot_position` rows of gate-passing sessions at or before `flown_upto`; positions of no-plan sessions are unreferenced but their sessions are marked. Proves: independent check of 5.2(A).

**B4. Backfill while live.** Run the writer in a loop while the fixture keeps inserting snapshots, positions and `last_seen` for connected pilots. Expected: connected pilots never written; the insert transactions never wait more than the collector's 5 s lock timeout. Proves: 5.1 "Locks".

**B5. Placeholders.** `SELECT count(*) FROM flight_summary WHERE sample_count = 0` on the restored live dump: expected 0 (5.3). If a fixture has placeholder rows, carry-over copies them with `sample_count` 0 and `airborne_minutes` NULL and marks their sessions; the writer never sees them as unprocessed. Proves: 5.3.

## 7. Mapping to the design test list (7.3)

| 7.3 item | Scenarios |
|---|---|
| 119, 120, 121 | J1, J2, J3 |
| logon within 120, first position later | J4, J5 |
| overlapping sessions join | J8, J7 |
| 10 h session is one flight | J11 |
| 3 h silence is one flight, zero airborne across | J12, A3 |
| plan filed late; frozen; field rules | P1, P4, P5 |
| no-plan marked, no flight | J15, J16, R2 |
| 7 h 59 / 8 h | Q1, Q2, Q3, F1, F3 |
| collector outage, feed outage, cold start | Q10, Q11, Q9 |
| second pilot or later flight does not delay | Q5, Q6, F2 |
| multi-leg | Q6, F2, F8, F9 |
| returning session new flight | J17 |
| setting changed / under 2.5 h | Q8 |
| idempotence, batch cap | R1, Q14 |
| pre-existing key | R7 |
| equal end times | J18 |

## 8. Where the design is silent or contradicts itself (listed, not guessed)

1. **Cadence and cap.** 1.4 says v3 polls every 30 s (cap 75 s); `docs/V1_COMPARISON.md` E1 and E7 say 15 s and a 40 s cap. The cap is configurable (`airborne_gap_cap_seconds`); scenarios A3 assume 75. The builder must say which value the test database uses.
2. **Cap boundary.** 1.5 says "a pair more than 120 minutes apart counts zero" and a gap of 119 min counts the cap (75 s). Is a gap of 100 s at gs 250 meant to count 75 s? Taken from the text as written (A3), but it makes airborne time depend on a tiny cap for any missed poll under 120 min.
3. **Lock behaviour of the advisory lock.** Step 1 says `pg_advisory_xact_lock`, which blocks. Whether a concurrent caller should wait or return at once is not stated (Q15 accepts either result only if no duplicates).
4. **Return value with conflicts.** Step 7 says the call "counts them" (conflicted pilots). Whether the return value is flights written only, or includes conflicts, is not stated (R7).
5. **Stray positions beyond `last_seen`.** Step 5 ignores them; later they would form a returning unit only if `last_seen` rises. If it never rises they stay unprocessed and the drop gate (item 1) keeps the partition. Not a defect but unstated (R6).
6. **NULL fields in the last session.** 1.4 table says plan fields come from the session that ended last, each its "own latest non-null value". If that session has NULL remarks while an earlier session had text, the result is NULL (P5). v1 behaves differently (COALESCE on updates). Confirm the intent.
7. **NULL groundspeed in airborne.** Treated here as not airborne; the design does not say (A2).
8. **Sessions with `last_seen > flown_upto` but no positions beyond `flown_upto`.** Picked again and marked; no unit, no flight. Not stated; presumably harmless. Needs one test.
9. **`p_max_pilots` of 0, negative or NULL.** Not specified (Q14).
10. **Non-numeric `flight_quiet_hours`.** Not specified (Q8): cast error or default?
11. **Overlapping sessions across callsigns.** Joins never cross keys; the design says live data has no overlaps, so J8, J14 and J18 are synthetic and only prove the fallback rules.
12. **Equality "same 3 v3-only flights".** 5.2(B) expects "the same 3 v3-only flights" but the 3 are "not yet explained" and not listed in the design. The runner needs their keys from the owner's earlier run (`--known-v3-only`), otherwise it can only check the count.
13. **v1 window cut.** The design excludes flights logging on before 07:52 UTC on 08-10-2026 or overlapping 15:30 to 15:50 on 09-10-2026 and ending before 10-10-2026 18:00 UTC, but does not say whether the cut is on v1 `completion_time` or on a position time (v1 `flight_summaries` has no end-of-flight column documented in the repo). The runner assumes `completion_time` is the flight's end (see comparison_b.sql).
14. **Section 1.2 versus 7.2 on `flight_legacy`.** 1.1 says nothing about `flight_legacy` existing before phase 3; 5.2(A) says comparison A repeats "against `flight_legacy`" just before phase 5, which only exists from phase 3. For the phase 1 gate the old side is the live `flight` view, as comparison_a.sql assumes.
15. **Per-flight rule, new gaps (12-10-2026).** (a) 1.3 and section 2 step 3 of the design still say per pilot and must be updated. (b) Is the cap `p_max_pilots` counted in pilots or groups? (c) When is a no-plan session marked: when it is itself 8 h behind, or with its pilot's next written flight? (d) F7: an unprocessed unit that fits the 120-minute window of an already-written flight: the design says a returning unit always starts a new flight, but with per-flight writing a live reconnect after the 8 h cannot join anyway; the builder should state the rule. (e) The group's quiet time must be the maximum `last_seen` of its units; confirm.
