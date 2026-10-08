# T15 notes (batch b): RAAFv and Monday event cards, 08-10-2026

Source: the owner's current Metabase SQL for cards 142, 143, 161, 184 and 156 (card `updated_at`: 142 = 22-09-2025, 143 = 18-10-2025, 161 = 07-10-2025, 184 = 02-01-2026, 156 = 06-10-2025). All times UTC. Branch `t15-b`. Rule reused from T14 (`docs/t14_notes/events.md`): RAAFv = flight-plan remarks contain `RAAFVIRTUAL.ORG` (ILIKE), any aircraft type. Panels were not run against a live database; `tests/integration/test_military_events_t15.py` executes them against synthetic rows when `VATPAC_TEST_DSN` points at a throwaway `_test` database (compiled, not run here).

## Column mapping used (v1 to v3)

| v1 | v3 | Honest difference |
|---|---|---|
| `flights.remarks` | `flight.remarks` / `flight_ext.remarks` | Same text. |
| `flights.logon_time` | `flight.logon_time` (not in `flight_ext`, so cards 142 and 161 read `flight`) | Same. |
| `flight_summaries.completion_time` | `flight_ext.end_time` | Last sighting, not a completion event. |
| `flight_summaries.total_enroute_time_minutes` | `flight_ext.airborne_minutes` | Closest measure (groundspeed 60 kt or more, SPEC section 5). v1's exact enroute rule is not known. Same choice as card 66. |
| `aircraft_type` | `upper(btrim(aircraft_icao))` | Normalised; blank shown as `(none)`. |
| v1 `flights` / `flight_summaries` rows | `flight` rows (one per session, gap segment and 8 hour part) | v3 applies the flight gate: departure and arrival must both be filed. v1 `flights` had no such gate, so a RAAFv pilot with no filed plan is missing from every v3 card here. |
| `flight_sector_occupancy` | `pilot_sample` view (new, migration 20261008000080) over `pilot_position` | See card 156. |

## Table

| Card | v1 rule in words | v3 implementation | Differences | Owner questions |
|---|---|---|---|---|
| 142 RAAFVIRTUAL flights last 7 days (table) | `SELECT DISTINCT` callsign, CID, name, aircraft type, departure, arrival, remarks, logon time where remarks contain RAAFVIRTUAL.ORG and logon in the last 7 days, newest first | Table "RAAFv flights (last 7 days)" on `flight`; panel time override `7d`, `logon_time BETWEEN $__timeFrom() AND $__timeTo()`; same `DISTINCT` columns; `LIMIT 500`. Names and CIDs shown (ADR-009, ADR-019). | Flight gate (see above); aircraft type normalised; `LIMIT 500` (v1 unlimited); the 7 days is the panel override, not the dashboard picker. | None. |
| 143 RAAFV flights by aircraft type, 30 days (table) | Per aircraft type: distinct (callsign, CID, logon) as flights, sum of enroute minutes, hours to 2 decimals, average hours per summary row; flights completed in the last 30 days; type ordered by total hours | Table "RAAFv flights by aircraft type (last 30 days)" on `flight_ext`; `end_time` window with panel override `30d`; `count(DISTINCT session_id)`; airborne minutes; average over flight rows; top 50 types by hours | Airborne minutes instead of enroute minutes; flights counted per session but the average is per flight row, as in v1, so a session split by a 120 minute gap or 8 hour span adds rows to the average denominator only; `LIMIT 50`; `(none)` for blank type. v1's `EXISTS` on `flights` is replaced by reading remarks directly (v3 has one flight table). | None. |
| 161 Most popular RAAFv aircraft last month, flights (pie) | RAAFv flights with logon in the last 30 days, one row per (callsign, logon time) keeping the latest `last_updated`; count and percentage per aircraft type | Pie "Most popular RAAFv aircraft last month (flights)" on `flight`; `row_number() OVER (PARTITION BY session_id ORDER BY end_time DESC) = 1` instead of `DISTINCT ON` (validator forbids it, same as card 78); panel override `30d`; share to 2 decimals; legend table with value and percent | Session key includes CID (v1 did not; differs only if two pilots share callsign and logon second); top 25 types so the pie stays readable (share is still of all RAAFv flights); flight gate. | None. |
| 184 Flights flown by RAAFv aircraft per week (bar) | Count of flight summaries per `DATE_TRUNC('week', completion_time)` over the last 98 days, ended before now, newest first | Bar-style time series "Flights flown by RAAFv aircraft per week (last 14 weeks)" on `flight_ext`; `end_time` window with panel override `98d`; weeks start Monday UTC; oldest first | Grafana time series needs ascending time (v1 listed newest first); `count(*)` of flight rows (a split session counts per row, as v1 counted summary rows); weeks with no flights are absent, as in v1; the current week is partial; v1 named the week start `week_end`. Shown as a bar-style time series, not a Grafana bar chart, so the axis is real time. | None. |
| 156 Airborne aircraft during last Monday's event (line) | For the most recent Monday (today if Monday), 5-minute slots from 0700Z to 1155Z (61 generated, 1200 removed by the hour filter), count distinct callsigns in `flight_sector_occupancy` whose sector entry was at or before the slot, exit missing or after the slot, same date, entry altitude above 1000 ft | Time series "Airborne aircraft during the latest Monday event (0700-1200Z, 5-minute slots)" in `events/monday-events.json` (panel 4, appended). Monday window 0700-1200Z, the repo convention (SPEC section 6, `monday_attendance`, T14 notes). Same slot grid (60 slots, 0700 to 1155). Counts distinct sessions with a raw position above 1000 ft in the 5 minutes starting at the slot, from the new view `pilot_sample` (plain column subset of `pilot_position`, migration `20261008000080_pilot_sample_view.sql`). | **Not sector logic in v3.** v1 used the sector table only as its source of "where was the aircraft"; the question asked (how many were airborne) needs no sectors, so it is built from raw positions and carries no sector or frequency matching (ADR-002 respected; a test asserts the SQL contains neither word). Differences: (1) v1 counted at the instant of sector entry/exit; v3 counts any sample in the 5 minute bucket, which can read slightly higher around take-off and landing; (2) like v1, "airborne" is altitude above 1000 ft only, so an aircraft parked at a high airfield (for example Canberra or Alice Springs) counts as airborne; (3) slots still in the future are not drawn (v1 would draw zeros); (4) data only exists for 90 days (position retention), fine for "last Monday"; (5) before 0700Z on a Monday the panel shows the current Monday with no slots, as v1 would show zeros. | Q1: add a groundspeed condition (60 kt or more, the repo's airborne rule) to remove parked aircraft at high airfields? It is a one-line change but departs from v1. Q2: on a Monday before 0700Z, show the previous Monday instead of an empty chart? |

## STAYS-DROPPED

None of the five. Card 156 is the only one that touched the sector table, but it does not need sector or frequency logic to answer its question (see above), so it is built without it. If the owner considers "airborne count from sector occupancy" itself sector logic, remove panel 4 from `monday-events.json`, the migration and its tests.

## Files changed

- `grafana/dashboards/military/military.json` (panels 5 to 8 appended; panels 1 to 4 untouched)
- `grafana/dashboards/events/monday-events.json` (panel 4 appended)
- `db/migrations/20261008000080_pilot_sample_view.sql` (new view `pilot_sample`; `grafana_ro` receives SELECT through the existing all-views grant in `db/roles.sql`)
- `tests/integration/test_military_events_t15.py` (new)
- `docs/t15_notes/b_military_events.md` (this file)

`docs/CHART_DEFINITIONS.md` and `docs/TRACKER.md` were not edited; the coordinator should add rows for 142, 143, 161, 184 and 156 and add `pilot_sample` to `docs/DATA_DICTIONARY.md`.

## Verification

`python grafana/validate_dashboards.py` (0 errors), `python grafana/check_columns.py` (bad 0), unit tests, ruff and py_compile were run. The integration tests and panel SQL were not executed against Postgres here.
