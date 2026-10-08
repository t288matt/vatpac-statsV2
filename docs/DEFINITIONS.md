# Definitions

Last reviewed 08-10-2026. Each definition is taken from SPEC.md section 5 and checked against the SQL in `db/migrations/` and the collector code. "v1" means the previous system (t288matt/vatpac-stats); its behaviour is described only as far as ADR-001, ADR-006, ADR-007 and its `docker-compose.yml` document it. Where this page says "not documented" the v1 behaviour was not checked.

## 1. Flight (pilot session)

**v3.** A pilot session is identified by `(cid, callsign, logon_time)`, where `logon_time` is in whole seconds (truncated, not rounded; the feed's fraction is dropped on purpose, ADR-021). A new whole-second `logon_time` for the same CID and callsign is a new session. Departure, arrival, aircraft and route are attributes, never part of the key.

A session becomes one or more **flights**, computed into the table `flight_summary` (refreshed every 5 minutes, so flight numbers lag live data by up to 5 minutes) and exposed by the `flight` view:

- **Gap split (exact):** a gap of more than 120 minutes between consecutive stored samples starts a new flight.
- **Span split (approximate):** within a gap segment, flights are cut into fixed 8-hour buckets measured from the segment's first sample. This approximates "restart after 8 hours" without a recursive query, so a split falls on a bucket boundary rather than on the exact 8-hour mark.
- **Start** = first stored sample; **end** = last stored sample; **duration** = end minus start.
- Plan fields are updated in place to the latest non-null values; `plan_changed_at` records when departure or arrival last changed.

**Difference from v1.** v1 keyed sessions on departure and arrival as well, which produced duplicates when a plan was amended (ADR-006). v1 closed a flight after `FLIGHT_COMPLETION_HOURS` of 8 hours from first logon (v1 compose); v3 splits by gap and span in the view and closes nothing in the collector.

## 2. The flight gate

**v3.** A session counts as a "flight" only when departure and arrival are both non-empty after `UPPER` and trimming. The gate is applied in the `flight` view, not at ingest, so every in-region session and all its positions are stored.

Trimming uses `btrim` with an explicit character set (space, tab, carriage return, line feed). Plain `trim()` strips spaces only, so tabs in an airport code used to slip past the gate; this was a defect found by the integration tests and fixed (ADR-015).

`poll_log.pilots_with_plan` counts, per snapshot, the in-region pilots that pass the same test, for monitoring.

**Difference from v1.** The v1 flight-plan gate sat in the ingest path, so a pilot who had not yet filed a plan lost their early positions (ADR-007). In v3 nothing is lost; the gate only decides which sessions appear in flight charts.

## 3. Airborne minutes

**v3.** The sum, over consecutive sample pairs, of the time between them where groundspeed is at least 60 kt at the earlier sample. Each gap is capped at `AIRBORNE_GAP_CAP_SECONDS` (default 40 s, about 2.5 times the 15 s interval), and pairs more than 120 minutes apart are not consecutive and count as zero. Result in minutes, rounded to 2 decimals.

The cap means missed polls are not counted as flying time. It also means airborne minutes are a floor, not a measurement, if the poller falls behind.

For sessions whose raw positions have all been removed by retention, the `flight` view still returns the session (start = `first_seen`, end = `last_seen`) with `airborne_minutes` NULL and `sample_count` 0, so long-term flight counts survive.

**Difference from v1.** Not documented in v1 compose. v1 polled about every 92 seconds and saw roughly one snapshot in six (ADR-005), so any v1 time-in-air figure was built on much sparser data. v3 figures are not directly comparable with v1 figures.

## 4. Plausibility caps

**v3.** At ingest, groundspeed outside 0..700 kt and altitude outside -1500..65000 ft are stored as NULL (not clamped to the limit) and counted in `poll_log.rejected_values`. The same ranges are CHECK constraints on `pilot_position`. NULL values are ignored by the maxima (`max_gs_kt`, `max_alt_ft`) and by the airborne test.

Frequency handling is the controller-side equivalent: `121.700` becomes 121700000 Hz, and `199.998` MHz ("no frequency") becomes NULL.

**Difference from v1.** Not documented in v1 compose; v3 adds these caps as a data-quality rule. Highest-groundspeed charts therefore cannot show an implausible value.

## 5. Controller session

**v3.** Identified by `(cid, callsign, logon_time)` with `logon_time` in whole seconds (ADR-021). Start is `logon_time`; end is the last sighting (`last_seen`). A controller is stored only if their callsign is on the whitelist (exact, case-sensitive).

**Merging.** The view `controller_session_merged` merges reconnects: a session that starts within 5 minutes of the previous session's last sighting (same CID and callsign) joins the same group. Start = earliest `logon_time`, end = latest sighting.

**Closed** is derived, never written: `is_open` is true when the end is within 30 seconds of the newest stored snapshot.

**Difference from v1.** The 5-minute reconnect merge is kept (v1 `CONTROLLER_RECONNECTION_THRESHOLD_MINUTES: 5`). v1 considered a session complete after 15 minutes of inactivity (`CONTROLLER_COMPLETION_MINUTES`) and ran a background summariser to archive it; v3 has no summariser, derives open/closed at query time, and treats a controller as no longer online after about two missed snapshots. v1 also filtered by callsign pattern (`ATIS`, `_OBS`) and by frequency (122.800); v3 uses the whitelist only (ADR-007).

## 6. Online now

**v3.** Seen within the last 2 snapshots. Two forms exist in the database:

- `online_now` (core view): `last_seen` within 30 seconds of the newest stored snapshot, and only while that newest snapshot is itself under 5 minutes old (migration 24), so a dead collector shows nobody online rather than the last people seen. Read it together with `data_freshness`.
- `pilot_online_now` and `controller_online_now` (analytics views): within the last two stored snapshots **and** within the last 5 minutes of wall-clock time, the same guard.

The two forms differ only slightly: 30 seconds of the newest snapshot against the last two stored snapshots, which on a healthy 15-second feed are the same people.

Which one a panel uses is recorded in [CHART_DEFINITIONS.md](CHART_DEFINITIONS.md).

**Difference from v1.** Not documented in v1 compose. With a 15 s feed, "two snapshots" is about 30 seconds; v1's 92 s poll cadence implies a coarser notion.

## 7. Near-miss controller

**v3.** A callsign that is not whitelisted but whose prefix (the text before the first `_`) equals the prefix of any whitelisted callsign. `_OBS` and `_SUP` (supervisor) callsigns are never near-misses. For example, a hypothetical `XX_NEW_TWR` where other `XX_` callsigns are whitelisted would be flagged, because a new position at a known airport has probably been opened and the whitelist needs updating.

Handling: the callsign is not stored as a session. It is upserted into `controller_unlisted_sighting` (`first_seen`, `last_seen`, `polls_seen`), logged once at WARNING on first sighting, and the watchdog alerts on the first sighting. The fix is to add the callsign to `config/controller_callsigns_list.txt` and reload (see [runbook: update reference files](RUNBOOKS/update-reference-files.md)).

**Difference from v1.** New in v3 (SPEC section 4). v1 had no equivalent that is documented in its compose file.

## 8. In-region pilot (polygon filter)

**v3.** A pilot is stored from the first snapshot in which they are inside the polygon `config/australian_airspace_polygon.json` (GeoJSON, lon,lat order), tested with shapely `contains`: a point exactly on the edge is outside. A pilot with no position (latitude or longitude absent or null) passes; one whose latitude or longitude is present but unparsable is a malformed record and is skipped and counted. Pilots outside are never stored, so a flight is recorded only from the moment it enters the polygon and stops when it leaves; it can re-enter as the same session.

**Difference from v1.** The polygon filter itself is kept from v1 (`ENABLE_BOUNDARY_FILTER` in the v1 compose). The edge rule and the no-position rule follow SPEC section 4; whether v1 treated an edge point the same way was not checked.

## 9. Flags on the `flight` view

| Flag | Definition |
|---|---|
| `is_circuit` | Normalised departure equals normalised arrival. |
| `is_zzzz` | Departure or arrival is `ZZZZ`. |
| `touches_home_region` | Departure or arrival starts with a prefix in `HOME_ICAO_PREFIXES` (default `Y`). |

## 10. Verification status of these definitions

Checked against the code and SQL on 07-10-2026 and again on 08-10-2026, and exercised by the integration tests (166 exist; the suite is run against a real PostgreSQL 16.4 and in CI). The live filter counts were cross-checked against independent counts of the live feed on 07-10-2026 (15 pilots inside the polygon, 14 with a flight plan; no whitelisted controllers were online at the time) and on 08-10-2026 (6 of 6 samples matched exactly at two times of day; only 2 whitelisted controllers were online, so the controller filter is still lightly tested; see docs/TRACKER.md, T16 and T21). Not yet verified on real, long-running data: airborne minutes over full flights, the 8-hour span split, controller merging across real reconnects, and any chart that depends on weeks of history.
