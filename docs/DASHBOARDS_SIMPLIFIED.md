# Dashboards: what was simplified, and why

Decision (SPEC sections 1, 10 and 13): **frequency matching and sector matching are not required in v3.** The v3 collector stores pilots inside the Australian boundary polygon and whitelisted controllers, with their sessions and positions. It does not work out which pilot was talking to which controller, nor which sector an aircraft was in. Every v1 chart that depended on either was dropped rather than approximated, because an approximation would show numbers that look authoritative and are not.

Full per-chart detail is in `docs/CHART_DEFINITIONS.md`. In total, 21 of the 57 non-archived v1 cards are dropped; the other 36 are rebuilt (17 kept, 19 changed).

## Dropped: needs frequency matching (ATC interaction)

| v1 card | Chart |
|---|---|
| 41 | Percentage of flights with at least one ATC interaction |
| 43 | Percentage of time pilots spent on an ATC frequency |
| 98 | Percentage of airborne time pilots spent on a frequency with ATC |
| 133 | Pilots on the same frequency as a controller (Monday 0800-1200Z) |

Also removed as a consequence: the ATC-contact percentage column on the popular VFR routes chart (card 59), and the "aircraft handled" component of the top 50 service providers score (card 108). The score is now hours online only. Aircraft-handled metrics in general are dropped, because "handled" meant "was on this controller's frequency".

Controller frequencies are still stored (as integer Hz) and the online-controllers list shows them in MHz; they are just not matched to pilots.

## Dropped: needs sector matching

| v1 card | Chart |
|---|---|
| 51 | Flights currently in each sector |
| 88 | Sector where flights spend the most time |
| 102 | Average altitude of aircraft moving from SYA to WOL sectors |
| 104 | Busiest time in the TSN sector |
| 105 | Most popular sector (90 days) |
| 106 | Busiest time in the IND sector |
| 107 | Busiest time in the GUN sector |

These need sector boundaries, occupancy tracking and transition detection, none of which is in the scope. If sectors are wanted later they belong in a separate project that reads `pilot_position`; raw positions are kept for 90 days, so nothing is lost in principle, but the charts would start from the day the work is done.

## Dropped: Milk Run Monday fastest and slowest

| v1 card | Chart |
|---|---|
| 62 | Fastest Milk Run Monday time (YSSY-YMML) |
| 86 | Fastest Milk Run Monday time (YMML-YSSY) |
| 132 | Slowest YMML-YSSY Milk Run Monday |

Decision recorded in SPEC section 10. The event attendance chart (card 131) and the local-pilot share (card 134) remain on the Monday event dashboard.

## Dropped: v1 pipeline monitors

| v1 card | Chart |
|---|---|
| 121 | Oldest open sector age |
| 122 | Unique flights past overdue processing |
| 123 | Flow: flight first associated with a controller record |
| 124 | Flow: controller first associated with a flight record |
| 125 | Data integrity: flight to controller mismatches |
| 126 | Data integrity: controller to flight mismatches |
| 139 | How healthy is enrichment (last 24 hours) |

These watched v1's enrichment and matching jobs, which do not exist in v3. They are replaced by the Monitoring dashboard (`grafana/dashboards/monitoring/collector-health.json`), which reads `poll_log`, `network_snapshot` and the freshness views: snapshot age, last successful poll, missed snapshots, errors, rejected values and near-miss controllers. The two v1 freshness gauges (cards 118 and 120) are kept in spirit as the snapshot-age and controller-sighting-age panels.

## Simplifications that are not drops

- Pilot "online" and "active" counts come from `network_snapshot` and the `flight` view, not from enrichment tables.
- Military aircraft are a boolean in the `aircraft_type` lookup instead of a long hard-coded list in each query (cards 65, 66, 78).
- Airline, aircraft category and pilot home region are lookups (`airline`, `aircraft_type`, `region_map`), seeded by migration `20261007000011_seed_analytics_lookups.sql`. Unlisted codes show as Other or Unknown instead of being silently dropped.
- Home-region tests use the `home_icao_prefixes` setting (default `Y`) rather than a hard-coded `Y`.
- No backfill: every chart starts empty at go-live, and each dashboard says "Data since" the first stored snapshot.

## Still unverified

The controller retention, lapsed and reactivated panels (`grafana/dashboards/atc/atc-retention.json`) and a few screenshot-only charts are built from screenshots, not v1 SQL, and are marked UNVERIFIED until the owner supplies the Metabase export.
