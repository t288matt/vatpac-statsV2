# Issue #89 phase 4: conformance table

> **Note, issue 89 phase 5 (LIVE since 13-10-2026, marker 7a71d30 at 10:05:56 UTC).** This is a dated record. It uses the names as they were when it was written: `flight_summary` meant the old pieces table, `flight_record` the table of finished flights. Migration 20261013000300 (live) dropped the pieces table, `flight_part`, `flight_legacy`, `refresh_flight_summary` and `refresh_flight_summary_sessions`, and renamed `flight_record` to `flight_summary`. Current description: [DATA_DICTIONARY.md](DATA_DICTIONARY.md) and ADR-030.

Written 12-10-2026, merged with main (writer of migration 20261012000200) on 13-10-2026. Branch `p4-drop-gate`, issues #89 and #140. **DEPLOYED 13-10-2026** (about 10:03 UTC on commit 7a71d3054506d5d6e5e0165860de0feb97e0f651 together with phase 5, migration applied in 33 ms, release marker 10:05:56 UTC). Requirements of `docs/DESIGN_89_FINAL.md` sections 3, 5.3, 6, 7.2 (phase 4), 7.4 and 7.5 that phase 4 builds, and the test that proves each. Files: `G` = `tests/integration/test_drop_gate.py`, `L` = `tests/integration/test_partition_drop_locks.py`, `I` = `tests/integration/test_integrity_checks.py`, `R` = `tests/integration/test_roles.py`, `T` = `tests/integration/test_audit_t30.py`, `F` = `tests/integration/test_flight_summary.py`. The migration is `db/migrations/20261013000210_drop_gate_reconciles_flights.sql`. The semantics of `flown_upto` (per session) are unchanged by the per-flight writer of migration 20261012000200, so the gate needs no change for it; every test builds its flights by running the real writer.

## Section 3: the gate

| Requirement | Test |
|---|---|
| 1. Processed: positions after `flown_upto` keep the partition; after the writer it drops | G `test_unprocessed_positions_keep_the_partition_and_after_the_writer_it_drops` |
| Marker one second short keeps; exactly at the last position is enough | G `test_a_marker_before_the_last_position_keeps_the_partition`, `test_a_marker_exactly_at_the_last_position_is_enough` |
| Writer not run (no snapshot) means nothing dropped (fail safe) | G `test_a_database_with_no_snapshot_writes_nothing_so_nothing_is_dropped` |
| A pilot not yet quiet (7 h 59 min) keeps it; at 8 h it drops | G `test_a_pilot_not_yet_quiet_keeps_the_partition` |
| A no-plan session needs only the marker; blank plan counts as no plan | G `test_a_session_without_a_plan_needs_only_the_marker`, `test_a_blank_plan_counts_as_no_plan_like_the_writer` |
| 2. Covered: marked but flight missing keeps it, with a NOTICE naming partition and session | G `test_a_marked_session_whose_flight_is_missing_keeps_the_partition` |
| Flight range must include every position (both ends inclusive) | G `test_a_flight_that_stops_short_of_a_position_keeps_the_partition`, `test_a_flight_that_starts_after_a_position_keeps_the_partition` |
| Coverage is by session, including later members of a joined flight | G `test_a_flight_of_another_session_does_not_cover`, `test_a_session_covered_as_a_later_member_of_a_joined_flight_is_covered` |
| A flight over two days covers both; the second day still drops once the first is gone | G `test_a_flight_across_two_days_is_covered_in_both_and_the_second_day_still_drops_after_the_first_is_gone` |
| A returning session keeps only the partitions with its new positions | G `test_a_returning_session_keeps_only_the_partitions_with_its_new_positions` |
| A plan filed after the first part was processed keeps the partition (design: kept, NOTICE) | G `test_a_plan_filed_after_the_first_part_was_processed_keeps_the_partition` |
| 3. Counted: `sample_count` against stored positions for flights wholly inside the day; zero-count placeholders ignored | G `test_a_flight_wholly_inside_the_day_with_a_wrong_sample_count_keeps_the_partition`, `test_a_flight_with_a_sample_count_of_zero_is_not_counted` |
| Old finalisation is no precondition (final pieces do not allow, missing pieces do not prevent); the body no longer mentions it | G `test_the_pieces_table_is_not_a_precondition_either_way`, `test_the_function_no_longer_mentions_the_old_finalisation` |
| Pinning rule kept (and the cut-off boundary) | G `test_pinning_still_keeps_a_processed_and_covered_partition`, T `test_session_last_seen_exactly_at_the_cutoff_pins_its_partition`, T `test_retention_drops_exactly_the_days_older_than_ninety` |
| "Kept beyond twice the retention" notice stays | G `test_a_beyond_twice_the_retention_kept_partition_says_so` |
| Empty partition drops; one bad partition does not stop the good ones | G `test_an_empty_partition_is_dropped`, `test_one_bad_partition_does_not_stop_the_good_ones` |
| The reconciliation function names each problem with its session; no rows for a clean or empty day; missing session row | G `test_the_reconciliation_names_each_problem_with_its_session`, `test_a_day_with_no_positions_has_nothing_unreconciled`, `test_a_position_with_no_session_row_is_unreconciled` |
| Set-based performance (90,000 positions in a day within seconds; measured 0.04 s locally) | G `test_a_busy_day_is_reconciled_in_a_few_seconds` |
| Flights survive the drop with the right numbers | F `test_flights_survive_partition_drop` |

## Changes to the drop function (design 3, second half)

| Requirement | Test |
|---|---|
| Same signature and return value; ONE drop per call (third review, 13-10-2026); collector loop (0.5 s pause, up to 600 calls) reaches the end state | L `test_one_day_per_call_and_the_collector_loop_reaches_the_end_state`, G `test_three_due_reconciled_days_are_dropped_one_per_call`, G `test_a_catch_up_through_the_collector_loop_drops_every_reconciled_day` |
| Due, unpinned, processed partitions dropped; written flights untouched | L `test_due_unpinned_processed_partitions_are_dropped_and_the_rest_kept` |
| Checks take no lock on the parent | L `test_the_parent_is_not_locked_while_the_partitions_are_checked` |
| Step 2 re-check (a late position between check and re-check keeps it) | L `test_a_late_position_between_the_check_and_the_recheck_keeps_the_partition` |
| In-lock re-check by session id (a session back between re-check and lock keeps it) | L `test_a_session_seen_again_between_the_recheck_and_the_lock_keeps_the_partition` |
| Lock and busy-partition behaviour unchanged (NOWAIT, 1 s timeout, retries only before the first drop, lock_timeout restored) | L `test_lock_timeout_is_restored`, `test_busy_partitions_are_kept_without_an_error_or_a_stall`, `test_a_reader_that_leaves_during_the_retries_lets_the_drop_through`, `test_an_open_writer_transaction_on_the_parent_does_not_hold_the_drop_for_the_session_lock_timeout`, `test_a_busy_partition_after_the_first_drop_is_skipped_without_waiting`, `test_a_pinned_partition_and_a_busy_one_in_the_same_run` |
| No writer advisory lock taken (monotone gate) | G `test_the_function_no_longer_mentions_the_old_finalisation` (body contains no writer lock or finalisation); not separately tested, see below |
| Permissions: helper not public, not granted, pinned `search_path`; collector can still call the drop function | R `test_maintenance_functions_are_not_public`, R `test_collector_can_write_data_and_run_maintenance` (existing, calls the drop function as `collector`), G `test_the_reconciliation_is_not_granted_to_anyone` |

## Refusal log line and watchdog alert

| Requirement | Test |
|---|---|
| A refusal writes a clear `DROP REFUSED` line naming partition, reason and session ids | G tests above that read `notices` (`refused(...)`) |
| Watchdog alert from the data: new integrity check fires on a processed plan-session in no written flight, clears when a flight lists it, ignores no-plan sessions; pack had 23 checks on main at the time of writing (21 on the live database until phase 4 was deployed; phase 5 later removed two, so 21 are live now), all with plain wording and an action | I `test_each_invariant_fires_on_broken_data` (new case), I `test_a_processed_session_that_is_in_a_written_flight_does_not_fire_the_gate_invariant`, I `test_the_cases_cover_exactly_the_invariants_in_the_pack`, `tests/test_humane_messages.py`, `tests/test_integrity_schedule.py` |

## Rehearsal (design 7.2, 7.5)

| Requirement | Test or document |
|---|---|
| Rehearsal script predicts exactly what the function drops (drop, refuse and pin cases) and checks flights and positions are accounted for | G `test_the_rehearsal_script_predicts_exactly_what_the_function_drops` (needs `psql`) |
| Script refuses a database whose name does not end in `_test` | G `test_the_rehearsal_script_refuses_a_database_not_named_for_testing` |
| Exact owner steps for a restored live dump, success criteria, clean-up | `docs/RUNBOOKS/drop-gate-rehearsal.md` section 4 (a document; the real rehearsal was run on the VM on the morning of 13-10-2026, before the deploy, two runs, both OK: see the runbook status line) |

## Review fixes (13-10-2026)

| Requirement | Test |
|---|---|
| 1. `search_path = public, pg_temp` and qualified tables on the drop function and the helper; temp tables named like the real ones cannot make the gate drop a day | G `test_a_temp_table_named_like_a_real_table_cannot_make_the_gate_drop_a_day` |
| 2. Midnight-spanning flights: sample_count compared with stored positions while every day of the flight is stored; skipped when a day is already gone | G `test_a_late_position_inside_a_midnight_spanning_flight_keeps_both_days`, `test_a_flight_across_two_days_is_covered_in_both_and_the_second_day_still_drops_after_the_first_is_gone` |
| 3. Alert at any age: `drop_gate_refusal` and the check `raw-position days kept by the deletion check for more than 3 days` | G `test_a_refusal_is_recorded_for_the_watchdog_and_cleared_when_the_day_reconciles_or_goes`, G `test_the_refusal_table_is_readable_by_the_watchdog_role_only_for_reading`, I `test_each_invariant_fires_on_broken_data`, I `test_a_fresh_refusal_does_not_fire_the_gate_refusal_check`; wording in `ops/watchdog/messages.py`, row in `integrity-checks.md`, counts in `alerts.md` |
| 4. Rehearsal: independent per-position count (`gate_too_lax`) and optional run as the collector's role | G `test_the_rehearsal_script_predicts_exactly_what_the_function_drops[None, collector]`; runbook 4.3 and 4.4 |
| 5. No date-to-text conversion (DateStyle) in the function or the script | G `test_the_gate_does_not_depend_on_the_date_style` |

## Second review fixes (13-10-2026; migration renamed 20261013000210 after merging phase 3, migration 20261013000200)

| Requirement | Test |
|---|---|
| 1. The refuse-on-wrong-database test builds its URI with `urlsplit` (the time-zone option contains a slash), so it also passes in the Australia/Sydney pass | G `test_the_rehearsal_script_refuses_a_database_not_named_for_testing` |
| 2. Re-run on the combined code: the gate reads only `pilot_session`, `pilot_position`, `flight_record`; it does not touch the pieces or the new views. `test_flight_summary.py` now reads `flight_legacy` (phase 3) and still passes with the gate; the 23rd check reads `drop_gate_refusal` only | whole suite run after the merge (see the report); G `test_the_pieces_table_is_not_a_precondition_either_way` |
| 3. After the lock the session list is rebuilt from the partition and the whole gate (pinning, processed, covered, counted) is repeated | G `test_a_position_added_by_a_session_not_in_the_earlier_list_between_check_and_lock_keeps_the_day` (event trigger inserts the position at the first DROP); L `test_a_session_seen_again_between_the_recheck_and_the_lock_keeps_the_partition` |
| 4. A refusal row and a `DROP REFUSED` notice on every keep path including lock failure; a row is cleared only when the day is dropped, no longer exists or is not due; a pinned day clears it | G `test_a_day_that_cannot_be_locked_is_refused_on_the_record_and_cleared_when_it_goes`, `test_refusal_rows_stay_until_the_day_is_dropped_or_gone_or_not_due` |
| 5. Rehearsal: the independent count uses a LEFT JOIN (a missing session row is uncovered); run 2 deletes exactly one flight; run 1 is preceded by a writer catch-up step and the text says how to tell a timing effect from a finding | `ops/rehearse_drop_gate.sql`; runbook 4.3b, 4.4, 4.5 (documents; the real rehearsal is not run) |

## Third review fix (13-10-2026): one drop per call

After the first DROP the parent table stays ACCESS EXCLUSIVE locked to the end of the call; each further day would then run the full in-lock reconciliation (about 90,000 positions) under that lock and could pass the collector's 5 second insert timeout. The function now checks at most 3 candidate days and drops at most one (`EXIT` after the first drop); `maintenance.drop_old_partitions` loops up to 600 times with a 0.5 second pause (`DROP_PAUSE_S`), so the lock is released between days. Refusal-row behaviour is unchanged. Tests: G `test_three_due_reconciled_days_are_dropped_one_per_call` (three due days, one per call, the next call the next), L `test_one_day_per_call_and_the_collector_loop_reaches_the_end_state` (25 days drained by the loop), L `test_a_busy_first_day_does_not_starve_the_next_and_only_one_is_dropped`, L `test_busy_partitions_are_kept_without_an_error_or_a_stall` (three candidates, one drop), and the race tests moved to a hook inside the function (L `test_a_session_seen_again_between_the_recheck_and_the_lock_keeps_the_partition`, `test_a_position_added_by_a_session_not_in_the_earlier_list_between_check_and_lock_keeps_the_day`). Not tested: lock hold time at live volume.

## Section 5.3 (placeholder rows)

Placeholder flights (`sample_count = 0`, no positions) never block a drop: G `test_a_flight_with_a_sample_count_of_zero_is_not_counted`. The carry-over itself belongs to phase 1.

## Requirements with no test, and why

- The expected placeholder count of zero on a restored dump (5.3): needs the live data; the runbook does not repeat it, the phase 1 backfill report owns it.
- Behaviour at live volume and on PostgreSQL 16.15 / 16.4: tests ran on a local PostgreSQL 16.2 (the bundled build has no time-zone database; the Australia/Sydney pass was emulated with a fixed +10 offset).
- "No writer lock": shown only by the function body having none; a concurrent writer race is argued in the migration header (monotone gate), not tested.
- Fallback to retention 365 (design 6): an owner action, unchanged by this phase.
- The deploy is done (13-10-2026). The first real drop is not: nothing is due before about 05-04-2027 at 180 days retention, and the lock-hold time of one drop at live volume (about 78,000 positions a day) is still to be measured before it (owner accepted on issue #140); see the checklist in the runbook, section 3.
