# T14 notes: Monday event and military (RAAFv) cards, re-checked 08-10-2026

> Working note from 08-10-2026, kept as a record of the T14 re-check. Statements such as "not run" or "owner question open" describe that moment; the current position is in [../TRACKER.md](../TRACKER.md) and [../CHART_DEFINITIONS.md](../CHART_DEFINITIONS.md), which win where they differ.

Source of "v1 now": the owner's current Metabase SQL for cards 66, 78, 131, 133, 134 (card `updated_at` dates: 66 = 18-10-2025, 78 = 07-10-2025, 131 = 06-10-2025, 133 = 06-10-2025, 134 = 08-10-2025). All times are UTC. Branch `t14-events`.

| Card | Name (v1 now) | What the repo said | What v1 says now | Classification | Action taken |
|---|---|---|---|---|---|
| 66 | Hours flown by RAAFv aircraft per week | Military = aircraft type flagged `is_military` in the `aircraft_type` lookup; airborne hours per UTC week, last 98 days (14 weeks) | RAAFv = flight-plan `remarks` contain `RAAFVIRTUAL.ORG` (ILIKE), any aircraft type; summed `time_online_minutes`, rounded to whole hours, by `DATE_TRUNC('week', completion_time)`, last 98 days; excludes null CID | CHANGED (identification rule; also renamed from "military" to "RAAFv") | Panel "Hours flown by RAAFv aircraft per week (last 14 weeks)" now filters `f.remarks ILIKE '%RAAFVIRTUAL.ORG%'` instead of `f.is_military`. Series renamed "RAAFv hours". Still airborne minutes and 1 decimal (see owner question 1). No migration needed: `flight_ext.remarks` already exists. |
| 78 | What were the most popular RAAFv routes last month? | Routes by `is_military` types, last 7 days, top 25, shares to 1 decimal | Same RAAFv remarks rule; window `NOW() - 1 month` (name says "last month"); one row per session via `DISTINCT ON (callsign, logon_time)` (latest `last_updated`), then grouped by departure and arrival; non-empty departure and arrival; shares to 2 decimals; no row limit | CHANGED (identification rule, time window 7 days to 1 month, session de-duplication) | Panel renamed "Most popular RAAFv routes (last month)", panel time override `1M`, RAAFv remarks rule, `row_number() OVER (PARTITION BY session_id ORDER BY end_time DESC) = 1` (keeps the latest flight row per session, mirroring v1 de-duplication; the dashboard validator forbids DISTINCT ON), shares to 2 decimals. Kept `LIMIT 25` and the `route_label` (`DEP-ARR`) as a deliberate dashboard choice. |
| 131 | Monday event attendance | `monday_attendance` view, window 0700-1200Z, sessions overlapping the window, CID lists dropped (CHART_DEFINITIONS said v1 was 0800-1200Z) | Window is now 0700-1200Z for both controllers and flights (the 06-10-2025 edit changed 0800 to 0700); still overlap rule (starts at or before 1200, ends at or after 0700); still outputs CID lists | SAME (the repo already used 0700; the CHART_DEFINITIONS note "v1 0800-1200Z" is now out of date: v1 caught up with the repo) | No panel change. Added a boundary test (end exactly 0700 counts, start exactly 1200 does not). Differences kept on purpose: the flight gate (departure and arrival filed) is applied; v3 merges controller reconnects within 5 minutes; the week list starts at go-live not at the oldest controller session; CID lists dropped (see owner question 2). |
| 133 | How many pilots were on the same freq as a controller? (0700-1200z monday) | DROPPED (needs frequency matching) | Title window edited from 0800 to 0700; SQL still joins 5-minute transceiver slots to controller frequencies (frequency matching) | STAYS-DROPPED (ADR-002 and owner decision 1: no frequency or sector logic anywhere) | None. Test asserts neither dashboard contains the chart. CHART_DEFINITIONS row title should say 0700-1200z (coordinator to update). |
| 134 | What percentage of Monday Event traffic were locals? | Share of YMML-YSSY (either direction) airborne minutes flown by local pilots, Mondays 0700-1200Z, by week of flight end; local = `home_icao` prefix from settings | Unchanged logic: flights ending Mondays 0800-1200Z, `time_online_minutes`, local = fourth-last character of `name` is `Y` (name ends with the home airfield), name required (4+ characters). Card was renamed from "MRM traffic" to "Monday Event traffic" | SAME as the snapshot (name only); repo deviates from v1 on three known points: 0700 start (v1 0800), airborne minutes (v1 time online), and local by home airfield prefix (v1 by name suffix) | Panel unchanged. Added a regression test on the window edges (0700 in, 1200 out), route filter and the weekday filter. Window mismatch is an owner question (3). |

Panel 65 (most popular military aircraft) is not in this batch. It uses `is_military`; if the owner switched card 65 to the RAAFv remarks rule too, that panel (and its title) needs the same change. Its current v1 SQL was not supplied, so nothing was changed.

## Files changed

- `grafana/dashboards/military/military.json` (cards 66 and 78)
- `tests/integration/test_events_t14.py` (new; cards 66, 78, 131, 133, 134)
- `docs/t14_notes/events.md` (this file)

No new migration.

## Needs owner input

1. Card 66 unit: v1 sums `time_online_minutes` (connected time) and rounds to whole hours; the repo shows airborne hours to 1 decimal. Keep airborne (the earlier choice) or switch to connected time? Not changed.
2. Card 131: v1 outputs the lists of pilot and controller CIDs per Monday. The repo dropped them (a snapshot-time choice, not recorded in DECISIONS.md). ADR-009 and ADR-019 say names and CIDs are shown. Should the Monday panel add a table of CIDs per Monday?
3. Card 134: v1 still uses 0800-1200Z while card 131 and the specification use 0700-1200Z. Should the local-share chart use 0700 (repo, consistent with 131) or 0800 (exactly v1)?
4. Card 65: has it also moved to the RAAFv remarks rule? If so, send the current SQL.

## Verification

Panels were not run against a live database (none available on this PC). `tests/integration/test_events_t14.py` executes the panel SQL against synthetic rows when `VATPAC_TEST_DSN` points at a throwaway `_test` database; it has been compiled but not executed.
