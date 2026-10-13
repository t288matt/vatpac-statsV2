# Issue #89 phase 4: conformance table

Written 12-10-2026, merged with main (writer of migration 20261012000200) on 13-10-2026. Branch `p4-drop-gate`, issues #89 and #140. **NOT YET DEPLOYED.** Requirements of `docs/DESIGN_89_FINAL.md` sections 3, 5.3, 6, 7.2 (phase 4), 7.4 and 7.5 that phase 4 builds, and the test that proves each. Files: `G` = `tests/integration/test_drop_gate.py`, `L` = `tests/integration/test_partition_drop_locks.py`, `I` = `tests/integration/test_integrity_checks.py`, `R` = `tests/integration/test_roles.py`, `T` = `tests/integration/test_audit_t30.py`, `F` = `tests/integration/test_flight_summary.py`. The migration is `db/migrations/20261012000210_drop_gate_reconciles_flights.sql`. The semantics of `flown_upto` (per session) are unchanged by the per-flight writer of migration 20261012000200, so the gate needs no change for it; every test builds its flights by running the real writer.

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
| Same signature and return value; at most 10 per call; collector loop reaches the end state | L `test_at_most_ten_per_call_and_the_collector_loop_reaches_the_end_state`, G `test_a_catch_up_through_the_collector_loop_drops_every_reconciled_day` |
| Due, unpinned, processed partitions dropped; written flights untouched | L `test_due_unpinned_processed_partitions_are_dropped_and_the_rest_kept` |
| Checks take no lock on the parent | L `test_the_parent_is_not_locked_while_the_partitions_are_checked` |
| Step 2 re-check (a late position between check and re-check keeps it) | L `test_a_late_position_between_the_check_and_the_recheck_keeps_the_partition` |
| In-lock re-check by session id (a session back between re-check and lock keeps it) | L `test_a_session_seen_again_between_the_recheck_and_the_lock_keeps_the_partition` |
| Lock and busy-partition behaviour unchanged (NOWAIT, 1 s timeout, retries only before the first drop, lock_timeout restored) | L `test_lock_timeout_is_restored`, `test_busy_partitions_are_kept_without_an_error_or_a_stall`, `test_a_reader_that_leaves_during_the_retries_lets_the_drop_through`, `test_an_open_writer_transaction_on_the_parent_does_not_hold_the_drop_for_the_session_lock_timeout`, `test_a_busy_partition_after_the_first_drop_is_skipped_without_waiting`, `test_a_pinned_partition_and_a_busy_one_in_the_same_run` |
| No writer advisory lock taken (monotone gate) | G `test_the_function_no_longer_mentions_the_old_finalisation` (body contains no writer lock or finalisation); not separately tested, see below |
| Permissions: helper not public, not granted, pinned `search_path`; collector can still call the drop function | R `test_maintenance_functions_are_not_public`, R `test_collector_can_run_the_maintenance_functions` (existing, calls the drop function as `collector`), G `test_the_reconciliation_is_not_granted_to_anyone` |

## Refusal log line and watchdog alert

| Requirement | Test |
|---|---|
| A refusal writes a clear `DROP REFUSED` line naming partition, reason and session ids | G tests above that read `notices` (`refused(...)`) |
| Watchdog alert from the data: new integrity check fires on a processed plan-session in no written flight, clears when a flight lists it, ignores no-plan sessions; pack has 22 checks, all with plain wording and an action | I `test_each_invariant_fires_on_broken_data` (new case), I `test_a_processed_session_that_is_in_a_written_flight_does_not_fire_the_gate_invariant`, I `test_the_cases_cover_exactly_the_invariants_in_the_pack`, `tests/test_humane_messages.py`, `tests/test_integrity_schedule.py` |

## Rehearsal (design 7.2, 7.5)

| Requirement | Test or document |
|---|---|
| Rehearsal script predicts exactly what the function drops (drop, refuse and pin cases) and checks flights and positions are accounted for | G `test_the_rehearsal_script_predicts_exactly_what_the_function_drops` (needs `psql`) |
| Script refuses a database whose name does not end in `_test` | G `test_the_rehearsal_script_refuses_a_database_not_named_for_testing` |
| Exact owner steps for a restored live dump, success criteria, clean-up | `docs/RUNBOOKS/drop-gate-rehearsal.md` section 4 (a document; the real rehearsal has not been run) |

## Section 5.3 (placeholder rows)

Placeholder flights (`sample_count = 0`, no positions) never block a drop: G `test_a_flight_with_a_sample_count_of_zero_is_not_counted`. The carry-over itself belongs to phase 1.

## Requirements with no test, and why

- The expected placeholder count of zero on a restored dump (5.3): needs the live data; the runbook does not repeat it, the phase 1 backfill report owns it.
- Behaviour at live volume and on PostgreSQL 16.15 / 16.4: tests ran on a local PostgreSQL 16.2 (the bundled build has no time-zone database; the Australia/Sydney pass was emulated with a fixed +10 offset).
- "No writer lock": shown only by the function body having none; a concurrent writer race is argued in the migration header (monotone gate), not tested.
- Fallback to retention 365 (design 6): an owner action, unchanged by this phase.
- Deploy and the first real drop: not done; see the checklist in the runbook, section 3.
