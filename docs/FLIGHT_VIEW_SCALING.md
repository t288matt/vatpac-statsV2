# The `flight` view and history growth (issue #89)

Written 10-10-2026 from the code review (issue #80). Status: measured, designed, NOT changed. Recommendation at the end.

## The problem

`flight` (migration `20261009000110_flight_merge_reconnects.sql`, last redefined by `20261009000130`) merges the sessions of one flight (ADR-023, 120 minute rule). It does this at query time: two window functions and about twenty ordered `array_agg` calls over EVERY row of `flight_part` (one row per session, gap segment and 8 hour part), sorted by (cid, callsign, departure, arrival, start_time). Nothing in a panel's time range can be pushed below a window function, so a panel that wants the last 7 days still pays for all of history. History is kept forever (ADR-026), so every panel gets slower as the database grows. `flight_ext`, `movements` (which reads `flight_ext` twice), `cohorts` and about 35 panels sit on top of it.

## Method

Synthetic data on a throwaway Postgres 16.15 container (own cluster, 1 GB memory cap, 1 CPU, `shared_buffers` 512 MB, `work_mem` 16 MB), generator `tests/scale/gen_synth.sql`:

| Data set | pilot_session | flight_summary | flights after merging |
|---|---|---|---|
| 1 year | 194,637 | 200,423 | 185,018 |
| 5 years | 1,029,076 | 1,059,703 | 978,357 |

Realistic features: about 8 percent of flights have a reconnect session (most within 100 minutes, a few at 125 to 300 minutes, a few at exactly 120), 1 percent a second reconnect, 3 percent a changed plan, about 3 percent circuits, 2 percent ZZZZ, 1 percent blank plans, about 1 percent placeholder rows (airborne NULL, no samples), 3 percent gap-split and 1 percent 8 hour span-split sessions, pilots with a skewed number of flights. Panels: every panel in `grafana/dashboards` whose SQL reads `flight`, `flight_ext`, `movements` or `cohorts` (33 targets), macros expanded to the panel's own look-back (or the dashboard default), run with `tests/scale/run_panels.py` as `grafana_ro` (statement timeout 30 s, work_mem 16 MB) under `EXPLAIN (ANALYZE, BUFFERS)`, best of two runs at one year and a single run at five years (a panel that exceeds 30 s is cancelled by Postgres, which is what Grafana would show as an error).

## Measurements of the current view (execution time, one query at a time, warm)

One year: nearly every panel takes 1.5 to 2.2 s. Slowest ten:

| Panel | ms |
|---|---|
| pilots-traffic: Busiest home-region airports | 12,238 |
| pilots-traffic: Quietest home-region airports | 11,865 |
| pilots-traffic: Most popular domestic routes | 7,759 |
| pilots-traffic: Most popular international routes | 7,091 |
| pilot-origins: Who spends most time (airborne hours) | 2,169 |
| pilots-traffic: Most popular aircraft by pilots | 2,134 |
| military: RAAFv flights by aircraft type | 2,122 |
| pilots-traffic: Highest groundspeeds | 2,115 |
| pilots-traffic: Most popular aircraft by flights | 2,102 |
| military: Most popular RAAFv routes | 2,094 |

Five years: the fixed cost per panel grows to 8 to 13 s, and four panels are cancelled by the 30 s statement timeout:

| Panel | ms |
|---|---|
| pilots-traffic: Busiest home-region airports | cancelled (over 30 s) |
| pilots-traffic: Quietest home-region airports | cancelled (over 30 s) |
| pilots-traffic: Most popular domestic routes | cancelled (over 30 s) |
| pilots-traffic: Most popular international routes | cancelled (over 30 s) |
| military: Most popular RAAFv aircraft last month | 13,355 |
| military: RAAFv flights (last 7 days) | 13,303 |
| pilot-onboarding: logged on in a month but never flew | 12,940 |
| military: Most popular RAAFv routes | 11,565 |
| pilot-onboarding: logged on but never flew (90 days) | 11,494 |
| military: RAAFv flights by aircraft type | 11,235 |

These are single queries on an otherwise idle 1 CPU container. A dashboard opens about ten panels at once and Grafana polls them, so the real latency is a multiple of this. The cost is linear in history (about 2 s per year per panel), so on the current growth the first panels would time out at roughly three years and all would be slow well before that. The movements panels (busiest and quietest airports, routes) fail first because `movements` evaluates the view twice.

For scale, the live database today holds about 1,100 sessions, so none of this is visible yet.

## Candidates

1. **Fast path for single-session groups** (prototyped, rejected). Split the view: groups of (cid, callsign, departure, arrival) that hold parts of one session only (nine flights in ten) are read straight from `flight_part`; only groups with more than one session go through the chain computation. It is logically equivalent and needs no stored state, and a row-for-row comparison against the old view over the one-year data returned 185,018 rows both ways with 3 rows differing (see below). But it is slower: it needs one more grouped pass and sort (to find the multi-session groups) and scans `flight_part` three times; panel times at one year were 3 to 27 s instead of 1.5 to 12 s (the container had `shared_buffers` at 256 MB for that run after an out-of-memory restart, which accounts for part of it, but buffer and temp-file counts for `SELECT count(*) FROM flight` were also higher: 25,590 against 9,184 buffers and about double the temp pages). The sort on four text keys is the cost, and this keeps it. Dropped; nothing of it is in the repository.
2. **Time-bounded pre-filter** (not possible). The chain id is a running sum, so a time window cannot be pushed under it without changing the chains that cross the window edge, and the planner will not push a predicate under a window function anyway. A hand-written bound would need every panel to carry it.
3. **Stored merged flights** (prototyped as a floor, recommended). Keep the merge result in a table that the existing 5 minute `refresh_flight_summary` also maintains, and make `flight` a thin view over it. Floor measured at five years with a plain table `flight_mat AS SELECT * FROM flight` (267 MB with two indexes) and the same panel SQL over views stacked on it:

| Panel (five years) | current view | stored table |
|---|---|---|
| pilots-traffic: Busiest home-region airports | over 30 s | 725 ms |
| pilots-traffic: Quietest home-region airports | over 30 s | 728 ms |
| pilots-traffic: Most popular international routes | over 30 s | 608 ms |
| pilots-traffic: Most popular domestic routes | over 30 s | 553 ms |
| pilots-traffic: Busiest days by pilots online | 11,186 | 697 ms |
| pilots-traffic: Most popular aircraft by flights | 9,964 | 372 ms |
| pilot-onboarding: logged on in a month but never flew | 12,940 | 1,061 ms |
| management: Australian-based pilots and ATC online each month | 10,847 | 1,563 ms |
| military: Most popular RAAFv aircraft last month | 13,355 | 483 ms |

4. **Materialised view refreshed every few minutes**. Same speed as 3 but a full rebuild each time (about 10 s at five years, growing), and the same staleness issues.

## Why it was not implemented

Candidate 3 is the right fix, but it is not contained and it is not provably identical:

- `flight` today reads the plan fields (departure, arrival, callsign case, aircraft, remarks, name, home airfield) LIVE from `pilot_session`, and the home-region setting live from `app_setting`; the merge groups depend on the normalised departure and arrival. A stored copy lags plan changes and a settings change until the next refresh, and a changed plan can move a part between groups, so the stored chain has to be recomputed for the affected group, not just for recent sessions.
- `is_in_progress` depends on `online_now` and must stay live, so the view cannot be a plain table select.
- In the old view the plan and pilot fields of a merged flight come from `array_agg(... ORDER BY end_time DESC)[1]`. When two parts end at exactly the same instant the pick is arbitrary and plan dependent: the comparison above found 3 merged flights in 185,018 where the old and the prototype views picked different parts (in all three the aircraft type differed between the two tied parts; real ties need two sessions of one pilot, callsign and route ending in the same snapshot, which the ADR-021 duplicate sessions could produce). A stored version would need a defined tie-break, which is a visible, if tiny, behaviour change that the owner's "identical rows" rule does not allow without a decision.
- It touches the refresh function that the collector calls every 5 minutes, new storage that must be backed up, restored (`ops/backup`) and covered by the data dictionary and the integrity checks (issue #20).

So the recommendation is the follow-up issue filed for stored merged flights (a reviewed change with its own acceptance test, not part of issue 89's branch). The tie-break question below stays open for the owner. Proposed design:

1. New table `flight_merged` (key: first part's session_id, gap_segment, span_part; the aggregate columns of `flight` EXCEPT the live ones, plus `last_session_id` and the group key) maintained by a new function called at the end of `refresh_flight_summary_sessions`: recompute the merge only for the groups (cid, callsign, departure, arrival) that contain a session touched in the refresh window plus groups that held the part's previous key (a changed plan). A one-off backfill in the migration.
2. `flight` becomes `flight_merged` joined to `pilot_session` on `last_session_id` for the live plan fields and `online_now` for `is_in_progress`, so plan fields and in-progress stay live exactly as today, and only the numbers lag by the refresh interval (as `flight_summary` already does).
3. Define the tie-break (end_time DESC, then session_id DESC) and get the owner's agreement, or accept "equal except on exact ties" in the comparison.
4. Acceptance: the row-for-row `EXCEPT ALL` comparison between the old view definition (kept as `flight_old` during the test) and the new one over this generator at 1 and 5 years and over a restore of a live dump, in the integration suite as a permanent test (the equivalence harness used for the prototype was a pair of views plus `EXCEPT ALL` in both directions on `flight`, `flight_ext`, `movements` and `cohorts`, and a column name and type comparison); the ten slowest panels under 3 s at five years; `test_every_panel_query_runs_as_grafana_ro` unchanged.
5. Until then, if a panel starts to time out, the cheapest stop-gap with no code is to raise grafana_ro's `statement_timeout` for the datasource; this does not fix latency. The earliest timeout at the current growth is a few years away, so there is time to do this properly.

## Reproducing

`tests/scale/gen_synth.sql` (psql variables `nflights`, `days`; 175000 and 365 gave the one-year set, 925000 and 1825 the five-year set) and `tests/scale/run_panels.py <dsn> <label> <dashboards dir> <runs>`. Use a database name ending in `_test`, a capped container and the migrations applied; run the timer as `grafana_ro`. Never run it on the live database.
