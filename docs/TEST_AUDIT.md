# Test audit (T30, parts 1 to 6)

> **Note, issue 89 phase 5 (LIVE since 13-10-2026, marker 7a71d30 at 10:05:56 UTC).** This is a dated record. It uses the names as they were when it was written: `flight_summary` meant the old pieces table, `flight_record` the table of finished flights. Migration 20261013000300 (live) dropped the pieces table, `flight_part`, `flight_legacy`, `refresh_flight_summary` and `refresh_flight_summary_sessions`, and renamed `flight_record` to `flight_summary`. Current description: [DATA_DICTIONARY.md](DATA_DICTIONARY.md) and ADR-030.

Independent audit of the unit tests, the integration tests and the check scripts in `ops/`, by a reviewer who did not write them. Dates are DD-MM-YYYY and all times UTC. Audited tree: `main` at `e1cfce3` (deployed code `fec82a4`). `main` has since moved on to `00630d4`; it now contains the T36 fix for issue #15 (`logon_time` to whole seconds, migration 100, new tests) and a dashboard change. `main` is merged into this branch, the full suite was re-run on the merged tree, and the audit's tests were adapted to the new CHECK constraint. The mutation numbers below were measured on `e1cfce3` (migration 100 only changes `logon_time`, so none of the mutated rules is affected). The data-integrity half of T30 (parts A to D) is done by another agent and is not repeated here. Branch `t30-test-audit`, nothing pushed.

## 1. Summary

The suite is better than a typical "does it run" suite in the places where the authors thought about rules: the flight-summary maths has an independent Python reference implementation, the T14/T15 panel tests state v1 rules and test edges by hand (for example "exactly 30 minutes is not over 30", "ends exactly 07:00"), and the ingest filters are covered by a golden fixture whose expected numbers come from an independent generator. But it was written rule by rule, and many rules were never given a test at their boundary, or at all.

Mutation check, on a throwaway PostgreSQL 16.4 cluster: **131 mutants** written (collector Python, migration SQL, panel SQL, panel JSON), 2 did not build (not counted), **129 valid**. Against the suite as found: **56 killed, 73 survived**. After the tests added by this audit (59 new tests in `tests/integration/test_audit_t30.py` and `collector/tests/unit/test_audit_t30_unit.py`, one commit each): **120 killed, 9 survive** (5 of those are equivalent mutants, 4 need a clock or time-zone control; section 6). The new tests were each proven to fail against the mutant they target.

Top findings (full list in section 2):

1. **No test of the daily maintenance wiring or of the partition-retention boundary** (`maintenance.py`, `20261007000021_partition_functions_secure.sql`). A mutant that made the collector drop partitions with a 1-day retention, one that halved the retention, and one that dropped a partition one day early all passed every test.
2. **The controller whitelist is not tested as an exact, case-sensitive match at the point where it is applied** (`store.py:215`); prefix matching and case-insensitive matching both survived.
3. **The 60 kt airborne threshold and the 120 minute pair rule were never tested at the boundary** (`20261007000020_flight_summary.sql:69,71`); `> 60` survived.
4. **`online_now`, `controller_online_now`, `pilot_online_now` and the 5-minute reconnect boundary had no tests of their windows** (cards 68, 69, 99 and everything built on merged controller sessions).
5. **40 of the 84 query panels were never executed by any test** (they were only parsed), 44 had no named test, and about 25 "KEPT" rows of `docs/CHART_DEFINITIONS.md` name a check in their Test column that does not exist.
6. **The suite only ever runs in a UTC database session.** Three tests failed as soon as the session time zone was Australia/Sydney (test bugs, fixed here), and three mutants that remove UTC handling still survive.
7. **Dead and conditional tests**: `test_ingest.py:106` always skips (the assertions behind it never run); `test_military_events_t15.py:193` silently skips on Mondays before 12:00 UTC; every integration test silently skips if `VATPAC_TEST_DSN` is missing.
8. **Issue #15 (logon_time precision) was not caught by any test**: no test varied the fractional digits of `logon_time` for the same session. The T36 fix on `main` adds regression tests (`test_logon_precision_ingest.py`, `test_logon_whole_seconds_migration.py`); this audit does not duplicate them.

## 2. Findings

Each finding has a suggested GitHub issue title and a one-line body. Severity is about the chance that a real defect reaches the live dashboards unnoticed.

### High

| # | Finding | Where | Evidence | Suggested issue |
|---|---|---|---|---|
| H1 | Daily maintenance and partition retention are barely tested. `run_maintenance` is never called by a test except as the `collector` role in `test_roles.py:79` (which only checks it runs); `trim_poll_log`, `refresh_flights`, `sync_settings`, the `PARTITIONS_AHEAD` default and the retention setting wiring have no test. The retention boundary (a partition ending exactly at the cut-off) and scale (days to hours) are untested: tests use 12 and 128 day old partitions only. | `collector/src/vatpac/maintenance.py:19-22,25,68,73,83-95`; `db/migrations/20261007000021_partition_functions_secure.sql:22,66,77,83`; `tests/integration/test_partitions.py:60-95` | Mutants C27 to C31, S20 to S22, S24 survived; now killed by 9 new tests. | "Test the daily maintenance task and partition retention boundaries" - "run_maintenance, trim_poll_log, refresh_flights and the 90-day cut-off have no boundary tests; mutants that drop partitions early survive." |
| H2 | The controller whitelist is applied inline in `apply_snapshot` and only tested through one fixture with no near-duplicate callsigns. Prefix match and case-insensitive match both survive. A relaxed match would store controllers that must not be stored (SPEC section 4). | `collector/src/vatpac/store.py:215`; `tests/integration/test_ingest.py:34-45`; the unit tests test only the loader and `is_near_miss` | Mutants C08, C09 survived; killed by `test_controller_whitelist_is_an_exact_case_sensitive_match`. | "Test that apply_snapshot keeps only exact whitelist callsigns" - "A callsign such as ML_APP2 or ml_app next to a listed ML_APP must not be stored; no test covered this." |
| H3 | Airborne minutes: no test at exactly 60 kt, none for two samples exactly 120 minutes apart, none for rounding. `gs_kt > 60` and `< 120 minutes` pass the whole suite. These decide card 94/95/93 and every airborne-hours chart. | `db/migrations/20261007000020_flight_summary.sql:69,71,97`; `tests/integration/test_flight_view.py:114-143` (uses 59, 250, 0, None only), `tests/integration/test_flight_summary.py:48-80` | S01, S03 survived; killed by 4 new tests. | "Add boundary tests for the 60 kt airborne rule and the 120 minute pair rule" - "gs=60 and a 120 minute pair are untested; flipping either comparison passes CI." |
| H4 | The "online now" views had no window tests: `online_now` (30 s window), `controller_online_now`, `pilot_online_now` (5 minute guard), `controller_session_merged.is_open`. Only the stale-snapshot guard of `online_now` is tested (`test_roles.py:146`). | `db/migrations/20261007000024_online_now_guard_and_tuning.sql:11,19,28`; `20261007000010_analytics_views.sql:131,158`; `20261007000004_views.sql:136` | V05, V06, V08, V09, V10 survived; killed by 3 new tests. | "Test the online-now windows" - "online_now, controller_online_now and pilot_online_now windows can be widened to hours without a failing test." |
| H5 | Reconnect merge: only a 3 minute gap is tested (`test_atc_sessions_t15.py:228`). A merge at exactly 5:00, a gap of 5:01 and of 7 minutes were untested; `>=` survived. | `db/migrations/20261007000004_views.sql:112` | V02 survived; killed by `test_reconnect_after_exactly_five_minutes_is_one_session`. | "Test the 5 minute controller reconnect boundary" - "Gaps of exactly 5:00, 5:01 and 7:00 are untested; the SPEC boundary can flip unnoticed." |
| H6 | `docs/CHART_DEFINITIONS.md` Test column overstates coverage: rows such as card 40, 45, 46, 57, 58, 59, 97, 99, 100, 101, 103, 128, 138, 68, 69, 118 and 120 describe checks ("manual count", "sums to") that no test performs. 44 of 84 panels have no test naming them, 40 were never executed at all (see section 4). | `docs/CHART_DEFINITIONS.md` rows 20-55 | Section 4 table; `test_every_panel_query_runs_as_grafana_ro` now executes all 84. | "Make the CHART_DEFINITIONS Test column truthful" - "Mark each card as asserted, smoke-only or untested, or add the test; many rows claim checks that do not exist." |
| H7 | No test protected session identity against logon_time formatting (issue #15). Existing identity tests only varied the second (`test_upserts_and_events.py` "new_logon_time_is_a_new_session"). Found in production, not by tests. Now covered on `main` by the T36 tests. | `collector/src/vatpac/model.py:97-104`, `store.py:35` | Closed by T36; the lesson is that identity keys need a "same session, different formatting" test, and the same for `controller_session`. | (already issue #15) - "Add an identity-flap check to the T16 permanent checks (ops/check_identity_flap.sql exists)." |

### Medium

| # | Finding | Where | Evidence | Suggested issue |
|---|---|---|---|---|
| M1 | The suite is blind to time zones: it only runs with a UTC session, so removing `AT TIME ZONE 'UTC'` survives (U03, U05, U07). Under `TimeZone=Australia/Sydney` three tests failed for test-side reasons (fixed here): `test_ingest.py:56` read the local date, `test_atc_sessions_t15.py:170` subtracted same-zone datetimes across a DST change, `test_pilots_t14.py:46` used `interval 'N days'` which shifts by an hour across DST. Production is safe because `grafana_ro` is pinned to UTC (`db/roles.sql`, tested), but nothing exercises the UTC handling. | `tests/integration/conftest.py:58` (connection), `.github/workflows/ci.yml:115-120` | Full suite green under UTC, Australia/Sydney and America/Los_Angeles after the fixes (470 passed, 1 skipped each before the merge; 495 passed, 1 skipped under UTC and Sydney after it). | "Run the integration tests a second time with a non-UTC session time zone in CI" - "Set options=-c TimeZone=Australia/Sydney in VATPAC_TEST_DSN for a second pass so UTC handling is exercised." |
| M2 | Silent skips and dead assertions. `test_ingest.py:106` always skips (the collector writes `poll_log` in `main.py`, not in `apply_snapshot`), so the `poll_log` count assertions never run; `write_poll_log` and `record_reference_change` have no test. `test_ingest.py:136` and `test_partitions.py:62-66` skip if behaviour or names change. `test_military_events_t15.py:193` skips on Mondays before 12:00 UTC. `test_roles.py` skips when the roles are absent (lines 29, 40, 51, 59, 68, 115). Without `VATPAC_TEST_DSN` every integration test skips and pytest exits 0 (`tests/integration/conftest.py:40-45`). | as listed | Run: 495 passed, 1 skipped (the :107 one; it was :106 before this audit's one-line fix above it). | "Remove or fix the silent skips in the integration tests" - "Turn skips into failures where CI sets the prerequisite; add a CI check that the number of skipped integration tests is 1 or fewer." |
| M3 | Stale counts in documentation: `docs/RUNBOOKS/run-tests.md` says 205 unit + 166 integration = 371; the audited tree had 205 + 208 = 413 (496 on the merged branch with the T36 and audit tests). The same runbook reports the VM method as "117 passed, 1 skipped". | `docs/RUNBOOKS/run-tests.md:3,57` | `pytest --collect-only` | "Update the test counts in run-tests.md" - "Counts are 205 unit + 208 integration; prefer not to quote counts." |
| M4 | The polygon test oracle is not independent of the production library: `expected.json` is generated with shapely and `test_fixture_integrity.py:48` checks it with shapely. A shapely behaviour change or a wrong use of `contains` would move both. Edge handling is tested with hand-picked points, which is good. | `tests/tools/make_fixture.py`, `collector/tests/unit/test_fixture_integrity.py:48-70` | T16 (a) already plans an independent check. | "Add an independent point-in-polygon oracle to the unit tests" - "Ray casting on the fixture would catch library or argument-order regressions." |
| M5 | Window rules live in panel JSON (`timeFrom`), which most tests bypass by supplying their own window (`test_events_t14.py:37`, `test_military_events_t15.py:36`, `test_atc_t14.py:46` uses 365 days). Changing a panel's window to 70 days passed (P39). | dashboards; tests as listed | P39 survived; killed by `test_panel_windows_match_their_titles`, which passes today (no mismatch between any "(last N days)" title and its window). | "Keep the title-versus-window test" - "Done in the audit branch; no action beyond review." |
| M6 | Boundary tests missing in 20 panels: exactly 60 minutes (cards 63, 116, 163, 115), 20/30 minute ATC minimums (94, 95), 31 versus 30 days (165), exactly 100 kt / 1000 ft / 750 kt (card 61), 30 airborne minutes (card 93), under 18 minutes (card 177), limits (108, 128, 78). | see section 3 | 28 P-series mutants survived; all killed by new tests. | "Add boundary tests for the panel thresholds" - "Done in the audit branch for 28 thresholds." |
| M7 | The scripts in `ops/` are checked only for syntax or by hand: `watchdog.py` (446 lines, sends the alerts) has no tests at all; `soak_report.sql` verdict thresholds (98 percent coverage, 99 percent snapshots, p95 17 s) have none; `check_panels.py` accepts empty results and only runs against a live Grafana; `smoke.sh` and the backup scripts run only on the VM. | `ops/watchdog/watchdog.py`, `ops/soak_report.sql:36-45` | Section 5. | "Unit-test the watchdog decision logic and the soak report verdict" - "Alerting and the go-live evidence script have no automated tests." |
| M8 | Nothing tests the path from a feed payload to a dashboard number. `apply_snapshot` is tested with the fixture, and the panels with hand-built `flight_summary` rows (T14/T15 tests insert them directly), but no test applies several snapshots, refreshes, and checks a panel. A mismatch between the two halves (for example a column the panel reads that the refresh does not fill) would pass. | `tests/integration/test_pilots_t14.py:51-65` and similar | Reasoned, no mutant needed. | "Add one end-to-end test: fixture snapshots to a panel value" - "Apply 3 snapshots, refresh flights, assert card 61 or 93 for a known pilot." |

### Low

| # | Finding | Where | Suggested issue |
|---|---|---|---|
| L1 | Remaining survivors are equivalent or need a clock (section 6): S06 (the 40 s default is shadowed by the seeded `app_setting` row), S27 (cannot trigger), V21 (every seeded `region_map` prefix is one letter, so ordering cannot matter), V23 (a second 3 h bound in the same view makes the first redundant), P32 (a zero-row filter that only adds a harmless 0.0 row), P35 and U03 (need a controlled clock/time zone). | see table | "Document the equivalent mutants" - "No action; recorded so the next audit does not rediscover them." |
| L2 | Data left behind: the `conn` fixture truncates data tables but not `app_setting`; `test_roles.py:88` writes `x_test` and never removes it. Harmless today (the flight view reads only two keys). | `tests/integration/conftest.py:29-33`, `test_roles.py:88` | "Clean app_setting in the test fixture" - "A leftover home_icao_prefixes row would change every flight view result." |
| L3 | Cross-test coupling: `test_card_90.py` imports from `test_pilots_t14.py`, `test_flight_summary.py` from `test_flight_view.py`. Verified order independent (reverse file order and two runs on the same database all pass), but a rename breaks collection. | as listed | "Move shared test helpers into helpers.py" - "Low risk; tidy-up." |
| L4 | `test_grafana_ro.py:14-15` lists the original seven views; roles.sql grants every view dynamically so the list is only a sample, but newer views (`controller_summaries`, `pilot_sample`, `monday_attendance`, ...) are not named. Covered now by `test_every_panel_query_runs_as_grafana_ro`. | `tests/integration/test_grafana_ro.py:14` | "Derive the view list from pg_views in test_grafana_ro" - "Keeps the privilege test in step with new migrations." |
| L7 | The shared retention helper `test_controller_retention.py:31-38` (changed by T36) truncates `logon_time` to whole seconds but not `first_seen`/`last_seen`, so every session it builds is a fraction of a second longer than requested; "exactly 60 minutes" boundary tests cannot use it (the audit's card 163 test inserts its own row). | `tests/integration/test_controller_retention.py:31-38` | "Truncate first_seen too in the retention test helper" - "Keeps session durations exact so boundary tests can use the helper." |
| L5 | Brittle string tests: `test_card_156_has_no_sector_or_frequency_logic` fails if a SQL comment contains the word "sector". | `test_military_events_t15.py:217-221` | none |
| L6 | One killed mutant (U01) is killed only because removing UTC makes the SQL invalid (`GROUP BY`), not by a semantic test. | table | none |

No production defect was found by this audit that is not already tracked (the data-integrity agent owns issue #15).

## 3. Inventory: what each test really asserts

### Unit tests (205; `collector/tests/unit`, no database)

| File (tests) | What it asserts | Quality |
|---|---|---|
| `test_filters_polygon.py` (41) | `load_polygon` validation (types, unclosed ring, coordinate range, missing file); inside/outside for a square and the real polygon; edge and vertex points are outside; missing coordinate passes; lat/lon order | Strong; edge cases by hand |
| `test_filters_whitelist.py` (41) | loader formats, comments, CRLF, duplicates, case sensitivity, fail-closed on missing/empty/malformed; `is_near_miss` (listed, `_OBS`, unrelated prefix, case, prefix-equals) | Strong for the loader; the keep decision itself is not here (H2) |
| `test_golden_filters.py` (18) | the fixture through polygon and whitelist against `expected.json`: kept pilots and controllers, edge pilots out, no-position pilot in, OBS/ATIS never kept, near-miss list, flight gate candidates | Good; expected values come from an independent generator, but both use shapely (M4) |
| `test_model_helpers.py` (48) | `parse_frequency_hz` (exact for every whitelist frequency, 199.998, junk), `home_icao`, `clamp_pilot` boundaries (0, 700, 65000, -1500) | Strong; missing "0.000" (C22, fixed) |
| `test_model_snapshot.py` (21) | parse of the fixture: timestamp, counts, ATIS excluded, field mapping, plans, no position, timestamp forms, empty lists | Good; no offset timestamp, heading 360, malformed global count (fixed) |
| `test_robustness.py` (11) | control characters and surrogates, `/health` freshness rules, reference reload rejection and the 70 percent whitelist guard, stuck apply, malformed over 20 percent, skipped_unchanged not counting as OK | Good (mocks the collector loop) |
| `test_settings.py` (12) | defaults equal `.env.example`, overrides, invalid values | Good |
| `test_fixture_integrity.py` (13) | the fixture is anonymised and consistent with `expected.json` and an independent polygon check | Fixture hygiene, not product behaviour |

### Integration tests (208; `tests/integration`, need PostgreSQL 16 with the migrations applied)

| File (tests) | What it asserts | Quality |
|---|---|---|
| `test_ingest.py` (11) | the fixture produces the expected rows, counts, null-ing of implausible values, idempotent replay, rollback on failure, near-miss rows | Good; one dead test at :106 (M2) |
| `test_upserts_and_events.py` (11) | pilot upsert (last_seen, plan, greatest, new logon), leaving/entering the region, rating and frequency events only on change, replay idempotent | Good |
| `test_poison_records.py` (4) | a bad record is skipped and counted, replay does not rewrite rows, out-of-order snapshot | Good |
| `test_schema_constraints.py` (17) | keys, checks, heading range, partition routing, object existence | Constraint tests; the existence test is presence only |
| `test_partitions.py` (6) | create and drop partitions; ensure/drop through maintenance; open session keeps a partition | Weak at boundaries (H1) |
| `test_flight_view.py` (19) | gate, normalisation, flags, 120 minute and 8 hour splits (exact edges), airborne sum, 59 versus 250 kt, 40 s cap | Good except 60 kt (H3) |
| `test_flight_summary.py` (6) | independent Python reference versus the SQL on mixed sessions, idempotent refresh, 12 h window, flights survive the partition drop, pinned partition | Strong |
| `test_online_hours.py` (5) | cards 94/95 hours, weekday/weekend split, midnight wrap versus v1 quirk, airborne filter, ATC minimum | Good, no boundary |
| `test_atc_t14.py` (9) | cards 63, 116, 137 per month, 30/60 minute edges, rating change, month attribution | Good |
| `test_atc_sessions_t15.py` (20) | cards 144/146/153/154/186 including exactly 240 minutes, overlaps, reconnect, limits | Strong |
| `test_controller_retention.py` (8) | the retention cards by days; one fixed-date month test; one smoke loop | Good counts, no 30/60/90 boundary |
| `test_pilots_t14.py` (13) | cards 61, 93, 129, 135, 55 by v1 rules with hand-built flights | Good |
| `test_pilots_people_t15.py` (12) | cards 155, 175, 180-183 | Good |
| `test_events_t14.py` (5) | cards 66, 78, 131, 134 including 07:00/12:00 edges | Good |
| `test_military_events_t15.py` (9) | cards 142, 143, 161, 184, 156 | Good; skip on Mondays (M2) |
| `test_card_90.py` (4) | card 90 window and filters | Good |
| `test_roles.py` (18), `test_grafana_ro.py` (31) | collector and backup_ro least privilege; maintenance functions not PUBLIC; `grafana_ro` read-only, UTC pinned, SELECT on views, nothing on raw tables | Strong for privileges; none of them runs a panel (new test does) |

Five "every query runs" smoke tests (`test_atc_sessions_t15.py:254` on empty data, `test_atc_t14.py:146`, `test_controller_retention.py:127`, `test_pilots_people_t15.py:251`, `test_pilots_t14.py:232`) only prove the SQL executes. Tests whose expected values are derived from the SQL they test: none found; the closest is `test_card_90.py:20` (a sum of the panel's own output compared to a count of inserted rows, which is acceptable).

### Check scripts in `ops/`

| Script | What it really checks | Gap |
|---|---|---|
| `check_config_drift.py` | settings fields, compose environment and `.env.example` agree (static, in CI) | none |
| `check_size_budget.py` | collector line budgets (CI) | not a quality check |
| `check_panels.py` | posts every panel query to a live Grafana; passes on errors = 0, EMPTY counted as acceptable | smoke only; needs the live stack |
| `smoke.sh` | containers healthy, `/health` 200, an ok poll in 90 s, Grafana database ok, `grafana_ro` cannot write | live stack only |
| `soak_report.sh`/`.sql` | PASS/INCOMPLETE/FAIL from `poll_log` cadence | verdict thresholds untested |
| `watchdog/watchdog.py` | alerts (Telegram) on stale polls, snapshots, disk, backups, Grafana | no tests |
| `backup/*.sh`, `restore*.sh` | backup and restore | manual drill only (`docs/RESTORE_DRILL.md`) |
| `check_env.sh`, `gen_env.sh` | env file sanity (CI compose job) | none |

## 4. Panel and rule coverage against `docs/CHART_DEFINITIONS.md`

"Before" is the existing test file(s) that name the panel (exact or prefix title). "smoke only" means it is only run by an "every query runs" loop. All 84 are now also executed as `grafana_ro` by `test_every_panel_query_runs_as_grafana_ro`. 40 panels had no test at all before (the four retention panels are smoke only).

| Dashboard | Panel | Before | After this audit |
|---|---|---|---|
| atc-overview.json | Controllers online now | none | still none (see gaps) |
| atc-overview.json | Controllers online over time | none | still none (see gaps) |
| atc-overview.json | Controllers online now (list) | none | still none (see gaps) |
| atc-overview.json | Controllers logged on (last 30 days) | none | test_controllers_logged_on_needs_more_than_sixty_minutes |
| atc-overview.json | Controllers logged on each month, by rating group | atc_t14 |  |
| atc-overview.json | CTR and FSS controllers active (30 days to month end, monthly) | atc_t14 |  |
| atc-overview.json | Controllers active (30 days to month end, monthly) | atc_t14 |  |
| atc-overview.json | Top 50 service providers (last 30 days) | none | test_top_50_providers_includes_short_sessions_and_stops_at_fifty |
| atc-overview.json | Controllers with the shortest average session (last 30 days) | none | still none (see gaps) |
| atc-overview.json | Top S1 service providers (last 30 days) | atc_sessions_t15 |  |
| atc-overview.json | Top S2 service providers (last 30 days) | atc_sessions_t15 |  |
| atc-overview.json | Controllers with the longest median session (last 90 days) | atc_sessions_t15 |  |
| atc-overview.json | Controllers with the shortest median session (last 90 days) | atc_sessions_t15 |  |
| atc-overview.json | Controllers on for more than 4 hours in any 8-hour window | atc_sessions_t15 |  |
| atc-retention.json | Active controllers | controller_retention |  |
| atc-retention.json | Lapsed 30 days | controller_retention |  |
| atc-retention.json | Lapsed 60 days | controller_retention |  |
| atc-retention.json | Lapsed 90+ days | controller_retention |  |
| atc-retention.json | Reactivated in the last 30 days after 60+ days lapsed | controller_retention |  |
| atc-retention.json | Controller retention rate | controller_retention |  |
| atc-retention.json | Activity levels of controllers | smoke only (test_controller_retention) |  |
| atc-retention.json | S1 new controller retention by cohort | smoke only (test_controller_retention) |  |
| atc-retention.json | Who might need help reactivating? | controller_retention |  |
| atc-retention.json | Who reactivated after being away for more than 60 days? | controller_retention |  |
| atc-retention.json | Lapsing controllers (30+ days inactive) | smoke only (test_controller_retention) |  |
| atc-retention.json | Controller engagement index (last 90 days) | smoke only (test_controller_retention) |  |
| atc-retention.json | Controller activity trends | controller_retention |  |
| pilot-origins.json | Where pilots currently online come from | none | still none (see gaps) |
| pilot-origins.json | Who flies most in Australian airspace: flights by home region (last 30 days) | none | still none (see gaps) |
| pilot-origins.json | Who spends most time in Australian airspace: airborne hours by home region (last | none | still none (see gaps) |
| pilot-origins.json | Flights by joining cohort (CID range, last 30 days) | none | still none (see gaps) |
| pilot-origins.json | Servers used by home-region pilots (last 24 hours) | none | test_servers_panel_counts_home_region_pilots_only |
| pilot-origins.json | Active pilots and controllers (last 90 days, log scale) | pilots_t14 |  |
| monday-events.json | Monday event attendance (0700-1200Z) | events_t14 |  |
| monday-events.json | Share of YMML-YSSY airborne time flown by local pilots (Mondays 0700-1200Z) | events_t14 |  |
| monday-events.json | Airborne aircraft during the latest Monday event (0700-1200Z, 5-minute slots) | military_events_t15 |  |
| management.json | Australian-based pilots and ATC online each month | pilots_people_t15 |  |
| management.json | Pilot hours per month (names with Y) | pilots_people_t15 |  |
| military.json | Most popular military aircraft (unique pilots, last 7 days) | none | test_military_aircraft_panel_lists_only_military_types; rebuilt 09-10-2026 with tests/integration/test_card_65.py (11 tests) |
| military.json | Most popular RAAFv routes (last month) | events_t14 |  |
| military.json | Hours flown by RAAFv aircraft per week (last 14 weeks) | events_t14 |  |
| military.json | RAAFv flights (last 7 days) | military_events_t15 |  |
| military.json | RAAFv flights by aircraft type (last 30 days) | military_events_t15 |  |
| military.json | Most popular RAAFv aircraft last month (flights) | military_events_t15 |  |
| military.json | Flights flown by RAAFv aircraft per week (last 14 weeks) | military_events_t15 |  |
| collector-health.json | Newest snapshot age | none | still none (monitoring, view-level only) |
| collector-health.json | Last successful poll age | none | still none (monitoring, view-level only) |
| collector-health.json | Newest pilot sighting age | none | still none (monitoring, view-level only) |
| collector-health.json | Newest controller sighting age | none | still none (monitoring, view-level only) |
| collector-health.json | Snapshots missed (last 24 hours) | none | still none (monitoring, view-level only) |
| collector-health.json | Unchanged polls (last 24 hours) | none | still none (monitoring, view-level only) |
| collector-health.json | Poll errors (last 24 hours) | none | still none (monitoring, view-level only) |
| collector-health.json | Rejected values (last 24 hours) | none | still none (monitoring, view-level only) |
| collector-health.json | Near-miss controllers | none | still none (monitoring, view-level only) |
| collector-health.json | Near-miss controller callsigns | none | still none (monitoring, view-level only) |
| collector-health.json | Polls by outcome | none | still none (monitoring, view-level only) |
| collector-health.json | Rows written (pilot and controller records kept per snapshot) | none | still none (monitoring, view-level only) |
| collector-health.json | Poll duration | none | still none (monitoring, view-level only) |
| collector-health.json | Recent poll errors | none | still none (monitoring, view-level only) |
| collector-health.json | Data freshness (data engineer view) | none | still none (monitoring, view-level only) |
| pilot-onboarding.json | Pilots who logged on but never flew (90 days) | pilots_people_t15 |  |
| pilot-onboarding.json | Pilots who logged on in a month but never flew that month | pilots_people_t15 |  |
| pilot-onboarding.json | Pilots who flew last month but not this month (lapsed) | pilots_people_t15 |  |
| pilots-now.json | Pilots online now (in region) | none | test_pilots_online_now_shows_the_newest_snapshot |
| pilots-now.json | Pilots online now (network-wide) | none | test_pilots_online_now_shows_the_newest_snapshot |
| pilots-now.json | Pilots online over time | none | still none (see gaps) |
| pilots-now.json | When are people online on weekdays (last 30 days) | online_hours |  |
| pilots-now.json | When are people online on weekends (last 30 days) | online_hours |  |
| pilots-now.json | Departure board | none | test_departure_board_airborne_starts_at_60_kt |
| pilots-traffic.json | Most popular international routes (last 30 days) | none | test_international_and_domestic_routes_are_separated |
| pilots-traffic.json | Most popular domestic routes (last 30 days) | none | test_international_and_domestic_routes_are_separated |
| pilots-traffic.json | Most popular VFR routes (last 30 days) | none | test_vfr_routes_need_three_distinct_pilots |
| pilots-traffic.json | Pilots with the most diverse routes (last 14 days) | none | test_diverse_routes_only_count_flights_over_sixty_minutes |
| pilots-traffic.json | Busiest home-region airports (last 30 days) | pilots_t14 |  |
| pilots-traffic.json | Quietest home-region airports (last 30 days) | none | still none (see gaps) |
| pilots-traffic.json | Most popular airlines (last 30 days) | none | test_airlines_need_three_letters_and_a_digit |
| pilots-traffic.json | Most popular callsigns (last 30 days) | none | test_popular_callsigns_need_two_distinct_pilots |
| pilots-traffic.json | Most popular aircraft by flights (last 6 months) | card_90 |  |
| pilots-traffic.json | Most popular aircraft by pilots (last 6 months) | pilots_t14 |  |
| pilots-traffic.json | Highest groundspeeds (last 7 days) | pilots_t14 |  |
| pilots-traffic.json | Busiest days by flights (last 6 months) | none | test_busiest_days_shows_five_rows |
| pilots-traffic.json | Busiest days by pilots online (last 6 months) | pilots_t14 |  |
| pilots-traffic.json | Flights with SELCAL in the remarks | none | test_selcal_percentage_uses_flights_with_remarks_and_the_sel_slash_marker |
| pilots-traffic.json | Which member spent the most time online? (last 7 days) | pilots_people_t15 |  |

Coverage gaps that remain (rule asserted nowhere): cards 118/120 and the whole monitoring dashboard (15 panels: only executed now); "Controllers online now/over time/list"; cards 99, 100, 101 (region split); card 76 (cohorts, view tested via V17 only); "Quietest airports"; "Pilots online over time"; the shortest-average-session panel (superseded); retention cards 164, 167, 173, 174 (smoke only). Recommended tests are in section 7.

## 5. Mutation check

Method: a throwaway cluster (container `vp-t30a-pg`, `postgres:16.4-bookworm`, random loopback port, own data directory, `fsync=off`), the migrations applied with the up half only and `db/roles.sql` with throwaway passwords, exactly as CI does. For each mutant: copy the tree, change it, rebuild the database (migration mutants) or clone a template (other mutants), run the **entire** suite (unit and integration, including the role tests, 413 tests, about 20 s) in a short-lived container from `vatpac-stats-collector:local` with `--network host`, one at a time, one CPU. A mutant is KILLED if any test fails. The repository files were never modified; mutants were applied to a scratch copy. Baseline of the unmodified tree: 412 passed, 1 skipped. `db/roles.sql` ran about 70 times against the throwaway cluster only (also a proof that it is safe to re-run).

Result: 131 mutants, 2 invalid (`ends_with` does not exist; a type change in a view), 56 killed, 73 survived. After the new tests: 120 killed, 9 survive. "First failing test" is the first of the failures listed by pytest; for survivors that are now killed it is the new test. Mutant ids: C collector, S migration SQL (flight, partitions), V other views, P panel SQL or JSON, U UTC handling (U02, U04 to U08 ran with the database session at Australia/Sydney).

| ID | File / panel | Mutation | Result | First failing test |
|---|---|---|---|---|
| C01 | filters.py | polygon edge points become inside (contains -> covers) | KILLED | ingest::test_first_apply_writes_expected_rows (+44) |
| C02 | filters.py | pilot with no position is dropped instead of passed | KILLED | ingest::test_first_apply_writes_expected_rows (+10) |
| C03 | filters.py | lat/lon swapped in the polygon test | KILLED | ingest::test_first_apply_writes_expected_rows (+26) |
| C04 | filters.py | _OBS controllers no longer excluded from near-miss | KILLED | ingest::test_near_miss_sighting_recorded (+5) |
| C05 | filters.py | near-miss compares the last token instead of the prefix | KILLED | filters_whitelist::test_near_miss_true[ML_XYZ_CTR] (+5) |
| C06 | filters.py | whitelist accepts a blank callsign | KILLED | filters_whitelist::test_file_with_only_malformed_lines_fails_closed[empty- (+3) |
| C07 | store.py | polygon filter dropped | KILLED | ingest::test_first_apply_writes_expected_rows (+5) |
| C08 | store.py | whitelist match becomes prefix match instead of exact | SURVIVED, now killed | new: audit_t30::test_controller_whitelist_is_an_exact_case_sensitive_ma |
| C09 | store.py | whitelist match becomes case-insensitive | SURVIVED, now killed | new: audit_t30::test_controller_whitelist_is_an_exact_case_sensitive_ma |
| C10 | store.py | plausibility caps not applied on write | KILLED | ingest::test_first_apply_writes_expected_rows (+4) |
| C11 | store.py | pilots_with_plan counts either end, not both | SURVIVED, now killed | new: audit_t30::test_pilots_with_plan_needs_both_departure_and_arrival |
| C12 | store.py | session max groundspeed keeps the minimum | KILLED | upserts_and_events::test_max_gs_and_alt_use_greatest |
| C13 | store.py | pilot last_seen never advances | KILLED | poison_records::test_older_snapshot_still_gets_its_position (+1) |
| C14 | store.py | pilot first_seen moves forward on out-of-order replay | KILLED | upserts_and_events::test_session_upsert_updates_last_seen_and_plan |
| C15 | store.py | rating/frequency events written on every poll | KILLED | upserts_and_events::test_rating_and_frequency_events_only_on_change |
| C16 | store.py | network_snapshot.pilots_in_region stores the global count | KILLED | ingest::test_network_snapshot_counts |
| C17 | store.py | near-miss polls_seen double counts a replay | SURVIVED, now killed | new: audit_t30::test_replaying_a_snapshot_does_not_count_a_near_miss_po |
| C18 | store.py | plan_changed_at bumped on every poll that carries a departure | KILLED | upserts_and_events::test_plan_changed_at_not_bumped_when_dep_arr_unchanged |
| C19 | model.py | groundspeed cap raised 700 -> 800 | KILLED | model_helpers::test_clamp_groundspeed_rejected[701] |
| C20 | model.py | altitude cap raised 65000 -> 70000 | KILLED | model_helpers::test_clamp_altitude_rejected[65001] |
| C21 | model.py | groundspeed cap boundary 700 rejected | KILLED | model_helpers::test_clamp_in_range_unchanged[700-65000] |
| C22 | model.py | frequency 0.000 stored as 0 Hz | SURVIVED, now killed | new: audit_t30_unit::test_zero_or_negative_frequency_is_none[0] |
| C23 | model.py | home_icao accepts a name that is only an ICAO code | KILLED | model_helpers::test_home_icao[Pilot (+1) |
| C24 | model.py | heading 360 dropped | SURVIVED, now killed | new: audit_t30_unit::test_heading_of_360_is_kept |
| C25 | model.py | pilots_global counts parsed instead of raw pilots | SURVIVED, now killed | new: audit_t30_unit::test_global_pilot_count_includes_malformed_entries |
| C26 | model.py | timestamp with an offset is mislabelled UTC instead of converted | SURVIVED, now killed | new: audit_t30_unit::test_update_timestamp_with_an_offset_is_converted_to_ut |
| C27 | maintenance.py | daily maintenance creates 1 partition ahead instead of 7 | SURVIVED, now killed | new: audit_t30::test_daily_maintenance_creates_seven_partitions_ahead_b |
| C28 | maintenance.py | poll_log trimmed at 4 days | SURVIVED, now killed | new: audit_t30::test_trim_poll_log_removes_only_rows_older_than_400_day |
| C29 | maintenance.py | flight refresh window 12 h -> 1 minute | SURVIVED, now killed | new: audit_t30::test_flight_refresh_covers_sessions_seen_in_the_last_tw |
| C30 | maintenance.py | maintenance drops with 1 day retention instead of the setting | SURVIVED, now killed | new: audit_t30::test_run_maintenance_uses_the_configured_retention |
| C31 | maintenance.py | poll_log trim deletes recent rows instead of old | SURVIVED, now killed | new: audit_t30::test_trim_poll_log_removes_only_rows_older_than_400_day |
| C32 | settings.py | default position retention 90 -> 30 days | KILLED | settings::test_defaults_match_env_example |
| C33 | settings.py | default airborne gap cap 40 -> 60 s | KILLED | settings::test_defaults_match_env_example |
| S01 | 20261007000020_flight_summary.sql | 60 kt threshold becomes strictly greater | SURVIVED, now killed | new: audit_t30::test_groundspeed_of_exactly_60_kt_is_airborne |
| S02 | 20261007000020_flight_summary.sql | airborne threshold lowered 60 -> 50 kt | KILLED | flight_summary::test_equivalence_on_mixed_sessions (+1) |
| S03 | 20261007000020_flight_summary.sql | airborne pair at exactly 120 minutes apart excluded | SURVIVED, now killed | new: audit_t30::test_samples_exactly_120_minutes_apart_still_count_as_a |
| S04 | 20261007000020_flight_summary.sql | gap split at exactly 120 minutes | KILLED | flight_view::test_gap_of_exactly_120_minutes_does_not_split |
| S05 | 20261007000020_flight_summary.sql | 8 h span split boundary (ceil -> floor) | KILLED | flight_summary::test_equivalence_on_mixed_sessions (+1) |
| S06 | 20261007000020_flight_summary.sql | default airborne gap cap 40 -> 45 s | SURVIVED |  |
| S07 | 20261007000020_flight_summary.sql | gap cap becomes a floor | KILLED | flight_summary::test_equivalence_on_mixed_sessions (+5) |
| S08 | 20261007000020_flight_summary.sql | airborne minutes rounded to whole minutes | KILLED | flight_summary::test_equivalence_on_mixed_sessions (+5) |
| S09 | 20261007000020_flight_summary.sql | flight gate AND -> OR | KILLED | flight_summary::test_gate_and_normalisation_with_tab (+6) |
| S10 | 20261007000020_flight_summary.sql | gate/normalisation ignores tab, CR, LF in departure | KILLED | flight_summary::test_gate_and_normalisation_with_tab |
| S11 | 20261007000020_flight_summary.sql | is_circuit inverted | KILLED | flight_view::test_flags |
| S12 | 20261007000020_flight_summary.sql | touches_home_region OR -> AND | KILLED | flight_view::test_flags |
| S13 | 20261007000020_flight_summary.sql | is_zzzz ignores the arrival | SURVIVED, now killed | new: audit_t30::test_a_zzzz_arrival_is_flagged_as_zzzz |
| S14 | 20261007000020_flight_summary.sql | refresh recomputes final sessions (raw positions may be gone) | KILLED | flight_summary::test_flights_survive_partition_drop |
| S15 | 20261007000020_flight_summary.sql | Monday window starts 0800 not 0700 | KILLED | events_t14::test_monday_attendance_window_0700_to_1200_utc |
| S16 | 20261007000020_flight_summary.sql | Monday window ends 1300 | KILLED | events_t14::test_monday_attendance_window_0700_to_1200_utc |
| S17 | 20261007000020_flight_summary.sql | controller overlap at exactly 0700 excluded | SURVIVED, now killed | new: audit_t30::test_monday_attendance_edges_for_controllers_and_unplan |
| S18 | 20261007000020_flight_summary.sql | Monday pilot attendance ignores the arrival gate | SURVIVED, now killed | new: audit_t30::test_monday_attendance_edges_for_controllers_and_unplan |
| S19 | 20261007000020_flight_summary.sql | monday_attendance stops one week early | SURVIVED, now killed | new: audit_t30::test_monday_attendance_includes_the_current_weeks_monda |
| S20 | 20261007000021_partition_functions_secure.sql | partition retention boundary: partition ending exactly at cutoff dropped | SURVIVED, now killed | new: audit_t30::test_retention_drops_exactly_the_days_older_than_ninety |
| S21 | 20261007000021_partition_functions_secure.sql | pinning boundary | SURVIVED, now killed | new: audit_t30::test_session_last_seen_exactly_at_the_cutoff_pins_its_p |
| S22 | 20261007000021_partition_functions_secure.sql | retention halves (days*24 -> days*12 hours) | SURVIVED, now killed | new: audit_t30::test_retention_drops_exactly_the_days_older_than_ninety |
| S23 | 20261007000021_partition_functions_secure.sql | pinning rule removed: open sessions no longer protect partitions | KILLED | flight_summary::test_flights_survive_partition_drop |
| S24 | 20261007000021_partition_functions_secure.sql | yesterday's partition no longer created | SURVIVED, now killed | new: audit_t30::test_ensure_partitions_creates_yesterday_and_seven_days |
| S25 | 20261007000021_partition_functions_secure.sql | one partition ahead missing | KILLED | partitions::test_maintenance_creates_today_plus_seven |
| S26 | 20261007000021_partition_functions_secure.sql | partition dropped without finalising flights | KILLED | flight_summary::test_flights_survive_partition_drop |
| S27 | 20261007000021_partition_functions_secure.sql | partition dropped even if its sessions could not be summarised | SURVIVED |  |
| S28 | 20261007000021_partition_functions_secure.sql | partition end miscomputed (12 h) | KILLED | flight_summary::test_flights_survive_partition_drop (+1) |
| V01 | 20261007000004_views.sql | reconnect merge window 5 -> 10 minutes | KILLED | atc_sessions_t15::test_reconnect_within_five_minutes_is_one_session |
| V02 | 20261007000004_views.sql | reconnect merge at exactly 5 minutes | SURVIVED, now killed | new: audit_t30::test_reconnect_after_exactly_five_minutes_is_one_sessio |
| V03 | 20261007000004_views.sql | merged session end = earliest last_seen | KILLED | atc_sessions_t15::test_reconnect_within_five_minutes_is_one_session |
| V04 | 20261007000004_views.sql | merged session start = latest logon | KILLED | atc_sessions_t15::test_reconnect_within_five_minutes_is_one_session |
| V05 | 20261007000004_views.sql | controller is_open window 30 s -> 30 min | SURVIVED, now killed | new: audit_t30::test_controller_is_open_only_when_seen_in_the_newest_sn |
| V06 | 20261007000024_online_now_guard_and_tuning.sql | online_now pilots: 2-snapshot window 30 s -> 30 min | SURVIVED, now killed | new: audit_t30::test_online_now_means_seen_in_the_last_two_snapshots |
| V07 | 20261007000024_online_now_guard_and_tuning.sql | online_now wall-clock guard 5 min -> 5 h | KILLED | roles::test_online_now_is_empty_when_the_newest_snapshot_is_st |
| V08 | 20261007000024_online_now_guard_and_tuning.sql | online_now controllers: window 30 s -> 30 min | SURVIVED, now killed | new: audit_t30::test_online_now_means_seen_in_the_last_two_snapshots |
| V09 | 20261007000010_analytics_views.sql | controller_online_now wall-clock guard 5 min -> 5 h | SURVIVED, now killed | new: audit_t30::test_online_lists_exclude_sessions_when_the_collector_h |
| V10 | 20261007000010_analytics_views.sql | pilot_latest is_online wall-clock guard 5 min -> 5 h | SURVIVED, now killed | new: audit_t30::test_online_lists_exclude_sessions_when_the_collector_h |
| V11 | 20261007000010_analytics_views.sql | rank_group_of: rating 7 labelled C1 | SURVIVED, now killed | new: audit_t30::test_rating_groups_s3_and_c3_have_their_own_columns |
| V12 | 20261007000010_analytics_views.sql | rank_group_of: rating 4 labelled S2 | SURVIVED, now killed | new: audit_t30::test_rating_groups_s3_and_c3_have_their_own_columns |
| V13 | 20261007000010_analytics_views.sql | rank_group_of: rating 8 becomes Unknown | KILLED | atc_t14::test_rating_groups_by_month |
| V14 | 20261007000010_analytics_views.sql | airline prefix no longer needs a digit (registrations look like airlines) | SURVIVED, now killed | new: audit_t30::test_airlines_need_three_letters_and_a_digit |
| V15 | 20261007000010_analytics_views.sql | SELCAL match too loose | SURVIVED, now killed | new: audit_t30::test_selcal_percentage_uses_flights_with_remarks_and_th |
| V16 | 20261007000010_analytics_views.sql | movements: ZZZZ departures counted | SURVIVED, now killed | new: audit_t30::test_movements_ignore_zzzz_and_cohorts_ignore_cids_belo |
| V17 | 20261007000010_analytics_views.sql | cohorts: CIDs below 100000 included | SURVIVED, now killed | new: audit_t30::test_movements_ignore_zzzz_and_cohorts_ignore_cids_belo |
| V18 | 20261007000004_views.sql | data_freshness counts a day not an hour | SURVIVED, now killed | new: audit_t30::test_data_freshness_counts_only_the_last_hour |
| V19 | 20261007000010_analytics_views.sql | FSS not recognised as CTR/FSS | KILLED | atc_t14::test_ctr_fss_monthly_count |
| V20 | 20261008000031_controller_summaries_end_time.sql | controller_summaries.session_end_time = start | KILLED | atc_sessions_t15::test_exactly_four_hours_is_not_enough_but_one_minute_mo (+6) |
| V21 | 20261007000010_analytics_views.sql | icao_region picks the shortest prefix | SURVIVED |  |
| V22 | 20261007000010_analytics_views.sql | icao_is_home matches the end of the code | INVALID | mutant does not build (not counted) |
| V23 | 20261007000010_analytics_views.sql | pilot_latest horizon 3 h -> 30 h | SURVIVED |  |
| P01 | atc/atc-overview.json / Controllers active (30 days to month end,  | card 137: exactly 30 minutes counts | KILLED | atc_t14::test_all_positions_monthly_count_uses_30_minutes |
| P02 | atc/atc-overview.json / CTR and FSS controllers active (30 days to | card 63: exactly 60 minutes counts | SURVIVED, now killed | new: audit_t30::test_card_63_exactly_sixty_minutes_does_not_count |
| P03 | atc/atc-overview.json / CTR and FSS controllers active (30 days to | card 63: 30-day window shortened to 25 | SURVIVED, now killed | new: audit_t30::test_card_63_window_is_30_days_to_month_end |
| P04 | atc/atc-overview.json / Controllers logged on each month, by ratin | card 116: exactly 60 minutes counts | SURVIVED, now killed | new: audit_t30::test_card_116_exactly_sixty_minutes_does_not_count |
| P05 | atc/atc-overview.json / Controllers logged on (last 30 days) | card 115: 60 minute threshold dropped to 6 | SURVIVED, now killed | new: audit_t30::test_controllers_logged_on_needs_more_than_sixty_minute |
| P06 | atc/atc-overview.json / Top 50 service providers (last 30 days) | card 108: top 50 becomes top 5 | SURVIVED, now killed | new: audit_t30::test_top_50_providers_includes_short_sessions_and_stops |
| P07 | atc/atc-overview.json / Top 50 service providers (last 30 days) | card 108: zero-length filter becomes 600 minutes | SURVIVED, now killed | new: audit_t30::test_top_50_providers_includes_short_sessions_and_stops |
| P08 | atc/atc-retention.json / Active controllers | card 165: active window 31 days | SURVIVED, now killed | new: audit_t30::test_retention_active_window_is_thirty_days |
| P09 | atc/atc-retention.json / Lapsed 30 days | card 166: lapsed-30 lower bound 60 -> 80 days | KILLED | controller_retention::test_counts_follow_the_v1_rules |
| P10 | atc/atc-retention.json / Controller retention rate | card 163: exactly 60 minutes counts | SURVIVED, now killed | new: audit_t30::test_retention_rate_needs_sessions_over_sixty_minutes |
| P11 | atc/atc-retention.json / Who might need help reactivating? | card 177: help threshold 18 -> 25 minutes | SURVIVED, now killed | new: audit_t30::test_help_list_threshold_is_under_18_minutes |
| P12 | atc/atc-retention.json / Reactivated in the last 30 days after 60+  | card 171: allows one earlier session | KILLED | controller_retention::test_reactivated_and_help_lists |
| P13 | pilots/pilots-now.json / When are people online on weekdays (last 3 | card 94: Saturday counts as a weekday | KILLED | online_hours::test_weekday_excludes_weekends_and_flights_without_airb |
| P14 | pilots/pilots-now.json / When are people online on weekdays (last 3 | card 94: ATC minimum 21 min | SURVIVED, now killed | new: audit_t30::test_online_hours_atc_minimums_are_inclusive |
| P15 | pilots/pilots-now.json / When are people online on weekdays (last 3 | card 94: window 30 -> 90 days | SURVIVED, now killed | new: audit_t30::test_online_hours_ignore_flights_older_than_thirty_days |
| P16 | pilots/pilots-now.json / When are people online on weekends (last 3 | card 95: ATC minimum 31 min | SURVIVED, now killed | new: audit_t30::test_online_hours_atc_minimums_are_inclusive |
| P17 | pilots/pilots-now.json / Departure board | card 138: 60 kt boundary on the departure board | SURVIVED, now killed | new: audit_t30::test_departure_board_airborne_starts_at_60_kt |
| P18 | pilots/pilots-now.json / Pilots online now (in region) | card 68: oldest snapshot shown as 'now' | SURVIVED, now killed | new: audit_t30::test_pilots_online_now_shows_the_newest_snapshot |
| P19 | pilots/pilots-traffic.json / Highest groundspeeds (last 7 days) | card 61: upper cap 750 -> 850 kt | SURVIVED, now killed | new: audit_t30::test_card_61_speed_and_altitude_boundaries |
| P20 | pilots/pilots-traffic.json / Highest groundspeeds (last 7 days) | card 61: altitude boundary | SURVIVED, now killed | new: audit_t30::test_card_61_speed_and_altitude_boundaries |
| P21 | pilots/pilots-traffic.json / Highest groundspeeds (last 7 days) | card 61: peak speed floor 100 -> 60 | KILLED | pilots_t14::test_card_61_filters_follow_current_v1 |
| P22 | pilots/pilots-traffic.json / Most popular VFR routes (last 30 days) | card 59: VFR route minimum 3 pilots -> 2 | SURVIVED, now killed | new: audit_t30::test_vfr_routes_need_three_distinct_pilots |
| P23 | pilots/pilots-traffic.json / Most popular international routes (last 30 | card 45: domestic routes leak into international | SURVIVED, now killed | new: audit_t30::test_international_and_domestic_routes_are_separated |
| P24 | pilots/pilots-traffic.json / Most popular callsigns (last 30 days) | card 57: popular callsigns minimum 2 pilots -> 1 | SURVIVED, now killed | new: audit_t30::test_popular_callsigns_need_two_distinct_pilots |
| P25 | pilots/pilots-traffic.json / Busiest days by flights (last 6 months) | card 128: top 5 days becomes 50 | SURVIVED, now killed | new: audit_t30::test_busiest_days_shows_five_rows |
| P26 | pilots/pilots-traffic.json / Most popular airlines (last 30 days) | card 58: registrations counted as airlines | SURVIVED, now killed | new: audit_t30::test_airlines_need_three_letters_and_a_digit |
| P27 | pilots/pilots-traffic.json / Flights with SELCAL in the remarks | card 103: SELCAL denominator includes flights without remarks | SURVIVED, now killed | new: audit_t30::test_selcal_percentage_uses_flights_with_remarks_and_th |
| P28 | pilots/pilots-traffic.json / Pilots with the most diverse routes (last  | card 46: flights over 60 minutes -> over 6 | SURVIVED, now killed | new: audit_t30::test_diverse_routes_only_count_flights_over_sixty_minut |
| P29 | pilots/pilots-traffic.json / Which member spent the most time online? ( | card 155: window 7 -> 17 days | KILLED | pilots_people_t15::test_155_top_pilots_by_airborne_hours |
| P30 | demographics/pilot-origins.json / Active pilots and controllers (last 90 day | card 93: pilots need 31 airborne minutes | SURVIVED, now killed | new: audit_t30::test_card_93_a_pilot_with_exactly_30_airborne_minutes_c |
| P31 | demographics/pilot-origins.json / Servers used by home-region pilots (last 2 | card 40: servers panel counts non-home pilots | SURVIVED, now killed | new: audit_t30::test_servers_panel_counts_home_region_pilots_only |
| P32 | demographics/pilot-origins.json / Who spends most time in Australian airspac | card 101: hours include zero rows (harmless)? | SURVIVED |  |
| P33 | events/monday-events.json / Share of YMML-YSSY airborne time flown by  | card 134: window opens 0600 | KILLED | events_t14::test_local_share_of_ymml_yssy_airborne_time_monday_wind |
| P34 | events/monday-events.json / Airborne aircraft during the latest Monday | card 156: exactly 1000 ft counts | KILLED | military_events_t15::test_card_156_airborne_count_per_slot_on_latest_monday |
| P35 | events/monday-events.json / Airborne aircraft during the latest Monday | card 156: future slots drawn | SURVIVED |  |
| P36 | military/military.json / Hours flown by RAAFv aircraft per week (la | card 66: RAAFv remarks match becomes case sensitive | KILLED | events_t14::test_raafv_hours_per_week_uses_remarks_not_aircraft_typ |
| P37 | military/military.json / Most popular military aircraft (unique pil | card 65: military flag inverted | SURVIVED, now killed | new: audit_t30::test_military_aircraft_panel_lists_only_military_types |
| P38 | military/military.json / Most popular RAAFv routes (last month) | card 78: top 25 routes -> 2 | SURVIVED, now killed | new: audit_t30::test_raafv_routes_panel_shows_at_most_25_routes |
| P39 | military/military.json / RAAFv flights (last 7 days) | card 142: timeFrom 7d -> 70d (JSON override) | SURVIVED, now killed | new: audit_t30::test_panel_windows_match_their_titles |
| U01 | pilots/pilots-traffic.json / Busiest days by pilots online (last 6 mont | card 129: UTC removed from logon date (UTC session) | KILLED | pilots_t14::test_card_129_counts_by_logon_date_with_rank (+3) |
| U02 | pilots/pilots-traffic.json / Busiest days by pilots online (last 6 mont | card 129: UTC removed from logon date (Sydney session) | KILLED | atc_sessions_t15::test_exactly_four_hours_is_not_enough_but_one_minute_mo (+6) |
| U03 | events/monday-events.json / Share of YMML-YSSY airborne time flown by  | card 134: UTC removed (UTC session) | SURVIVED |  |
| U04 | events/monday-events.json / Share of YMML-YSSY airborne time flown by  | card 134: UTC removed (Sydney session) | KILLED | atc_sessions_t15::test_exactly_four_hours_is_not_enough_but_one_minute_mo (+3) |
| U05 | 20261007000020_flight_summary.sql | monday_attendance: UTC removed from week start (Sydney session) | SURVIVED | only the 3 time-zone-fragile baseline tests fail (since fixed); no test fails because of the mutant |
| U06 | 20261007000020_flight_summary.sql | monday_attendance: UTC removed from window start (Sydney session) | INVALID | mutant does not build (not counted) |
| U07 | pilots/pilots-now.json / When are people online on weekdays (last 3 | card 94: UTC removed from weekday test (Sydney session) | SURVIVED | only the 3 time-zone-fragile baseline tests fail (since fixed); no test fails because of the mutant |
| U08 | pilots/pilots-now.json / When are people online on weekdays (last 3 | card 94: UTC removed from logon hour (Sydney session) | KILLED | atc_sessions_t15::test_exactly_four_hours_is_not_enough_but_one_minute_mo (+4) |


Notes on the survivors that remain after the new tests:

- S06 equivalent: `COALESCE(..., 40)` is never reached because migration 3 seeds the `app_setting` row.
- S27 equivalent in practice: needs `refresh_flight_summary_sessions` to leave a session without a final row.
- V21 equivalent with today's seed: all `region_map` prefixes are one letter.
- V23 equivalent: the lateral join has its own 3 hour bound.
- P32 equivalent for the displayed figures (adds a 0.0 row for regions with only parked flights; arguably a difference, recommend a test that decides it).
- P35: needs the clock (the slot filter `slot <= now()` only matters on Mondays 07:00 to 12:00 UTC).
- U03, U05, U07: need the tests to run in a non-UTC session; the new suite is green in three zones, so adding a second CI pass with `options=-c TimeZone=Australia/Sydney` in the DSN would kill U05 and U07 only if a test is written that changes the outcome (the panel tests insert times at fixed UTC hours; recommended test below).

## 6. Date and environment dependence

- **Month boundaries.** `test_atc_t14.py` builds its data from `datetime.now()` (months two and one ago, day 15, so every session is inside its own month's 30 day window and outside the neighbours' for any month length 28 to 31); this holds on any date, including the 1st and the 31st. `test_controller_retention.py::test_activity_trends_by_month` uses fixed dates (January to March 2026) and the panel has no time filter, so it does not age out. No test fails at a month boundary.
- **Weekends and weekdays.** `test_online_hours.py` picks the most recent Wednesday and Saturday at least two days ago (always inside 30 days). The new tests reuse it.
- **Mondays.** `test_military_events_t15.py:190-193` skips on Mondays before 12:00 UTC (the slots would be in the future), so the card 156 test does not run for 5 hours a week; P35 survives for the same reason. `test_events_t14.py` uses Mondays two weeks back and is independent of today.
- **now()-dependent tests.** Roughly 28 occurrences of `datetime.now()`; all are relative offsets with margins of hours or days, except `test_card_93_session_ending_inside_the_window_counts` (10 minute margin; made DST-proof) and the card 129 date tests (a 1-in-86400 race at 00:00 UTC). `online_now` tests insert snapshots relative to the database `now()`.
- **Time zone.** Not set anywhere in tests; whatever the connection gives. Results: UTC green; Australia/Sydney 3 failed (all test bugs, fixed in commit "Fix three tests that depended on the database session time zone"); America/Los_Angeles green after the fixes. Final full run on the merged branch: 495 passed, 1 skipped under UTC and under Australia/Sydney.
- **Order and isolation.** The `conn` fixture truncates data tables per test and drops leftover partitions. Verified by running the integration files in reverse order and twice on the same database: green. Leftover `app_setting` rows (L2) are the only residue.
- **Skips.** Section 2, M2. The one skip in the VM run is `test_ingest.py:106` (now line 107 after the fix above).
- **Fixed dates.** Many tests use 07-10-2026 and `DATE '2026-10-07'` on purpose (deterministic); they do not depend on the clock.

## 7. Recommended further tests (for the survivors and gaps)

1. CI second pass with `options=-c TimeZone=Australia/Sydney` in `VATPAC_TEST_DSN`, plus a panel test that inserts a flight ending at 23:30 UTC and one at 00:30 UTC on a Monday and asserts card 134 and 94 buckets (kills U03, U05, U07).
2. A clock seam for card 156: make the panel read a Grafana variable for "now" or test it on a Monday before 12:00 UTC by moving the slot generation into a function that takes `now` (kills P35 and removes the skip).
3. `region_map` with overlapping prefixes (for example `Y` and `YS`) and a test of `icao_region` (V21); decide whether P32's zero-hour region row is wanted.
4. End-to-end test: apply 3 snapshots of the fixture 15 s apart, `refresh_flight_summary(NULL)`, assert the `flight` view has the 20 gated flights and `movements` the expected airports (M8).
5. Unit tests for `watchdog.py` decisions and a synthetic `poll_log` test for the soak verdict thresholds (M7).
6. A test that changes `app_setting.airborne_gap_cap_seconds` and expects the airborne minutes to change (covers the settings plumbing that S06 hides).
7. Cards still without a rule test: 40 (partly now), 99, 100, 101, 118, 120, 164, 167, 173, 174, monitoring panels (`Snapshots missed` arithmetic in particular).
8. An independent point-in-polygon oracle (M4) and a CI guard that fails when integration tests were skipped.

## 8. How the role tests and the VM method are covered

- `test_roles.py` (18 tests) and `test_grafana_ro.py` (31) need real roles. CI creates them by running `db/roles.sql` against its service container. On the VM, the safe method of `run-tests.md` section 2 must exclude them (running `roles.sql` there would reset the live passwords). They were **run for this audit on the throwaway cluster** with `roles.sql` applied (about 70 times, including re-runs, which also shows it is idempotent) and `VATPAC_TEST_GRAFANA_RO_DSN` pointing at the `grafana_ro` login: all 49 pass in every run (included in the 412 passed of the baseline). The collector-role maintenance test and the privilege-denied cases are therefore genuinely exercised.
- The roles tests skip (instead of fail) when a role is missing. In CI that cannot happen silently because `roles.sql` runs with `ON_ERROR_STOP`, but a local run without roles shows green with skipped tests.
- What the throwaway-database method cannot cover: Grafana itself (provisioning, the data source's connection as `grafana_ro` and its time-zone behaviour, `ops/check_panels.py`), the real feed and its quirks (see issue #15), `dbmate`'s transaction handling of migrations (CI applies them with `psql`), compose, healthchecks and restart behaviour (`ops/smoke.sh`), backups and restores, the watchdog and Telegram alerts, query timing and `EXPLAIN` on real volumes (tracker items for cards 186 and 156), and clock-dependent behaviour (the Monday window).

## 9. Data integrity

Out of scope for this document by instruction (handled by another agent, T30 parts A to D).

## 10. Not finished / could not verify

- Section 9 is intentionally empty here.
- The audit ran on `e1cfce3`; it was not repeated on later `main` commits (they add the T36 fix; the suite is green on the merged tree, but the mutants were not re-run on it).
- I could not control the system clock, so date logic is reasoned (section 6) rather than run at a month end or on a Monday morning.
- The mutants are hand-written and finite (131); a surviving class not represented here may exist. A tool such as `mutmut` for the collector would complement this.
- No coverage measurement was made.

## 11. Cleanup and clocks

- VM clock (UTC), read with `ssh vatpac-v3 'date -u'`: **start 08-10-2026 12:40:43**, end **08-10-2026 17:51:10**. The work spanned a usage-limit pause.
- Containers used: `vp-t30a-pg` only (`postgres:16.4-bookworm`, `127.0.0.1:` random port, own cluster) plus short-lived `--rm` containers from `vatpac-stats-collector:local` with `--network host`. I never touched `vp-t30b-*`, `vp-fix-pg`, `vp-rev-*` or any compose container; no `docker compose` command was run; `db/roles.sql` ran only inside `vp-t30a-pg`; the live database `vatpac` was not connected to by me.
- Cleanup at the end (VM output, 2026-10-08T17:51:10Z): `docker rm -f vp-t30a-pg`; `/tmp/vp-t30a` removed (its pytest folder was owned by the container user, so it was deleted with a short-lived root container, `rm -rf /t/vp-t30a`); `ls /tmp | grep vp-` shows only `vp-rev-src`, which belongs to another agent; `docker ps -a | grep -c vp-t30a` printed `0`. No `/tmp/vtest` was ever created.
- Local scratch files in the Windows temp folder were deleted; the working tree of `.worktrees/t30-tests` is clean.
