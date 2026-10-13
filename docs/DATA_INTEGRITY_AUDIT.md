# Data integrity audit (T30, data-integrity half)

> **Note, issue 89 phase 5 (13-10-2026, branch p5-remove-old, NOT YET DEPLOYED).** This is a dated record. It uses the names as they were when it was written: `flight_summary` meant the old pieces table, `flight_record` the table of finished flights. Migration 20261013000300 drops the pieces table, `flight_part`, `flight_legacy`, `refresh_flight_summary` and `refresh_flight_summary_sessions`, and renames `flight_record` to `flight_summary`. Current description: [DATA_DICTIONARY.md](DATA_DICTIONARY.md) and ADR-030.

Audit date 08-10-2026, 12:40 to 13:41 UTC for the measurements, finished 17:05 UTC (VM clock at start 12:40:43 UTC, at last check 17:02:31 UTC). Independent auditor: did not write the collector, views or panels.
Code under test: the deployed collector image (built 12:37:18 UTC on 08-10-2026, collector container started 12:37:21 UTC) and migrations 20261007000001 to 20261008000090 (all 15 applied). Branch `t30-data-integrity`, scripts in `ops/audit/`.
The live database held data from 07-10-2026 21:38:19 UTC; at the time of the final capture it held 3,821 snapshots, 417 pilot sessions, 111,571 positions and 46 controller sessions.

## 0. Headline

**One clear defect affects numbers on live dashboards (F1).** On 08-10-2026 at 04:08:27 UTC the VATSIM feed restarted (a 38 s hole in `update_timestamp`) and afterwards published `logon_time` without its fractional seconds for every connection that was already online (`...03:30:22.516991Z` became `...03:30:22Z`). A pilot session is identified by `(cid, callsign, logon_time)`, so every in-region pilot online at that moment was stored as a second session: **31 pilot sessions and 3 controller sessions were duplicated**. The controller views hide the duplicates (5-minute reconnect merge); the pilot views do not. Effect on the data as it stands now (382 gated flights): flights **+30 (7.9 %)**, home-airport departures **+25** and arrivals **+21**; YSSY movements 181 against 166 corrected; "Most popular international routes" shows YSSY-NZAA 6 where 5 is right and ranks it differently; busiest days, airlines, flights by region and cohorts carry the same inflation. Any future feed restart will do the same, and the 90-day windows will carry the inflation for 90 days. No code was changed (audit rules); a fix design is in section 2, F1.

Everything else that was checked agrees:

| Question | Result |
|---|---|
| Ingest filter against the raw feed | 44 live feed snapshots (12:43 to 13:36:57 UTC), recomputed with an independent implementation: **0 unexplained differences** in counts, in the exact set of stored pilots and controllers, in position fields, frequency and rating. |
| Polygon rule (edge excluded) | 700,554 synthetic points (3,772 exactly on an edge or vertex) through an exact-arithmetic implementation and shapely 2.2.0 (the collector's version): **0 disagreements**. |
| Flights (gap split, 8 h parts, airborne minutes) | All 418 `flight_summary` rows recomputed from raw positions in plain Python: **0 differences**. Plus 14 synthetic edge-case flights through the SQL function: 0 differences. |
| Panels | 26 panels recomputed from raw tables in one read-only transaction, for two time windows: **52 of 52 agree (26 of 26 in each window)**. |
| Rows lost or double counted | Sum of `pilots_in_region` over all snapshots equals the number of stored positions exactly (111,571); every snapshot has its `poll_log` row; no orphans; no duplicate identities. |
| Retention | A 90-day drill on a throwaway database: flights and airborne minutes survive the partition drop; a session straddling the cutoff pins its partition. |

Two medium findings (F2 future-dated feed timestamp freezes ingestion; F3 pilot reconnects are not merged, about 20 % more flights than a reconnect-merged count) and a short list of low findings follow. Limits of this audit are in section 7: the sampling window was off-peak for Australia (23:43 to 00:36 AEDT), and nothing recorded the raw feed during the 04:08 event.

## 1. Method and evidence index

All access to the live database was SELECT only (psql inside the `db` container, `SET default_transaction_read_only = on`, repeatable-read read-only transactions where several tables had to describe one instant). No compose command, restart, build, VACUUM or `roles.sql` was run. The only fetches of the public feed were `GET https://data.vatsim.net/v3/vatsim-data.json`, one every 75 s (44 in the audit window; `Cache-Control: public, max-age=15`, so never faster than the cache lifetime). Anything that writes ran in a throwaway container `vp-t30b-pg` (postgres:16.4-bookworm, 127.0.0.1, random port), removed at the end (section 8).

| Script (`ops/audit/`) | What it proves |
|---|---|
| `independent.py` | The independent rules (exact rational point-in-polygon, whitelist, near miss, frequency, caps). Imports nothing from `collector/`, does not use shapely. |
| `feed_sampler.py`, `compare_samples.py` | Part A: raw feed payload against the stored rows of the same `update_timestamp`. |
| `polygon_edge_test.py` | Part A: polygon edge cases against shapely. |
| `throwaway/replay_check.py` | Part A: real payloads, poison records, stale and future timestamps through the collector's own code (this one does run the collector's code, by design, since it tests behaviour). |
| `capture.py`, `recompute_flights.py`, `panel_recompute.py` | Part B: raw tables, flights and 26 panels recomputed independently. |
| `logon_precision_impact.py` | F1 and F3 quantification. |
| `invariants.sql` | Read-only invariant pack (18 checks; 17 when audited, one added later), now run by the watchdog every 3 days (T16). |
| `throwaway/synthetic_flights.sql`, `throwaway/retention_drill.sql` | Gap, span and cap edge cases; retention. These two and `replay_check.py` write, so they live in `throwaway/` and refuse to run on any database whose name does not end in `t30b` or `_test`. Delete the folder if "read-only only" in the brief is meant literally. |

## 2. Findings by severity

### HIGH

**F1 (GitHub issue #15, fix being built separately). Session identity breaks when the feed changes the precision of `logon_time`: 34 duplicate sessions, flights and movements inflated by 7 to 8 %.**
- Evidence (database): `SELECT first_seen, count(*) FROM pilot_session WHERE logon_time = date_trunc('second', logon_time) GROUP BY 1` returns 31 sessions with `first_seen = 2026-10-08 04:08:27.273165+00`, each the continuation of a session that ended at `04:07:49.142214` (31 sessions have `last_seen` at that instant). The same query on `controller_session` returns 3 at the same instant. Every pair has the same `cid` and `callsign`, a `logon_time` that differs only by its fractional part, and positions that continue without overlap (the last position of the first session and the first of the second are at most 5.7 nm apart for all 31 pairs, consistent with 38 s at their groundspeeds, average 289 kt, maximum 529 kt). `ops/audit/invariants.sql` reports them as "identity flap": 31 pilot pairs, 3 controller pairs.
- Evidence (feed): in the live feed at 13:03 UTC, 25 pilots have a whole-second `logon_time`, the latest at 04:06:12Z; every pilot with a later logon has a fractional one (earliest 04:08:17.5071081Z). So the feed itself changed at about 04:08, which matches the 38 s hole in `update_timestamp` (`04:07:49` to `04:08:27`). `network_snapshot` shows 32 pilots in region before and after, so nobody actually logged off.
- Impact (`logon_precision_impact.py`, raw tables, own flight code): stored 382 flights against 352 with the pairs merged; home departures 331 against 306; home arrivals 297 against 276; YSSY movements 181 against 166. The extra rows are a flight that "ends" at 04:07:49 (counted as an arrival at its destination while still airborne) and a flight that "starts" at 04:08:27 (counted as a departure from its origin hours after it left). Panels affected: popular routes, diverse routes, busiest and quietest airports (movements), airlines, callsigns, aircraft, busiest days, flights by home region, cohorts, the military flight counts, and in general every panel that counts rows of `flight`, `flight_ext`, `movements` or `cohorts` (the SELCAL percentage only marginally, as numerator and denominator both grow); "Highest groundspeeds" shows the earlier half of an affected flight ending at 04:07:49. Airborne hours are almost unchanged (339.18 against 339.45 h). Controller panels are protected by the 5-minute merge in `controller_session_merged` (checked when there were 45 controller sessions: 40 merged groups, 3 of the 5 merges are this event).
- Why it matters beyond one day: it recurs on every feed restart, and the SPEC identity `(cid, callsign, logon_time)` has no tolerance. Nothing in the repository archives a feed payload, so the 04:08 payloads cannot be replayed.
- Suggested fix (not applied): (1) truncate `logon_time` to whole seconds at parse for pilots and controllers, so the identity survives either precision; (2) a new forward migration that truncates stored `logon_time`, merges the duplicate pairs (re-point `pilot_position` and the controller event tables, rebuild `flight_summary` for the merged sessions) and adds a unique index on the truncated value; (3) a regression test with both payload shapes; (4) the T16 identity-flap check from `invariants.sql` so a recurrence alerts the same day. Until then the pairs can be found and excluded with the query above.

### MEDIUM

**F2. No sanity bound on the feed's `update_timestamp`: one future-dated snapshot freezes ingestion until the collector is restarted.**
- Evidence (throwaway database, collector code via `Collector.poll_once` with a stub feed): T0 real -> `ok`; T0 again -> `skipped_unchanged`; T0 minus 1 h -> `skipped_unchanged`; T0 + 15 s -> `ok`; **T0 + 3 days -> `ok`**; T0 + 30 s and T0 + 45 s (genuine updates) -> **`skipped_unchanged`**. A garbled or missing timestamp -> `error` (handled correctly). In `main.py` `poll_once` the only test is `snap_ts <= self.last_ts`; a timestamp within the partition horizon (today + 7 days) is accepted and becomes `last_ts`.
- Effect observed on the throwaway database after the future snapshot: `max(network_snapshot.observed_at)` is three days ahead, `data_freshness.snapshot_age_seconds` is -257,714, `online_now` shows only controllers, `pilot_online_now` is empty. In production `/health` would go 503 after 60 s (an unchanged feed is not progress) and the watchdog would page, so it is detected, but data is lost until someone restarts the container and the poisoned row stays in `network_snapshot`.
- Likelihood is low (the feed has not been seen to do this) and the guard is a few lines: reject a snapshot more than, say, 120 s ahead of the wall clock as a `FeedError`.

**F3. Pilot sessions are not reconnect-merged although controller sessions are; flight counts are inflated by reconnect churn (design question for the owner).**
- Evidence: 171 pairs of sessions (same `cid` and `callsign`, different `logon_time`) exist besides the F1 pairs; 95 of them start within 5 minutes of the previous session's last sighting, 74 with identical departure and arrival (examples: DLH380, five sessions in 12 minutes; KQA296, six logons in 30 minutes in the raw feed). These are real in the feed (63 callsigns changed `logon_time` inside my first 22 minutes of sampling), not a collector artefact. Merging with the controllers' rule (gap of 5 minutes or less) takes 417 sessions to 305 and gated flights from 382 to 280: after F1 is corrected (352) a further 72 flights (20 %) would merge.
- SPEC section 5 defines a pilot session by logon, so the behaviour is as specified, and v1 parity was not checked (T21). It is listed because every "flights" panel counts these as separate flights, while the same dashboards treat a controller reconnect as one session.

### LOW

| ID | Finding | Evidence |
|---|---|---|
| F4 | `poll_log.rejected_values` and the "Rejected values" panel understate bad data. Values outside int32 (altitude 3,000,000,000) and headings outside 0..360 are set to NULL silently, and malformed entries (empty callsign, bad `logon_time`, non-numeric `cid`) are skipped and only mentioned in `poll_log.error` while `status` stays `ok`. | `throwaway/replay_check.py`: five values nulled, `rejected_values` = 3; six malformed entries skipped, none counted. Live: 0 events so far, so no dashboard figure is wrong today. |
| F5 | An unparsable latitude or longitude (string `"abc"`) is treated as "no position": the pilot passes the filter and is stored with NULL lat/lon. NaN is excluded correctly. | `throwaway/replay_check.py` (INFO line). Real feed: 0 pilots without a position in 44 live snapshots. |
| F6 | A flight still in progress counts as a completed flight and, at its latest sample, as an arrival movement. | 18 of 382 flights were still connected at the capture; 12 of 297 home arrivals belonged to them. Fades with the 30-day windows. |
| F7 | `pilot-origins.json` panel 7 (active people by rating) labels rating 11 "Administrator" and 12 "Unknown"; the feed and the `controller_rating` lookup say 11 = SUP, 12 = ADM. | Feed `ratings` array against the SQL `CASE`. No 11 or 12 in the data yet, so latent. |
| F8 | The near-miss rule flags supervisors (`AS_SUP`: 317 polls, `EN_SUP`: 15 polls at the time) as possible new positions. Both rows in `controller_unlisted_sighting` are SUP. | `SELECT * FROM controller_unlisted_sighting`. Alert noise, not a data error. |
| F9 | Replay and ordering behaviour: after a collector restart the first poll re-applies the current snapshot and logs it `ok` (3,618 ok rows for 3,617 distinct snapshots at the time; the extra row is at 12:37:24 UTC, 3 s after the deploy). Applying an older snapshot after a newer one overwrites plan fields with the older values and changes the event tables (not reachable through the poll loop, which skips older timestamps). Idempotence itself holds: replaying real payloads again inserts 0 positions and changes no table. | `throwaway/replay_check.py`; `poll_log` ids 3682 and 3683. |
| F10 | About 0.4 % of 15-second ticks are never stored: 15 gaps of exactly 30 s, one of 38 s (the F1 event), one of 45 s (reboot test): 18 to 19 missing ticks, matching the "Snapshots missed" panel (3,821 of 3,839 expected, 0.47 %). The 30 s gaps are a poll-phase effect: `polled_at - snapshot_ts` ranges between about 8 s and about 25 s and drops by exactly 15.0 s at the next stored snapshot (13 of the 15 30 s gaps; the other two, at 03:12:49 and 04:05:49 UTC, show changes of +0.4 s and -13.1 s), once every 65 to 76 minutes, because the loop polls at the feed's own period and the CDN serves an object up to 15 s old. Harmless to airborne minutes (the 40 s cap exceeds 30 s) but the 38 s and 45 s holes lose a few seconds. | `ops/audit/poll_phase.py` on a capture; `poll_log` ids 3047 and 3048 (10:02:06 and 10:02:21 UTC, lag 24.0 s then 9.0 s). Setting `POLL_INTERVAL_SECONDS` to 10 would remove nearly all of them at the cost of two extra conditional requests a minute. |
| F11 | Text fields have no length cap: a 100,000-character name is stored as is. | `throwaway/replay_check.py`. |

### Informational (no action needed)

- The 8-hour split is the documented bucket approximation (parts of 8 h from the start of the gap segment), not the SPEC wording "a span of more than 8 hours starts a new session". On all real data, and on 14 synthetic cases, the two readings give the same flights; one real session is longer than 8 h.
- `controllers_global` counts every entry of the feed's `controllers` array including observers and supervisors (about 117 against about 100 real positions); `atis` is a separate array and never enters, as the SPEC says.
- `home_icao` (trailing four capital letters of the name) gave 31 distinct values starting with Y in the data, all plausible airports; no surname false positives were seen.
- Mappings: `controller_rating`, `facility` and `pilot_rating` equal the lookup arrays published inside the live feed row for row; facility numbers agree with the callsign suffix for all 45 controller sessions (TWR 4, GND 3, APP 5, CTR 6).
- The whitelist file is well formed: 254 unique callsigns, no duplicates, no skipped lines. The unlisted callsigns seen in the live samples are summarised in section 3.1: none is a Y-prefixed Australian position and only `EN_SUP` is a near miss.

## 3. Part A: is incoming data filtered correctly?

### 3.1 Live feed against stored rows (44 snapshots)

For each sampled payload the independent code produced the in-region pilot set (cid, callsign, logon_time), the kept controller set, `pilots_global`, `controllers_global`, `pilots_with_plan`, `rejected_values`, the capped groundspeed and altitude, the frequency in Hz and the near-miss list. `compare_samples.py` then read, for the same `update_timestamp`, `network_snapshot`, the `ok` `poll_log` row, every `pilot_position` row at that instant (with its session), every controller session spanning it with its rating and frequency event as at that instant, and the near-miss table.

Result: **44 snapshots compared, 44 with identical counts and identical entity sets, 0 unexplained differences.** 32 snapshots carry a `pilot_attr_changed_later` note (see below); no other difference type occurred. Across the samples the independent code kept 19 to 25 in-region pilots of 1197 to 1321 in the feed, and 3 to 5 whitelisted controllers of 100 to 120; every kept pilot was stored with identical latitude, longitude, groundspeed, altitude and heading, every kept controller with identical rating and frequency, and the near-miss callsign seen (`EN_SUP`) was recorded.

Feed hygiene across the 44 payloads (55,891 pilot records): 0 duplicate pilot keys, 0 pilots without a position, 2076 without a flight plan; 168 distinct unlisted controller callsigns, none Y-prefixed, and the only near miss is `EN_SUP`.

| Difference type | Count | Explanation |
|---|---|---|
| `pilot_attr_changed_later` | 117 | Plan field amended by the pilot after the sampled snapshot; stored value is the later one by design |
| `snapshot_missing` | 0 | |
| `network_snapshot_count` | 0 | |
| `poll_log_count` | 0 | |
| `pilot_missing_in_db` | 0 | |
| `pilot_extra_in_db` | 0 | |
| `pilot_field` | 0 | |
| `pilot_attr` | 0 | |
| `controller_missing_in_db` | 0 | |
| `controller_extra_in_db` | 0 | |
| `controller_rating` | 0 | |
| `controller_frequency` | 0 | |
| `near_miss_missing` | 0 | |

`pilot_attr_changed_later` is not an ingest difference: the stored plan field (route, departure time) is "latest non-null wins", and the pilot amended the plan after the sampled snapshot (for example VOZ919 `deptime` 1315 in the feed at 12:43 and 1253 stored). The 117 lines across 32 snapshots are 11 plan amendments by 8 pilots (8 routes, 3 departure times); for all 11 the raw feed value itself changed during the sampling window and the stored value follows the later feed value.

| Snapshot (UTC) | Feed pilots | Independent in-region | Stored | Independent controllers | Stored | Unexplained differences | Plan amended later |
|---|---|---|---|---|---|---|---|
| 2026-10-08 12:43:12 | 1206 | 23 | 23 | 5 | 5 | 0 | 5 |
| 2026-10-08 12:44:27 | 1198 | 23 | 23 | 5 | 5 | 0 | 4 |
| 2026-10-08 12:45:42 | 1197 | 21 | 21 | 5 | 5 | 0 | 4 |
| 2026-10-08 12:46:57 | 1206 | 21 | 21 | 5 | 5 | 0 | 4 |
| 2026-10-08 12:48:12 | 1206 | 22 | 22 | 5 | 5 | 0 | 5 |
| 2026-10-08 12:49:27 | 1212 | 24 | 24 | 5 | 5 | 0 | 5 |
| 2026-10-08 12:50:42 | 1213 | 24 | 24 | 5 | 5 | 0 | 5 |
| 2026-10-08 12:51:57 | 1227 | 24 | 24 | 5 | 5 | 0 | 5 |
| 2026-10-08 12:53:12 | 1232 | 24 | 24 | 5 | 5 | 0 | 5 |
| 2026-10-08 12:54:27 | 1236 | 24 | 24 | 5 | 5 | 0 | 4 |
| 2026-10-08 12:55:42 | 1247 | 25 | 25 | 4 | 4 | 0 | 3 |
| 2026-10-08 12:56:57 | 1247 | 24 | 24 | 4 | 4 | 0 | 3 |
| 2026-10-08 12:58:12 | 1254 | 22 | 22 | 3 | 3 | 0 | 3 |
| 2026-10-08 12:59:27 | 1265 | 21 | 21 | 3 | 3 | 0 | 3 |
| 2026-10-08 13:00:42 | 1265 | 21 | 21 | 3 | 3 | 0 | 3 |
| 2026-10-08 13:01:57 | 1274 | 21 | 21 | 3 | 3 | 0 | 3 |
| 2026-10-08 13:03:12 | 1272 | 21 | 21 | 3 | 3 | 0 | 2 |
| 2026-10-08 13:04:27 | 1260 | 21 | 21 | 3 | 3 | 0 | 4 |
| 2026-10-08 13:05:42 | 1265 | 21 | 21 | 3 | 3 | 0 | 4 |
| 2026-10-08 13:06:57 | 1258 | 21 | 21 | 3 | 3 | 0 | 4 |
| 2026-10-08 13:08:12 | 1266 | 21 | 21 | 3 | 3 | 0 | 4 |
| 2026-10-08 13:09:27 | 1271 | 21 | 21 | 3 | 3 | 0 | 4 |
| 2026-10-08 13:10:42 | 1276 | 22 | 22 | 3 | 3 | 0 | 4 |
| 2026-10-08 13:11:57 | 1281 | 23 | 23 | 3 | 3 | 0 | 4 |
| 2026-10-08 13:13:12 | 1281 | 22 | 22 | 3 | 3 | 0 | 4 |
| 2026-10-08 13:14:27 | 1282 | 22 | 22 | 4 | 4 | 0 | 4 |
| 2026-10-08 13:15:42 | 1283 | 23 | 23 | 4 | 4 | 0 | 5 |
| 2026-10-08 13:16:57 | 1282 | 23 | 23 | 3 | 3 | 0 | 2 |
| 2026-10-08 13:18:12 | 1288 | 23 | 23 | 3 | 3 | 0 | 2 |
| 2026-10-08 13:19:27 | 1299 | 23 | 23 | 3 | 3 | 0 | 2 |
| 2026-10-08 13:20:57 | 1302 | 24 | 24 | 3 | 3 | 0 | 2 |
| 2026-10-08 13:22:12 | 1307 | 24 | 24 | 3 | 3 | 0 | 2 |
| 2026-10-08 13:23:27 | 1308 | 24 | 24 | 3 | 3 | 0 | 0 |
| 2026-10-08 13:24:42 | 1303 | 24 | 24 | 3 | 3 | 0 | 0 |
| 2026-10-08 13:25:42 | 1301 | 23 | 23 | 3 | 3 | 0 | 0 |
| 2026-10-08 13:26:57 | 1311 | 23 | 23 | 3 | 3 | 0 | 0 |
| 2026-10-08 13:28:12 | 1313 | 23 | 23 | 3 | 3 | 0 | 0 |
| 2026-10-08 13:29:27 | 1318 | 21 | 21 | 3 | 3 | 0 | 0 |
| 2026-10-08 13:30:42 | 1311 | 21 | 21 | 3 | 3 | 0 | 0 |
| 2026-10-08 13:31:57 | 1314 | 19 | 19 | 3 | 3 | 0 | 0 |
| 2026-10-08 13:33:12 | 1303 | 19 | 19 | 3 | 3 | 0 | 0 |
| 2026-10-08 13:34:27 | 1312 | 19 | 19 | 3 | 3 | 0 | 0 |
| 2026-10-08 13:35:42 | 1318 | 19 | 19 | 3 | 3 | 0 | 0 |
| 2026-10-08 13:36:57 | 1321 | 19 | 19 | 3 | 3 | 0 | 0 |

### 3.2 Polygon edge cases

`polygon_edge_test.py`: the polygon file is one closed ring of 24 vertices, no holes (the auditor's implementation asserts this). Points tested: every vertex and its four one-ulp neighbours, 19 interior points on every edge and their one-ulp neighbours in both axes, a 0.05 by 0.25 degree grid over the bounding box, 200,000 random points, YPDN, YSSY, NZAA, WSSS and (0, 0). Totals: 700,554 points; 505,305 inside, 191,477 outside, 3,772 exactly on an edge or vertex. Disagreements with `shapely.contains_xy` (what `inside_polygon` calls) and with `Polygon.contains(Point)`: **0 and 0**. Every edge point is excluded, as SPEC section 4 says. For documentation, a polygon with a hole under shapely `contains_xy`: inside the hole false, on the hole edge false, in the shell outside the hole true (the production polygon has no hole). Antimeridian: not relevant (longitudes 75 to 163).

### 3.3 Poison records, duplicates, stale timestamps (collector code, throwaway database)

| Case (inside a real payload) | Result |
|---|---|
| Same pilot twice in one snapshot | One session, one position, `pilots_global` counts both array entries |
| Same callsign, new `logon_time` | Second session |
| Pilot with no position | Passes (v1 rule), stored with NULL lat/lon |
| Latitude NaN | Not stored |
| Latitude string `"abc"` | Treated as no position, stored with NULL position (F5) |
| Point exactly on polygon vertex; point in NZ | Not stored |
| gs 701, gs -5, alt 65001 | Stored with NULL gs/alt, counted in `rejected_values` |
| Altitude 3,000,000,000; heading 400 | Pilot kept, value NULL, not counted (F4) |
| Empty callsign; missing or garbled `logon_time`; non-numeric `cid` | Skipped (malformed) |
| NUL inside callsign; lone surrogate in name | Cleaned, pilot kept |
| `cid` as numeric string | Accepted |
| No flight plan; blank departure and arrival | Stored with NULL plan, fails the flight gate |
| Controller frequency 199.998, "abc", empty | Stored with NULL frequency |
| Controller callsign with trailing space | Matched after stripping |
| `SY_OBS`, lower-case `sy_twr`, unknown prefix `ZZ_NEW` | Not stored, not a near miss |
| `SY_NEW`, `SY_ATIS` | Not stored, recorded as near misses |
| Controller with null `cid` or empty callsign | Skipped |
| Replay of the same snapshots | 0 positions inserted, all tables identical |
| Real payloads applied in reverse order | Sessions and positions identical; only the event tables differ (F9) |
| Duplicate delivery of the same `update_timestamp`; one hour older | `skipped_unchanged` |
| Timestamp three days ahead | **accepted, then every genuine update skipped (F2)** |
| Timestamp `garbage` or missing | `error`, process continues |

Live, the poll log held 0 `error` polls over the whole period, so outage behaviour (HTTP errors, an empty feed, the circuit breaker) was not exercised live; the timestamp cases above were, and the repository's own unit tests cover the rest (not re-run here).

All 44 real payloads replayed through `parse_snapshot` + `apply_snapshot` reproduced the independent per-pilot and per-controller result (0 of 44 differ); the replay script ends with 34 checks passed and 0 failed. Rating and facility mappings and the 199.998 rule are covered in section 2 (informational) and 3.1 (live: `MK_TWR` carried 199.998 and was stored with NULL frequency, matching the independent result).

## 4. Part B: do the reports reflect the API data?

### 4.1 Lineage

| Measure | Raw feed field | Stored | View | Panel | Independent check |
|---|---|---|---|---|---|
| Pilots in region | `pilots[].latitude/longitude` inside polygon | `pilot_session`, `pilot_position` (one row per snapshot), `network_snapshot.pilots_in_region` | `network_history` | Pilots online now (in region), over time | positions at the newest snapshot; hourly means from positions |
| Pilots network-wide | `len(pilots)` | `network_snapshot.pilots_global`, `poll_log.pilots_seen` | `network_history` | Pilots online now (network-wide) | `poll_log` and live feed counts |
| Sessions | `cid`, `callsign`, `logon_time` | `pilot_session` identity | `flight` | every flight panel | F1, F3; no duplicate identities |
| Flights | positions | `flight_summary` (every 5 min) | `flight`, `flight_ext`, `movements`, `cohorts` | routes, airports, airlines, days, regions | 418 rows recomputed exactly |
| Airborne minutes | `groundspeed` | `pilot_position.gs_kt` | `flight.airborne_minutes` | airborne hours by region | recomputed, total 343.83 h both ways |
| Controller sessions | `controllers[]` on the whitelist | `controller_session`, events | `controller_session_merged`, `_ranked`, `controller_summaries` | ATC panels | merge groups recomputed: 40 against 40, identical start and end |
| Online now | newest snapshot | `network_snapshot`, `last_seen` | `online_now`, `pilot_online_now`, `controller_online_now`, `pilot_latest` | stats, departure board | all five agree: 21 pilots, 3 controllers at 13:03:12 UTC |

### 4.2 Counts add up

- Sum of `network_snapshot.pilots_in_region` over 3,821 snapshots = 111,571 = number of `pilot_position` rows; per snapshot, positions equal `pilots_in_region` (0 mismatches); no position at a timestamp without a snapshot.
- Every snapshot has exactly one `ok` `poll_log` row except one with two (F9); the counts in the two tables agree for all rows.
- `pilots_in_region` never exceeds `pilots_global`; no value outside the plausibility caps; `first_seen` and `last_seen` of every session equal the first and last stored position; no session without positions; no orphan; no duplicate `(cid, callsign, logon_time)`.
- `flight_summary.sample_count` sums to the stored positions less the samples newer than the last 5-minute refresh (documented lag).
- The 17 checks of `invariants.sql` return 0 except: gap over 45 s (1, the reboot test), the two identity-flap counts (F1: 31 and 3), and snapshots that created sessions for over half the in-region pilots (2: go-live and F1).

### 4.3 Panel recompute (26 panels, two windows)

Method: `capture.py` runs, in one repeatable-read read-only transaction, the panel's own SQL (Grafana macros replaced; time range = everything, then from 08-10-2026 06:00 UTC) and copies of the raw tables; `panel_recompute.py` recomputes each figure from the raw tables in Python (flights from raw positions with its own gap, span and airborne code; controller merging written from the 5-minute rule; lookups read as plain tables). The flight summary is refreshed every 5 minutes, so each session is cut at the last sample the summary knows about; nothing else is borrowed from the views.

Window: everything (capture 08-10-2026 13:38:17 UTC)

| Panel | Displayed (panel SQL) | Independent recompute | Agree |
|---|---|---|---|
| pilots_online_region | 18 | 18 | YES |
| pilots_online_global | 1330 | 1330 (poll_log.pilots_seen at newest snapshot) | YES |
| ctrl_online_now | 3 | 3 | YES |
| pilots_over_time | 17 hourly rows, mean in-region 28.7226 | 17 rows, mean 28.7226 | YES |
| ctrl_over_time | 17 hourly rows, mean 3.2795 | 17 rows, mean 3.2795 | YES |
| ctrl_logged_on_30d | S1=5; S2=7; S3=6; C1=3; C3=1; I1+=2 | S1=5; S2=7; S3=6; C1=3; C3=1; I1+=2 | YES |
| ctrl_monthly_by_rating | ['2026-10-01', 5, 7, 6, 3, 1, 2] | ['2026-10-01', 5, 7, 6, 3, 1, 2] | YES |
| ctrl_ctr_fss_monthly | ['2026-10-01', 5] | ['2026-10-01', 5] | YES |
| ctrl_active_monthly | ['2026-10-01', 27] | ['2026-10-01', 27] | YES |
| ctrl_top50 | 34 rows, top Mitchell Williamson 4.6 h, sum 54.4 h | 34 rows, top Mitchell Williamson 4.6 h, sum 54.4 h | YES |
| ctrl_shortest_avg | 0 rows | 0 rows | YES |
| routes_international | YSSY-NZAA 6/4; YSSY-WSSS 6/4; YMML-LFBO 5/2 ... | YSSY-NZAA 6/4; YSSY-WSSS 6/4; YMML-LFBO 5/2 ... | YES |
| airports_busiest | YSSY 121/60; YMML 54/57; YBBN 22/27; YPPH 19/14 ... | YSSY 121/60; YMML 54/57; YBBN 22/27; YPPH 19/14 ... | YES |
| airports_quietest | 20 rows; first YBAF 1; last YPMH 2 | 20 rows; first YBAF 1; last YPMH 2 | YES |
| routes_diverse | 3/3; 3/3; 2/3; 2/2; 2/2 | 3/3; 3/3; 2/3; 2/2; 2/2 | YES |
| mgmt_monthly | 2026-10-01 Y=105 all=203 atc=34 | 2026-10-01 Y=105 all=203 atc=34 | YES |
| airlines | Qantas 57/32; Jetstar 53/30; Virgin Australia 26/12 ... | Qantas 57/32; Jetstar 53/30; Virgin Australia 26/12 ... | YES |
| flights_by_region | Australia 220; Europe 48; Unknown 35; S & SE Asia 29 ... total 382 | Australia 220; Europe 48; Unknown 35; S & SE Asia 29 ... total 382 | YES |
| airborne_by_region | Australia 154.4; Europe 56.3; S & SE Asia 45.2 ... total 339.0 | Australia 154.4; Europe 56.3; S & SE Asia 45.2 ... total 339.0 | YES |
| busiest_days_flights | 2026-10-08 342; 2026-10-07 40 | 2026-10-08 342; 2026-10-07 40 | YES |
| selcal | 27.49 | 27.49 | YES |
| most_time_online | 2010197:6; 1975840:5; 1189567:5; 1772222:5 ... | 2010197:6; 1975840:5; 1189567:5; 1772222:5 ... | YES |
| highest_gs | QFA64 625; QTR19E 600; QFA10 583 ... | QFA64 625; QTR19E 600; QFA10 583 ... | YES |
| active_people | Pilots=158; S1=6; S2=8; S3=7; C1=3; C3=1; I1=1; I3=1 | Pilots=158; S1=6; S2=8; S3=7; C1=3; C3=1; I1=1; I3=1 | YES |
| snapshots_missed | 0.47 % | 0.47 % (3821 of 3839 expected) | YES |
| rejected_values | 0 | 0 (NULL gs/alt in pilot_position) | YES |

panels compared: 26; agree: 26; disagree: 0

Window: from 08-10-2026 06:00 UTC (capture 08-10-2026 13:38:20 UTC)

| Panel | Displayed (panel SQL) | Independent recompute | Agree |
|---|---|---|---|
| pilots_online_region | 18 | 18 | YES |
| pilots_online_global | 1330 | 1330 (poll_log.pilots_seen at newest snapshot) | YES |
| ctrl_online_now | 3 | 3 | YES |
| pilots_over_time | 8 hourly rows, mean in-region 34.1497 | 8 rows, mean 34.1497 | YES |
| ctrl_over_time | 8 hourly rows, mean 4.3972 | 8 rows, mean 4.3972 | YES |
| ctrl_logged_on_30d | S1=2; S2=3; S3=3; C1=2; C3=1; I1+=2 | S1=2; S2=3; S3=3; C1=2; C3=1; I1+=2 | YES |
| ctrl_monthly_by_rating | ['2026-10-01', 5, 7, 6, 3, 1, 2] | ['2026-10-01', 5, 7, 6, 3, 1, 2] | YES |
| ctrl_ctr_fss_monthly | ['2026-10-01', 5] | ['2026-10-01', 5] | YES |
| ctrl_active_monthly | ['2026-10-01', 27] | ['2026-10-01', 27] | YES |
| ctrl_top50 | 23 rows, top Rex L 3.1 h, sum 29.6 h | 23 rows, top Rex L 3.1 h, sum 29.6 h | YES |
| ctrl_shortest_avg | 0 rows | 0 rows | YES |
| routes_international | YSSY-WSSS 5/4; YBBN-PHNL 3/1; YSSY-KLAX 3/1 ... | YSSY-WSSS 5/4; YBBN-PHNL 3/1; YSSY-KLAX 3/1 ... | YES |
| airports_busiest | YSSY 54/33; YMML 25/41; YBBN 11/14; YBOK 10/10 ... | YSSY 54/33; YMML 25/41; YBBN 11/14; YBOK 10/10 ... | YES |
| airports_quietest | 20 rows; first YBAF 1; last YBAS 4 | 20 rows; first YBAF 1; last YBAS 4 | YES |
| routes_diverse | 3/3; 2/2; 2/2; 2/2; 2/2 | 3/3; 2/2; 2/2; 2/2; 2/2 | YES |
| mgmt_monthly | 2026-10-01 Y=105 all=203 atc=34 | 2026-10-01 Y=105 all=203 atc=34 | YES |
| airlines | Jetstar 34/24; Qantas 27/18; Virgin Australia 18/11 ... | Jetstar 34/24; Qantas 27/18; Virgin Australia 18/11 ... | YES |
| flights_by_region | Australia 123; Europe 29; Unknown 20; S & SE Asia 13 ... total 213 | Australia 123; Europe 29; Unknown 20; S & SE Asia 13 ... total 213 | YES |
| airborne_by_region | Australia 101.0; Europe 38.8; S & SE Asia 27.5 ... total 213.3 | Australia 101.0; Europe 38.8; S & SE Asia 27.5 ... total 213.3 | YES |
| busiest_days_flights | 2026-10-08 180 | 2026-10-08 180 | YES |
| selcal | 28.17 | 28.17 | YES |
| most_time_online | 2010197:6; 1975840:5; 1189567:5; 1772222:5 ... | 2010197:6; 1975840:5; 1189567:5; 1772222:5 ... | YES |
| highest_gs | QTR19E 600; AQC7 569; QFA7 566 ... | QTR19E 600; AQC7 569; QFA7 566 ... | YES |
| active_people | Pilots=112; S1=5; S2=6; S3=5; C1=3; C3=1; I1=1; I3=1 | Pilots=112; S1=5; S2=6; S3=5; C1=3; C3=1; I1=1; I3=1 | YES |
| snapshots_missed | 0.49 % | 0.49 % (3821 of 3840 expected) | YES |
| rejected_values | 0 | 0 (NULL gs/alt in pilot_position) | YES |

panels compared: 26; agree: 26; disagree: 0

In the list of top-N panels a tie at the cut can legitimately differ ("Highest groundspeeds" row 20: QFA292 and SIA285 both peaked at 539 kt, and the SQL orders by speed alone); the recompute accepts differences only among rows tied at the cut speed, and none other occurred.

Time zone: the same panels run under `SET TIME ZONE 'Australia/Sydney'` return the same instants and values (only the display offset of timestamps differs), so no panel depends on the session time zone (T27).

Not covered by the recompute and why: Monday event attendance (no Monday since go-live on Wednesday 07-10-2026), the retention panels (no history), and panels outside the sample (VFR routes, popular callsigns, aircraft, military, pilot-onboarding, pilot hours per month, the retention dashboard). They read the same views the sampled panels read, so the lineage above applies. The recompute reproduces the panels' own definitions; where a definition is itself questionable (F1, F3, F6) the panels and the recompute agree and are both inflated.

### 4.4 Partition retention (90 days)

`throwaway/retention_drill.sql` on a throwaway database: a session wholly 100 days old, one straddling the cutoff (91 d to 90 d ago) and one recent, 3 flights, 180.00 airborne minutes. First drop run (today): old partitions dropped, the straddling session's two partitions kept (pinned by `last_seen >= cutoff`), flight view identical (0 rows differ). Second run (one day later): the straddling session's partitions dropped after finalisation (`final = true`), flights and airborne minutes unchanged (3 flights, 180.00), a refresh afterwards leaves the final rows alone. Reading the migrations: only `pilot_latest` (3 hours) and `pilot_sample` (card 156, latest Monday event) read raw positions; everything else reads `flight_summary` or the session tables, which are never trimmed. No panel needs positions older than 7 days.

## 5. Part C: v1 comparison

Deferred (no overlap, T21). Noted only: v1 identified sessions by `(cid, callsign, logon_time)` plus departure and arrival (ADR-006), and its handling of reconnects and of the feed's `logon_time` precision was not examined, so F1 and F3 cannot be called differences from v1 until T21.

## 6. Part D: recommendations

### 6.1 Make these checks permanent (feeds T16)

| # | Check | Where | Cadence | Source |
|---|---|---|---|---|
| 1 | Invariant pack (17 SQL checks: poll_log against network_snapshot, positions against `pilots_in_region`, duplicates, caps, gaps, orphans, partitions, reference hashes) | `ops/check_data_integrity.py` wrapping `invariants.sql`; integration test on the `_test` database with seeded violations | daily and after every deploy | `ops/audit/invariants.sql` |
| 2 | Identity-flap detector: sessions of one `cid` + `callsign` with `logon_time` within 2 s, or a snapshot that creates sessions for more than half the in-region pilots | same pack; alert through the watchdog | every 5 minutes (cheap query on the last hour) | `invariants.sql` |
| 3 | Feed cross-check: independent recompute of a fresh feed snapshot against the stored rows (counts, exact pilot and controller sets, positions, frequency, rating, near misses) | read-only sidecar or cron, not CI (T16 item c) | every 5 minutes, results to a status file the watchdog reads | `independent.py` + `compare_samples.compare()` |
| 4 | Timestamp guard test and code guard (F2) | unit test: future timestamp, equal, older, garbled | CI | new |
| 5 | Panel recompute regression: the 26 panels of this audit | run from a workstation or the VM | after each deploy and monthly | `capture.py` + `panel_recompute.py` |
| 6 | Flight recompute: `flight_summary` against raw positions for finished sessions | same | daily sample of 50 sessions, full weekly while data is under 90 days | `recompute_flights.py` |
| 7 | Retention drill (partition drop keeps flights) | integration test, partly present in `test_partitions.py`; add the straddling-session case | CI | `throwaway/retention_drill.sql` |
| 8 | Unit test with the exact-arithmetic polygon implementation against the fixture feed (T16 item a) | `tests/` | CI | `independent.py` |
| 9 | Counters: `rejected_values` to include nulled out-of-range heading and int32 values; a `malformed_entries` column (F4) | collector + migration | n/a | new |

### 6.2 Keep a rolling sample of raw feed payloads: yes

Measured sizes (live payloads): full payload 1.44 million characters, about 310 KB gzipped; a compact audit record (every pilot: cid, callsign, logon_time, latitude, longitude, groundspeed, altitude; every controller: cid, callsign, logon_time, frequency, rating, facility; the timestamp) is 104 KB, about 42 KB gzipped. Storing every 15 s would cost 1.7 GB per day for full payloads, so do not.

Sketch (an alert-only sidecar `auditor`, independent of the collector so it also catches collector faults):

```
every 300 s, aligned to the clock:
  payload = GET feed                              (own User-Agent, honours max-age)
  record  = compact(payload)                      -> /audit/compact/YYYYMMDD/HHMMSS.json.gz   (about 42 KB)
  hourly  : payload                               -> /audit/full/YYYYMMDD/HH0000.json.gz      (about 310 KB)
  anomaly : keep the full payload 90 days when the previous or next snapshot has a gap over 20 s,
            when `update_timestamp` jumps, or when the identity-flap check fires
  wait until network_snapshot has the timestamp (up to 90 s), run compare(), write /audit/status.json
  retention: compact 30 days (about 12 MB per day, 360 MB), hourly full 7 days (50 MB), anomaly 90 days
daily: run invariants.sql, append to /audit/status.json
```

About 0.5 GB in total on a volume with 174 GB free. Names and CIDs are public (ADR-019), so the archive needs no scrubbing, but keep the volume out of any public path. The archive's second use is a golden replay: `throwaway/replay_check.py` feeds archived payloads into a scratch database, so any collector change can be regression-tested on real data. The 04:08 pair (before and after the precision change) would have been the ideal fixture for F1; the next anomaly would be captured automatically.

### 6.3 Order of work

1. F1 fix with migration and regression test (the only defect that moves numbers today), then run `invariants.sql` and `panel_recompute.py` again.
2. F2 guard (a few lines, one test).
3. Owner decision on F3 (merge pilot reconnects within 5 minutes like controllers, or keep and label).
4. T16 items 1 to 3 above; the auditor sidecar after the owner's go.

## 7. What could not be verified

- **Busy hours.** A later attempt to add Australian-evening samples was not possible: the session resumed at 17:02 UTC (04:02 AEDT), and the next evening peak (about 09:00 to 12:00 UTC) is hours away. The live sampling ran 12:43 to 13:36:57 UTC (23:43 to 00:36 AEDT): in-region pilots 19 to 25 and 3 to 5 controllers (global pilots 1,197 to 1,321, so the global side was busy). The Australian evening peak (about 09:00 to 12:00 UTC) cannot be compared against the raw feed because nothing archived it; the 12 of 12 samples recorded in docs/RESUME.md section 2 (including the 08-10 evening peak) are the only earlier evidence. Stored data for the peak is internally consistent (all invariants hold on it).
- **The 04:08:27 event itself.** Reconstructed from stored rows and from today's feed, not from a recorded payload.
- **Gap split (more than 120 minutes) and the 8-hour split on real data.** No real session has a gap over 120 minutes; one session exceeds 8 hours. Both are verified on synthetic data only (14 edge-case flights, SQL against independent Python, 0 differences).
- **Retention panels, Monday attendance, 30 to 90 day windows**: no history yet (no backfill).
- **v1 comparison** (section 5).
- **Roles and `grafana_ro` tests**: out of scope here (T30 test-audit half).

## 7a. Suggested GitHub issues

| Finding | Title | One-line body |
|---|---|---|
| F1 | (already #15) | Feed re-published logon_time without fractional seconds at 04:08:27 UTC; 31 pilot and 3 controller sessions duplicated. |
| F2 | Reject or ignore future-dated feed update_timestamp | A timestamp 3 days ahead is accepted and every genuine update is then skipped until restart; add a bound (for example now + 120 s) and a test. |
| F3 | Decide whether pilot reconnects within 5 minutes should merge into one flight | Controllers merge reconnects, pilots do not; merging would cut gated flights by about 20 % (382 to 280; 352 to 280 once F1 is fixed). |
| F4 | Count nulled out-of-range values and malformed feed entries in poll_log | Altitude beyond int32, heading outside 0..360 and skipped malformed records are not in rejected_values, so the panel understates. |
| F5 | Treat unparsable latitude/longitude as malformed, not as no position | A string latitude passes the polygon filter and is stored with NULL position. |
| F6 | Flag open flights in flight views | Flights still connected count as completed flights and arrivals (18 of 382 now). |
| F7 | Fix rating labels 11/12 in pilot-origins panel 7 | 11 is SUP and 12 is ADM in the feed; the panel says Administrator for 11 and Unknown for 12. |
| F8 | Exclude _SUP callsigns from near-miss alerts | AS_SUP and EN_SUP are supervisors, not new positions. |
| F9 | Log a replayed snapshot after restart as skipped, and guard plan fields against older snapshots | First poll after restart writes a second ok poll_log row; older-after-newer apply overwrites plan fields. |
| F10 | Lower POLL_INTERVAL_SECONDS to 10 to stop periodic missed ticks | Poll phase wraps against the feed about hourly, losing one 15 s tick (about 0.4 %). |
| F11 | Cap text field lengths at ingest | A 100,000 character name is stored unchanged. |
| T16 | Add ops/audit/invariants.sql and the feed cross-check as permanent checks, and a rolling raw payload archive | See section 6 of the audit. |

## 8. Cleanup and reproducibility

- Throwaway container `vp-t30b-pg` removed with its anonymous volume (`docker rm -f -v`); `docker ps -a` afterwards lists no `t30b` container and `docker volume rm` of its volume answers "no such volume".
- VM scratch folder `/tmp/vp-t30b` and the password file `/tmp/vp-t30b.pw` deleted; `ls /tmp` shows no `t30b` entry. The other agent's `vp-t30a-pg` and the other containers present at the end were not touched.
- No production container was started, stopped, restarted or rebuilt by this audit (the collector container's start at 12:37:21 UTC on 08-10-2026 is the deploy, before the audit began). `db/roles.sql` was never run.
- Local temporary folders (raw feed samples, captures, scratch copies) deleted; the evidence is reproducible with `ops/audit/README.md`. The git worktree `.worktrees/t30-integrity` (git-ignored) is left in place for review; nothing was pushed.

To reproduce: `ops/audit/README.md`.
