# V1 against v3 comparison (issue #19, tracker T21)

Status: STEP 1 of 2. This commit records the method and the EXPECTED differences, written on 09-10-2026 (about 07:45 UTC) BEFORE any v1 and v3 figures were compared. Results are added in a later commit so that the predictions cannot be fitted to the answers.

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
