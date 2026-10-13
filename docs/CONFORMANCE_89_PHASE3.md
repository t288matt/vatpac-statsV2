# Issue #89 phase 3: conformance table

Written 12-10-2026 (branch `p3-switch-views`, issues #89 and #139). **Deployed 13-10-2026 (marker 38e3958, 08:14:12 UTC). When written it was not run against PostgreSQL**: no database was available on the machine that built this branch (no local PostgreSQL, Docker Desktop not running), so none of the database tests below has been executed; see "What was not verified" at the end. Each requirement of `docs/DESIGN_89_FINAL.md` (branch `opus-89`, sections 1.2, 4, 7.2 phase 3, 7.4, 7.5) that phase 3 builds, and the test or check that proves it. Test files: `V` = `tests/integration/test_flight_views_switch.py` (new), `P` = `tests/test_phase3_switch.py` (new, no database), `FV` = `test_flight_view.py`, `RM` = `test_flight_reconnect_merge.py`, `E2E` = `test_end_to_end.py`, `A` = `test_audit_t30.py`, `C65` = `test_card_65.py`, `EQ` = `test_flight_writer_equivalence.py`, `CF` = `test_compare_flights.py`. All integration files are in `tests/integration/`.

## Migration `20261013000200_switch_flight_views.sql`

| Requirement | Test or check |
|---|---|
| `flight` is a thin view over the flight table: stored columns plus `duration`, `duration_minutes`, `is_circuit`, `is_zzzz`, `touches_home_region` (live `home_icao_prefixes`); no merge, no join to `pilot_session`, no read of `pilot_position` or the pieces | P `test_the_new_flight_view_reads_only_flight_record_and_the_home_prefix_setting`; V `test_no_panel_view_reaches_the_pieces_the_legacy_view_the_positions_or_pilot_session` |
| `gap_segment`, `span_part` and `is_in_progress` are gone from `flight`; every other column keeps its name, order and type | V `test_the_old_view_is_still_there_under_its_new_name_with_its_old_columns`; FV `test_the_old_columns_are_gone`; `python grafana/check_columns.py` (static: every panel column exists in the new view, "bad 0") |
| `flight` returns the same rows as the old view for finished flights (same data, old path against new path) | V `test_flight_returns_the_same_rows_as_the_old_view_for_finished_flights` |
| `touches_home_region` follows the live home-prefix setting | V `test_touches_home_region_follows_the_home_prefix_setting_in_both_views`; FV `test_flags` |
| The old merge body is kept as `flight_legacy` until phase 5, for the comparison only; nothing else reads it | V `test_the_old_view_is_still_there_under_its_new_name_with_its_old_columns` |
| `flight_ext`: same definition, same columns, without `WHERE NOT is_in_progress` | V `test_flight_ext_returns_the_same_rows_as_the_old_view`; P `test_flight_ext_has_no_in_progress_filter` |
| `movements` and `cohorts`: same definitions | V `test_movements_return_the_same_rows_as_the_old_view`, `test_cohorts_return_the_same_rows_as_the_old_view` |
| `monday_attendance`: no special case; pilot side = distinct CIDs of `flight` rows overlapping 07:00 to 12:00Z; controller side and columns unchanged | V `test_monday_attendance_returns_the_same_rows_as_the_old_view`, `test_monday_attendance_counts_a_pilot_only_once_the_flight_is_written` |
| Re-creation in the same migration with `SET lock_timeout = '5s'` | P `test_the_migration_is_the_newest_and_forward_only` (checks the statement is there); the lock behaviour itself is not tested |
| Forward-only, new number after `20261012000190` | P `test_the_migration_is_the_newest_and_forward_only` |
| Phase boundary 7.5: views, panels and tests only; **nothing dropped or renamed except the old view's rename to `flight_legacy`**; no table, function or index touched; writer, pieces refresh and `flown_upto` untouched | P `test_the_migration_drops_nothing_and_touches_no_table` |
| Grants: `grafana_ro` can read `flight` (and the other panel views) and nothing behind them | V `test_grafana_ro_reads_every_panel_view_and_not_the_tables_behind_them`; `db/roles.sql` grants every view on each deploy and the migration grants `flight` itself when the role exists (no `roles.sql` change); existing `test_grafana_ro.py` (`test_can_select_every_view_in_the_schema`, `test_cannot_select_raw_tables` incl. `flight_record`) |

## Panels

| Requirement | Test or check |
|---|---|
| The 11 `NOT is_in_progress` filters are removed from the dashboards | P `test_no_panel_carries_an_in_progress_filter_or_a_piece_column`, `test_the_eleven_panels_that_read_flight_directly_still_exist_and_are_not_filtered_on_progress`, `test_the_removed_filters_left_valid_sql_behind` |
| Live panels (online now, departure board) are unchanged | P `test_live_panels_do_not_filter_on_in_progress`; V `test_departure_board_still_shows_a_connected_pilot_with_no_flight_row` |
| Every panel runs as `grafana_ro` | V `test_every_panel_runs_as_grafana_ro_on_data_with_the_new_views` (on data); A `test_every_panel_query_runs_as_grafana_ro` (existing, empty database) |
| No panel query needs `pilot_position`, the pieces table (or the legacy view, `flight_record`, `pilot_session`) | P `test_no_panel_reads_the_pieces_the_legacy_view_flight_record_or_the_raw_positions`; V `test_no_panel_view_reaches_the_pieces_the_legacy_view_the_positions_or_pilot_session` (follows the view dependencies to the tables) |
| `validate_dashboards.py` refuses `flight_legacy` in a panel (and `flight_part`) | P `test_the_validator_refuses_a_panel_that_reads_the_legacy_or_piece_views`; `python grafana/validate_dashboards.py` (RESULT: OK) |
| Counting panels show finished (written) flights only; a pilot still connected, or a collector stopped, never makes a flight appear early | V `test_a_flight_not_yet_written_is_in_no_flight_view`, `test_count_panel_over_flight_ext_counts_only_written_flights`, `test_count_panel_over_movements_counts_only_written_flights`, `test_count_panel_reading_flight_directly_counts_only_written_flights`, `test_a_written_flight_is_counted_by_the_panel_that_reads_flight`, `test_a_reconnecting_pilot_is_written_whole_or_not_at_all`, `test_a_stopped_collector_never_makes_a_flight_appear_early`; C65 `test_a_flight_not_yet_written_is_not_counted`; E2E `test_a_pilot_gone_for_8_hours_is_counted_and_pilots_still_connected_are_not` |

## Keep panel results equivalent: old against new on the same data

| Requirement | Test or check |
|---|---|
| Each affected view (`flight`, `flight_ext`, `movements`, `cohorts`, `monday_attendance`) is compared old against new, by EXCEPT ALL in both directions, on the same sessions and positions run through both paths; the old definitions are copied into the test, not derived | V `test_flight_returns_the_same_rows_as_the_old_view_for_finished_flights`, `test_flight_ext_returns_the_same_rows_as_the_old_view`, `test_movements_return_the_same_rows_as_the_old_view`, `test_cohorts_return_the_same_rows_as_the_old_view`, `test_monday_attendance_returns_the_same_rows_as_the_old_view` |
| The same comparison for the owner, on the live data or a restore of a dump, read only, one statement | `ops/compare_views_89_phase3.sql`; V `test_the_phase_3_comparison_script_is_one_read_only_statement`, `test_the_phase_3_comparison_script_reports_no_difference_on_ordinary_data` |
| The expected differences (design 5.2) stay exactly what they are: a session over 8 hours is two old rows and one new; the views built on `flight` follow; Monday attendance counts the pilot once | V `test_a_session_longer_than_8_hours_is_two_old_rows_and_one_new_flight_and_every_view_follows`, `test_the_phase_3_comparison_script_shows_the_expected_difference_for_a_session_over_8_hours` |
| 7.5: "comparison (A) still passes against `flight_legacy`" | `ops/compare_flights_89.sql` now reads `flight_legacy`; CF `test_comparison_a_reports_the_move_and_the_explained_differences`, `test_comparison_a_is_read_only_one_statement_and_clean_when_old_and_new_agree`; EQ (all, repointed to `flight_legacy`) |

## Tests that moved with the switch

| Requirement | Test or check |
|---|---|
| Helper builds sessions and positions, puts a newer snapshot, calls the writer; the files that assert the flight numbers keep their assertions | `helpers.write_all` and `helpers.flights`, used by FV (gate, normalisation, flags, airborne minutes), RM (reconnect rule, 119/120/121 style boundaries, chain, changed plan, movements), A (airborne boundaries), E2E |
| Where v1's rule changes a result, the assertion changes with it and says why | FV `test_gap_over_120_minutes_inside_one_session_is_one_flight`, `test_span_over_8_hours_is_one_flight` (were: split in two); A `test_samples_121_minutes_apart_add_no_airborne_time` (was: two flights) |
| Panel test files that insert flights themselves now insert the row the writer would write (`helpers.record_flight`, plan fields copied from the session, flight gate applied); plan fields are passed to the helper instead of updating the session afterwards | `test_events_t14`, `test_military_events_t15`, `test_online_hours`, `test_panel_rules_gaps`, `test_pilots_people_t15`, `test_time_zones` and `helpers.add_flight` (used by `test_pilots_t14`, `test_card_90`, `test_card_65`, A); `test_watchdog_flap` also checks that `watchdog_ro` cannot write `flight_record` |
| The in-progress tests are deleted with the mechanism | `test_flight_in_progress.py`, `test_exclude_in_progress.py` and `tests/test_in_progress_panels.py` removed (`git rm`); their live-panel and stopped-collector cases live on in V |
| The pieces tests keep working until phase 5 | `test_flight_summary.py` reads the old merged rows through `flight_legacy` |

## Documents

| Requirement | Test or check |
|---|---|
| CHART_DEFINITIONS: the in-progress section becomes one sentence; Monday attendance by the standard rule; phase 3 marked not yet deployed (switched to live on 13-10-2026) | `docs/CHART_DEFINITIONS.md`; `tests/test_chart_definitions_tests.py` (every named test exists) |
| DATA_DICTIONARY: `flight` (thin), `flight_legacy`, `flight_ext`, `movements`, `cohorts`, `monday_attendance`, `flight_record` readers; header names the latest migration | `docs/DATA_DICTIONARY.md`; `tests/test_data_dictionary_tests.py` |
| FLIGHT_VIEW_SCALING, DEFINITIONS, CHANGELOG | noted "not yet deployed" at the time; flipped to live on 13-10-2026 |

## Not built, with the reason

- **ADR-030, SPEC 5 and 6, ARCHITECTURE** (listed under phase 3 in design 7.2/7.4): not written. The brief for this branch named CHART_DEFINITIONS, DATA_DICTIONARY and the tests tied to them; the ADR changes decisions (supersedes ADR-016's piece table and ADR-025's mechanism, amends ADR-023) and is better written once, with the manager, when the phases are merged.
- **Writer-based inserts in the eight panel test files** (design 7.4 says the helper calls the writer): the panel tests insert the frozen row directly (`record_flight`); the writer path is covered by `test_flight_writer*.py`, FV, RM, A, E2E and V (sessions, positions, snapshot, writer, then the views and panels).
- **Re-creation of the four dependents with `DROP VIEW flight CASCADE`** (design section 4): the migration renames the old view instead and uses `CREATE OR REPLACE` for the two views that must change; `movements` and `cohorts` follow `flight_ext` untouched. The result (new `flight`, `flight_legacy`, same dependents, same grants) is the same and nothing is dropped; P `test_the_migration_drops_nothing_and_touches_no_table` pins it.
- **Ops scripts that read the old `flight`** (`ops/audit/throwaway/retention_drill.sql`, `ops/audit/capture.py`, `panel_recompute.py`, `recompute_flights.py`, `tests/scale/gen_synth.sql`): design 7.4 re-points them in phase 5; untouched here.

## What was not verified

- Nothing in this table that needs PostgreSQL has been run: no integration test, no migration apply, no `ops/compare_views_89_phase3.sql`. Statically checked here: Python compiles; `tests/test_phase3_switch.py` passes; `python grafana/validate_dashboards.py` and `python grafana/check_columns.py` pass (the second parses and resolves the new `flight` view and `CREATE OR REPLACE` of `flight_ext` against the new table with sqlglot); the data-dictionary and chart-definition document tests pass.
- Timing: no query plan or speed of the new `flight` view was measured (design section 8 quotes under 1 s at 5 years; it was not re-measured).
- The lock behaviour of the migration on a busy database (`lock_timeout` 5 s) and the order of `migrate.sh` (dbmate, then `roles.sql`) on the VM.
