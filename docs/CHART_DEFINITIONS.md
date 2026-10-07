# Chart definitions

Every in-scope v1 chart and what replaced it, for the Grafana dashboards in `grafana/dashboards/`. Dates are DD-MM-YYYY and all times UTC.

Status key: **KEPT** same question and definition (SQL rewritten against the v3 views); **CHANGED** same question, definition altered (see the difference column); **UNVERIFIED** the definition is the analytics agent's best reading and awaits the owner's Metabase export; **DROPPED** not rebuilt (reason given).

Panels read views and lookups only: `flight`, `controller_session_merged`, `online_now` and `data_freshness` from migration `20261007000004_views.sql`, the rest from migration `20261007000010_analytics_views.sql` (the `flight` view is now a thin view over the incrementally maintained `flight_summary` table, migration `20261007000020`). There is no backfill, so every chart starts empty at go-live.

Verification on 07-10-2026: every panel query was run against the live local stack through Grafana's own API (`ops/check_panels.py`): 61 queries, 0 errors, 45 returned rows and 16 were empty because the data was only minutes old (no controllers online, no history for weekly or 30-day charts). They are also parsed with sqlglot and every column resolved against the view definitions (`grafana/validate_dashboards.py`, `grafana/check_columns.py`). "Runs without error" is not the same as "answers the question correctly": the Test column says what to check once enough data exists.

## Summary of the 57 non-archived v1 cards

| Status | Count |
|---|---|
| KEPT | 17 |
| CHANGED | 19 |
| UNVERIFIED | 0 |
| DROPPED | 21 |
| Total | 57 |

Charts seen only in screenshots (no v1 SQL recovered) are listed separately below: 11 panels, all UNVERIFIED, plus panels that are new in v3.

## v1 cards rebuilt

| v1 card | Definition | Source view | Panel (file, title) | Difference from v1 | Status | Test |
|---|---|---|---|---|---|---|
| 40 What server do Australian pilots connect to? | Distinct home-region pilots per network server, last 24 hours. | flight_ext | demographics/pilot-origins.json: Servers used by home-region pilots (last 24 hours) | Home region from the ICAO code in the pilot's name and the home prefix setting (v1 hard-coded the fourth-last character 'Y'). | KEPT | Server counts sum to at least the distinct home-region pilots seen in 24 h. |
| 45 What are the 10 most popular international routes (last 30 days)? | Ten most flown departure-arrival pairs, last 30 days, where at least one end is outside the home region. | flight_ext | pilots/pilots-traffic.json: Most popular international routes (last 30 days) | Excludes ZZZZ ends and same-airport circuits; home prefixes from settings. | KEPT | Compare with a manual count of flight rows for the top pair. |
| 46 Which pilots had the most diverse routes in the last fortnight? | Pilots with the most distinct routes in 14 days, flights over 60 minutes only. | flight_ext | pilots/pilots-traffic.json: Pilots with the most diverse routes (last 14 days) | None of substance. | KEPT | Top pilot's unique routes <= total flights. |
| 55 Which airport had the most movements (last 30 days)? | Departures plus arrivals at home-region airports, top 10, last 30 days. | movements | pilots/pilots-traffic.json: Busiest home-region airports (last 30 days) | Counted from the movements view: departures at flight start, arrivals at flight end (v1 used completion time for both). | KEPT | Total equals departures + arrivals. |
| 57 What is the most popular callsign? | Callsign and route flown by at least 2 distinct pilots, top 10. | flight_ext | pilots/pilots-traffic.json: Most popular callsigns (last 30 days) | None of substance. | KEPT | unique_pilots >= 2 on every row. |
| 58 Which airline is most popular? (last 30 days) | Top 10 airlines by flights, last 30 days. | flight_ext | pilots/pilots-traffic.json: Most popular airlines (last 30 days) | Airline = three letters followed by a digit, named through the airline lookup (v1 took the first three characters of every callsign, so private registrations appeared as airlines). | CHANGED | No row has a registration such as VHABC as its airline. |
| 59 What were the most popular VFR routes? (last 30 days) | Most flown VFR routes, last 30 days. | flight_ext | pilots/pilots-traffic.json: Most popular VFR routes (last 30 days) | Counts distinct CIDs, minimum 3 pilots; drops the ATC-contact percentage (needs frequency matching). | CHANGED | All rows are flight rules V with departure <> arrival. |
| 61 Which flights had the highest groundspeed (last 7 days)? | Peak groundspeed per callsign, last 7 days, top 20, excluding military, supersonic, business jets, turboprops and regional jets. | flight_ext | pilots/pilots-traffic.json: Highest groundspeeds (last 7 days) | Uses the session maximum groundspeed (capped at 700 kt at ingest) and the aircraft_type lookup instead of a long type list; altitude is the highest reached, not the altitude at peak speed. | CHANGED | No value above 700 kt; no excluded category appears. |
| 63 How many CTR/FSS controllers were active in the last 30 days? | Per week, distinct controllers who started a CTR or FSS session over 60 minutes in the 30 days to the end of that week. | controller_session_ranked | atc/atc-overview.json: CTR and FSS controllers active (rolling 30 days, weekly) | Zero weeks are drawn as zeros instead of gaps; starts at go-live (no backfill). | KEPT | Weekly value <= the all-position chart (card 137) for the same week. |
| 65 What were the most popular military aircraft last week (unique pilots)? | Distinct pilots per military aircraft type, last 7 days. | flight_ext | military/military.json: Most popular military aircraft (unique pilots, last 7 days) | Military types come from aircraft_type.is_military, replacing a 400-entry IN list with wildcards. | CHANGED | Types shown all have is_military = true. |
| 66 Hours flown by military aircraft per week | Hours flown by military aircraft per week. | flight_ext | military/military.json: Hours flown by military aircraft per week (last 14 weeks) | Airborne hours (v1 summed time online); last 14 weeks. | CHANGED | Hours <= pilots x week length. |
| 68 How many pilots online? | Pilots online now. | network_history | pilots/pilots-now.json: Pilots online now (in region) | Taken from network_snapshot (pilots in region) rather than a count of recent flight rows. | KEPT | Equals online_now rows of kind pilot (within one snapshot). |
| 69 How many controllers online? | Controllers online now. | network_history | atc/atc-overview.json: Controllers online now | Taken from network_snapshot (controllers in region). | KEPT | Equals online_now rows of kind controller (within one snapshot). |
| 76 How many flight were there for each joining cohort (last 30 days)? | Flights and distinct pilots per CID block of 100,000 (joining cohort), last 30 days. | cohorts | demographics/pilot-origins.json: Flights by joining cohort (CID range, last 30 days) | CIDs below 100,000 excluded as in v1; uses the cohorts view. | KEPT | Sum of flights equals flights in the period with CID >= 100000. |
| 78 What were the most popular military routes last week? | Routes flown by military aircraft types, last 7 days, with share of military flights and pilots. | flight_ext | military/military.json: Most popular military routes (last 7 days) | Military types from the lookup (see card 65). | KEPT | Every route's aircraft type has is_military = true. |
| 90 What are the most popular aircraft? (no. of flights) | Flights per aircraft category, last 30 days. | flight_ext | pilots/pilots-traffic.json: Most popular aircraft by flights (last 30 days) | Categories from the aircraft_type lookup; unlisted codes show as 'Other / uncategorised' (v1 used a LIKE ladder that mis-classified types). | CHANGED | Slice total equals total flights in the period. |
| 93 How many active pilots and controllers do we have (LOG SCALE)? | Distinct pilots (30 minutes or more airborne) and distinct controllers by rating group (30 minutes or more), last 90 days, log scale. | controller_session_ranked, flight_ext | demographics/pilot-origins.json: Active pilots and controllers (last 90 days, log scale) | Pilots use airborne minutes (v1 used enroute minutes). | CHANGED | Pilot bar <= distinct CIDs in the flight view. |
| 94 When are people online on weekdays (last 90 days)? | Average pilots and controllers online by UTC hour, weekdays, last 90 days. | network_history | pilots/pilots-now.json: When are pilots and controllers online: weekdays (UTC, last 90 days) | Average concurrent users from snapshots (v1 counted sessions touching each hour and treated Sun/Mon/Sat as weekdays); Monday to Friday here. | CHANGED | Hourly averages never exceed the peak in Pilots online over time. |
| 95 What time are Pilots/ATC online weekend? | Average pilots and controllers online by UTC hour, weekends, last 90 days. | network_history | pilots/pilots-now.json: When are pilots and controllers online: weekends (UTC, last 90 days) | Average concurrent users from snapshots (v1 counted sessions overlapping each hour). | CHANGED | As card 94. |
| 97 What are the 10 most popular domestic routes (last 30 days)? | Ten most flown departure-arrival pairs where both ends are in the home region, last 30 days. | flight_ext | pilots/pilots-traffic.json: Most popular domestic routes (last 30 days) | Excludes ZZZZ and circuits; home prefixes from settings. | KEPT | Both ends start with a home prefix. |
| 99 Where do pilots currently in Australian airspace come from? | Home region of pilots online now. | pilot_online_now | demographics/pilot-origins.json: Where pilots currently online come from | Region from region_map; unmapped or missing show as 'Unknown'; no top-6 cut; online = last two snapshots (v1 used 5 minutes). | CHANGED | Counts sum to pilots online now. |
| 100 Who flies most Australian airspace (last 30 days)? | Flights per pilot home region, last 30 days. | flight_ext | demographics/pilot-origins.json: Who flies most in Australian airspace: flights by home region (last 30 days) | Counts flights (sessions) rather than distinct callsign-date pairs; includes 'Unknown'. | CHANGED | Slices sum to total flights in the period. |
| 101 Who spends most time in Australian airspace (last 30 days)? | Airborne hours per pilot home region, last 30 days. | flight_ext | demographics/pilot-origins.json: Who spends most time in Australian airspace: airborne hours by home region (last 30 days) | Airborne hours (v1 summed time online); includes 'Unknown'. | CHANGED | Slices sum to total airborne hours. |
| 103 What percentage of flights have a SELCAL code in their remarks ? | Share of flights with SEL/ in the remarks, out of flights that have remarks. | flight_ext | pilots/pilots-traffic.json: Flights with SELCAL in the remarks | None (same denominator as v1). | KEPT | Between 0 and 100. |
| 108 Top 50 service providers (last 30 days) | Top 50 controllers by hours online, last 30 days. | controller_session_ranked | atc/atc-overview.json: Top 50 service providers (last 30 days) | Score is hours online only; v1 added aircraft handled (needs frequency matching), so that column is dropped. | CHANGED | Rows sorted by hours descending; at most 50 rows. |
| 115 How many controllers logged on (last 30 days)? | Distinct controllers by rating group with more than 60 minutes online in total, last 30 days. | controller_session_ranked | atc/atc-overview.json: Controllers logged on (last 30 days) | Uses merged sessions; observer and C2 left out as in v1. | KEPT | Bars sum to the number of controllers over 60 minutes. |
| 116 How many controllers logged on last 3 months? | As card 115 over 3 months. | controller_session_ranked | atc/atc-overview.json: Controllers logged on (last 3 months) | As card 115. | KEPT | Each bar >= the 30-day bar. |
| 118 Flights table freshness | Age of the newest data (flights table freshness). | monitor_freshness | monitoring/collector-health.json: Newest snapshot age | Replaced by the age of the newest network_snapshot, in seconds, with watchdog thresholds. | CHANGED | Keeps ageing when the collector is stopped. |
| 120 Controller table freshness | Age of the newest controller record (controller table freshness). | monitor_freshness | monitoring/collector-health.json: Newest controller sighting age | Replaced by the age of the newest controller sighting, in seconds. | CHANGED | Large during quiet hours with no controllers (expected). |
| 128 Which date had the most flights? | Five UTC dates with the most flights, last 6 months. | flight_ext | pilots/pilots-traffic.json: Busiest days by flights (last 6 months) | By flight start from the flight view; no backfill so the window starts at go-live. | KEPT | Top date's count equals a manual count for that day. |
| 129 What days had the most people online? | Five UTC dates with the most distinct pilots, last 6 months. | flight_ext | pilots/pilots-traffic.json: Busiest days by pilots online (last 6 months) | As card 128. | KEPT | Count <= flights on that date. |
| 131 Monday event attendance | Distinct pilot CIDs and controller CIDs present in each Monday event window. | monday_attendance | events/monday-events.json: Monday event attendance (0700-1200Z) | Window 0700-1200Z (v1 0800-1200Z) per the specification; sessions overlapping the window; CID lists dropped. | CHANGED | A Monday with known attendees returns at least that many CIDs. |
| 134 What percentage of MRM traffic were locals? | Weekly share of YMML-YSSY (either direction) airborne minutes flown by local pilots on Mondays. | flight_ext | events/monday-events.json: Share of YMML-YSSY airborne time flown by local pilots (Mondays 0700-1200Z) | Airborne minutes (v1 time online); home prefixes from settings; 0700 start. | CHANGED | Value between 0 and 100. |
| 135 What are the most popular aircraft? (no. of pilots) | Distinct pilots per aircraft category, last 30 days. | flight_ext | pilots/pilots-traffic.json: Most popular aircraft by pilots (last 30 days) | Same category change as card 90. | CHANGED | Pilot count <= flight count for the category. |
| 137 How many controllers were active in the last 30 days? | Per week, distinct controllers (any position) who started a session over 60 minutes in the trailing 30 days. | controller_session_ranked | atc/atc-overview.json: Controllers active (rolling 30 days, weekly) | Zero weeks drawn; starts at go-live. | KEPT | Weekly value >= the CTR/FSS chart (card 63). |
| 138 departure boards | Departure board for an airport (default YSSY): callsign, destination, aircraft, status. | pilot_latest | pilots/pilots-now.json: Departure board | Status from groundspeed (60 kt or more = airborne); airport chosen by a variable; no invented gate or ON TIME columns. | CHANGED | All rows have the chosen airport as departure. |

## v1 cards dropped

| v1 card | What it showed | Reason | Status |
|---|---|---|---|
| 41 What percentage of flights had at least one ATC interaction? | share of flights with an ATC interaction | needs frequency matching (flight to controller frequency overlap), not part of v3 | DROPPED |
| 43 % of time pilots spent on an ATC frequency (with ATC) | percentage of time pilots spent on an ATC frequency | needs frequency matching | DROPPED |
| 51 How many flights are currently in each sector? | flights currently in each sector | needs sector tracking and occupancy, not part of v3 | DROPPED |
| 62 What was the fastest Milk Run Monday time (YSSY-YMML)? | fastest Milk Run Monday YSSY-YMML | fastest/slowest Milk Run Monday dropped by decision | DROPPED |
| 86 What was the fastest Milk Run Monday time (YMML-YSSY)? | fastest Milk Run Monday YMML-YSSY | fastest/slowest Milk Run Monday dropped by decision | DROPPED |
| 88 In which sector do flights spend  the most time (last 30 days)? | time flights spend in each sector | needs sector tracking | DROPPED |
| 98 % of time airborne pilots spent on a frequency with ATC | percentage of airborne time on a frequency with ATC | needs frequency matching | DROPPED |
| 102 What the avg altitude of acft transitioning from SYA to WOL sectors? | average altitude of aircraft moving from SYA to WOL sectors | needs sector transition tracking | DROPPED |
| 104 What is the busiest time in the TSN sector? | busiest time in the TSN sector | needs sector tracking | DROPPED |
| 105 Which is the most 'popular' sector? (last 90 days?) | most popular sector | needs sector tracking | DROPPED |
| 106 What is the busiest time in the IND sector? | busiest time in the IND sector | needs sector tracking | DROPPED |
| 107 What is the busiest time in the GUN sector? | busiest time in the GUN sector | needs sector tracking | DROPPED |
| 121 Oldest open sector age | oldest open sector age | v1 pipeline monitor (sector processing); v3 has no sector or enrichment stage | DROPPED |
| 122 Numer of unique flights past overdue processing | flights past overdue processing | v1 pipeline monitor (enrichment backlog); no enrichment in v3 | DROPPED |
| 123 Flow - when a flight is first associated with a controller record | flow: flight first associated with a controller record | v1 pipeline monitor of the flight-controller matching job; no matching in v3 | DROPPED |
| 124 Flow - when a controller is first associated with a flight record | flow: controller first associated with a flight record | v1 pipeline monitor of the matching job; no matching in v3 | DROPPED |
| 125 Is there data integrity (Flight→controller mismatches)? | data integrity, flight to controller mismatches | v1 pipeline monitor of matching; nothing to mismatch in v3 | DROPPED |
| 126 Is there data integrity (Controller → Flight mismatches)? | data integrity, controller to flight mismatches | v1 pipeline monitor of matching; nothing to mismatch in v3 | DROPPED |
| 132 Slowest YMML-YSSY Milkrun Monday | slowest Milk Run Monday YMML-YSSY | fastest/slowest Milk Run Monday dropped by decision | DROPPED |
| 133 How many pilots were on the same freq as a controller? (0800-1200z monday) | pilots on the same frequency as a controller (Monday window) | needs frequency matching | DROPPED |
| 139 How healthy is enrichment last 24 hours? | enrichment health, last 24 hours | v1 enrichment monitor; replaced by the Monitoring dashboard (poll_log, freshness) | DROPPED |

Cards 118 and 120 (freshness gauges) are listed above as CHANGED because the Monitoring dashboard replaces them. Aircraft-handled metrics (a column of card 108 and others) go with frequency matching; see `docs/DASHBOARDS_SIMPLIFIED.md`.

## Screenshot-only charts (no v1 SQL recovered)

| Chart | Definition (analytics agent's reading) | Source view | Panel (file, title) | Status | Test |
|---|---|---|---|---|---|
| Pilots online over time | Pilots in region and network-wide per snapshot, averaged into buckets that follow the dashboard range. | network_history | pilots/pilots-now.json: Pilots online over time | UNVERIFIED | Last bucket matches Pilots online now. |
| Controllers online over time | Whitelisted controllers per snapshot, averaged into buckets. | network_history | atc/atc-overview.json: Controllers online over time | UNVERIFIED | Last bucket matches Controllers online now. |
| Who's online? (archived v1 card 67) | Controllers seen in the last two snapshots, with rating, server and frequency in MHz. | controller_online_now | atc/atc-overview.json: Controllers online now (list) | UNVERIFIED | Row count equals Controllers online now. |
| Which airport had the least movements | The 20 home-region airports with the fewest movements; every airport with at least one movement appears. | movements | pilots/pilots-traffic.json: Quietest home-region airports (last 30 days) | UNVERIFIED | Minimum movements >= 1. |
| Which controllers have the shortest avg session duration? | The 15 controllers with the lowest average session length, minimum 3 sessions (assumed). | controller_session_ranked | atc/atc-overview.json: Controllers with the shortest average session (last 30 days) | UNVERIFIED | Every row has at least 3 sessions. |
| Active controllers (stat, showed 178) | Controllers whose latest session started in the last 90 days (assumed). | controller_activity_summary | atc/atc-retention.json: Active controllers | UNVERIFIED | Awaiting Metabase export. |
| Lapsed 30 days (stat, showed 28) | Active in the last 90 days but latest session older than 30 days (assumed). | controller_activity_summary | atc/atc-retention.json: Lapsed 30 days | UNVERIFIED | Awaiting Metabase export. |
| Controller retention rate (gauge, showed 84%) | (active - lapsed) / active, which reproduces the 178 / 28 / 84% screenshot figures. | controller_activity_summary | atc/atc-retention.json: Controller retention rate | UNVERIFIED | Awaiting Metabase export. |
| Reactivated in the last 30 days after 60+ days lapsed (showed 74) | Controllers with a session in the last 30 days whose previous session ended over 60 days earlier (assumed). | controller_activity_summary | atc/atc-retention.json: Reactivated in the last 30 days after 60+ days lapsed | UNVERIFIED | Awaiting Metabase export; not meaningful until 60 days of data exist. |
| Who might need help reactivating? | Controllers with sessions in the last 30 days but under 1 hour in total and none over 30 minutes (assumed). | controller_activity_summary | atc/atc-retention.json: Who might need help reactivating? | UNVERIFIED | Awaiting Metabase export. |
| Who reactivated after being away for more than 60 days? | Table behind the reactivated count. | controller_activity_summary | atc/atc-retention.json: Who reactivated after being away for more than 60 days? | UNVERIFIED | Awaiting Metabase export. |

The controller retention, lapsed and reactivated panels stay UNVERIFIED pending the owner's Metabase export. Because there is no backfill, 60-day lapse measures are not meaningful until 60 days after go-live.

## New panels (no v1 equivalent)

| Panel (file, title) | Purpose | Source view |
|---|---|---|
| pilots/pilots-now.json: Pilots online now (network-wide) | Network-wide pilots online, context for the in-region figure. | network_history |
| monitoring/collector-health.json: Last successful poll age | Last successful poll age. | monitor_freshness |
| monitoring/collector-health.json: Newest pilot sighting age | Newest pilot sighting age. | monitor_freshness |
| monitoring/collector-health.json: Snapshots missed (last 24 hours) | Missed snapshots, acceptance target under 1%. | data_since, network_history |
| monitoring/collector-health.json: Unchanged polls (last 24 hours) | Polls skipped because the feed was unchanged. | poll_history |
| monitoring/collector-health.json: Poll errors (last 24 hours) | Poll errors. | poll_history |
| monitoring/collector-health.json: Rejected values (last 24 hours) | Values rejected by the plausibility caps. | poll_history |
| monitoring/collector-health.json: Near-miss controllers | Near-miss controllers (SPEC section 4). | near_miss_controllers |
| monitoring/collector-health.json: Near-miss controller callsigns | Near-miss callsigns. | near_miss_controllers |
| monitoring/collector-health.json: Polls by outcome | Polls by outcome. | poll_history |
| monitoring/collector-health.json: Rows written (pilot and controller records kept per snapshot) | Rows written per snapshot. | poll_history |
| monitoring/collector-health.json: Poll duration | Poll duration. | poll_history |
| monitoring/collector-health.json: Recent poll errors | Recent poll errors. | poll_history |
| monitoring/collector-health.json: Data freshness (data engineer view) | The data_freshness view as supplied. | data_freshness |
