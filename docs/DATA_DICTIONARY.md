# Data dictionary

Checked against the migrations up to 20261114000210 and, for the earlier ones, against the live database on 05-11-2026 (read-only queries). The live database was at migration 20261013000300 (`schema_migrations`: latest version 20261013000300, 29 rows, read on 05-11-2026); migration 20261114000210 is the latest; `schema_migrations` on a database shows which migrations it has applied. History of the migration markers (13-10-2026; the release marker is shown by `bash ops/deploy.sh --status`, not here; the phase 4 and 5 marker was 7a71d3054506d5d6e5e0165860de0feb97e0f651, marked 13-10-2026 10:05:56 UTC): 20261012000190 (issue 89 phase 1, marker c70e72f, 12-10-2026 17:58:21 UTC), 20261012000200 (phase 1b, quiet per flight, marker 3d4b5b6, 13-10-2026 08:01:09 UTC), 20261013000200 (phase 3, marker 38e3958, 13-10-2026 08:14:12 UTC), 20261013000210 (phase 4, drop gate) and 20261013000300 (phase 5, the old pieces removed and the finished-flights table named `flight_summary`), the last two with marker 7a71d30, are all LIVE. Source: `db/migrations/*.sql` (43 migrations, `20261007000001` to `20261114000210`; applied by the `migrate` service with dbmate; the first 29 are recorded in `schema_migrations` on the VM, 29 rows read on 13-10-2026 and again on 05-11-2026; the later ones are applied at the next deploy) and `db/roles.sql` (plus the `watchdog_ro` block of `ops/migrate/migrate.sh`). The database image is PostgreSQL 16.15 (CI tests on 16.4 until updated). All timestamps are `timestamptz` (UTC). Ids are `bigint` unless stated. The meaning of the derived measures is in [DEFINITIONS.md](DEFINITIONS.md); the decisions behind them are in [DECISIONS.md](DECISIONS.md) (ADR-018, 021, 023, 024, 025, 026, 030).

*Describes main (the commit production runs is shown by `bash ops/deploy.sh --status`); behaviour added by later changes that are not yet deployed is listed in CHANGELOG.md.*

Objects on the live database, counted read-only on 05-11-2026 (migration 20261013000300): 19 base tables (18 ordinary, among them `schema_migrations`, `flight_summary` and `drop_gate_refusal`, plus the partitioned `pilot_position`, which has 38 daily partitions, `pilot_position_20261006` to `pilot_position_20261112`), 22 views, 8 functions in the `public` schema, 3 declared foreign keys (not counting the clones on the partitions), no materialised views, no triggers (foreign keys, triggers and materialised views re-read on 05-11-2026). Row counts read on 05-11-2026 (after the v1 history load of that day): `flight_summary` 82,796, `controller_session` 12,513, `pilot_session` 14,301, `network_snapshot` 85,848; database size 400 MB. The earlier figures read before the 13-10-2026 deploy were 24 views, 9 functions, 4 foreign keys.

**Phase 5 (issue #141, migration `20261013000300`, LIVE since 13-10-2026, marker 7a71d30; forward-only, the way back was the pre-deploy dump).** It removed the old per-piece model: the pieces table, the two views built on it and the two refresh functions (their names are in [CHANGELOG.md](../CHANGELOG.md) and ADR-030 in [DECISIONS.md](DECISIONS.md); nothing in this file describes them any more). It renamed the finished-flights table to `flight_summary` together with its primary key (`flight_summary_pkey`), CHECK constraint (`flight_summary_check`) and four indexes (`flight_summary_cid_idx`, `_start_time_idx`, `_end_time_idx`, `_logon_time_idx`), and re-created the writer, the reconciliation helper and the drop function with only the table name changed. The pieces table's cascade to `pilot_session` went with it, and `flight_summary` has no foreign key, so deleting a session no longer deletes its flights: delete them by `session_ids` (ADR-026 as amended in ADR-030). The collector holds EXECUTE on `write_finished_flights(int)` and calls it directly every 5 minutes with 50 pilots; it has no access to the table itself. The watchdog's invariant pack has 20 checks (21 until 22-10-2026, when issue 243 removed "positions outside plausibility caps"; the three CHECK constraints on `pilot_position` are unchanged but nothing alerts if they are dropped). `watchdog_ro` and `backup_ro` read `flight_summary`; `grafana_ro` and `collector` cannot (read with `has_table_privilege` on 13-10-2026).

## 0. Orientation

**What is stored.** The collector polls the VATSIM data feed every 30 seconds (the feed itself updates every 15 seconds; every second update is read, ADR-024). It keeps:

- pilots whose position is inside the home-region polygon (`pilot_session` plus a position sample per poll in `pilot_position`),
- controllers whose callsign is on the whitelist (`controller_session`), plus unknown callsigns (`controller_unlisted_callsign`),
- one row per processed snapshot (`network_snapshot`) and one per poll attempt (`poll_log`).

**How a session is identified.** A pilot or controller session is the triple `(cid, callsign, logon_time)`. `logon_time` is the connection time from the feed truncated to **whole seconds** (never rounded), enforced by CHECK constraints, because the feed sometimes sends the fraction and sometimes does not (ADR-021). A reconnect is a new session; the `flight` view (pilots) and `controller_session_merged` (controllers) join reconnects back together for counting.

**The layers.**

| Layer | What | Who uses it |
|---|---|---|
| Base tables (section 2) | Written only by the collector (sessions, positions, snapshots, logs) and by `write_finished_flights` (`flight_summary`, and `pilot_session.flown_upto`), which the collector calls every 5 minutes. | `collector` (writes), `backup_ro` (dump), `watchdog_ro` (a few tables). |
| Lookups (section 3) | Small reference tables: ratings, facilities, airlines, aircraft types, regions. Seeded by migrations. | Views, and `grafana_ro` directly. |
| Functions (section 5) | Partition maintenance, the finished-flights writer, and three SQL helpers used by the views. | Collector (EXECUTE on four of them), views. |
| Views (section 4) | The only thing the dashboards read. All derived rules (flight merging, online windows, normalisation) live here. | Grafana, as `grafana_ro`, which sees views and the six lookups only. |
| Roles (section 6) | `vatpac` (owner), `collector`, `grafana_ro`, `backup_ro`, `watchdog_ro`. | |

Dashboards read these 13 views and no others (checked by `python grafana/validate_dashboards.py`, which refuses base tables): `cohorts`, `controller_session_ranked`, `controller_summaries`, `data_freshness`, `data_since`, `flight`, `flight_ext`, `monitor_freshness`, `movements`, `network_history`, `pilot_online_now`, `poll_cadence`, `poll_history`; and the lookup `aircraft_type` (`airline` is reached through `flight_ext`). Views that no panel reads directly but others build on or that remain for ad-hoc use: `monday_attendance` (no panel reads it; the view remains, built on `flight`), `controller_online_now` (no panel reads it), `controller_session_merged`, `online_now`, `pilot_sample` (no panel reads it since card 156 moved to the flight summary on 08-11-2026, issue 324), `pilot_latest` (no panel reads it directly since the Departure board was removed on 08-11-2026, issue 313; `pilot_online_now` is built on it), `home_prefix`, `controller_activity_daily`.

## 1. Relationships

### 1.1 Entity diagram

```
                       pilot_session  (cid, callsign, logon_time) unique
                      /      |
                     /       | FK, no cascade
                    /        |
                       pilot_position  (partitioned by day on observed_at)
                       (session_id, observed_at)
                        |  pilot_position_YYYYMMDD ... each inherits the FK
                        `-- views: pilot_latest, online_now, pilot_sample

   controller_session  (cid, callsign, logon_time) unique
        |  FK, no cascade            |  FK, no cascade
   controller_rating_event      controller_frequency_event
   (session_id, observed_at)    (session_id, observed_at)

   Stand-alone (no foreign key):  network_snapshot, poll_log, controller_unlisted_callsign,
                                  reference_data_change, app_setting, schema_migrations,
                                  flight_summary (the finished flights; names its sessions in session_ids; no foreign key)

   Joined by VALUE (no foreign key), see 1.3:
     pilot_session.aircraft_icao      -> aircraft_type.icao        (via flight_ext)
     flight.callsign (3 letters)      -> airline.prefix            (via flight_ext)
     pilot_session.home_icao          -> region_map.prefix         (via icao_region)
     pilot_session.pilot_rating       -> pilot_rating.id
     controller_session.rating        -> controller_rating.id
     controller_session.facility      -> facility.id

   flight_summary --> flight --> flight_ext --> movements, cohorts   (and flight --> monday_attendance)
   controller_session --> controller_session_merged --> controller_session_ranked
                          --> controller_summaries
```

### 1.2 Foreign keys

There are exactly three declared foreign keys. There are no foreign keys to or from the lookup tables.

| Child column | Parent column | On delete | Constraint |
|---|---|---|---|
| `pilot_position.session_id` | `pilot_session.id` | no action (a session that has positions cannot be deleted) | `pilot_position_session_id_fkey` |
| `controller_rating_event.session_id` | `controller_session.id` | no action | `controller_rating_event_session_id_fkey` |
| `controller_frequency_event.session_id` | `controller_session.id` | no action | `controller_frequency_event_session_id_fkey` |

The daily partitions of `pilot_position` **inherit** the foreign key: each `pilot_position_YYYYMMDD` carries its own clone of `pilot_position_session_id_fkey` (the live catalogue shows one per partition). Nothing needs to be declared when a partition is created.

### 1.3 Joins by value (no foreign key)

| From | To | Rule |
|---|---|---|
| `pilot_session.aircraft_icao` | `aircraft_type.icao` | Exact match after `norm_code()`, in `flight_ext`; no match gives a NULL category and `is_military` false. |
| `flight.callsign` | `airline.prefix` | Only when the callsign matches `^[A-Z]{3}[0-9]`; the prefix is its first three letters (`airline_prefix` in `flight_ext`). |
| `pilot_session.home_icao` | `region_map.prefix` | `icao_region()`: the longest prefix that the normalised code (`norm_code()`) starts with; `Unknown` otherwise (also for a blank code). |
| `pilot_session.departure` / `arrival` | home region | `icao_is_home()`: the normalised code (`norm_code()`) starts with any prefix in `home_prefix` (setting `home_icao_prefixes`, default `Y`). |
| `controller_session.rating` | `controller_rating.id` | Unknown ids show as Unknown/Other on the dashboards (no foreign key, on purpose). |
| `controller_session.facility` | `facility.id` | As above. |
| `pilot_session.pilot_rating` | `pilot_rating.id` | The feed value is a bit-flag; only 0, 1, 3, 7, 15, 31, 63 are seeded. |
| sessions of one flight | `flight_summary` | **Join rule** (v1; ADR-030, which supersedes the merge rule of ADR-023): sessions with the same `(cid, callsign, departure, arrival)` (departure and arrival normalised upper case) belong to one flight when the later session's `logon_time` is no more than 120 minutes after the latest last position of the earlier ones. A connection is never cut inside (no gap or 8-hour split). A changed plan (different airports) is a different flight. Applied once, by `write_finished_flights`, when the flight is finished; not at read time. |
| `controller_session` reconnects | `controller_session_merged` | Same `(cid, callsign)`; a new merged session starts when `first_seen` is more than 5 minutes after the latest earlier `last_seen`. |

### 1.4 Keys at a glance

| Table | Primary key | Other unique key |
|---|---|---|
| `pilot_session` | `id` | `(cid, callsign, logon_time)` (`pilot_session_identity`) |
| `pilot_position` and each partition | `(session_id, observed_at)` | |
| `flight_summary` | `(session_id, start_time)` | `session_id` alone is not unique: a session that comes back after being processed starts a second flight |
| `controller_session` | `id` | `(cid, callsign, logon_time)` (`controller_session_identity`) |
| `controller_rating_event`, `controller_frequency_event` | `(session_id, observed_at)` | |
| `network_snapshot` | `observed_at` | |
| `poll_log` | `id` | |
| `controller_unlisted_callsign` | `callsign` | |
| `reference_data_change` | `id` | |
| `app_setting` | `key` | |
| `controller_rating`, `facility`, `pilot_rating` | `id` | |
| `airline`, `region_map` | `prefix` | |
| `aircraft_type` | `icao` | |
| `schema_migrations` | `version` | |

### 1.5 Partitions and retention

`pilot_position` is `PARTITION BY RANGE (observed_at)` with one partition per UTC day, named `pilot_position_YYYYMMDD` (bounds `00:00:00+00` to the next midnight). The primary key includes the partition key, as PostgreSQL requires.

- **Created** by `ensure_pilot_position_partitions(days_ahead, p_today)`: yesterday through today + `days_ahead` (default 7). The collector calls it at start-up and daily. Existing partitions are skipped. On 13-10-2026 there are 15 partitions (`pilot_position_20261006` to `pilot_position_20261020`).
- **Dropped** by `drop_old_pilot_position_partitions(retention_days, p_today)`: a partition is dropped when it ends on or before `today - retention_days` (collector setting `POSITION_RETENTION_DAYS`; the compose literal and the code default are both 180, so the first partition, `pilot_position_20261006`, can first be dropped about 05-04-2027), unless it still holds positions of a session whose `last_seen` is inside the window (the partition is "pinned", and a notice is raised once it is pinned beyond twice the retention). **Drop gate (migration 20261013000210, issue 89 phase 4, LIVE since 13-10-2026, marker 7a71d30):** a partition that is due and not pinned is dropped only when every position in it is reconciled with the written flights: each session's `flown_upto` is at or after its last position in the day; each position of a session with a flight plan lies inside the `start_time` to `end_time` of a `flight_summary` row that lists the session in `session_ids` (a session without a plan needs only the first rule); and every flight wholly inside the day has `sample_count` equal to the positions stored for it. Anything else keeps the partition with a notice that starts `DROP REFUSED` (the collector logs it as a warning) and a row in `drop_gate_refusal`. Since migration 20261009000170 a call takes locks without waiting (`LOCK ... NOWAIT`, 1 second lock timeout, up to three attempts); from migration 20261113000190 (issue 202) a call walks the due days oldest first, passing over pinned and refused ones (each refused day is noted in `drop_gate_refusal`), stops at the first day that reconciles, repeats the whole check on that day under the lock and drops AT MOST ONE, because a DROP leaves `pilot_position` locked until the call ends and the in-lock re-check reads the whole day, so the collector loops with a 0.5 second pause between days (up to 600 per run); a partition that cannot be locked or changed meanwhile is kept with a notice and dropped by a later run; the collector calls the function until it drops nothing (issue 98). Flights therefore outlive the raw positions: the surviving flights are the rows of `flight_summary` (written once, 8 hours after the flight's last position). The lock-hold time of one real drop at live volume (about 78,000 positions a day) has not been measured; the owner accepted on 13-10-2026 (issue #140) that it is measured before the first real drop.
- `pilot_session`, `controller_session`, the event tables, `network_snapshot` and `flight_summary` are **not** trimmed (data retention for them is an open owner decision, issue 21). Only `poll_log` is trimmed, to 90 days, by the collector's maintenance task.

## 2. Base tables

Example values are illustrative.

### app_setting
Settings the SQL views read. The collector writes them at start-up and daily (`maintenance.sync_settings`), so the environment variables stay the single source. Primary key `key`.

| Column | Type | Null | Meaning |
|---|---|---|---|
| `key` | text | no | `home_icao_prefixes` (default `Y`, comma separated), `airborne_gap_cap_seconds`, `poll_interval_seconds`, `flight_quiet_hours`, `flight_quiet_check_grace_minutes`. |
| `value` | text | no | `Y`, `75`, `30`; `flight_quiet_hours` is `8` (seeded by migration 20261012000190) and `flight_quiet_check_grace_minutes` is `30` (seeded by migration 20261112000010). |

`airborne_gap_cap_seconds` is derived by the collector as 2.5 x the poll interval (minimum 15), so 75 at 30 s polling, and written at every start; it has no seed, so a new database has no row until the collector has started. `poll_interval_seconds` is what makes the "online" windows follow the cadence (ADR-024); until the collector has published it, those windows are empty. `flight_quiet_hours` (8) is the quiet period of `write_finished_flights`; the collector does not touch it. The owner changes it with `UPDATE app_setting SET value = '6' WHERE key = 'flight_quiet_hours';` (no migration or restart); the writer refuses a value under 2.5. A change never alters flights already written. `flight_quiet_check_grace_minutes` (30) is how long past the quiet period the watchdog waits before it reports a flight as overdue, so the writer's own 5-minute run interval is not mistaken for a stopped writer.

Every numeric setting is read through the function `setting_num(key)` (section 5). There is no built-in default: a missing row, or a value that is not a finite number, stops the reader with an error, so the flight writer stops and the watchdog alerts instead of the writer using a guessed number. The views `ops_setting` and `ops_pending_flight` (section 4.4) show the settings and the pending-flight rule to the watchdog.

### pilot_session
One row per pilot connection inside the polygon. Identity is `(cid, callsign, logon_time)`; flight-plan fields are attributes updated in place to the latest non-null value. Storage options `fillfactor=70`, `autovacuum_vacuum_scale_factor=0.02` (updated on every poll). Indexes: primary key on `id`; unique `pilot_session_identity (cid, callsign, logon_time)`; `pilot_session_last_seen_idx (last_seen)`; expression indexes `pilot_session_departure_code_idx (norm_code(departure))` and `pilot_session_arrival_code_idx (norm_code(arrival))` (for the flight gate; built CONCURRENTLY, so the collector keeps writing while they build).

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
| `departure`, `arrival` | text | yes | Filed airports, un-normalised (the views and functions normalise them with `norm_code()`). |
| `aircraft_icao`, `aircraft_faa` | text | yes | Filed aircraft type, for example `A388`. |
| `flight_rules`, `route`, `remarks`, `planned_alt`, `deptime` | text | yes | Flight plan as filed. |
| `plan_changed_at` | timestamptz | yes | Snapshot time when a non-null departure or arrival differed from the stored value (including the first time a plan appears on an existing session). |
| `max_gs_kt`, `max_alt_ft` | integer | yes | Maximum plausible groundspeed and altitude seen (GREATEST on each upsert). |
| `pilot_rating`, `military_rating` | integer | yes | Feed values. |
| `flown_upto` | timestamptz | yes | Added by migration 20261012000190 (issue 89 phase 1). Bookkeeping for `write_finished_flights`: this session's positions up to this time have been processed (written into `flight_summary`, or the session has no flight). NULL = never processed; `last_seen > flown_upto` = unprocessed positions. Set only for the sessions of a flight that has been written (or for a session that forms no flight once that session is itself quiet); a session of a flight that is still active keeps it unchanged. The collector does not touch it. Partial index `pilot_session_unwritten_idx (cid) WHERE flown_upto IS NULL OR last_seen > flown_upto` is the writer's work list. |

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

### drop_gate_refusal
Added by migration 20261013000210 (issue 89 phase 4, LIVE since 13-10-2026, marker 7a71d30; 0 rows when read on 13-10-2026). One row per day of `pilot_position` that `drop_old_pilot_position_partitions()` kept because it is not reconciled with the written flights; removed when the day reconciles or is dropped. Written only by that function; the watchdog role reads it (integrity check `raw-position days kept by the deletion check for more than 3 days`).

| Column | Type | Null | Meaning |
|---|---|---|---|
| `part_name` | text | no | Primary key, `pilot_position_YYYYMMDD`. |
| `first_refused`, `last_refused` | timestamptz | no | First and latest refusal. |
| `detail` | text | no | Count and reasons of the latest refusal. |

### flight_summary
Added by migration 20261012000190 (issue 89 phase 1) under a temporary name and renamed `flight_summary` by migration 20261013000300 (phase 5, live since 13-10-2026, marker 7a71d30). One row per **finished flight**, written once by `write_finished_flights()` and never changed. It is the only flight store: since migration 20261013000200 (phase 3, live since 13-10-2026 08:14:12 UTC) the `flight` view reads it, and so, through `flight`, do `flight_ext`, `movements`, `cohorts` and `monday_attendance`. It held 1797 rows (26 columns) on the live database on 13-10-2026 after the phase 5 deploy. A flight is one session, or several sessions of the same `cid`, `callsign`, `departure` and `arrival` joined across reconnects by v1's rule (a new connection joins when its `logon_time` is no more than 120 minutes after the latest last position of the earlier sessions; no 8-hour parts and no cut inside a connection). Written when **the flight itself** has had no position for `flight_quiet_hours` (default 8) of stored data (owner decision 12-10-2026, migration 20261012000200, live since 13-10-2026 08:01:09 UTC; see [DEFINITIONS.md](DEFINITIONS.md) section 1b). Plan and name fields are frozen at write time. Primary key `flight_summary_pkey (session_id, start_time)`; indexes `flight_summary_cid_idx`, `flight_summary_start_time_idx`, `flight_summary_end_time_idx`, `flight_summary_logon_time_idx`; CHECK `flight_summary_check` (`end_time >= start_time`); no foreign key. Not readable by `grafana_ro` or `collector`; readable by `backup_ro` and `watchdog_ro`.

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

**Imported v1 rows (ADR-035, accepted; loaded on 05-11-2026: 73,532 imported flights).** The one-off loader `ops/v1_import/load.sh` ([import-v1-history.md](RUNBOOKS/import-v1-history.md)) wrote finished flights from v1's `flight_summaries` straight into this table. They are told apart from live rows by the key: **`session_id` is negative** (the negative of the v1 row id; live ids come from a bigserial and are positive), and `session_ids` is `ARRAY[session_id]`. `WHERE session_id < 0` is the whole definition of "imported" (the rollback uses it). Column by column, for an imported row:

| Column | Value |
|---|---|
| `start_time` | v1 `completion_time` minus `time_online_minutes` minutes (v1 has no stored start). |
| `end_time` | v1 `completion_time` (v1's last stored position of the leg). Next to a gap in v1's own data this can be earlier than the true end ([DEFINITIONS.md](DEFINITIONS.md) section 1b.6). |
| `logon_time` | v1 `logon_time`, whole seconds. |
| `airborne_minutes` | `least(total_enroute_time_minutes, time_online_minutes)` from v1, whole minutes; 0 stays 0. NOT NULL for imported rows, unlike carried-over flights. See [DEFINITIONS.md](DEFINITIONS.md) section 3. |
| `max_gs_kt`, `max_alt_ft` | NULL (v1 summaries hold neither). |
| `sample_count` | 0. |
| `remarks` | NULL (v1 summaries hold none), so `has_remarks` and `has_selcal` are false for all imported flights. |
| `aircraft_icao` | v1 `aircraft_type`, upper-cased and trimmed. |
| `home_icao` | Parsed from `name` by the collector's rule (a trailing four-capital token). |
| Other columns | Copied from v1 with the collector's text rules (tab and newline to a space, control characters dropped, blank to NULL, capped at 2000 characters; callsign, departure and arrival also 500 bytes). |

Imported rows are in the range flights completed 24-08-2025 to 06-03-2026. Because they have no speed or altitude, panels that need `max_gs_kt` or `max_alt_ft` leave them out. How to tell the two kinds apart in a query: `sample_count = 0 AND max_gs_kt IS NULL AND session_id < 0`.

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

**Imported v1 rows (ADR-035, accepted; loaded on 05-11-2026: 11,270 imported sessions).** The same loader wrote controller sessions from v1's `controller_summaries`. They are told apart by **`id` being negative** (the negative of the v1 row id; live ids are positive). For an imported row: `logon_time` and `first_seen` are v1 `session_start_time` truncated to whole seconds; `last_seen` is v1 `session_end_time`; `frequency_hz` is the first element of v1's `frequencies_used` that is usable (MHz times 1,000,000 as a whole number; `199.998` and junk are skipped; NULL when none is usable, about 700 sessions); `visual_range` is NULL; `rating`, `facility`, `server` and `name` are copied. Only callsigns on the controller whitelist are loaded. The two event tables (`controller_rating_event`, `controller_frequency_event`) get no rows for imported sessions. Range: sessions started 24-08-2025 to 15-03-2026. v1 holds duplicates from reconnects; `controller_session_merged` combines them under the usual 5 minute rule.

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
One row per poll attempt, including failures. Trimmed to 90 days by maintenance. Primary key `id`; index `poll_log_polled_at_idx (polled_at)`.

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

### controller_unlisted_callsign
Unknown controller callsigns (see DEFINITIONS). Primary key `callsign`. Columns: `callsign` text no; `cid` bigint yes; `first_seen`, `last_seen` timestamptz no; `polls_seen` bigint no, default 1. A temporary view, `controller_unlisted_sighting`, carries the previous table name (with one extra column, `xmax`) so that a collector or watchdog of the previous release keeps working during a deploy; nothing current reads it, and no role but `collector` and `watchdog_ro` has a right on it.

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

There are 22 views on the live database (read on 13-10-2026 after the phase 5 deploy, migration 20261013000300, and again on 05-11-2026) and no materialised views; migration 20261112000010 adds two more, `ops_setting` and `ops_pending_flight`, for the watchdog only. Dashboards use the 13 listed in section 0. Views are created in migration 4 (core), 10 (analytics), 20 (`flight` over the first flight table), 24 (wall-clock guard on `online_now`), 30 and 31 (`controller_summaries`), 80 (`pilot_sample`), 100 (whole-second `logon_time`), 110 (the per-piece view and the merged `flight`), 120 (`poll_cadence`, cadence-driven windows), 130 (`flight.is_in_progress`) and 140 (`flight_ext` excludes in-progress flights); migration 20261107000010 (issue 285) re-created `pilot_latest` with one row per flight; migration 90 dropped the unused `controller_activity_summary`; migration 150 is a comment change only; migration 200 (issue 89 phase 3, live since 13-10-2026) renamed the merging `flight` to a comparison-only view, created the thin `flight` over the finished flights, and re-created `flight_ext` and `monday_attendance` over it; migration 300 (phase 5, live since 13-10-2026) dropped the comparison-only view and the per-piece view. Only `flight`, `flight_ext`, `movements`, `cohorts`, `monday_attendance`, `controller_summaries` and `poll_cadence` carry a database comment (`COMMENT ON VIEW`, 7 of the 22, read on 13-10-2026); this section is the description for the rest.

There are **two "online" definitions**, both deliberate:

- `online_now` and `controller_session_merged.is_open` use **2 x the poll interval** (`app_setting.poll_interval_seconds`, so 60 s at 30 s polling) before the newest stored snapshot. `online_now` is empty unless that snapshot is itself under 5 minutes old.
- `pilot_latest.is_online` and `controller_online_now` use **the two newest stored snapshots** (seen at or after the older of the two) and also require `last_seen` within 5 minutes of `now()`.

At the current cadence they agree. Until 13-10-2026 the merging `flight` view had an `is_in_progress` column that used the first definition; the thin `flight` view has no such column.

### 4.1 Flights

| View | Reads | What it returns and the rules |
|---|---|---|
| `flight` (migration 20261013000200; phase 3 of issue 89, LIVE since 13-10-2026 08:14:12 UTC) | `flight_summary`, `home_prefix` | A **thin view over `flight_summary`**: one row per finished flight, no merge at read time, no read of `pilot_session` or `pilot_position`. 29 columns (the merging view before it had 32, including `gap_segment`, `span_part` and `is_in_progress`), with the same types: `session_id` (the flight's first session), `cid, callsign, logon_time, name, home_icao, server, departure, arrival, aircraft_icao, aircraft_faa, flight_rules, route, remarks, planned_alt, deptime, pilot_rating, military_rating, start_time, end_time, duration, duration_minutes` (one decimal)`, airborne_minutes, max_gs_kt, max_alt_ft, sample_count, is_circuit, is_zzzz, touches_home_region` (true when the normalised departure or arrival starts with a prefix in `home_prefix`: the same home prefixes and rule as `dep_is_home` and `arr_is_home` in `flight_ext`; setting `home_icao_prefixes`). Plan fields are frozen when the flight is written. Unique per `(session_id, start_time)`. A flight appears about 8 hours after its pilot's last connection ends (`flight_quiet_hours`), so nothing is shown in progress. |
| `flight_ext` | `flight`, `airline`, `aircraft_type`, `icao_is_home`, `icao_region` | Derived fields over `flight`. Every row of `flight` is a finished flight, so there is no in-progress filter (removed by migration 20261013000200; ADR-025 as superseded by ADR-030). 31 columns: `session_id, cid, callsign, name, home_icao, server, departure, arrival, route_label` (`departure-arrival`)`, aircraft_icao` (upper, trimmed)`, flight_rules, remarks, start_time, end_time, duration_minutes` (unrounded)`, airborne_minutes, max_gs_kt, max_alt_ft, is_circuit, is_zzzz, dep_is_home, arr_is_home, pilot_is_local, pilot_region, cid_group` (`cid / 100000`)`, airline_prefix, airline_name, aircraft_category, is_military, has_selcal` (remarks contain `SEL/`)`, has_remarks`. |
| `movements` | `flight_ext` | Departures (at `start_time`) and arrivals (at `end_time`) at home-region airports, `ZZZZ` excluded. Counts every completed flight-gate session, as v1 does (ADR-018, issue 74): parked sessions with no airborne time and circuits count as one departure and one arrival. A flight still connected contributes neither, because it is not written yet. Columns: `airport, movement_type` (`departure` or `arrival`)`, movement_time, session_id, cid, callsign`. |
| `cohorts` | `flight_ext` | Completed flights grouped by joining cohort: `cid_group = cid / 100000`, label such as `1000000-1099999`; group 0 excluded. Columns: `session_id, cid, cid_group, cohort, start_time, end_time`. |
| `monday_attendance` | `flight`, `controller_session_merged`, `data_since` | Per Monday (UTC) from the week of go-live, distinct pilot CIDs (finished `flight` rows with `start_time` before 12:00Z and `end_time` at or after 07:00Z, the standard rule, so a flight joined across a reconnect covers the gap and the Monday is complete about 8 hours after the last pilot in the window disconnects; before migration 20261013000200 the rule counted per-piece rows) and distinct controller CIDs (merged sessions overlapping the window) present between 07:00Z and 12:00Z. Columns: `monday_date, win_start, win_end, pilot_cids, controller_cids`. |

### 4.2 Controllers

| View | Reads | What it returns and the rules |
|---|---|---|
| `controller_session_merged` | `controller_session`, `network_snapshot`, `app_setting` | Reconnects merged per `(cid, callsign)` (a gap over 5 minutes starts a new group). Columns: `cid, callsign, merge_group, first_session_id, session_count, name, rating, facility, server, frequency_hz` (latest values)`, start_time` (earliest `logon_time`)`, end_time` (last sighting)`, duration, duration_hours, is_open` (`end_time` within 2 x poll interval of the newest snapshot). |
| `controller_session_ranked` | `controller_session_merged`, `rank_group_of` | Adds `rank_group` and renames the times. Columns: `cid, callsign, name, rating, rank_group, facility, server, session_start, session_end, duration_minutes, is_ctr_fss` (callsign ends `_CTR` or `_FSS`). |
| `controller_summaries` | `controller_session_ranked` | v1-compatible shape so the recovered v1 retention SQL runs unchanged (migrations 30, 31). Columns: `cid, name, rating, session_start_time, session_duration_minutes, callsign, session_end_time`. One row per merged session (v1 counted a quick reconnect twice). Read by `atc/atc-retention.json` and the online-hours panels (cards 94 and 95). |
| `controller_online_now` | `controller_session`, `network_snapshot` | Controllers seen in the two newest stored snapshots and within 5 minutes of now. Columns: `session_id, cid, callsign, name, rating, rank_group, facility, server, frequency_hz, frequency_mhz, logon_time, last_seen, hours_online`. |

### 4.3 Live pilots

| View | Reads | What it returns and the rules |
|---|---|---|
| `online_now` | `network_snapshot`, `pilot_session`, `pilot_position`, `controller_session`, `app_setting` | Pilots and controllers seen within 2 x the poll interval of the newest stored snapshot (`as_of`), only while that snapshot is under 5 minutes old (otherwise empty). The pilot's newest position is a `LEFT JOIN LATERAL ... LIMIT 1` probe on the primary key (one probe per online pilot, so the view stays fast on many daily partitions). Columns: `kind` (`pilot` or `controller`), `session_id, cid, callsign, name, logon_time, first_seen, last_seen, as_of`, pilot fields `departure, arrival, aircraft_icao, lat, lon, alt_ft, gs_kt` (NULL for controllers), controller fields `rating, facility, frequency_hz` (NULL for pilots). |
| `pilot_latest` | `pilot_session`, `pilot_position`, `network_snapshot` | Latest position of each **flight** seen in the last 3 hours: one row per `(cid, callsign, departure)` (`departure`, `arrival` and `aircraft_icao` are `norm_code()` values: NULL when blank), the most recent logon only (highest `last_seen`, ties by highest `session_id`; migration 20261107000010, issue 285, not yet deployed; before it the view had one row per logon, so a reconnect or relog listed the flight twice). A round trip under one callsign keeps both legs; the planned departure time is not in the key, so a refiled plan still collapses. Edge case: the rule ranks only sessions that have a position in the 3 hour window, so if the newest logon has none yet, the older logon still shows. Sessions with no position in that window are absent. Normalised departure, arrival and aircraft, and `is_online`. Columns (unchanged by that migration): `session_id, cid, callsign, name, home_icao, server, departure, arrival, aircraft_icao, flight_rules, deptime, logon_time, last_seen, position_time, lat, lon, alt_ft, gs_kt, is_online`. |
| `pilot_online_now` | `pilot_latest`, `icao_region` | `pilot_latest` where `is_online`, plus `region` from `icao_region(home_icao)`. |
| `pilot_sample` | `pilot_position` | A plain column subset of `pilot_position`: `observed_at, session_id, alt_ft, gs_kt`. No filter, aggregation or join, so a predicate on `observed_at` still prunes the daily partitions. It exists because `grafana_ro` has no base-table access and card 156 (airborne aircraft per 5-minute slot during the latest Monday event) needed raw altitude samples. No panel reads it since 08-11-2026 (issue 324: card 156 is now a bar chart per Monday from the flight summary); the view stays. Carries no sector or frequency logic (ADR-002). Retention is that of `pilot_position`; `alt_ft` and `gs_kt` are NULL where ingest rejected the value. |

### 4.4 Collector and data health

| View | Reads | What it returns |
|---|---|---|
| `data_freshness` | `network_snapshot`, `poll_log` | One row: `checked_at, latest_snapshot_ts, snapshot_age_seconds, data_since, last_ok_poll_at, last_ok_age_seconds, ok_last_hour, skipped_last_hour, error_last_hour`. |
| `data_since` | `network_snapshot` | One row: `go_live` = earliest `observed_at` (07-10-2026 21:38 UTC on the live database). |
| `monitor_freshness` | `network_snapshot`, `pilot_session`, `controller_session`, `poll_log` | Four rows `source, newest_at, age_seconds` for `network_snapshot`, `pilot_session`, `controller_session` and `poll_log_ok`. |
| `network_history` | `network_snapshot` | The table as a view (time series of in-region and global counts): `observed_at, pilots_in_region, controllers_in_region, pilots_global, controllers_global`. |
| `poll_history` | `poll_log` | `poll_log` without the two SHA columns: `id, polled_at, status, snapshot_ts, duration_ms, http_status, pilots_seen, pilots_inside, pilots_with_plan, pilots_kept, controllers_seen, controllers_kept, rejected_values, error`. |
| `poll_cadence` | `app_setting` | One row `poll_interval_s` numeric: the collector poll interval published on start (migration 120); the monitoring panel and soak report read it because `grafana_ro` sees views only. |
| `home_prefix` | `app_setting` | The configured home ICAO prefixes, one row each, column `prefix`, each normalised with `norm_code()` (default `Y` if the setting is missing). |
| `ops_setting` | `app_setting` (through `ops_setting_num`) | Two columns, `key` and `value`, for `flight_quiet_hours` (hours), `airborne_gap_cap_seconds` (seconds) and `flight_quiet_check_grace_minutes` (minutes). A missing or non-numeric row raises an error. For the watchdog role only (`watchdog_ro`); `grafana_ro` has no access, as for every view named `ops_*`. |
| `ops_pending_flight` | `pilot_session`, `network_snapshot`, `ops_setting_num` | The one definition of the pending-flight rule: one row per flight made of unprocessed sessions (the v1 join rule; a session without a plan is its own row), with `cid, callsign, departure, arrival, session_count, newest_seen` and two flags. `is_quiet` is true when `newest_seen` is `flight_quiet_hours` or more behind the newest stored snapshot (capped at now plus 5 minutes), the writer's rule; `is_overdue` adds `flight_quiet_check_grace_minutes`, which is what the watchdog check reads. With no snapshot nothing is quiet. The audit queries and `ops/backfill_flights.sh` read it instead of keeping their own copy. For the watchdog role only. |

## 5. Functions

| Function | Returns | Security | Purpose |
|---|---|---|---|
| `ensure_pilot_position_partitions(days_ahead int DEFAULT 7, p_today date DEFAULT NULL)` | int | DEFINER | Creates daily partitions for yesterday through today + `days_ahead` (UTC); returns the number created. `days_ahead` must be at least 0. Run by the collector at start-up and daily. |
| `drop_old_pilot_position_partitions(retention_days int, p_today date DEFAULT NULL)` | int | DEFINER | Drops partitions older than the retention unless pinned by a recently seen session, and only when `unreconciled_partition_sessions` finds nothing for the day (migration 20261013000210, live since 13-10-2026). Migration 20261013000300 re-created it with only the table name changed. Walks the due days oldest first to the first one that is not pinned and reconciles (migration 20261113000190, issue 202; the pinned test starts from sessions seen inside the window), re-checks only that day under the lock and drops it; at most one per call (the collector repeats the call, with a pause), keeping any partition it cannot lock or that changed meanwhile (section 1.5). Returns the number dropped. `retention_days` must be at least 1. |
| `unreconciled_partition_sessions(p_start timestamptz, p_end timestamptz)` | table (session_id bigint, reason text) | DEFINER, STABLE | Added by migration 20261013000210 (issue 89 phase 4, live since 13-10-2026). Read-only. The sessions (and flights) that stop the day `[p_start, p_end)` of `pilot_position` from being dropped, each with a reason: `session row missing`, `positions not yet processed by the flight writer`, `positions not inside a written flight`, or `flight sample_count differs from its stored positions` (the last for flights wholly inside the day only). No rows means the day is reconciled. Set-based: one aggregate per session, one range semi-join against the flights overlapping the day. Not granted to any role (only the drop function and the owner's rehearsal, `ops/rehearse_drop_gate.sql`, call it). |
| `write_finished_flights(p_max_pilots int DEFAULT 500)` | int | DEFINER | Added by migration 20261012000190 (issue 89 phase 1). Writes each finished FLIGHT (sessions joined by the v1 rule) whose own newest `last_seen` is `app_setting.flight_quiet_hours` (read through `setting_num`, so a missing row raises; minimum 2.5, else it raises) or more behind the newest `network_snapshot` into `flight_summary`, then sets `pilot_session.flown_upto` for that flight's sessions only. Other flights of the same pilot, active or not, do not matter (quiet per flight; migration 20261012000200, live since 13-10-2026; the first version of 20261012000190 looked at the whole pilot). "Quiet" is measured against the newest `network_snapshot`, not the clock; an empty `network_snapshot` writes nothing. One writer at a time (a second caller does not wait, it returns 0 at once), one transaction, idempotent, examines at most `p_max_pilots` pilots per round (oldest unprocessed session first), and repeats up to 10 rounds with the pilots that made no progress left out, so flights held open by an active reconnect or a conflict cannot starve the rest. A pilot whose flight key already exists is skipped, left unmarked and reported in a WARNING. Returns the number of flights written. Granted EXECUTE to `collector` (migration 20261013000300), which calls it directly every 5 minutes with 50 pilots; a statement timeout is logged and the job halves the batch and retries; a pilot that times out alone is left out of the 5-minute job for 6 hours (the daily maintenance still tries it). Since migration 20261113000200 the call leaves out the pilots listed in the session setting `vatpac.flight_writer_skip` (a `bigint[]` literal; empty or unset means nobody) and each round raises a NOTICE naming the pilots it picked; with the setting empty the rows written are unchanged. |
| `setting_num(p_key text)` | numeric | invoker, STABLE | Migration 20261112000010. The one reader of numeric settings in `app_setting`: returns the value, and raises an error when the row is missing or is not a finite number. There is no default. No role has EXECUTE; the DEFINER functions `write_finished_flights` and `ops_setting_num` call it as the owner. |
| `ops_setting_num(p_key text)` | numeric | DEFINER, STABLE | Migration 20261112000010. `setting_num` for `flight_quiet_hours`, `airborne_gap_cap_seconds` and `flight_quiet_check_grace_minutes` only; any other key is refused. It exists so the views `ops_setting` and `ops_pending_flight` work for `watchdog_ro`, which has no right on `app_setting`. EXECUTE is granted to `watchdog_ro` only. |
| `norm_code(x text)` | text | invoker, IMMUTABLE, PARALLEL SAFE | The one rule for an ICAO text code: strips spaces, tabs, carriage returns and line feeds from both ends, upper-cases, and returns NULL when nothing is left. Every view, function, index and script that normalises an airport, aircraft type or prefix calls it. EXECUTE stays with PUBLIC (it only reads its argument), so `grafana_ro`, `collector` and `watchdog_ro` can call it. An index does not notice a change to a function body: never change this body without rebuilding the indexes built on it (`pilot_session_departure_code_idx`, `pilot_session_arrival_code_idx`). |
| `icao_is_home(code text)` | boolean | invoker, STABLE | True when `norm_code(code)` starts with a `home_prefix`. |
| `icao_region(code text)` | text | invoker, STABLE | Region from `region_map` by longest prefix that `norm_code(code)` starts with, else `Unknown`. |
| `rank_group_of(rating int)` | text | invoker, IMMUTABLE | 1 OBS, 2 S1, 3 S2, 4 S3, 5 C1, 6 C2, 7 C3, 8 or above `I1+`, else `Unknown`. |

DEFINER functions run with owner rights and `search_path = public`, so the collector needs no DDL rights (migration 21). Maintenance is driven from `collector/src/vatpac/maintenance.py`: partitions daily, finished flights every 5 minutes, `poll_log` trim to 90 days, settings sync at start-up and daily.

## 6. Roles and grants

| Role | Created by | Rights |
|---|---|---|
| `vatpac` (owner, from `POSTGRES_USER`) | Postgres image | Owns everything; used by `migrate` and, for restores only, by the `backup` service's restore scripts (ADR-017). Not used by the collector. |
| `collector` | `db/roles.sql` | Login. SELECT, INSERT, UPDATE on `pilot_session`, `pilot_position`, `controller_session`, `controller_rating_event`, `controller_frequency_event`, `network_snapshot`, `poll_log`, `controller_unlisted_callsign`, `reference_data_change`, `app_setting`, plus DELETE on `poll_log` only (the retention trim); USAGE on sequences; EXECUTE on `ensure_pilot_position_partitions`, `drop_old_pilot_position_partitions`, `write_finished_flights`. No DDL; no access to `flight_summary`, the lookups or the views. |
| `grafana_ro` | `db/roles.sql` | Login; connection limit 10; `default_transaction_read_only = on`, `statement_timeout = 30s`, `work_mem = 16MB`, `TimeZone = UTC` (T27). SELECT on every view and the six lookup tables; nothing else; cannot create. Re-granted on every deploy so new views are picked up. |
| `backup_ro` | `db/roles.sql` | Login; member of `pg_read_all_data`; read-only by default. Used by the nightly `pg_dump`. |
| `watchdog_ro` | `db/roles.sql` | Login; read-only by default; `statement_timeout = 10s`; member of `pg_monitor`; SELECT on `poll_log`, `network_snapshot`, `controller_unlisted_callsign`, `pilot_session` and `controller_session` (the last two for the identity-flap alert), and, from the integrity-checks deploy (T16; read on the VM on 13-10-2026: it can read all four), `pilot_position`, `flight_summary` (the finished flights), `drop_gate_refusal` and `reference_data_change` (for the scheduled invariant sweep and archive cross-check), SELECT on the views `ops_setting` and `ops_pending_flight` and EXECUTE on `ops_setting_num` (the flight settings and the pending-flight rule, without a right on `app_setting`). |

## 7. Known gaps

- The long-window and retention charts have no real history yet: there is no backfill (ADR-011), so 30 to 90 day measures say little until months after go-live (07-10-2026) (ADR-035 added v1 history for 24-08-2025 to 15-03-2026 on 05-11-2026, which leaves a gap with no data from 15-03-2026 to 07-10-2026; see [RUNBOOKS/import-v1-history.md](RUNBOOKS/import-v1-history.md)), and the retention rule that counts every controller as reactivated holds until 60 days of history exist. They are tested on synthetic rows (`tests/integration/test_controller_retention.py`).
- Only `poll_log` and raw `pilot_position` partitions have a retention. Sessions, events, snapshots and `flight_summary` grow without limit (owner decision pending, issue 21).
- Fifteen of the views have no `COMMENT ON VIEW`; section 4 is their only description. Adding comments is a comment-only migration.
- Two "online" definitions exist (section 4). They agree at 30 s polling but are separate expressions; a change of cadence logic must touch both.
- `rating`, `facility` and `pilot_rating` are not foreign keys, so an id outside the seeded lookups is stored and shows as Unknown/Other.
- The lookup seeds (airline 62 rows, aircraft types 534, regions 21) are a static snapshot of v1 lists, not a complete code list; unseen codes show as "Other".
- The schema was read from the live database and compared with the migrations by name, column and constraint; a clean apply of all 29 migrations to an empty database was not repeated for this review (the integration tests do that in CI).

## 8. How to keep this file current

Refresh it after any migration, or when the live database is changed.
1. Dump the live schema (read-only; the script sets `default_transaction_read_only = on`, only reads the catalogue, prints no secret). From your machine, in the repository root:
   ```sh
   ssh vatpac-v3 'cd ~/vatpac-statsV2 && docker compose exec -T db psql -U vatpac -d vatpac -P pager=off -A -F" | "' < ops/schema_dump.sql > schema_dump.txt
   ```
   The file is piped over ssh, so nothing is copied to the VM. `ops/schema_dump.sql` has twelve numbered sections: applied migrations; relations with comments; table columns; view columns; constraints; indexes; partitions; which relations each view reads; functions and grants; table and view grants; roles; settings and lookup sizes.
2. Check `schema_migrations` (section 1) against `ls db/migrations`. Migrations are applied in file-name order; if the VM is behind or ahead of the repository, say so in the dictionary header and report it.
3. Compare each section with the dictionary and edit the file. For view rules read the definition: `SELECT pg_get_viewdef('public.<view>'::regclass, true);` (same read-only session).
4. Update the dated line at the top ("Checked against ... up to <latest migration> on DD-MM-YYYY") and the object counts.
5. Run `PYTHONPATH=collector/src python -m pytest -q -m "not integration" tests/test_data_dictionary_tests.py`. It fails when a table, view or function created by a migration is missing from the dictionary, or when the header names a migration other than the latest.
6. Delete `schema_dump.txt` (it is not for git).

Do not run `db/roles.sql` or any migration to "check" the schema; both write.
