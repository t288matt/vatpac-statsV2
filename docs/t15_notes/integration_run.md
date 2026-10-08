# T15 integration run (branch t15-integration, 08-10-2026, VM clock about 08:30 UTC)

Merged t15-a, t15-b, t15-c into t15-integration in that order with `git merge --no-edit`: no conflicts. Nothing pushed, nothing merged into main.

## Static checks (all green after the merges)
validate_dashboards.py: OK, 0 errors, 4 warnings (189 SQL statements parsed; the warnings are "no time range" on the all-history monthly panels: atc-retention 15, management 3, pilot-onboarding 4 and 5, all intentional). check_columns.py: bad 0. Unit tests: 205 passed. ruff: clean. Size budget: 1335 of 1400. Config drift: OK.

## Integration suite (VM, throwaway database vatpac_test, method of RESUME section 5)
All 14 migrations applied in order with ON_ERROR_STOP=1 (including 20261008000030, 000031 and the new 20261008000080 pilot_sample view). test_roles.py and test_grafana_ro.py deselected, roles.sql never run.

| Run | Result |
|---|---|
| First, as merged | 154 passed, 4 failed, 1 skipped |
| After fixes | 158 passed, 0 failed, 1 skipped |

The one skip is pre-existing (tests/integration/test_ingest.py:106, apply_snapshot does not write poll_log).

## Failures and fixes
1. test_card_156_airborne_count_per_slot_on_latest_monday (test bug): helper `_add_samples` used the first and last list items as first_seen and last_seen; the C sample list was not in time order, which violated check constraint pilot_session_seen_order. Fixed with min and max of the sample times. Date dependence: the test skips before 12:00 UTC on the Monday, so it ran today only because the latest Monday (05-10-2026) is complete.
2. test_181_never_flew_that_month, test_182_lapsed_pilots_by_month, test_182_a_month_without_flights_is_skipped_as_in_v1 (real, cosmetic panel defect): the month label was `TRIM(TO_CHAR(month, 'Month YYYY'))`. `Month` pads to nine characters, so TRIM only removed the ends and the label came out as "January   2026" (v1 had the same padding; the t15-a note claims TRIM fixes it, it does not). Changed to `TO_CHAR(..., 'FMMonth YYYY')` in panels 4 and 5 of grafana/dashboards/pilots/pilot-onboarding.json; this also corrects the retention_summary text. The tests were already expecting the unpadded form.

No other defects. The `RANGE ... interval FOLLOWING` window construct flagged as highest risk in the 186 note executed correctly on PostgreSQL 16 (test_atc_sessions_t15.py all pass).

## New and changed panel SQL run on vatpac_test (substitutions as specified: 200 days / now())
16 queries (every panel changed or added by the three branches that has no $__timeFilter, $__timeGroup or $__interval macro; none skipped), all ran without error. The database held only leftover test rows, so counts and timings are not representative of live volumes; each took 0.9 to 6.2 ms.

| Dashboard / panel | Rows | ms |
|---|---|---|
| atc-overview 11 Top S1 | 0 | 6.2 |
| atc-overview 12 Top S2 | 3 | 1.4 |
| atc-overview 13 longest median | 0 | 2.1 |
| atc-overview 14 shortest median | 0 | 1.7 |
| atc-overview 15 (card 186) | 6 | 4.5 |
| monday-events 4 (card 156) | 60 | 1.7 |
| management 3 (175) | 1 | 3.2 |
| management 4 (183) | 0 | 1.1 |
| military 5 | 0 | 1.2 |
| military 6 | 0 | 1.6 |
| military 7 | 0 | 1.1 |
| military 8 | 0 | 0.9 |
| pilot-onboarding 3 | 0 | 1.5 |
| pilot-onboarding 4 (181) | 0 | 0.9 |
| pilot-onboarding 5 (182) | 0 | 1.5 |
| pilots-traffic 16 (155) | 0 | 0.9 |

## EXPLAIN (COSTS OFF, no ANALYZE), on the throwaway database only
Only vatpac_test was used (hard rule); costs and row estimates therefore reflect near-empty tables. Re-check on the live database after deploy with EXPLAIN (ANALYZE, BUFFERS) in a quiet moment.

- Card 186 (atc-overview panel 15): reads `controller_session` with a Seq Scan, sorts the whole table by (cid, callsign, first_seen, id) for the 5-minute merge, and applies the 200-day time filter only after merging (a HAVING-style filter on the GroupAggregate), so cost grows with total controller_session history, not with the dashboard range. Candidate windows are a hash join of sessions with a VALUES list (three starts), followed by a window sum per CID and window; no heavy nested loop except the final detail join against the few violating CIDs. Fine for tens of thousands of sessions; if the table reaches millions, push the time predicate into the first CTE (keeping a margin of 8 h plus the merge gap) before the merge.
- Card 156 (monday-events panel 4, view pilot_sample): `pilot_position` is scanned with a Bitmap Index Scan on the primary key, restricted to `observed_at >= monday 07:00 and < monday 12:00`, with `alt_ft > 1000` as a filter. Only one partition (pilot_position_20261007) appeared in the plan, so partition pruning is in use (the bounds come from an InitPlan on the `mon` CTE, so pruning is at run time). Shape concern: the slots are joined to the positions with a Nested Loop plus Materialize and a range Join Filter (61 slots times every position row in the window); with a real Monday (five hours of positions for a few hundred aircraft) that is a few million comparisons per run, probably well under a second to a few seconds, but it should be measured on live data. A cheaper form would bucket each position into its slot with date_bin and count distinct per bucket.

## Open items
- 156 and 186 timings on live volumes still to be measured after deploy.
- The t15-a note claim that TRIM removes the padding is wrong; the FM fix supersedes it.
- Docs rows (CHART_DEFINITIONS.md, DATA_DICTIONARY.md pilot_sample) still to be added by the coordinator.

## Clean-up
vatpac_test dropped (pg_database lists only postgres, vatpac, template0, template1); /tmp/vtest and the helper scripts deleted; five containers (db, collector, grafana, backup, watchdog) untouched, no compose command and no roles.sql run; nothing written inside ~/vatpac-statsV2.
