# Issue #89 phase 1: conformance table

Written 12-10-2026 (branch `p1-flight-record`, issues #89 and #137). Each requirement of `docs/DESIGN_89_FINAL.md` (branch `opus-89`) that phase 1 builds, and the test that proves it. Test files: `W` = `tests/integration/test_flight_writer.py`, `E` = `tests/integration/test_flight_writer_equivalence.py`, `C` = `tests/integration/test_compare_flights.py`. A requirement with no test is listed at the end, with the reason.

## 1.3 The quiet period

| Requirement | Test |
|---|---|
| A pilot is written once quiet for 8 hours of stored data: 7 h 59 min not written, 8 h written | W `test_a_pilot_quiet_for_7h59m_is_not_written_and_at_8h_is_written` |
| Measured against the newest snapshot, not the clock | W `test_the_clock_is_not_used_a_pilot_last_seen_years_ago_is_not_quiet_until_a_snapshot_says_so` |
| Cold start (empty `network_snapshot`) writes nothing | W `test_cold_start_with_no_snapshot_writes_nothing` |
| A collector outage cannot end a live flight | W `test_a_collector_outage_cannot_end_a_live_flight` |
| A feed outage (collector running, nothing stored) cannot end a live flight | W `test_a_feed_outage_with_the_collector_running_cannot_end_a_live_flight` |
| A pilot who left during an outage is written after the first stored poll | W `test_a_pilot_who_left_during_an_outage_is_written_after_the_first_stored_poll` |
| Per pilot: another pilot's activity does not delay the first | W `test_another_pilots_activity_does_not_delay_the_first` |
| Multi-leg pilot written only when quiet after the last leg | W `test_a_multi_leg_pilot_is_written_only_when_quiet_after_the_last_leg` |
| Setting `flight_quiet_hours` seeded 8; a change applies; under 2.5 h refused, nothing written | W `test_the_table_the_column_the_setting_and_the_indexes_exist`, `test_a_changed_quiet_setting_applies_and_a_value_under_2_5_hours_is_refused` |

## 1.4 The flight rule (exactly v1)

| Requirement | Test |
|---|---|
| Reconnect joins at 119 and 120 minutes, is new at 121 (logon against the previous last position) | W `test_a_reconnect_joins_when_its_logon_is_within_120_minutes_of_the_previous_last_position[119-joins, 120-joins, 121-new]` |
| The join uses `logon_time`, not the first position (logon in window, first position later: joins; logon outside: new) | W `test_the_join_is_measured_from_logon_time_not_the_first_position` |
| Overlapping sessions of one key join | W `test_overlapping_sessions_of_one_key_join` |
| Key is cid, callsign, departure, arrival; two pilots never join | W `test_a_different_callsign_departure_or_arrival_is_a_different_flight`, `test_two_pilots_never_join` |
| No 8-hour parts: a 10-hour session is one flight | W `test_a_ten_hour_session_is_one_flight` |
| No cut inside a connection: a 3-hour silence is one flight | W `test_a_three_hour_silence_inside_one_session_is_one_flight_with_no_airborne_time_across_it` |
| Flight gate: normalised airports; no-plan sessions form no flight but are marked | W `test_the_flight_gate_normalises_the_airports`, `test_a_session_without_a_plan_makes_no_flight_but_is_marked` |
| A plan filed late is in the flight | W `test_a_plan_filed_late_is_in_the_flight` |
| Plan fields frozen: route latest non-null, `deptime` maximum, others from the session that ended last; a written row is never rewritten | W `test_plan_fields_are_frozen_route_latest_non_null_deptime_maximum_others_from_the_last_session` |
| Equal end times take the higher session id | W `test_equal_end_times_take_the_plan_fields_of_the_higher_session_id` |
| A returning session forms a new flight and leaves the written one alone | W `test_a_returning_session_forms_a_new_flight_and_leaves_the_written_one_alone` |
| A returning unit always starts a new flight (never joins a neighbour) | W `test_a_returning_session_never_joins_a_neighbouring_session_of_its_key` |

## 1.5 Airborne time, maxima

| Requirement | Test |
|---|---|
| v3's measured-gap rule; maxima; sample count; all columns | W `test_one_session_becomes_one_flight_with_the_right_numbers` |
| Capped gap over a long session; zero across a silence over 120 minutes | W `test_a_ten_hour_session_is_one_flight`, `test_a_three_hour_silence_inside_one_session_is_one_flight_with_no_airborne_time_across_it` |

## 1.6 The table

| Requirement | Test |
|---|---|
| Columns, key `(session_id, start_time)`, four indexes, partial work-list index, `flown_upto` | W `test_the_table_the_column_the_setting_and_the_indexes_exist` |
| `end_time >= start_time` | W `test_end_time_may_not_be_before_start_time` |

## 2 The writer

| Requirement | Test |
|---|---|
| One writer at a time (advisory lock) | W `test_a_second_writer_does_not_wait_it_returns_zero` |
| Sessions locked `FOR UPDATE` so a reconnecting pilot waits; a failed call changes nothing | W `test_the_writer_locks_the_sessions_it_marks_so_a_reconnecting_pilot_waits` |
| Pre-existing key: that pilot skipped, unmarked, warned; the others written | W `test_a_pre_existing_key_skips_that_pilot_unmarked_with_a_warning_and_the_others_are_written` |
| Sessions marked `flown_upto = last_seen`, with or without plan or positions | W `test_one_session_becomes_one_flight_with_the_right_numbers`, `test_a_session_without_a_plan_makes_no_flight_but_is_marked` |
| Idempotent: a second call writes 0 | W `test_a_second_call_writes_nothing` |
| Batch cap, oldest-quiet first | W `test_the_batch_cap_limits_pilots_per_call_oldest_quiet_first` |
| Called by the existing 5-minute job, guarded: a writer fault cannot stop the pieces refresh | W `test_refresh_flight_summary_also_runs_the_writer`, `test_a_writer_fault_cannot_stop_the_pieces_refresh` |
| SECURITY DEFINER, `search_path`, not public; collector cannot call it or read the table | W `test_the_writer_is_not_public_and_runs_with_the_owners_rights`; `tests/integration/test_roles.py` (`writer-direct`, `read-flight-record`, `test_maintenance_functions_are_not_public`); `tests/integration/test_grafana_ro.py` (`flight_record` in RAW) |

## 5 Backfill and comparisons

| Requirement | Test |
|---|---|
| Backfill rebuilds every flight in batches and the loop ends at zero | W `test_the_backfill_rebuilds_every_flight_in_batches_and_the_script_loop_ends_at_zero`, `test_the_backfill_script_calls_the_writer_in_its_own_transaction_and_stops_at_zero` |
| 5.3 Placeholder (no-position) flights carried over once and marked | W `test_placeholder_flights_without_positions_are_carried_over_once_and_marked` |
| 5.2 A, agreement: identical rows on every shared column | E `test_ordinary_flights_and_a_reconnect_join_are_identical_in_old_and_new` |
| 5.2 A, expected differences 1 (8-hour parts), 2 (120-minute silence), 3 (join by logon), 4 (frozen plan), 5 (rounding) | E `test_expected_difference_1_...`, `_2_...`, `_3_...`, `_4_...`, `_5_...` |
| 5.2 A, the independent position count | E `test_the_independent_count_new_sample_count_equals_the_positions_of_gate_passing_sessions` |
| 5.2 A tooling: one read-only statement, scope, explained lines, detail rows | C `test_comparison_a_reports_the_move_and_the_explained_differences`, `test_comparison_a_is_read_only_one_statement_and_clean_when_old_and_new_agree` |
| 5.2 B tooling: classes exact, split, v1-only (amendment), v3-only; window and power-cut exclusion | C `test_comparison_b_classifies_exact_split_amended_and_v3_only`, `test_comparison_b_leaves_out_flights_outside_the_window_and_in_the_power_cut` |

## 4 and 7.4 Consumers touched in phase 1

| Requirement | Test |
|---|---|
| Three new watchdog checks fire on broken data and nothing else fires; the pack lists 21 | `tests/integration/test_integrity_checks.py` (`test_each_invariant_fires_on_broken_data[...]` for the three new names, `test_the_cases_cover_exactly_the_invariants_in_the_pack`) |
| Plain wording and action hint for each new check | `tests/test_humane_messages.py` (`test_every_invariant_has_a_plain_wording_and_none_is_stale`, `test_every_invariant_has_its_own_action_hint`, `test_each_invariant_produces_a_sentence_from_the_real_summary_text`) |
| `watchdog_ro` can read `flight_record`; the sweep reads nothing wider | `tests/test_watchdog_checks.py::test_migrate_grants_the_watchdog_role_the_session_tables`, `tests/test_integrity_schedule.py::test_migrate_grants_the_watchdog_role_what_the_checks_read`, `test_watchdog_ro_can_read_everything_the_sweep_and_the_cross_check_need` |
| Restore check counts `flight_record` (and accepts a dump that predates it) | `tests/integration/test_restore_sanity.py` (two new tests; cannot run on this Windows PC, see below) |
| Data dictionary mentions every new object; header names the latest migration | `tests/test_data_dictionary_tests.py` |
| Data tables emptied between tests include `flight_record` | `tests/integration/conftest.py` (`DATA_TABLES`) |

## Added from the independent QA list (`tests/integration/test_flight_writer_qa.py`, `Q` = that file)

| QA scenario | Test |
|---|---|
| J7 running maximum | Q `test_j7_...` |
| J12 silences of 119, 120, 121 minutes never cut | Q `test_j12_...` |
| J13, J14 flight id and earliest logon | Q `test_j13_...`, `test_j14_...` |
| J10, J16, P3, Q7 | Q `test_j10_...`, `test_j16_...`, `test_p3_...`, `test_q7_...` |
| Q8 missing setting defaults to 8; non-numeric raises | Q `test_q8_...` |
| A2, A3, A4, A5, A6, P7 airborne and maxima (75 s cap) | Q `test_a2_...` to `test_a6_...`, `test_p7_...` |
| R2, R5, R6, R7 | Q `test_r2_...`, `test_r5_r6_...`, `test_r7_...` |

Decisions taken from the coordinator: cadence 30 s and cap 75 s (tests that depend on the cap set it); a skipped conflict pilot is not counted in the return value; a second concurrent writer returns 0 without waiting (`pg_try_advisory_xact_lock`). Simplest safe options where the design is silent: NULL groundspeed is not airborne; plan fields come from the last session with NULLs included (route excepted); a non-numeric `flight_quiet_hours` raises; `p_max_pilots` below 1 or NULL returns 0; a session with no positions after `flown_upto` is marked and makes no flight. Not added: QA Q13 (re-check under lock between the pick and the lock), Q15 true parallelism, R3/R4 trigger-injected failure, B1 to B4 (300-pilot batch independence and live load).

## Deliberate deviations from the design

- **Section 2 step 1 (advisory lock).** The design says `pg_advisory_xact_lock`, which waits. The built writer uses `pg_try_advisory_xact_lock` and returns 0 at once if another writer holds it (the manager's decision of 12-10-2026). Test: `test_a_second_writer_does_not_wait_it_returns_zero`.
- **Section 2 "Where it runs".** The guarded call inside `refresh_flight_summary` writes 50 pilots, not the default 500, and names `query_canceled` in its handler, because `WHEN OTHERS` does not catch a statement timeout (the collector uses 20 s) and a cancel would roll back the pieces refresh. Test: `test_a_statement_timeout_in_the_writer_cannot_roll_back_the_pieces_refresh` (fails if the handler is removed). `set_config('statement_timeout', ...)` inside the function cannot re-arm the running statement's timer, so it is not relied on.
- **Function search_path** is `public, pg_temp` on the two functions this migration defines.

## Changes after the Opus second pass (PR 144)

- **Lock and snapshot (defect 1).** The writer keeps the ids of the sessions it locked and uses only those in the main statement and the marker; the quiet re-check runs over all of the pilot's unprocessed sessions. Test `test_opus1_a_session_inserted_after_the_lock_is_neither_written_nor_marked` (holds ACCESS EXCLUSIVE on `pilot_position` so the writer stops after its lock, inserts a session in that window).
- **Starvation (defect 2).** The call loops (at most 10 rounds) and leaves pilots skipped for a key conflict out of the next pick; the WARNING stays. Test `test_opus2_...` (50 conflict pilots and 1 normal pilot, cap 50). A pilot too large for the collector's 20 s statement timeout is NOT handled by this: the cancel is caught by the refresh, but that pilot is picked first again every run until the owner backfills it by hand (`ops/backfill_flight_record.sh`, no timeout).
- **Backfill script (defect 3).** Stops only when no finished pilot is left, retries a busy lock, exits 1 after 5 rounds without progress. Tests: script text and `test_the_backfill_scripts_remaining_query_...`.
- **Migration locking (defect 5).** `SET LOCAL lock_timeout = '5s'`; the carry-over runs only if `flight_summary` has a `sample_count = 0` row; the plain `CREATE INDEX` is kept (the CONCURRENTLY pattern of migrations 22 and 23 needs its own `transaction:false` file, not worth it for a table of a few thousand rows). `SET LOCAL` warns outside a transaction and does nothing, harmless.
- **Future snapshot (defect 6).** `ref` is `LEAST(newest snapshot, now() + 5 minutes)`, test `test_opus6_...`; the position scan also stops at the newest `last_seen` of the locked sessions.

## Known limitation (Opus defect 4): the 8-hour quiet period assumes `logon_time` is close to the first sighting

v1 joins a new connection when its `logon_time` is within 120 minutes of the previous last point. The writer decides at write time, and a flight is written once the pilot has been quiet for 8 hours of stored data. Failing input: session A (YSSY to YMML) is last seen 10:00; session B of the same pilot, callsign and route logs on at 11:00 (within 120 minutes, v1 joins) but is first stored at 19:00 (for example the pilot flew outside the kept area first). At 18:00 the pilot has been quiet for 8 hours, so A is written; B then arrives as a new flight. v1 would have joined them. Rare, accepted, logic unchanged.

## Not proven by a test, and why

- **Atomicity of a crash mid-call** (design section 2, "a crash rolls back inserts and markers together"). The inserts and the markers are one SQL statement inside one function, so PostgreSQL gives this by construction; no fault can be injected between them. The failed-lock tests show a failed call leaves rows and markers untouched.
- **Expected differences 6 and 7 of comparison A** (a session that came back with a changed plan; stale old pieces). Both need live-data conditions; the owner reads them off the real run.
- **Writer speed on the 1-CPU VM** (target under 2 s a batch): not measurable here. `ops/backfill_flight_record.sh` takes `BATCH` for that reason.
- **Comparison B against real v1 data**: the classification is tested on synthetic rows only. The v1 column names in `ops/compare_flights_v1_export.sql` are taken from `docs/reference/v1_metabase_cards.json`, not from the v1 schema.
- **The restore tests** in `test_restore_sanity.py` start `bash` from Python; on this Windows PC `bash` resolves to WSL, which cannot see the `C:` path, so the whole file fails here before and after this change. The new logic was run by hand under Git Bash (old dump: no `flight_record` field; with the table: `flight_record=2`; unreadable: exit 1). CI on Linux runs the file.
