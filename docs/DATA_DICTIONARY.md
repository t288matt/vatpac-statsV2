# Data dictionary

Checked against the migrations up to 20261012000210 on 12-10-2026 (the live database was last checked at 20261009000170; 20261011000180, 20261012000190 and 20261012000210 are not deployed yet; 20261012000190 is phase 1 of issue 89, branch p1-flight-record, and 20261012000210 is phase 4, branch p4-drop-gate, NOT YET DEPLOYED). Source: `db/migrations/*.sql` (26 migrations, `20261007000001` to `20261012000210`; applied by the `migrate` service with dbmate; the first 23 are recorded in `schema_migrations` on the VM, in file-name order) and `db/roles.sql` (plus the `watchdog_ro` block of `ops/migrate/migrate.sh`). The database image is PostgreSQL 16.15 (CI tests on 16.4 until updated). All timestamps are `timestamptz` (UTC). Ids are `bigint` unless stated. The meaning of the derived measures is in [DEFINITIONS.md](DEFINITIONS.md); the decisions behind them are in [DECISIONS.md](DECISIONS.md) (ADR-018, 021, 023, 024, 025).

*Describes main; behaviour added by changes not yet deployed is listed in CHANGELOG.md.*

Objects at the time of checking (live counts as of 11-10-2026, plus the additions of migrations 20261012000190 and 20261012000210): 19 base tables (18 ordinary plus the partitioned `pilot_position`, which has 11 daily partitions on the live database), 23 views, 10 functions, 4 declared foreign keys, no materialised views, no triggers.

## 0. Orientation

**What is stored.** The collector polls the VATSIM data feed every 30 seconds (the feed itself updates every 15 seconds; every second update is read, ADR-024). It keeps:

- pilots whose position is inside the home-region polygon (`pilot_session` plus a position sample per poll in `pilot_position`),
- controllers whose callsign is on the whitelist (`controller_session`), plus near-miss callsigns (`controller_unlisted_sighting`),
- one row per processed snapshot (`network_snapshot`) and one per poll attempt (`poll_log`).

**How a session is identified.** A pilot or controller session is the triple `(cid, callsign, logon_time)`. `logon_time` is the connection time from the feed truncated to **whole seconds** (never rounded), enforced by CHECK constraints, because the feed sometimes sends the fraction and sometimes does not (ADR-021). A reconnect is a new session; the `flight` view (pilots) and `controller_session_merged` (controllers) join reconnects back together for counting.

**The layers.**

| Layer | What | Who uses it |
|---|---|---|
| Base tables (section 2) | Written only by the collector (sessions, positions, snapshots, logs), by `refresh_flight_summary` (`flight_summary`) and by `write_finished_flights` (`flight_record`, and `pilot_session.flown_upto`). | `collector` (writes), `backup_ro` (dump), `watchdog_ro` (a few tables). |
| Lookups (section 3) | Small reference tables: ratings, facilities, airlines, aircraft types, regions. Seeded by migrations. | Views, and `grafana_ro` directly. |
| Functions (section 5) | Partition maintenance, the flight-summary refresh, and three SQL helpers used by the views. | Collector (EXECUTE on four of them), views. |
| Views (section 4) | The only thing the dashboards read. All derived rules (flight merging, online windows, normalisation) live here. | Grafana, as `grafana_ro`, which sees views and the six lookups only. |
| Roles (section 6) | `vatpac` (owner), `collector`, `grafana_ro`, `backup_ro`, `watchdog_ro`. | |

Dashboards read these 18 views and no others (checked by `python grafana/validate_dashboards.py`, which refuses base tables): `cohorts`, `controller_online_now`, `controller_session_ranked`, `controller_summaries`, `data_freshness`, `data_since`, `flight`, `flight_ext`, `monday_attendance`, `monitor_freshness`, `movements`, `near_miss_controllers`, `network_history`, `pilot_latest`, `pilot_online_now`, `pilot_sample`, `poll_cadence`, `poll_history`; and the lookups `airline` and `aircraft_type`. Views that no panel reads directly but others build on or that remain for ad-hoc use: `flight_part`, `controller_session_merged`, `online_now`, `home_prefix`, `controller_activity_daily`.

## 1. Relationships

### 1.1 Entity diagram

```
                       pilot_session  (cid, callsign, logon_time) unique
                      /      |
        ON DELETE    /       | FK, no cascade
         CASCADE    /        |
   flight_summary  /   pilot_position  (partitioned by day on observed_at)
   (session_id,           (session_id, observed_at)
    gap_segment,           |  pilot_position_YYYYMMDD ... each inherits the FK
    span_part)             `-- views: pilot_latest, online_now, pilot_sample

   controller_session  (cid, callsign, logon_time) unique
        |  FK, no cascade            |  FK, no cascade
   controller_rating_event      controller_frequency_event
   (session_id, observed_at)    (session_id, observed_at)

   Stand-alone (no foreign key):  network_snapshot, poll_log, controller_unlisted_sighting,
                                  reference_data_change, app_setting, schema_migrations,
                                  flight_record (names its sessions in session_ids; no foreign key, nothing reads it yet)

   Joined by VALUE (no foreign key), see 1.3:
     pilot_session.aircraft_icao      -> aircraft_type.icao        (via flight_ext)
     flight.callsign (3 letters)      -> airline.prefix            (via flight_ext)
     pilot_session.home_icao          -> region_map.prefix         (via icao_region)
     pilot_session.pilot_rating       -> pilot_rating.id
     controller_session.rating        -> controller_rating.id
     controller_session.facility      -> facility.id

   flight_summary --> flight_part --> flight --> flight_ext --> movements, cohorts
   controller_session --> controller_session_merged --> controller_session_ranked
                          --> controller_summaries, controller_activity_daily
```

### 1.2 Foreign keys

There are exactly four declared foreign keys. There are no foreign keys to or from the lookup tables.

| Child column | Parent column | On delete | Constraint |
|---|---|---|---|
| `pilot_position.session_id` | `pilot_session.id` | no action (a session that has positions cannot be deleted) | `pilot_position_session_id_fkey` |
| `flight_summary.session_id` | `pilot_session.id` | CASCADE | `flight_summary_session_id_fkey` |
| `controller_rating_event.session_id` | `controller_session.id` | no action | `controller_rating_event_session_id_fkey` |
| `controller_frequency_event.session_id` | `controller_session.id` | no action | `controller_frequency_event_session_id_fkey` |

The daily partitions of `pilot_position` **inherit** the foreign key: each `pilot_position_YYYYMMDD` carries its own clone of `pilot_position_session_id_fkey` (the live catalogue shows one per partition). Nothing needs to be declared when a partition is created.

### 1.3 Joins by value (no foreign key)

| From | To | Rule |
|---|---|---|
| `pilot_session.aircraft_icao` | `aircraft_type.icao` | Exact match after `upper(btrim(...))`, in `flight_ext`; no match gives a NULL category and `is_military` false. |
| `flight.callsign` | `airline.prefix` | Only when the callsign matches `^[A-Z]{3}[0-9]`; the prefix is its first three letters (`airline_prefix` in `flight_ext`). |
| `pilot_session.home_icao` | `region_map.prefix` | `icao_region()`: the longest prefix that the upper-cased code starts with; `Unknown` otherwise. |
| `pilot_session.departure` / `arrival` | home region | `icao_is_home()`: starts with any prefix in `home_prefix` (setting `home_icao_prefixes`, default `Y`). |
| `controller_session.rating` | `controller_rating.id` | Unknown ids show as Unknown/Other on the dashboards (no foreign key, on purpose). |
| `controller_session.facility` | `facility.id` | As above. |
| `pilot_session.pilot_rating` | `pilot_rating.id` | The feed value is a bit-flag; only 0, 1, 3, 7, 15, 31, 63 are seeded. |
| parts of one flight | `flight` | **Merge rule** (ADR-023, issue 35): rows of `flight_part` with the same `(cid, callsign, departure, arrival)` (departure and arrival normalised upper case) belong to one flight when they come from **different sessions** and the next part starts no more than 120 minutes after everything before it ended. Parts of one session are never joined (the 8-hour split stays). A changed plan (different airports) is a different flight. |
| `controller_session` reconnects | `controller_session_merged` | Same `(cid, callsign)`; a new merged session starts when `first_seen` is more than 5 minutes after the latest earlier `last_seen`. |

### 1.4 Keys at a glance

| Table | Primary key | Other unique key |
|---|---|---|
| `pilot_session` | `id` | `(cid, callsign, logon_time)` (`pilot_session_identity`) |
| `pilot_position` and each partition | `(session_id, observed_at)` | |
| `flight_summary` | `(session_id, gap_segment, span_part)` | |
| `flight_record` | `(session_id, start_time)` | `session_id` alone is not unique: a session that comes back after being processed starts a second flight |
| `controller_session` | `id` | `(cid, callsign, logon_time)` (`controller_session_identity`) |
| `controller_rating_event`, `controller_frequency_event` | `(session_id, observed_at)` | |
| `network_snapshot` | `observed_at` | |
| `poll_log` | `id` | |
| `controller_unlisted_sighting` | `callsign` | |
| `reference_data_change` | `id` | |
| `app_setting` | `key` | |
| `controller_rating`, `facility`, `pilot_rating` | `id` | |
| `airline`, `region_map` | `prefix` | |
| `aircraft_type` | `icao` | |
| `schema_migrations` | `version` | |

### 1.5 Partitions and retention

`pilot_position` is `PARTITION BY RANGE (observed_at)` with one partition per UTC day, named `pilot_position_YYYYMMDD` (bounds `00:00:00+00` to the next midnight). The primary key includes the partition key, as PostgreSQL requires.

- **Created** by `ensure_pilot_position_partitions(days_ahead, p_today)`: yesterday through today + `days_ahead` (default 7). The collector calls it at start-up and daily. Existing partitions are skipped. On 09-10-2026 there are 11 partitions (`pilot_position_20261006` to `pilot_position_20261016`).
- **Dropped** by `drop_old_pilot_position_partitions(retention_days, p_today)`: a partition is dropped when it ends on or before `today - retention_days` (collector setting `POSITION_RETENTION_DAYS`, default 90), unless it still holds positions of a session whose `last_seen` is inside the window (the partition is "pinned", and a notice is raised once it is pinned beyond twice the retention). **Drop gate (migration 20261012000210, issue 89 phase 4, NOT YET DEPLOYED; until it is deployed the live function still finalises the flights of the partition's sessions in `flight_summary` first and keeps the partition if that fails):** a partition that is due and not pinned is dropped only when every position in it is reconciled with the written flights: each session's `flown_upto` is at or after its last position in the day; each position of a session with a flight plan lies inside the `start_time` to `end_time` of a `flight_record` row that lists the session in `session_ids` (a session without a plan needs only the first rule); and every flight wholly inside the day has `sample_count` equal to the positions stored for it. Anything else keeps the partition with a notice that starts `DROP REFUSED` (the collector logs it as a warning); the function never finalises the pieces table and never reads it. Since migration 20261009000170 a call checks at most 10 eligible partitions, re-checks them, and drops them last in one short block (`LOCK ... NOWAIT`, 1 second lock timeout, up to three attempts while no drop has succeeded yet), so a catch-up does not hold the lock on `pilot_position` while it checks later partitions; a partition that cannot be locked or changed meanwhile is kept with a notice and dropped by a later run; the collector calls the function until it drops nothing (issue 98). Flights therefore survive the 90 days; raw positions do not. After the phase 4 deploy the surviving flights are the rows of `flight_record` (written once, 8 hours after the pilot was last seen); the old pieces rows of the dropped sessions are left untouched, but a full refresh of the pieces (`refresh_flight_summary(NULL)`) deletes the non-final pieces of a session whose positions are gone, so do not run it after the first drop (docs/RUNBOOKS/drop-gate-rehearsal.md, 'Before the first real drop').
- `pilot_session`, `controller_session`, the event tables, `network_snapshot` and `flight_summary` are **not** trimmed (data retention for them is an open owner decision, issue 21). Only `poll_log` is trimmed, to 400 days, by the collector's maintenance task.

## 2. Base tables

Example values are illustrative.

### app_setting
Settings the SQL views read. The collector writes them at start-up and daily (`maintenance.sync_settings`), so the environment variables stay the single source. Primary key `key`.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `key` | text | no | `home_icao_prefixes` (default `Y`, comma separated), `airborne_gap_cap_seconds`, `poll_interval_seconds`, `flight_quiet_hours`. |
| `value` | text | no | Live values on 09-10-2026: `Y`, `75`, `30` (`flight_quiet_hours` is seeded `8` by migration 20261012000190). |

`airborne_gap_cap_seconds` is derived by the collector as 2.5 x the poll interval (minimum 15), so 75 at 30 s polling. The migration seeds 40 (the value for the old 15 s cadence) and the SQL falls back to 40 only if the row is missing; the collector overwrites it on start. `poll_interval_seconds` is what makes the "online" windows follow the cadence (ADR-024); until the collector has published it, those windows are empty. `flight_quiet_hours` (default 8, seeded `ON CONFLICT DO NOTHING`) is the quiet period of `write_finished_flights`; the collector does not touch it. The owner changes it with `UPDATE app_setting SET value = '6' WHERE key = 'flight_quiet_hours';` (no migration or restart); the writer refuses a value under 2.5. A change never alters flights already written. The watchdog check "pilots quiet longer than the quiet period plus 1 hour" uses a fixed 9 hours (the watchdog cannot read `app_setting`), so it must follow a changed value.

### pilot_session
One row per pilot connection inside the polygon. Identity is `(cid, callsign, logon_time)`; flight-plan fields are attributes updated in place to the latest non-null value. Storage options `fillfactor=70`, `autovacuum_vacuum_scale_factor=0.02` (updated on every poll). Indexes: primary key on `id`; unique `pilot_session_identity (cid, callsign, logon_time)`; `pilot_session_last_seen_idx (last_seen)`; expression indexes on the normalised departure and arrival, `NULLIF(upper(btrim(departure, E' \t\r\n')), '')` and the same for `arrival` (migrations 22 and 23, for the flight gate).

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | bigint (serial) | no | Primary key. |
| `cid` | bigint | no | VATSIM member number (public). |
| `callsign` | text | no | CHECK non-empty. Example `QFA6`. |
| `logon_time` | timestamptz | no | Whole seconds (CHECK `pilot_session_logon_whole_second`; ADR-021). |
| `name` | text | yes | Name as in the feed (stored and shown, ADR-009). |
| `home_icao` | text | yes | Trailing 4-letter upper-case token of `name` (a home airport), CHECK `^[A-Z]{4}$`; otherwise NULL. |
| `server` | text | yes | VATSIM server name. |
| `first_seen`, `last_seen` | timestamptz | no | First and latest snapshot with the pilot inside the polygon. CHECK `last_seen >= first_seen`. |
| `departure`, `arrival` | text | yes | Filed airports, un-normalised (the views upper-case and trim them). |
| `aircraft_icao`, `aircraft_faa` | text | yes | Filed aircraft type, for example `A388`. |
| `flight_rules`, `route`, `remarks`, `planned_alt`, `deptime` | text | yes | Flight plan as filed. |
| `plan_changed_at` | timestamptz | yes | Snapshot time when a non-null departure or arrival differed from the stored value (including the first time a plan appears on an existing session). |
| `max_gs_kt`, `max_alt_ft` | integer | yes | Maximum plausible groundspeed and altitude seen (GREATEST on each upsert). |
| `pilot_rating`, `military_rating` | integer | yes | Feed values. |
| `flown_upto` | timestamptz | yes | Added by migration 20261012000190 (issue 89 phase 1). Bookkeeping for `write_finished_flights`: this session's positions up to this time have been processed (written into `flight_record`, or the session has no flight). NULL = never processed; `last_seen > flown_upto` = unprocessed positions. The collector does not touch it. Partial index `pilot_session_unwritten_idx (cid) WHERE flown_upto IS NULL OR last_seen > flown_upto` is the writer's work list. |

### pilot_position
Raw position samples, one per session per processed poll (every 30 s at the current cadence). Partitioned (section 1.5). Primary key `(session_id, observed_at)`; foreign key to `pilot_session.id`.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `observed_at` | timestamptz | no | Snapshot time (`general.update_timestamp`). Partition key. |
| `session_id` | bigint | no | FK to `pilot_session.id`. |
| `lat`, `lon` | double precision | yes | Position. |
| `alt_ft` | integer | yes | CHECK -1500..65000; out-of-range values are stored as NULL, not clamped. |
| `gs_kt` | integer | yes | CHECK 0..700; out-of-range values are stored as NULL. |
| `hdg` | integer | yes | CHECK 0..360. |

### flight_summary
Per-flight aggregates computed from `pilot_position` (migration 20, ADR-016). One row per `(session_id, gap_segment, span_part)`. Maintained by `refresh_flight_summary()`, which the collector calls every 5 minutes for sessions seen in the last 12 hours; a row is marked `final` and finalised before its raw-position partition is dropped, so flights survive the position retention. Read through `flight_part` and `flight`, never directly by Grafana. Indexes: primary key; `flight_summary_start_time_idx (start_time)`; `flight_summary_end_time_idx (end_time)`.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `session_id` | bigint | no | FK to `pilot_session.id`, ON DELETE CASCADE. |
| `gap_segment` | bigint | no | The segment after splitting the session on gaps over 120 minutes. |
| `span_part` | integer | no | The 8-hour bucket within the segment. |
| `cid` | bigint | no | Copied from the session. |
| `start_time`, `end_time` | timestamptz | no | First and last stored sample of the part. |
| `sample_count` | bigint | no | Number of samples. |
| `airborne_minutes` | numeric | yes | See [DEFINITIONS.md](DEFINITIONS.md) section 3 (groundspeed at least 60 kt at the earlier sample, each gap capped at `airborne_gap_cap_seconds`). NULL for sessions whose raw positions were already gone when the table was introduced. |
| `max_gs_kt`, `max_alt_ft` | integer | yes | Maxima for this part. |
| `final` | boolean | no | Default false. True when the session is dead and fully summarised, so its raw partitions may be dropped. |
| `computed_at` | timestamptz | no | Default `now()`; when the row was last recomputed. |

### flight_record
Added by migration 20261012000190 (issue 89 phase 1, design `docs/DESIGN_89_FINAL.md`). One row per **finished flight**, written once by `write_finished_flights()` and never changed. Nothing reads it yet: the `flight` view is switched to it in phase 3, and the table is renamed `flight_summary` in phase 5. A flight is one session, or several sessions of the same `cid`, `callsign`, `departure` and `arrival` joined across reconnects by v1's rule (a new connection joins when its `logon_time` is no more than 120 minutes after the latest last position of the earlier sessions; no 8-hour parts and no cut inside a connection). Written when the pilot has been quiet for `flight_quiet_hours` (default 8) of stored data. Plan and name fields are frozen at write time. Primary key `(session_id, start_time)`; indexes on `cid`, `start_time`, `end_time`, `logon_time`; CHECK `end_time >= start_time`; no foreign key. Not readable by `grafana_ro` or `collector`; readable by `backup_ro` and `watchdog_ro`.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `session_id` | bigint | no | Id of the flight's FIRST session (the flight id panels will use). |
| `start_time`, `end_time` | timestamptz | no | First and last stored position of the flight. |
| `cid` | bigint | no | The pilot. |
| `callsign`, `departure`, `arrival` | text | no | The join key; departure and arrival are upper-cased and trimmed (the flight gate has passed). |
| `session_ids` | bigint[] | no | Every session in the flight, in order. |
| `logon_time` | timestamptz | no | Earliest logon of its sessions. |
| `name`, `home_icao`, `server`, `aircraft_icao`, `aircraft_faa`, `flight_rules`, `remarks`, `planned_alt`, `pilot_rating`, `military_rating` | as in `pilot_session` | yes | From the session that ended last (ties: higher session id), NULLs included. |
| `route` | text | yes | Latest non-null route over the sessions. |
| `deptime` | text | yes | Maximum non-null departure time over the sessions. |
| `airborne_minutes` | numeric | yes | Sum over the sessions, rounded once to 2 decimals, by the measured-gap rule of [DEFINITIONS.md](DEFINITIONS.md) section 3. NULL only for carried-over flights without raw positions. |
| `max_gs_kt`, `max_alt_ft` | integer | yes | Maxima over the flight. |
| `sample_count` | bigint | no | Positions in the flight; 0 only for carried-over flights. |
| `written_at` | timestamptz | no | Default `now()`; when the row was written. |

### controller_session
One row per controller connection on a whitelisted callsign. Identity `(cid, callsign, logon_time)`. Indexes: primary key; unique `controller_session_identity`; `controller_session_last_seen_idx (last_seen)`.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | bigint (serial) | no | Primary key. |
| `cid`, `callsign`, `logon_time` | bigint, text, timestamptz | no | Identity. CHECK callsign non-empty; `logon_time` whole seconds (`controller_session_logon_whole_second`, ADR-021). |
| `name` | text | yes | Feed name at the latest sighting. |
| `rating` | integer | yes | Joins to `controller_rating.id` by value. |
| `facility` | integer | yes | Joins to `facility.id` by value. |
| `server` | text | yes | VATSIM server name. |
| `frequency_hz` | bigint | yes | Integer Hz (`121.700` MHz is `121700000`); NULL when the feed says `199.998`. |
| `visual_range` | integer | yes | Feed value. |
| `first_seen`, `last_seen` | timestamptz | no | First and latest snapshot. CHECK `last_seen >= first_seen`. |

### controller_rating_event and controller_frequency_event
Insert-only history, written on the first sighting and whenever the value changes. Primary key `(session_id, observed_at)`; `session_id` is a foreign key to `controller_session.id`.

| Table | Columns (type, nullable) |
|---|---|
| `controller_rating_event` | `session_id` bigint no; `observed_at` timestamptz no; `rating` integer yes |
| `controller_frequency_event` | `session_id` bigint no; `observed_at` timestamptz no; `frequency_hz` bigint yes |

### network_snapshot
One row per processed snapshot; primary key `observed_at`. Written even when nothing is in the region. All counts are integer, not null.

| Column | Meaning |
|---|---|
| `observed_at` | Snapshot time. |
| `pilots_in_region` | Pilots stored: inside the polygon and not rejected, so it equals `poll_log.pilots_kept` and the positions written for the snapshot (it is below `poll_log.pilots_inside` when a record was skipped). |
| `controllers_in_region` | Controllers stored (on the whitelist). |
| `pilots_global`, `controllers_global` | Totals in the whole feed. |

### poll_log
One row per poll attempt, including failures. Trimmed to 400 days by maintenance. Primary key `id`; index `poll_log_polled_at_idx (polled_at)`.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `id` | bigint (serial) | no | PK. |
| `polled_at` | timestamptz | no | Wall-clock time of the attempt (default `now()`). |
| `status` | text | no | CHECK in `ok`, `skipped_unchanged`, `error`. |
| `snapshot_ts` | timestamptz | yes | Feed timestamp, when one was read. |
| `duration_ms`, `http_status` | integer | yes | Timing and HTTP status of the fetch. |
| `pilots_seen` | integer | yes | Pilots in the whole feed. |
| `pilots_inside` | integer | yes | Pilots inside the polygon, including any record that was rejected (compare `pilots_kept`). |
| `pilots_with_plan` | integer | yes | Of those, pilots with non-empty departure and arrival (the flight gate, informational). |
| `pilots_kept` | integer | yes | Pilots actually written; equals `pilots_inside` unless a malformed record was skipped. |
| `controllers_seen`, `controllers_kept` | integer | yes | Controllers in the feed and on the whitelist. |
| `rejected_values` | integer | yes | Values dropped or cut at ingest: speed/altitude nulled by the plausibility caps, integers outside int32, unparsable numbers, headings outside 0 to 360 (stored records only), every malformed feed entry skipped, and every text field cut to its length cap (issue 43). A bad record bisected out of the insert counts once. |
| `polygon_sha`, `whitelist_sha` | text | yes | SHA-256 of the reference files in use. |
| `error` | text | yes | Message (cut to 2000 characters) for `error` rows. |

Count columns are NULL on rows that carry no snapshot result (`skipped_unchanged`, `error`).

### controller_unlisted_sighting
Near-miss controller callsigns (see DEFINITIONS). Primary key `callsign`. Columns: `callsign` text no; `cid` bigint yes; `first_seen`, `last_seen` timestamptz no; `polls_seen` bigint no, default 1.

### reference_data_change
Audit of reference-file changes. Primary key `id`. Columns: `id` bigint (serial); `changed_at` timestamptz default `now()`; `file` text (`australian_airspace_polygon.json` or `controller_callsigns_list.txt`); `old_sha` text, nullable; `new_sha` text. A row is inserted only when the hash differs from the last recorded one.

### schema_migrations
dbmate's own bookkeeping: `version` varchar(128), primary key, one row per applied migration file (the 14-digit prefix). Currently 22 rows, `20261007000001` to `20261009000160`. Not granted to any application role.

## 3. Lookup tables

Granted SELECT to `grafana_ro`. Seeded by migrations 3 and 11 (`ON CONFLICT DO NOTHING`; unknown codes show as "Other"/"Unknown" on the dashboards). Row counts are those of 09-10-2026.

| Table | Key | Columns | Contents |
|---|---|---|---|
| `controller_rating` | `id` integer | `short`, `long` text, not null | -1 INAC, 0 SUS, 1 OBS, 2 S1, 3 S2, 4 S3, 5 C1, 6 C2, 7 C3, 8 I1, 9 I2, 10 I3, 11 SUP, 12 ADM (14 rows). |
| `facility` | `id` integer | `short`, `long` | 0 OBS, 1 FSS, 2 DEL, 3 GND, 4 TWR, 5 APP, 6 CTR (7 rows). |
| `pilot_rating` | `id` integer | `short`, `long` | Bit-flag values 0 NEW, 1 PPL, 3 IR, 7 CMEL, 15 ATPL, 31 FI, 63 FE (7 rows). |
| `airline` | `prefix` text | `name` text, not null | Three-letter callsign prefixes, for example `ACA` Air Canada (62 rows; migration 11). |
| `aircraft_type` | `icao` text | `category` text (nullable); `is_military` boolean not null default false; `in_v1_card65_list` boolean not null default false | 534 exact-match ICAO codes in 31 categories (migration 11; the last column added by migration 160). |
| `region_map` | `prefix` text | `region` text, not null | ICAO prefix to region, for example `C` Canada (21 rows); used by `icao_region()`. |

**`is_military` versus `in_v1_card65_list`.** `is_military` (232 codes) is the broad flag used by `flight_ext.is_military` and the charts that split military from civil traffic. `in_v1_card65_list` (225 codes, migration 20261009000160) marks the exact codes in the IN list of v1 Metabase card 65 ("most popular military aircraft") and is read only by the card 65 panel. Every card 65 code is also `is_military`; the seven extra `is_military` codes are EA18, EUFI, FA18, HAWK, K35R, KC10 and KC30. To change either set, add a new migration (see [RUNBOOKS/update-reference-files.md](RUNBOOKS/update-reference-files.md)).

## 4. Views

There are 23 views and no materialised views. Dashboards use the 18 listed in section 0. Views are created in migration 4 (core), 10 (analytics), 20 (`flight` over `flight_summary`), 24 (wall-clock guard on `online_now`), 30 and 31 (`controller_summaries`), 80 (`pilot_sample`), 100 (whole-second `logon_time`), 110 (`flight_part` and the merged `flight`), 120 (`poll_cadence`, cadence-driven windows), 130 (`flight.is_in_progress`) and 140 (`flight_ext` excludes in-progress flights); migration 90 dropped the unused `controller_activity_summary`; migration 150 is a comment change only. Only `flight`, `flight_ext`, `flight_part`, `movements`, `cohorts`, `controller_summaries` and `poll_cadence` carry a database comment (`COMMENT ON VIEW`); this section is the description for the rest.

There are **two "online" definitions**, both deliberate:

- `online_now` and `controller_session_merged.is_open` use **2 x the poll interval** (`app_setting.poll_interval_seconds`, so 60 s at 30 s polling) before the newest stored snapshot. `online_now` is empty unless that snapshot is itself under 5 minutes old.
- `pilot_latest.is_online` and `controller_online_now` use **the two newest stored snapshots** (seen at or after the older of the two) and also require `last_seen` within 5 minutes of `now()`.

At the current cadence they agree. `flight.is_in_progress` uses the first definition.

### 4.1 Flights

| View | Reads | What it returns and the rules |
|---|---|---|
| `flight_part` | `flight_summary`, `pilot_session`, `app_setting` | One row per `(session, gap_segment, span_part)` with the **flight gate** applied: departure and arrival both non-empty after `btrim` of space, tab, CR and LF. Departure and arrival are upper-cased; `is_circuit` is departure = arrival; `is_zzzz` is either being `ZZZZ`; `touches_home_region` is either starting with a home prefix. 31 columns: `session_id, gap_segment, span_part, cid, callsign, logon_time, name, home_icao, server, departure, arrival, aircraft_icao, aircraft_faa, flight_rules, route, remarks, planned_alt, deptime, pilot_rating, military_rating, start_time, end_time, duration, duration_minutes, airborne_minutes, max_gs_kt, max_alt_ft, sample_count, is_circuit, is_zzzz, touches_home_region`. Plan fields are live from `pilot_session`; the numbers lag by up to 5 minutes. |
| `flight` | `flight_part`, `online_now` | One row per **flight**: parts merged across reconnects by the rule in section 1.3. The same 31 columns as `flight_part` plus a last column `is_in_progress` (32). `session_id`, `gap_segment`, `span_part` are those of the first part; `logon_time` is the earliest; `start_time` the earliest and `end_time` the latest; `airborne_minutes` and `sample_count` are sums; maxima are maxima; the flags are OR-ed; name, plan and rating fields come from the part that ended last. `is_in_progress` is true while any session of the flight is in `online_now` (false for every flight when the collector is stale). `flight` **lists** in-progress flights; the counting panels that read it directly add `NOT is_in_progress` themselves. Unique per `(session_id, gap_segment, span_part)` of the first part. |
| `flight_ext` | `flight`, `airline`, `aircraft_type`, `icao_is_home`, `icao_region` | **Completed flights only** (`WHERE NOT is_in_progress`, migration 140, ADR-025) with derived fields. Columns: `session_id, cid, callsign, name, home_icao, server, departure, arrival, route_label` (`departure-arrival`)`, aircraft_icao` (upper, trimmed)`, flight_rules, remarks, start_time, end_time, duration_minutes` (unrounded)`, airborne_minutes, max_gs_kt, max_alt_ft, is_circuit, is_zzzz, dep_is_home, arr_is_home, pilot_is_local, pilot_region, cid_group` (`cid / 100000`)`, airline_prefix, airline_name, aircraft_category, is_military, has_selcal` (remarks contain `SEL/`)`, has_remarks`. |
| `movements` | `flight_ext` | Departures (at `start_time`) and arrivals (at `end_time`) at home-region airports, `ZZZZ` excluded. Counts every completed flight-gate session, as v1 does (ADR-018, issue 74): parked sessions with no airborne time and circuits count as one departure and one arrival. A flight still connected contributes neither. Columns: `airport, movement_type` (`departure` or `arrival`)`, movement_time, session_id, cid, callsign`. |
| `cohorts` | `flight_ext` | Completed flights grouped by joining cohort: `cid_group = cid / 100000`, label such as `1000000-1099999`; group 0 excluded. Columns: `session_id, cid, cid_group, cohort, start_time, end_time`. |
| `monday_attendance` | `flight_summary`, `pilot_session`, `controller_session_merged`, `data_since` | Per Monday (UTC) from the week of go-live, distinct pilot CIDs (flight-summary parts overlapping the window, with non-empty departure and arrival) and distinct controller CIDs (merged sessions overlapping the window) present between 07:00Z and 12:00Z. Columns: `monday_date, win_start, win_end, pilot_cids, controller_cids`. |

### 4.2 Controllers

| View | Reads | What it returns and the rules |
|---|---|---|
| `controller_session_merged` | `controller_session`, `network_snapshot`, `app_setting` | Reconnects merged per `(cid, callsign)` (a gap over 5 minutes starts a new group). Columns: `cid, callsign, merge_group, first_session_id, session_count, name, rating, facility, server, frequency_hz` (latest values)`, start_time` (earliest `logon_time`)`, end_time` (last sighting)`, duration, duration_hours, is_open` (`end_time` within 2 x poll interval of the newest snapshot). |
| `controller_session_ranked` | `controller_session_merged`, `rank_group_of` | Adds `rank_group` and renames the times. Columns: `cid, callsign, name, rating, rank_group, facility, server, session_start, session_end, duration_minutes, is_ctr_fss` (callsign ends `_CTR` or `_FSS`). |
| `controller_summaries` | `controller_session_ranked` | v1-compatible shape so the recovered v1 retention SQL runs unchanged (migrations 30, 31). Columns: `cid, name, rating, session_start_time, session_duration_minutes, callsign, session_end_time`. One row per merged session (v1 counted a quick reconnect twice). Read by `atc/atc-retention.json` and the online-hours panels (cards 94 and 95). |
| `controller_activity_daily` | `controller_session_ranked` | One row per controller per UTC day (session attributed to the day it started). Columns: `activity_date, cid, name, rank_group` (of the highest rating)`, sessions, minutes, ctr_fss_minutes`. |
| `controller_online_now` | `controller_session`, `network_snapshot` | Controllers seen in the two newest stored snapshots and within 5 minutes of now. Columns: `session_id, cid, callsign, name, rating, rank_group, facility, server, frequency_hz, frequency_mhz, logon_time, last_seen, hours_online`. |
| `near_miss_controllers` | `controller_unlisted_sighting` | The table as a view: `callsign, cid, first_seen, last_seen, polls_seen`. |

### 4.3 Live pilots

| View | Reads | What it returns and the rules |
|---|---|---|
| `online_now` | `network_snapshot`, `pilot_session`, `pilot_position`, `controller_session`, `app_setting` | Pilots and controllers seen within 2 x the poll interval of the newest stored snapshot (`as_of`), only while that snapshot is under 5 minutes old (otherwise empty). The pilot's newest position is a `LEFT JOIN LATERAL ... LIMIT 1` probe on the primary key (one probe per online pilot, so the view stays fast on many daily partitions). Columns: `kind` (`pilot` or `controller`), `session_id, cid, callsign, name, logon_time, first_seen, last_seen, as_of`, pilot fields `departure, arrival, aircraft_icao, lat, lon, alt_ft, gs_kt` (NULL for controllers), controller fields `rating, facility, frequency_hz` (NULL for pilots). |
| `pilot_latest` | `pilot_session`, `pilot_position`, `network_snapshot` | Latest position of each pilot session seen in the last 3 hours (sessions with no position in that window are absent), with normalised departure, arrival and aircraft, and `is_online`. Columns: `session_id, cid, callsign, name, home_icao, server, departure, arrival, aircraft_icao, flight_rules, deptime, logon_time, last_seen, position_time, lat, lon, alt_ft, gs_kt, is_online`. |
| `pilot_online_now` | `pilot_latest`, `icao_region` | `pilot_latest` where `is_online`, plus `region` from `icao_region(home_icao)`. |
| `pilot_sample` | `pilot_position` | A plain column subset of `pilot_position`: `observed_at, session_id, alt_ft, gs_kt`. No filter, aggregation or join, so a predicate on `observed_at` still prunes the daily partitions. It exists because `grafana_ro` has no base-table access and card 156 (airborne aircraft per 5-minute slot during the latest Monday event) needs raw altitude samples. Carries no sector or frequency logic (ADR-002). Retention is that of `pilot_position`; `alt_ft` and `gs_kt` are NULL where ingest rejected the value. |

### 4.4 Collector and data health

| View | Reads | What it returns |
|---|---|---|
| `data_freshness` | `network_snapshot`, `poll_log` | One row: `checked_at, latest_snapshot_ts, snapshot_age_seconds, data_since, last_ok_poll_at, last_ok_age_seconds, ok_last_hour, skipped_last_hour, error_last_hour`. |
| `data_since` | `network_snapshot` | One row: `go_live` = earliest `observed_at` (07-10-2026 21:38 UTC on the live database). |
| `monitor_freshness` | `network_snapshot`, `pilot_session`, `controller_session`, `poll_log` | Four rows `source, newest_at, age_seconds` for `network_snapshot`, `pilot_session`, `controller_session` and `poll_log_ok`. |
| `network_history` | `network_snapshot` | The table as a view (time series of in-region and global counts): `observed_at, pilots_in_region, controllers_in_region, pilots_global, controllers_global`. |
| `poll_history` | `poll_log` | `poll_log` without the two SHA columns: `id, polled_at, status, snapshot_ts, duration_ms, http_status, pilots_seen, pilots_inside, pilots_with_plan, pilots_kept, controllers_seen, controllers_kept, rejected_values, error`. |
| `poll_cadence` | `app_setting` | One row `poll_interval_s` numeric: the collector poll interval published on start (migration 120); the monitoring panel and soak report read it because `grafana_ro` sees views only. |
| `home_prefix` | `app_setting` | The configured home ICAO prefixes, one row each, column `prefix` (default `Y` if the setting is missing). |

## 5. Functions

| Function | Returns | Security | Purpose |
|---|---|---|---|
| `ensure_pilot_position_partitions(days_ahead int DEFAULT 7, p_today date DEFAULT NULL)` | int | DEFINER | Creates daily partitions for yesterday through today + `days_ahead` (UTC); returns the number created. `days_ahead` must be at least 0. Run by the collector at start-up and daily. |
| `drop_old_pilot_position_partitions(retention_days int, p_today date DEFAULT NULL)` | int | DEFINER | Drops partitions older than the retention unless pinned by a recently seen session, and (from migration 20261012000210, not yet deployed) only when `unreconciled_partition_sessions` finds nothing for the day; before that migration it finalises their flights in `flight_summary` first. Checks, re-checks, then drops them together at the end (at most 10 per call; the collector repeats the call), keeping any partition it cannot lock or that changed meanwhile (section 1.5). Returns the number dropped. `retention_days` must be at least 1. |
| `unreconciled_partition_sessions(p_start timestamptz, p_end timestamptz)` | table (session_id bigint, reason text) | DEFINER, STABLE | Added by migration 20261012000210 (issue 89 phase 4, not yet deployed). Read-only. The sessions (and flights) that stop the day `[p_start, p_end)` of `pilot_position` from being dropped, each with a reason: `session row missing`, `positions not yet processed by the flight writer`, `positions not inside a written flight`, or `flight sample_count differs from its stored positions` (the last for flights wholly inside the day only). No rows means the day is reconciled. Set-based: one aggregate per session, one range semi-join against the flights overlapping the day. Not granted to any role (only the drop function and the owner's rehearsal, `ops/rehearse_drop_gate.sql`, call it). |
| `write_finished_flights(p_max_pilots int DEFAULT 500)` | int | DEFINER | Added by migration 20261012000190 (issue 89 phase 1). Writes the finished flights of pilots who have been quiet for `app_setting.flight_quiet_hours` (default 8, minimum 2.5, else it raises) of stored data into `flight_record`, then sets `pilot_session.flown_upto`. "Quiet" is measured against the newest `network_snapshot`, not the clock; an empty `network_snapshot` writes nothing. One writer at a time (a second caller does not wait, it returns 0 at once), one transaction, idempotent, at most `p_max_pilots` pilots per call, oldest-quiet first. A pilot whose flight key already exists is skipped, left unmarked and reported in a WARNING. Returns the number of flights written. Not granted to any role: it runs through `refresh_flight_summary`. |
| `refresh_flight_summary(p_since interval DEFAULT '12 hours')` | int | DEFINER | Recomputes `flight_summary` for every non-final session seen within `p_since` (NULL means every session on or after the oldest partition). Idempotent; called by the collector every 5 minutes. Returns the rows changed. Since migration 20261012000190 it ends with a guarded call of `write_finished_flights()` (a writer fault is a WARNING and cannot stop this refresh). |
| `refresh_flight_summary_sessions(p_ids bigint[], p_final boolean DEFAULT false)` | int | invoker | Internal worker for the two functions above: the gap, span and airborne rules. Not granted to the collector. |
| `refresh_materialized_views()` | int | DEFINER | Refreshes every materialised view in `public` (concurrently where possible); none exist today. |
| `icao_is_home(code text)` | boolean | invoker, STABLE | True when the upper-cased code starts with a `home_prefix`. |
| `icao_region(code text)` | text | invoker, STABLE | Region from `region_map` by longest matching prefix, else `Unknown`. |
| `rank_group_of(rating int)` | text | invoker, IMMUTABLE | 1 OBS, 2 S1, 3 S2, 4 S3, 5 C1, 6 C2, 7 C3, 8 or above `I1+`, else `Unknown`. |

DEFINER functions run with owner rights and `search_path = public`, so the collector needs no DDL rights (migration 21). Maintenance is driven from `collector/src/vatpac/maintenance.py`: partitions daily, flight summary every 5 minutes, `poll_log` trim to 400 days, settings sync at start-up and daily.

## 6. Roles and grants

| Role | Created by | Rights |
|---|---|---|
| `vatpac` (owner, from `POSTGRES_USER`) | Postgres image | Owns everything; used by `migrate` and, for restores only, by the `backup` service's restore scripts (ADR-017). Not used by the collector. |
| `collector` | `db/roles.sql` | Login. SELECT, INSERT, UPDATE on `pilot_session`, `pilot_position`, `controller_session`, `controller_rating_event`, `controller_frequency_event`, `network_snapshot`, `poll_log`, `controller_unlisted_sighting`, `reference_data_change`, `app_setting`, plus DELETE on `poll_log` only (the retention trim); USAGE on sequences; EXECUTE on `ensure_pilot_position_partitions`, `drop_old_pilot_position_partitions`, `refresh_flight_summary`, `refresh_materialized_views`. No DDL; no access to `flight_summary`, `flight_record`, the lookups or the views; `write_finished_flights` is not granted (it runs through `refresh_flight_summary`). |
| `grafana_ro` | `db/roles.sql` | Login; connection limit 10; `default_transaction_read_only = on`, `statement_timeout = 30s`, `work_mem = 16MB`, `TimeZone = UTC` (T27). SELECT on every view and the six lookup tables; nothing else; cannot create. Re-granted on every deploy so new views are picked up. |
| `backup_ro` | `db/roles.sql` | Login; member of `pg_read_all_data`; read-only by default. Used by the nightly `pg_dump`. |
| `watchdog_ro` | `ops/migrate/migrate.sh` | Login; read-only by default; `statement_timeout = 10s`; member of `pg_monitor`; SELECT on `poll_log`, `network_snapshot`, `controller_unlisted_sighting`, `pilot_session` and `controller_session` (the last two for the identity-flap alert), and, from the integrity-checks deploy (T16, not yet applied on the VM), `pilot_position`, `flight_summary`, `flight_record` (from migration 20261012000190) and `reference_data_change` (for the scheduled invariant sweep and archive cross-check). |

## 7. Known gaps

- The long-window and retention charts have no real history yet: there is no backfill (ADR-011), so 30 to 90 day measures say little until months after go-live (07-10-2026), and the retention rule that counts every controller as reactivated holds until 60 days of history exist. They are tested on synthetic rows (`tests/integration/test_controller_retention.py`).
- Only `poll_log` and raw `pilot_position` partitions have a retention. Sessions, events, snapshots and `flight_summary` grow without limit (owner decision pending, issue 21).
- Sixteen of the 23 views have no `COMMENT ON VIEW`; section 4 is their only description. Adding comments is a comment-only migration.
- Two "online" definitions exist (section 4). They agree at 30 s polling but are separate expressions; a change of cadence logic must touch both.
- `rating`, `facility` and `pilot_rating` are not foreign keys, so an id outside the seeded lookups is stored and shows as Unknown/Other.
- The lookup seeds (airline 62 rows, aircraft types 534, regions 21) are a static snapshot of v1 lists, not a complete code list; unseen codes show as "Other".
- `airborne_gap_cap_seconds` in the migration seed (40) differs from the live value (75): the collector overwrites it on start, so it is harmless, but the seed is the old 15 s value.
- The schema was read from the live database and compared with the migrations by name, column and constraint; a clean apply of all 22 migrations to an empty database was not repeated for this review (the integration tests do that in CI).

## 8. How to keep this file current

1. Run the read-only dump on the VM from your machine (nothing is written, no secret is printed):

   ```
   ssh vatpac-v3 'cd ~/vatpac-statsV2 && docker compose exec -T db psql -U vatpac -d vatpac -P pager=off -A -F" | "' < ops/schema_dump.sql
   ```

   It prints the applied migrations, tables, columns, view columns, constraints, indexes, partitions, view dependencies, functions, grants, roles and settings. Details are in [RUNBOOKS/data-dictionary.md](RUNBOOKS/data-dictionary.md).
2. Compare it with sections 1 to 6, edit, and update the dated line at the top.
3. `tests/test_data_dictionary_tests.py` fails when a table, view or function created by a migration is missing from this file, or when the header names a migration other than the latest. Run it before pushing.
