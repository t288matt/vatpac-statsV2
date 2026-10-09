# V1 against v3 comparison (issue #19, tracker T21)

Status: COMPLETE for pilots, controllers, flights and movements (09-10-2026). Sections 1 to 5 (method and the EXPECTED differences) were committed on their own at about 07:45 UTC BEFORE any v1 and v3 figures were compared (commit 24f07d5); section 4 is unchanged. Sections 6 onward are the results, added in a later commit. Cards 94 and 95 cannot be compared yet (section 6.8).

## 1. Rules for touching v1 (owner approval 09-10-2026, read-only)
- SELECT only, inside a session with `SET default_transaction_read_only = on` and `statement_timeout = '30s'`.
- Every query bounded by time on an indexed column, or by a primary-key range. `flights_archive` (65 GB) and `transceivers` (3.7 GB) are never queried.
- No restarts, no writes, no change to the box, containers, cron or monitors.
- SQL goes over ssh from a file (stdin), run with the container's own environment so no credential is ever printed or copied.

## 2. What v1 holds (catalogue and newest-row probes, 09-10-2026 about 07:30 UTC)
| Table | Rows (estimate) | Size | Newest row | Note |
|---|---|---|---|---|
| `flights` (raw pilot rows) | 1.0 M | 1.1 GB | 09-10-2026 07:26:50 | live; written by the polling service |
| `controllers` (raw controller rows) | 0.68 M | 277 MB | 09-10-2026 07:26:24 | live |
| `flight_summaries` | 87 k | 136 MB | completion 08-10-2026 23:10:32 | rows are still being created today (created_at 09-10-2026 07:27) |
| `controller_summaries` | 13.4 k | 23 MB | session start 09-10-2026 05:52:49, end 06:44:16 | producing now |
| `flights_archive` | 75 M | 65 GB | not touched | never queried |

Raw coverage of `flights` per hour (distinct `last_updated` values, which is the poll count): 07-10-2026 15:00 to 21:xx UTC, a hole until about 07:00 UTC on 08-10-2026, then continuous to now at 38 to 39 polls an hour (one poll about every 92 s). v1 therefore stopped around 21:22 UTC on 07-10-2026, restarted in the early morning of 08-10-2026, and has run since.

v3 has collected since 07-10-2026 21:38 UTC. The comparable window is therefore whole clock hours from **08-10-2026 08:00 UTC** (the first hour with a full set of v1 polls) up to the last whole hour before the queries are run. This is confirmed against v3's `poll_log` in the results commit; any hour in which v3 itself had a gap is excluded and listed.

## 3. Method
For each whole UTC clock hour in the window, compute the same quantity from each system and tabulate v1, v3, difference and ratio:
1. Pilots in region: distinct CIDs seen in the hour, and the mean number online per poll.
2. Controllers online: distinct CIDs and distinct callsigns seen in the hour, and the mean online per poll.
3. Completed flights per hour (by flight end) from `flight_summaries` (v1) and the `flight` view (v3), with the summary lag taken into account.
4. Controller sessions per hour (by session start), v1 `controller_summaries` against v3 `controller_session_merged`.
5. Distinct CIDs, airport movements, and total hours online.
6. A handful of v1 card SQL statements (from `docs/reference/v1_metabase_cards.json`, for example 55 movements, 94 and 95 online hours) run against v1, and the matching v3 panel SQL (`grafana/dashboards/**/*.json`) on the same window.

Every difference then goes into exactly one of three groups: **expected** (matches section 4), **explained** (a cause found, with evidence) or **unexplained** (needs the owner or a code change).

## 4. Expected differences, written before the comparison
| # | Difference | Expected effect on the numbers |
|---|---|---|
| E1 | Cadence: v3 polls every 15 s (the feed's own update interval), v1 about every 92 s, so v1 sees about one snapshot in six (HANDOVER section 11, ADR-005). | Per-hour distinct CIDs: v1 lower or equal, because short visits fall between v1 polls (a pilot or controller present for under about 90 s can be missed entirely). Mean-online per poll: close in both systems, within sampling noise. Anything time-integrated (hours online) is within a few percent, noisier for controllers on short shifts. |
| E2 | Flight gate: both require a non-empty departure and arrival, but v1 applies it at ingest and v3 in the `flight` view. | Roughly equal flight counts. v3 keeps early positions that v1 dropped, so v3 start times can be earlier. |
| E3 | v1 summarises only after the newest raw row is `FLIGHT_COMPLETION_HOURS` old (8 h in compose), v3 recomputes every 5 minutes. | Flights ended in the most recent 8 hours are missing from v1 summaries but present in v3. The comparison must therefore use hours at least 9 hours old, or count v3 only up to the same cut-off. v1 summary production is also known to have stalled for months (last summary 12-08-2026 before the restart). |
| E4 | Flight splitting: v3 splits on a 120-minute sample gap and every 8 h of span; v1 splits on a 120-minute gap only, on the key `(callsign, cid, departure, arrival)`. | Very long sessions (over 8 h) give more v3 rows than v1 rows. Rare. |
| E5 | Duplicates in v1: summaries keyed on mutable fields (departure, arrival, route) give extra rows when a plan was amended. | v1 count at or above v3 for an affected flight; I expect a small number (tens, not hundreds) over the window. |
| E6 | Controller sessions: both merge reconnects within 5 minutes; v1 closes a session after 15 minutes of inactivity and archives it, v3 derives open or closed in the view and treats a controller as offline after about two missed snapshots. | Roughly equal session counts. v3 session end is up to 15 minutes earlier than v1 end, so v3 durations are slightly shorter, a few minutes per session. |
| E7 | Airborne time: v3 airborne minutes are the sum of gaps where groundspeed is at least 60 kt, each gap capped at 40 s. v1 reports enroute minutes (planned or sectored time, `total_enroute_time_minutes`) and `time_online_minutes`. | Not directly comparable. v3 airborne is a floor and should be below v1 online minutes for the same flight. Only a rough ratio check is possible. |
| E8 | `logon_time`: v3 truncates to whole seconds (ADR-021), v1 keeps the fraction. | No count effect, but exact joins on `logon_time` must truncate the v1 value first. |
| E9 | Polygon: the same polygon file in both. v3 treats an edge point as outside and a pilot with no position as inside. | Counts match to within a pilot or two around the boundary; any larger gap is unexplained. |
| E10 | v3 sets groundspeed over 700 kt and altitude outside -1500 to 65000 ft to NULL; v1 stores them as received. | Highest-speed and highest-altitude figures can differ, v3 never higher. |
| E11 | Outages: v1 has no recorded polls in some hours (and the 07-10 to 08-10 hole). v3 has `poll_log` for every attempt. | Hours with a v1 poll count well below 38 are flagged and treated separately. |
| E12 | Controller whitelist: both use a whitelist; v1's failed open in some configurations (HANDOVER section 11). v3 also drops `_OBS` and the radio-silent 199.998 entries. | v1 may hold extra controllers (observers) that v3 excludes; check the callsign lists for any v1-only callsigns. |
| E13 | Time column: v1 `last_updated` is the time of the poll that stored the row, v3 `observed_at` is the feed's own `update_timestamp`. | Up to about 15 s plus polling latency of offset, which only matters for rows at an hour boundary. |

## 5. Success criteria
- Every difference sits in one of the three groups, with a table of numbers.
- No unexplained difference is larger than the combined effect of E1 to E13 for that metric; anything larger becomes a GitHub issue and a tracker row.
- A reader can rerun every query from this file.

## 6. Results (queries run 09-10-2026 between 07:30 and 08:50 UTC)

### 6.1 Window and data quality of the two sides
- Window: 23 whole UTC hours, **08-10-2026 08:00 to 09-10-2026 07:00**. Flights are compared only for ends from 08:00 to 23:00 on 08-10-2026, so that v1's 8 hour completion wait (E3) has passed.
- v1: 885 polls in the window, one every 91 to 93 s (two gaps over 150 s, the longest 290 s). 38 to 39 polls an hour except 08-10 10:00 (36).
- v3: about 5,490 snapshots in the window. `poll_log` shows no error rows in the whole period, 3 to 12 `skipped_unchanged` rows an hour (the feed had not changed), and two hours with a few missed snapshots (08-10 17:00: 235 of 240, 18:00: 237). No hour is excluded.
- Size of what was read from v1: about 23,000 pilot rows, 2,200 controller rows, 240 flight summaries and 45 controller summaries, all through time or primary-key bounds. `flights_archive` and `transceivers` were never queried. The v1 box was not changed.

### 6.2 Pilots in region, per hour
"Mean online" is rows per poll (v1) or per snapshot (v3). "v3 all" counts every in-region pilot; "v3 plan" counts only pilots whose session ended with a departure and arrival (the flight gate), which is what v1 stores. Distinct CIDs are per clock hour.

| Hour (UTC) | v1 mean online | v3 mean, all | v3 mean, plan | v1 CIDs | v3 CIDs, all | v3 CIDs, plan |
|---|---|---|---|---|---|---|
| 08-10 08 | 35.8 | 37.7 | 36.0 | 58 | 66 | 62 |
| 08-10 09 | 42.1 | 43.9 | 43.3 | 64 | 66 | 65 |
| 08-10 10 | 40.3 | 41.7 | 40.8 | 63 | 67 | 63 |
| 08-10 11 | 28.2 | 28.5 | 28.0 | 41 | 42 | 41 |
| 08-10 12 | 21.4 | 21.5 | 21.4 | 33 | 34 | 33 |
| 08-10 13 | 20.4 | 20.6 | 20.6 | 26 | 28 | 28 |
| 08-10 14 | 14.2 | 14.3 | 14.3 | 23 | 24 | 23 |
| 08-10 15 | 13.4 | 13.4 | 13.4 | 18 | 18 | 18 |
| 08-10 16 | 12.0 | 11.9 | 11.9 | 17 | 17 | 17 |
| 08-10 17 | 12.6 | 12.6 | 12.6 | 17 | 17 | 17 |
| 08-10 18 | 13.4 | 13.4 | 13.4 | 15 | 15 | 15 |
| 08-10 19 | 11.4 | 11.5 | 11.5 | 20 | 20 | 20 |
| 08-10 20 | 15.0 | 16.0 | 15.3 | 22 | 24 | 23 |
| 08-10 21 | 21.0 | 21.2 | 21.2 | 29 | 30 | 30 |
| 08-10 22 | 22.8 | 23.2 | 23.2 | 32 | 33 | 33 |
| 08-10 23 | 22.8 | 23.1 | 23.1 | 35 | 36 | 36 |
| 09-10 00 | 28.9 | 29.9 | 29.9 | 43 | 45 | 45 |
| 09-10 01 | 40.4 | 40.8 | 40.7 | 52 | 52 | 52 |
| 09-10 02 | 40.5 | 42.6 | 41.2 | 56 | 59 | 56 |
| 09-10 03 | 38.5 | 39.0 | 38.7 | 56 | 58 | 57 |
| 09-10 04 | 39.3 | 40.4 | 39.3 | 56 | 59 | 57 |
| 09-10 05 | 35.6 | 36.4 | 36.0 | 55 | 58 | 55 |
| 09-10 06 | 31.9 | 33.5 | 32.2 | 54 | 59 | 56 |
| **Sum of hourly means (pilot-hours)** | **601.9** | **617.1 (+2.5 %)** | **608.0 (+1.0 %)** | | | |
| **Sum of hourly distinct CIDs** | | | | **885** | **927 (+4.7 %)** | **902 (+1.9 %)** |
| **Distinct CIDs, whole window** | | | | **249** | **260** | **251** |

### 6.3 Pilot sets, poll by poll (the strongest check)
For each of the 885 v1 polls, the set of pilots in v1 was compared with the set in the v3 snapshot at or just before the same moment.

| Observation | Count | Group |
|---|---|---|
| v1 pilot not in v3 | 7 observations in 7 polls | see below |
| v3 pilot not in v1, session never had a plan | 336 | expected (E2) |
| v3 pilot not in v1, observed before the plan was filed (the session's `plan_changed_at` is later) | 223 | expected (E2) |
| v3 pilot not in v1, plan already filed | 12 (11 distinct pilots, one or two polls each) | explained: the first or last v1 poll of the pilot falls one poll either side of the boundary (E1, E13) |

- The pre-plan cases are confirmed by v3's own `plan_changed_at`: for example a pilot parked from 00:43:12 with `plan_changed_at` 00:57:57, and v1 first stores the pilot at 00:59:15; another with plan time 22:34:27 and first v1 row 22:36:12.
- Of the 7 v1-only observations, every one is the **last** row of a session and sits 30 to 36 s after v3's last sighting. Over all 468 sessions that ended in the window, v1 still shows the pilot in about 85 % of the polls stamped 0 to 30 s after v3's last sighting, and in about 13 % of polls from 40 s onwards (the 13 % is reconnects under the same callsign). The best alignment of v1 poll stamps to v3 snapshots is a shift of 10 to 15 s (exact set agreement peaks at 15 s). So v1 stamps appear to trail its data by roughly 10 to 15 s, with a tail of up to about 30 s. This is **unexplained at the cause** (it may be v1 processing delay or feed caching), but the effect is under 1 % of observations and nothing in v3 is wrong.
- After removing the pre-plan and never-planned observations, the sets agree except for the 12 boundary cases above. No polygon-edge case was found (E9).

### 6.4 Controllers
| Check | v1 | v3 |
|---|---|---|
| Sum of hourly mean controllers online (controller-hours) | 57.9 | 57.9 |
| Distinct controller CIDs, window | 31 | 31 |
| Distinct callsigns, window | 23 | 23 (the identical set; no v1-only callsign, so E12 not seen) |
| Hours with a different distinct-CID count | 0 of 20 active hours | |
| Sessions matched on (CID, callsign, whole-second logon) | 46 of 46 v1 sessions found in v3 | v3 has 9 more: 7 begin before or end after the window, 2 are short (45 s and 6 s of sightings, missed by v1, E1) |

Session edges follow the polling difference: v3 first sees a controller a median **37 s** before v1 (mean 70 s, because of a few late v1 starts) and last sees them a median **42 s** after v1; a 93 s poll predicts about 46 s each way (E1). Median session length 53.8 min in v1 against 57.5 min in v3.

Controller sessions per start hour (hours with no session omitted), v1 summaries against v3 merged sessions:

| Hours (UTC) | v1 sessions | v3 merged | v1 hours | v3 hours |
|---|---|---|---|---|
| 08-10 08 to 14 | 20 | 20 | 29.3 | 29.7 |
| 08-10 20 to 23 | 7 | 6 | 13.0 | 12.8 |
| 09-10 00 to 04 | 13 | 10 | 12.4 | 10.6 |
| **Total** | **40** | **36** | **54.8** | **53.1 (v1 +3.1 %)** |

**Explained:** v1's `controller_summaries` holds **overlapping duplicate rows** for reconnects (not predicted). CID 1906788 on SY_TWR has three v1 rows (22:33:56 to 23:25:56, 22:54:27 to 00:55:28, 00:27:44 to 01:07:58, 213 minutes in total) for a single stint that v3 merges into one 157 minute session (22:33:56 to 01:10:57). CID 1617174 on PH_APP has three overlapping rows (191 minutes) against 111 minutes in v3. Those two stints alone put 136 minutes of double counting into v1 "hours online". v3's merge works as designed (E6). v1's raw `controllers` rows agree with v3 (table above), so the inflation is in v1's summary step.

### 6.5 Completed flights
Per hour of flight end (UTC), 08-10-2026. v1 = `flight_summaries` by `completion_time`; v3 = the `flight` view by `end_time`.

| Hour | v1 summaries | v3 flights | of which v3 with 3 or more samples |
|---|---|---|---|
| 08 | 29 | 38 | 35 |
| 09 | 16 | 26 | 25 |
| 10 | 30 | 34 | 33 |
| 11 | 20 | 22 | 22 |
| 12 | 12 | 18 | 17 |
| 13 | 11 | 16 | 16 |
| 14 | 13 | 15 | 15 |
| 15 | 4 | 13 | 11 |
| 16 | 5 | 9 | 7 |
| 17 | 3 | 4 | 4 |
| 18 | 3 | 6 | 6 |
| 19 | 8 | 8 | 8 |
| 20 | 2 | 2 | 2 |
| 21 | 4 | 8 | 8 |
| 22 | 13 | 15 | 15 |
| **Total** | **173** | **234** | **224** |

Matching on (CID, callsign, whole-second logon) gives:
- **All 173 v1 summaries have a v3 flight** (zero v1-only). The v1 completion time equals the v3 end time within half a minute for the middle half of the pairs (median 0.0 minutes); the rest are v3 splits (E4) and v1 plan-based splits.
- **59 v3 flights have no v1 summary.** Their cause, by checking v1's own raw `flights` table:
  - 11: v1's raw table has no rows for the session. Nine are one-sample or sub-3-minute sessions (E1). Two are 12 minute parked connections (RSCU201 and QFA213 on 08-10) in which the plan appeared only in the very last sample before the pilot reconnected under a new logon time, so v1's ingest gate never admitted them (E2).
  - **48: v1's raw table holds the pilot (up to 134 rows) but v1 never produced a summary.** Of the 223 v3 flights that v1's raw table holds, v1 summarised 175 (78 %); about 89 % of flights over an hour, about 47 % of flights under five minutes, about 55 % of circuits. This is v1's known summary-pipeline weakness (HANDOVER section 11), now measured; it was not predicted at this size. It is **explained as a v1 defect**, not a difference in ingest: the raw pilot sets agree (6.3).
- v1 created summaries 5.9 to 15.4 hours after flight end (median 8.4 h), so any v1 chart is always at least about 6 hours behind.
- v1 duplicates (E5): 3 sessions with 2 summary rows each since 07-10-2026 (the plan changed mid-flight, for example VOZ711 YBCS-YSCB then YSCB-YSSY). v3 has 6 sessions with more than one flight row, all from the 120 minute and 8 hour splits (E4), none from plan changes.
- Zero-airborne flights: 49 of the 234 v3 flights (21 %) have no airborne minutes (parked connections, ground-only sessions). Of the 59 v3-only flights, 20 have no airborne minutes. They count by design (card 55 test); see 6.7.

### 6.6 Airborne against enroute time (E7)
For the 205 flights present on both sides, 167 have positive v3 airborne minutes and positive v1 `total_enroute_time_minutes` (v1 has enroute 0 for 20 of the 205).
- v3 airborne divided by v1 enroute: median **1.05**, middle half 1.03 to 1.17, with a long tail (maximum 49) where v1's enroute is tiny. The two measures broadly agree for normal flights; v3 airborne is about 5 % higher than v1 enroute.
- v3 airborne divided by v1 `time_online_minutes`: median 0.77, middle half 0.59 to 1.01, as predicted (airborne is part of time online). Above 1.0 happens where v1 online time starts late (pre-plan, E2).
- v3 duration minus v1 time online: median +2.0 minutes, middle half +0.5 to +16 (v3 starts earlier).

### 6.7 Airport movements (card 55, home-region airports)
Both systems counted with v1's own rule (departures and arrivals both by flight end, `LIKE 'Y%'`), over flights ending 08-10 08:00 to 23:00.

| Airport | v1 dep | v1 arr | v1 total | v3 dep | v3 arr | v3 total |
|---|---|---|---|---|---|---|
| YSSY | 55 | 25 | 80 | 80 | 29 | 109 |
| YMML | 24 | 29 | 53 | 30 | 40 | 70 |
| YBBN | 7 | 11 | 18 | 11 | 13 | 24 |
| YPPH | 11 | 6 | 17 | 12 | 7 | 19 |
| YPAD | 3 | 9 | 12 | 4 | 13 | 17 |
| All home-region airports | | | **273** | | | **368** |

- **On the flights both systems hold, movements are identical: 273 and 273.** The 95 extra v3 movements come entirely from the 59 v3-only flights.
- Of the 368 v3 movements, 283 come from flights with airborne time and **85 (23 %) from flights with no airborne minutes**. This follows from the existing decision that a parked connection counts (card 55 test, ADR-018) but it is now a measurable effect, and the owner should decide whether it is wanted (finding F2).

### 6.8 Cards that cannot be compared yet
- **Cards 94 and 95** (people online by hour of day, 90 days): v1's summary tables hold nothing between 12-08-2026 and 07-10-2026 and v3 holds 1.5 days. The v3 panels also use 30 days, not 90. A like-for-like run is meaningless today. Revisit when v3 has about 30 days of data; until then they are "fills over time" on the owner's checklist.
- Retention cards: not before 30 to 90 days of v3 history.

## 7. Scorecard of the predictions in section 4
| # | Prediction | Outcome |
|---|---|---|
| E1 | v1 distinct counts lower, means close | Held. v1 distinct CIDs 885 against 902 to 927; mean online 1 to 2.5 % lower; controller edges at 37 s and 42 s against 46 s predicted. |
| E2 | Flight gate: v3 keeps early positions | Held, and it is the main cause of every pilot difference: 559 of 571 v3-only observations. |
| E3 | v1 summaries lag 8 hours | Held (median 8.4 h). Beyond what was predicted, v1 also skips about 22 % of the flights it holds. |
| E4 | v3 splits more | Held, rarely (6 sessions). |
| E5 | v1 duplicates, tens | Held, smaller: 3 flight sessions; plus overlapping controller rows (not predicted). |
| E6 | Controller sessions equal, v3 slightly shorter | Partly. Counts equal where v1 has no duplicates; v3 hours are 3.1 % lower because v1 double counts overlaps. v3 sessions were not shorter. |
| E7 | Airborne below online, enroute not comparable | Partly. Airborne against online 0.77 as predicted; airborne against enroute is closer than expected (median 1.05). |
| E8 | Logon second truncation | Not needed: v1 `logon_time` has no fractional seconds in this window; every join matched. |
| E9 | Polygon edge | Not seen. |
| E10 | Caps | Not tested (no implausible values in the window). |
| E11 | Outage hours | One v1 hour with 36 polls; v3 two hours with a few missed snapshots. Neither distorts the totals. |
| E12 | Whitelist | Identical callsign sets (23 each). |
| E13 | Time column | Corrected: v3 `observed_at` is the collector's poll time (about 0.26 s after the 15 s grid), not the feed timestamp, and v1 stamps appear to trail its data by 10 to 15 s. |

## 8. The three groups
**Expected (section 4):** pilot-set differences from the plan gate and polling (E1, E2); controller session edges (E1); one-sample sessions (E1); v3 splits (E4); v1 flight duplicates (E5).

**Explained (cause found, evidence above):**
1. v1 flight summaries cover only 78 % of the flights v1's own raw table holds (48 of 223 missing), so v1 flight and movement figures are lower than v3 by about a quarter to a third. Evidence: 6.5.
2. v1 controller summaries contain overlapping duplicate rows for reconnects, inflating v1 controller hours by about 3 % in this window. Evidence: 6.4.
3. 20 of the 59 v3-only flights and 85 of 368 v3 movements come from sessions with no airborne time. Evidence: 6.5 and 6.7.
4. Two 12 minute parked sessions count as v3 flights because the plan appeared in their last sample. Evidence: 6.5.

**Unexplained (needs the owner or a code change):** none that points to a v3 defect. One item is unexplained at the cause but harmless: v1's pilot rows trail its stamps by 10 to 15 s with a tail of 30 s (6.3).

## 9. Findings and suggested issue titles
| Id | Finding | Suggested issue title | Action |
|---|---|---|---|
| F1 | v1 flight and controller summaries are incomplete and double count | v1 summaries omit about a fifth of flights and double count overlapping controller sessions (comparison finding) | Information for the owner; v1 is out of scope (decision 16). When comparing Metabase and Grafana by eye, expect v3 flight and movement counts about a quarter to a third higher. |
| F2 | 21 % of v3 flights and 23 % of v3 movements have no airborne time; the plan gate uses the latest plan, so a parked session that gained a plan in its last sample counts as a flight | Decide whether sessions with no airborne time should count as flights and movements | Owner decision. Recommendation: keep as is for parity with v1 (ADR-018), and add a "flights with airborne time" panel or filter so the number can be read both ways. |

No change to v3 is recommended by this comparison.

## 10. How to repeat this
Keep to section 1. Pull per-poll rows from v1 `flights` (primary-key range from id 6,694,000, time bounded), `controllers` (time bounded), `flight_summaries` and `controller_summaries` (time bounded), and the same period from v3 `pilot_position`, `pilot_session`, `controller_session`, `controller_session_merged`, `flight`, `network_snapshot` and `poll_log` through a SELECT-only session, then join on (CID, callsign, whole-second logon time). The analysis scripts are not committed; the method above is enough to recreate them. Whole run: under 30,000 v1 rows.

## 11. Owner decisions on the differences (09-10-2026, one at a time)
| # | Difference | Decision |
|---|---|---|
| 1 | Pilot sessions split by reconnects (v1 merged them) | Merge like v1: same CID, callsign, departure and arrival, gap of 120 minutes or less. Built on branch flight-reconnect-merge (ADR-023, T41, issue #35). |
| 2 | Parked sessions with no airborne time still count as flights and movements (issue #74) | Behave as v1 does: no change. |
| 3 | v3 stores pilots' positions before they file a plan; v1 dropped them | Keep v3 as it is. |
