# Data dictionary

Last reviewed 08-10-2026 against the repository. Source: `db/migrations/*.sql` (15 migrations, `20261007000001` to `20261008000090`; applied by the `migrate` service with dbmate, tested on PostgreSQL 16.4) and `db/roles.sql`. All timestamps are `timestamptz` (UTC). Ids are `bigint`. Definitions of the derived measures are in [DEFINITIONS.md](DEFINITIONS.md).

Dashboards read **views and lookups only** through the role `grafana_ro`.

## 1. Base tables

### app_setting
Settings the SQL views read. The collector writes them at start-up and daily (`maintenance.sync_settings`), so the environment variables stay the single source.

| Column | Type | Notes |
|---|---|---|
| `key` | text, PK | `home_icao_prefixes` (default `Y`, comma separated), `airborne_gap_cap_seconds` (default `40`). |
| `value` | text, not null | |

### pilot_session
One row per pilot connection. Identity is `(cid, callsign, logon_time)`; flight-plan fields are attributes updated in place to the latest non-null value.

| Column | Meaning |
|---|---|
| `id` | PK (bigserial). |
| `cid`, `callsign`, `logon_time` | Identity; unique together (`pilot_session_identity`). `callsign` must be non-empty. |
| `name` | Name as in the feed (stored and shown, ADR-009). |
| `home_icao` | Trailing 4-letter uppercase token of `name` (for example a home airport); CHECK `^[A-Z]{4}$`; otherwise NULL. |
| `server` | VATSIM server name. |
| `first_seen`, `last_seen` | First and latest snapshot in which the pilot was inside the polygon. CHECK `last_seen >= first_seen`. Index on `last_seen`. |
| `departure`, `arrival`, `aircraft_icao`, `aircraft_faa`, `flight_rules`, `route`, `remarks`, `planned_alt`, `deptime` | Flight plan as filed (text, un-normalised). |
| `plan_changed_at` | Set to the snapshot time when a non-null departure or arrival differs from the stored value (this includes the first time a plan appears on an existing session). |
| `max_gs_kt`, `max_alt_ft` | Maximum plausible groundspeed and altitude seen (GREATEST on each upsert). |
| `pilot_rating`, `military_rating` | Feed values. |

### pilot_position
Raw 15-second samples. Partitioned by range on `observed_at`, daily partitions named `pilot_position_YYYYMMDD`.

| Column | Meaning |
|---|---|
| `observed_at` | Snapshot time (`general.update_timestamp`). |
| `session_id` | FK to `pilot_session.id`. |
| `lat`, `lon` | Position. |
| `alt_ft` | CHECK -1500..65000; out-of-range values are stored as NULL. |
| `gs_kt` | CHECK 0..700; out-of-range values are stored as NULL. |
| `hdg` | CHECK 0..360. |

Primary key `(session_id, observed_at)`. Retention 90 days (`POSITION_RETENTION_DAYS`) by dropping whole daily partitions.

### controller_session
One row per controller connection on a whitelisted callsign. Identity `(cid, callsign, logon_time)` (`controller_session_identity`).

| Column | Meaning |
|---|---|
| `id` | PK. |
| `cid`, `callsign`, `logon_time` | Identity. |
| `name`, `rating`, `facility`, `server`, `visual_range` | Feed values at the latest sighting (`rating` joins to `controller_rating.id`, `facility` to `facility.id`). |
| `frequency_hz` | Frequency in integer Hz (`121.700` MHz is `121700000`); NULL when the feed says `199.998`. |
| `first_seen`, `last_seen` | First and latest snapshot. CHECK `last_seen >= first_seen`. Index on `last_seen`. |

### controller_rating_event / controller_frequency_event
Insert-only history, written on the first sighting and whenever the value changes.

| Table | Columns | Key |
|---|---|---|
| `controller_rating_event` | `session_id` (FK), `observed_at`, `rating` | `(session_id, observed_at)` |
| `controller_frequency_event` | `session_id` (FK), `observed_at`, `frequency_hz` | `(session_id, observed_at)` |

### network_snapshot
One row per processed snapshot (`observed_at` is the PK). Written even when nothing is in the region.

| Column | Meaning |
|---|---|
| `pilots_in_region` | Pilots stored (inside the polygon). |
| `controllers_in_region` | Controllers stored (on the whitelist). |
| `pilots_global`, `controllers_global` | Totals in the whole feed. |

### poll_log
One row per poll attempt, including failures. Trimmed to 400 days by maintenance.

| Column | Meaning |
|---|---|
| `id`, `polled_at` | PK and wall-clock time of the attempt (default `now()`). Index on `polled_at`. |
| `status` | CHECK in `ok`, `skipped_unchanged`, `error`. |
| `snapshot_ts` | Feed timestamp, when one was read. |
| `duration_ms`, `http_status` | Timing and HTTP status of the fetch. |
| `pilots_seen` | Pilots in the whole feed. |
| `pilots_inside` | Pilots inside the polygon. |
| `pilots_with_plan` | Of those, pilots with non-empty departure and arrival (the flight gate, informational). |
| `pilots_kept` | Pilots actually written; equals `pilots_inside` unless a malformed record was skipped. |
| `controllers_seen` | Controllers in the whole feed. |
| `controllers_kept` | Controllers on the whitelist. |
| `rejected_values` | Speed/altitude values nulled by the plausibility caps. |
| `polygon_sha`, `whitelist_sha` | SHA-256 of the reference files in use. |
| `error` | Message (truncated to 2000 characters) for `error` rows. |

Count columns are NULL on rows that carry no snapshot result (`skipped_unchanged`, `error`).

### controller_unlisted_sighting
Near-miss controller callsigns (see DEFINITIONS). PK `callsign`; columns `cid`, `first_seen`, `last_seen`, `polls_seen` (default 1).

### reference_data_change
Audit of reference-file changes: `id`, `changed_at` (default `now()`), `file` (`australian_airspace_polygon.json` or `controller_callsigns_list.txt`), `old_sha`, `new_sha`. A row is inserted only when the hash differs from the last recorded one.

### flight_summary
Per-flight aggregates computed from `pilot_position` (migration 20, ADR-016). One row per `(session_id, gap_segment, span_part)`, primary key on those three columns; `session_id` references `pilot_session` with `ON DELETE CASCADE`. Maintained by `refresh_flight_summary()`, which the collector calls every 5 minutes for sessions seen in the last 12 hours; a row is marked `final` and finalised before its raw-position partition is dropped, so flights survive the 90-day position retention. Read through the `flight` view, never directly by Grafana.

| Column | Meaning |
|---|---|
| `session_id`, `gap_segment`, `span_part` | Identity: the session, the segment after splitting on gaps over 120 minutes, and the 8-hour bucket within the segment. |
| `cid` | Copied from the session. |
| `start_time`, `end_time`, `sample_count` | First and last stored sample, and the number of samples. Indexed on `start_time` and on `end_time`. |
| `airborne_minutes` | See [DEFINITIONS.md](DEFINITIONS.md), section 3. NULL for sessions whose raw positions were already gone when the table was introduced. |
| `max_gs_kt`, `max_alt_ft` | Maxima for this part. |
| `final` | True when the session is dead and fully summarised, so its raw partitions may be dropped. |
| `computed_at` | When the row was last recomputed. |

## 2. Lookup tables

Granted SELECT to `grafana_ro`. Seeded by migrations 3 and 11 (`ON CONFLICT DO NOTHING`; unknown codes show as "Other"/"Unknown" on the dashboards).

| Table | Key | Columns | Seed |
|---|---|---|---|
| `controller_rating` | `id` int | `short`, `long` | -1 INAC through 12 ADM (14 rows). |
| `facility` | `id` int | `short`, `long` | 0 OBS to 6 CTR (7 rows). |
| `pilot_rating` | `id` int | `short`, `long` | Bit-flag values 0, 1, 3, 7, 15, 31, 63. |
| `airline` | `prefix` text | `name` | Three-letter callsign prefixes (migration 11). |
| `aircraft_type` | `icao` text | `category`, `is_military` (default false) | Exact-match ICAO codes; military codes from the v1 lists (migration 11). |
| `region_map` | `prefix` text | `region` | ICAO prefix to region, used by `icao_region()` (migration 11). |

## 3. Functions

| Function | Returns | Purpose |
|---|---|---|
| `ensure_pilot_position_partitions(days_ahead int DEFAULT 7, p_today date DEFAULT NULL)` | int | Creates daily partitions for yesterday through today + `days_ahead` (UTC). Returns the number created. Run by the collector at start-up and daily (migration 21 made it `SECURITY DEFINER`). |
| `drop_old_pilot_position_partitions(retention_days int, p_today date DEFAULT NULL)` | int | Drops partitions that end before today minus `retention_days`, unless they hold positions of a session whose `last_seen` is inside the window. Returns the number dropped. `retention_days` must be at least 1. Before dropping, it finalises the flights of the partition's sessions in `flight_summary`; it keeps the partition if that fails (migration 21). |
| `refresh_flight_summary(p_since interval DEFAULT '12 hours')` | int | Recomputes `flight_summary` for every non-final session seen within `p_since` (NULL means every session on or after the oldest partition). Idempotent, `SECURITY DEFINER`, called by the collector every 5 minutes and by the maintenance task. Returns the rows changed. Its helper `refresh_flight_summary_sessions(bigint[], boolean)` is internal and not granted to the collector. |
| `refresh_materialized_views()` | int | Refreshes every materialized view in `public` (concurrently where possible); none exist today. `SECURITY DEFINER`. |
| `icao_is_home(code text)` | boolean | True when the code starts with any prefix in `home_icao_prefixes`. |
| `icao_region(code text)` | text | Region from `region_map` by longest matching prefix, else `Unknown`. |
| `rank_group_of(rating int)` | text | 1 OBS, 2 S1, 3 S2, 4 S3, 5 C1, 6 C2, 7 C3, 8 or above `I1+`, else `Unknown`. |

## 4. Views

Created in migration 4 (core) and migration 10 (analytics); migration 20 redefines `flight` and `monday_attendance` over `flight_summary`, migration 24 adds the wall-clock guard to `online_now`, migrations 30 and 31 add `controller_summaries`, migration 80 adds `pilot_sample`, and migration 90 drops the unused view `controller_activity_summary` (owner decision 08-10-2026). No materialized views exist at present (the maintenance task refreshes any that are added later).

The dashboards read these 17 relations and no others (checked by `python grafana/validate_dashboards.py`): `cohorts`, `controller_online_now`, `controller_session_ranked`, `controller_summaries`, `data_freshness`, `data_since`, `flight`, `flight_ext`, `monday_attendance`, `monitor_freshness`, `movements`, `near_miss_controllers`, `network_history`, `pilot_latest`, `pilot_online_now`, `pilot_sample`, `poll_history`. The validator refuses base tables. Views no panel reads directly, but which others build on or which remain for ad-hoc use: `controller_session_merged`, `online_now`, `home_prefix`, `controller_activity_daily`.

### Core (migration 4)

| View | Meaning and key columns |
|---|---|
| `flight` | One row per flight: a thin view over `flight_summary` joined to `pilot_session` (plan fields are live; the numbers lag by up to 5 minutes), with the flight gate applied. Columns: `session_id`, `gap_segment`, `span_part`, `cid`, `callsign`, `logon_time`, `name`, `home_icao`, `server`, `departure`, `arrival` (normalised: upper-case, trimmed), `aircraft_icao`, `aircraft_faa`, `flight_rules`, `route`, `remarks`, `planned_alt`, `deptime`, `pilot_rating`, `military_rating`, `start_time`, `end_time`, `duration`, `duration_minutes`, `airborne_minutes`, `max_gs_kt`, `max_alt_ft`, `sample_count`, `is_circuit`, `is_zzzz`, `touches_home_region`. Unique per `(session_id, gap_segment, span_part)`. |
| `controller_session_merged` | Controller sessions with reconnects merged. Columns: `cid`, `callsign`, `merge_group`, `first_session_id`, `session_count`, `name`, `rating`, `facility`, `server`, `frequency_hz` (latest values), `start_time` (earliest `logon_time`), `end_time` (last sighting), `duration`, `duration_hours`, `is_open`. |
| `online_now` | Pilots and controllers seen within 30 seconds of the newest stored snapshot (`as_of`), but only while that snapshot is itself under 5 minutes old (otherwise the view is empty). Columns: `kind` (`pilot` or `controller`), `session_id`, `cid`, `callsign`, `name`, `logon_time`, `first_seen`, `last_seen`, `as_of`, then pilot fields (`departure`, `arrival`, `aircraft_icao`, `lat`, `lon`, `alt_ft`, `gs_kt`) or controller fields (`rating`, `facility`, `frequency_hz`). Check `data_freshness` for how current the data is. |
| `data_freshness` | One row: `checked_at`, `latest_snapshot_ts`, `snapshot_age_seconds`, `data_since`, `last_ok_poll_at`, `last_ok_age_seconds`, `ok_last_hour`, `skipped_last_hour`, `error_last_hour`. |

### Analytics (migration 10)

| View | Meaning |
|---|---|
| `home_prefix` | The configured home prefixes, one row each (default `Y`). |
| `network_history` | `network_snapshot` as a view (time series of in-region and global counts). |
| `data_since` | One row: `go_live` = earliest `network_snapshot.observed_at`. |
| `poll_history` | `poll_log` without the SHA columns. |
| `near_miss_controllers` | `controller_unlisted_sighting` as a view. |
| `monitor_freshness` | Age in seconds of the newest record in `network_snapshot`, `pilot_session`, `controller_session` and the newest `ok` poll. |
| `pilot_latest` | Latest position of each pilot session seen in the last 3 hours, with `is_online`. |
| `pilot_online_now` | `pilot_latest` where `is_online`, plus `region` from `icao_region(home_icao)`. |
| `controller_online_now` | Controllers online now, with `rank_group`, `frequency_mhz`, `hours_online`. |
| `flight_ext` | `flight` plus derived fields: `route_label`, `dep_is_home`, `arr_is_home`, `pilot_is_local`, `pilot_region`, `cid_group`, `airline_prefix`, `airline_name`, `aircraft_category`, `is_military`, `has_selcal`, `has_remarks`. |
| `movements` | Departures (at `start_time`) and arrivals (at `end_time`) at home-region airports; ZZZZ excluded. Columns `airport`, `movement_type`, `movement_time`, `session_id`, `cid`, `callsign`. |
| `cohorts` | Flights grouped by joining cohort: `cid_group` = `cid / 100000`, label such as `1000000-1099999`; group 0 excluded. |
| `controller_session_ranked` | `controller_session_merged` with `rank_group`, `session_start`, `session_end`, `duration_minutes` and `is_ctr_fss` (callsign ends `_CTR` or `_FSS`). Columns: `cid`, `callsign`, `name`, `rating`, `rank_group`, `facility`, `server`, `session_start`, `session_end`, `duration_minutes`, `is_ctr_fss`. |
| `controller_summaries` | A v1-compatible view over `controller_session_ranked` so the recovered v1 Metabase SQL for the retention dashboard runs unchanged (migrations 30 and 31). Columns: `cid`, `name`, `rating`, `session_start_time`, `session_duration_minutes`, `callsign`, `session_end_time`. One row per merged controller session (a reconnect of the same CID and callsign within 5 minutes is one row; v1 counted such sessions twice). Read by `atc/atc-retention.json` and by the online-hours panels (cards 94 and 95) in `pilots/pilots-now.json`. |
| `pilot_sample` | A plain column-subset view over `pilot_position` (migration 80, 08-10-2026): `observed_at`, `session_id`, `alt_ft`, `gs_kt`. No filter, no aggregation and no join, so a predicate on `observed_at` still prunes the daily partitions. It exists because dashboards may read views only (`grafana_ro` has no base-table access) and card 156 (airborne aircraft per 5-minute slot during the latest Monday event, `events/monday-events.json`) needs raw altitude samples. It carries no sector or frequency logic (ADR-002). Retention is that of `pilot_position` (90 days); `alt_ft` and `gs_kt` are NULL where the collector rejected an out-of-range value. `grafana_ro` receives SELECT through the all-views grant in `db/roles.sql`. |
| `controller_activity_daily` | One row per controller per UTC day (session attributed to the day it started): `sessions`, `minutes`, `ctr_fss_minutes`, `rank_group`. |
| `monday_attendance` | Per Monday since go-live, distinct pilot and controller CIDs present between 07:00Z and 12:00Z. Columns: `monday_date`, `win_start`, `win_end`, `pilot_cids`, `controller_cids`. |

## 5. Roles

| Role | Created by | Rights |
|---|---|---|
| `vatpac` (owner, from `POSTGRES_USER`) | Postgres image | Owns everything; used by `migrate` and, for restores only, by the `backup` service's restore scripts (ADR-017). Not used by the collector. |
| `collector` | `db/roles.sql` | Login; SELECT, INSERT, UPDATE and DELETE on the tables it writes (`pilot_session`, `pilot_position`, `controller_session`, the two event tables, `network_snapshot`, `poll_log`, `controller_unlisted_sighting`, `reference_data_change`, `app_setting`); USAGE on sequences; EXECUTE on `ensure_pilot_position_partitions`, `drop_old_pilot_position_partitions`, `refresh_flight_summary` and `refresh_materialized_views`. No DDL, no access to `flight_summary` or the views. |
| `backup_ro` | `db/roles.sql` | Login; member of `pg_read_all_data`; read-only by default. Used by the nightly `pg_dump`. |
| `grafana_ro` | `db/roles.sql` | Login; connection limit 10; read-only by default; `statement_timeout` 30 s; session `timezone` pinned to UTC (`ALTER ROLE ... SET timezone`, T27); SELECT on all views and the six lookup tables; no base-table access; cannot create. |
| `watchdog_ro` | `ops/migrate/migrate.sh` | Login; read-only by default; `statement_timeout` 10 s; `pg_monitor`; SELECT on `poll_log`, `network_snapshot`, `controller_unlisted_sighting` only. |

## 6. Known gaps

- The long-window and retention charts have no real history yet: there is no backfill (ADR-011), so 30 to 90 day measures say little until months after go-live, and the retention rule that counts every controller as reactivated holds until 60 days of history exist. They are tested on synthetic rows (`tests/integration/test_controller_retention.py`).
