# Definitions

Last reviewed 08-10-2026. Each definition is taken from SPEC.md section 5 and checked against the SQL in `db/migrations/` and the collector code. "v1" means the previous system (t288matt/vatpac-stats); its behaviour is described only as far as ADR-001, ADR-006, ADR-007 and its `docker-compose.yml` document it. Where this page says "not documented" the v1 behaviour was not checked.

## 1. Flight (pilot session)

**v3.** A pilot session is identified by `(cid, callsign, logon_time)`, where `logon_time` is in whole seconds (truncated, not rounded; the feed's fraction is dropped on purpose, ADR-021). A new whole-second `logon_time` for the same CID and callsign is a new session. Departure, arrival, aircraft and route are attributes, never part of the key.

**Since 13-10-2026 (issue 89: phase 3 live at 08:14:12 UTC, commit 38e3958; phases 4 and 5 live at 10:05:56 UTC, commit 7a71d30) the `flight` view is a thin view over the table `flight_summary`: one row per finished flight, as defined in section 1b.** The earlier model cut a connection into pieces by silence (more than 120 minutes) and by 8-hour span, merged reconnects at read time and flagged flights still connected; phase 5 removed it, and it survives only in dated records (CHANGELOG.md, DECISIONS.md ADR-016 and ADR-030). The rules in force are:

- **No split inside a connection (v1's rule, since phase 3):** a long silence or a connection of more than 8 hours is ONE flight. The earlier gap split (more than 120 minutes) and 8-hour span split belonged to the old pieces table and are gone.
- **Start** = first stored sample; **end** = last stored sample; **duration** = end minus start.
- **Reconnect join (v1's rule; ADR-030 supersedes ADR-023's merge):** sessions with the same CID, callsign, departure and arrival are one flight when a later session's logon time is 120 minutes or less after the latest last position of the earlier ones. A changed departure or arrival is a different flight.
- Plan fields are updated in place to the latest non-null values; `plan_changed_at` records when departure or arrival last changed.

**Difference from v1.** v1 keyed sessions on departure and arrival as well, which produced duplicates when a plan was amended (ADR-006); v3 joins reconnects with the same airports as v1 does (ADR-030, which replaced the merge rule of ADR-023), but keeps a changed plan as a separate flight without v1's duplicate rows. v1 closed a flight after `FLIGHT_COMPLETION_HOURS` of 8 hours from first logon (v1 compose); v3 cuts nothing inside a connection and writes a flight 8 hours after its last position (section 1b).

## 1b. A finished flight, and when it is written (issue 89; live since 13-10-2026)

**Status (13-10-2026).** All live on the VM: phase 1 (the table of finished flights, then under a temporary name; writer `write_finished_flights()`, deployed 12-10-2026, marker c70e72f at 17:58:21 UTC), phase 1b (the "quiet per flight" rule below, migration 20261012000200, marker 3d4b5b6 at 13-10-2026 08:01:09 UTC), phase 3 (the `flight` view and everything built on it read the table, migration 20261013000200, marker 38e3958 at 08:14:12 UTC), phase 4 (the raw-position drop gate, migration 20261013000210) and phase 5 (removal of the old pieces; the table is named `flight_summary`, migration 20261013000300), the last two with marker 7a71d30 at 10:05:56 UTC. Because a flight is written only once it has been quiet for 8 hours, the charts show a flight about 8 hours after its last position.

**1b.1 What a flight is.** One connection (a session), or several connections of the same pilot joined together by v1's rule: same CID, same callsign, same departure, same arrival, and the new connection **logged on** no more than 120 minutes after the latest earlier position of that flight. A reconnect that happens within two hours is therefore the same flight; a connection that logs on later than that, or with another callsign or another departure or arrival (an amended plan), is a different flight. A connection is never cut: a long session, or a long silence inside one, is still one flight. A session with no departure or no arrival makes no flight.

**1b.2 When a flight is written.** A flight is written into `flight_summary`, once, and never changed afterwards, as soon as **the flight itself has had no position for 8 hours** (`app_setting.flight_quiet_hours`). The 8 hours are counted on stored data: the clock is the time of the newest stored snapshot (never the wall clock, never later than now plus 5 minutes), so a stopped collector or a frozen feed cannot make a flight that is still flying look finished. Why 8 hours: any wait longer than 120 minutes guarantees that no later reconnect can still join the flight; 8 hours is the owner's choice and matches v1.

**1b.3 What "per flight" means, with a worked example.** A pilot flies YSSY to YMML from 08:00 to 10:00, disconnects, and flies YMML to YSSY from 17:00 to 19:00.
- At 12:00 nothing is written: the first flight has been quiet for only 2 hours.
- At 18:30 the first flight has been quiet for 8.5 hours, so it **is written**, even though the pilot is flying right now. The second flight is still in progress and is left alone (nothing is marked, nothing is written).
- At 03:00 the next day the second flight has been quiet for 8 hours and is written.
Under the earlier "quiet per pilot" rule nothing would have appeared until 03:00, and a pilot who flew two days ago and is online again today would have kept the two-day-old flight hidden. The first live run showed this: 141 pilots who were online again within 8 hours were holding back all their older flights.
Two cases the rule decides on purpose:
- *Reconnect that continues the flight.* The first connection ends at 10:00 and the pilot logs on again at 11:30 with the same callsign and airports (within 120 minutes). That is one flight, and it is quiet only 8 hours after the **second** connection ends. Nothing is written for the first connection on its own.
- *Reconnect that does not continue it.* The pilot logs on again at 12:01 (121 minutes later) or with another arrival. That is a different flight, so the first flight is written 8 hours after 10:00 and is not held up.
A session that forms no flight (no flight plan, or no positions) is marked as processed once that session is itself quiet for 8 hours; it never holds anything up.

**1b.4 History: the per-pilot version (superseded).** Phase 1 first went live on 12-10-2026 with a quiet test on the whole pilot: nothing of a pilot was written until the pilot's newest session was 8 hours old. Same flights, same numbers, only later. Phase 1b replaced it on 13-10-2026 08:01:09 UTC; the backfill (`ops/backfill_flights.sh`, called `ops/backfill_flight_record.sh` until phase 5) wrote the 9 held-back flights that remained.

**1b.5 How it can be checked.** `docs/CONFORMANCE_89_PHASE1.md` lists each requirement against the test that proves it; the tests for this rule are in `tests/integration/test_flight_quiet_per_flight.py`. Two watchdog checks report flights that are overdue (`docs/RUNBOOKS/integrity-checks.md`).

## 2. The flight gate

**v3.** A session counts as a "flight" only when departure and arrival are both non-empty after `UPPER` and trimming. The gate is applied in the `flight` view, not at ingest, so every in-region session and all its positions are stored.

Trimming uses `btrim` with an explicit character set (space, tab, carriage return, line feed). Plain `trim()` strips spaces only, so tabs in an airport code used to slip past the gate; this was a defect found by the integration tests and fixed (ADR-015).

`poll_log.pilots_with_plan` counts, per snapshot, the in-region pilots that pass the same test, for monitoring.

**Difference from v1.** The v1 flight-plan gate sat in the ingest path, so a pilot who had not yet filed a plan lost their early positions (ADR-007). In v3 nothing is lost; the gate only decides which sessions appear in flight charts.

## 3. Airborne minutes

**v3.** The sum, over consecutive sample pairs, of the time between them where groundspeed is at least 60 kt at the earlier sample. Each gap is capped at `AIRBORNE_GAP_CAP_SECONDS` (derived as 2.5 times `POLL_INTERVAL_SECONDS`: 75 s at the default 30 s polling, ADR-024), and pairs more than 120 minutes apart are not consecutive and count as zero. Result in minutes, rounded to 2 decimals.

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

**Closed** is derived, never written: `is_open` is true when the end is within 2 x the poll interval (60 s at the default 30 s polling, ADR-024) of the newest stored snapshot.

**Difference from v1.** The 5-minute reconnect merge is kept (v1 `CONTROLLER_RECONNECTION_THRESHOLD_MINUTES: 5`). v1 considered a session complete after 15 minutes of inactivity (`CONTROLLER_COMPLETION_MINUTES`) and ran a background summariser to archive it; v3 has no summariser, derives open/closed at query time, and treats a controller as no longer online after about two missed snapshots. v1 also filtered by callsign pattern (`ATIS`, `_OBS`) and by frequency (122.800); v3 uses the whitelist only (ADR-007).

## 6. Online now

**v3.** Seen within the last 2 snapshots. Two forms exist in the database:

- `online_now` (core view): `last_seen` within 2 x the poll interval (setting `poll_interval_seconds`, 60 s at the default of 30 s; ADR-024) of the newest stored snapshot, and only while that newest snapshot is itself under 5 minutes old (migration 24), so a dead collector shows nobody online rather than the last people seen. Read it together with `data_freshness`.
- `pilot_online_now` and `controller_online_now` (analytics views): within the last two stored snapshots **and** within the last 5 minutes of wall-clock time, the same guard.

The two forms differ only slightly: 2 x the poll interval from the newest snapshot against the last two stored snapshots, which on a healthy feed polled every 30 s are the same people.

Which one a panel uses is recorded in [CHART_DEFINITIONS.md](CHART_DEFINITIONS.md).

**Difference from v1.** Not documented in v1 compose. With polling every 30 s (15 s before 09-10-2026), "two snapshots" is about 60 seconds; v1's 92 s poll cadence implies a coarser notion.

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
| `is_in_progress` | (Removed from `flight` by migration 20261013000200, issue 89 phase 3, live since 13-10-2026 08:14:12 UTC; the old merging view that still had it was dropped by phase 5. Nothing is shown in progress now, because a flight is written only 8 hours after its last position. The description below is the old meaning, kept as history.) A session of the (merged) flight is in `online_now` (issue #38). Counting panels left such flights out (ADR-025). When the collector is stale nothing is in progress, so nothing is hidden. |

## 10. Verification status of these definitions

Checked against the code and SQL on 07-10-2026 and again on 08-10-2026, and exercised by the integration tests (the suite is run against a real PostgreSQL 16 and in CI, which uses 16.4 until updated). The live filter counts were cross-checked against independent counts of the live feed on 07-10-2026 (15 pilots inside the polygon, 14 with a flight plan; no whitelisted controllers were online at the time) and on 08-10-2026 (6 of 6 samples matched exactly at two times of day; only 2 whitelisted controllers were online, so the controller filter is still lightly tested; see docs/TRACKER.md, T16 and T21). Not yet verified on real, long-running data: airborne minutes over full flights, the 8-hour span split, controller merging across real reconnects, and any chart that depends on weeks of history.
