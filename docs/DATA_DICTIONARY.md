# Data dictionary

Last updated 07-10-2026. Source: `db/migrations/*.sql` (six migrations, all applied cleanly on PostgreSQL 16.4 on 07-10-2026) and `db/roles.sql`. All timestamps are `timestamptz` (UTC). Ids are `bigint`. Definitions of the derived measures are in [DEFINITIONS.md](DEFINITIONS.md).

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
| `pilots_kept` | Pilots stored (equals `pilots_inside`). |
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
| `ensure_pilot_position_partitions(days_ahead int DEFAULT 7, p_today date DEFAULT NULL)` | int | Creates daily partitions for yesterday through today + `days_ahead` (UTC). Returns the number created. Run once by migration 2 and by the collector at start-up and daily. |
| `drop_old_pilot_position_partitions(retention_days int, p_today date DEFAULT NULL)` | int | Drops partitions that end before today minus `retention_days`, unless they hold positions of a session whose `last_seen` is inside the window. Returns the number dropped. `retention_days` must be at least 1. |
| `icao_is_home(code text)` | boolean | True when the code starts with any prefix in `home_icao_prefixes`. |
| `icao_region(code text)` | text | Region from `region_map` by longest matching prefix, else `Unknown`. |
| `rank_group_of(rating int)` | text | 1 OBS, 2 S1, 3 S2, 4 S3, 5 C1, 6 C2, 7 C3, 8 or above `I1+`, else `Unknown`. |

## 4. Views

Created in migration 4 (core) and migration 10 (analytics). No materialized views exist at present (the maintenance task refreshes any that are added later).

### Core (migration 4)

| View | Meaning and key columns |
|---|---|
| `flight` | One row per flight: a pilot session after the gap/span split, with the flight gate applied. Columns: `session_id`, `gap_segment`, `span_part`, `cid`, `callsign`, `logon_time`, `name`, `home_icao`, `server`, `departure`, `arrival` (normalised: upper-case, trimmed), `aircraft_icao`, `aircraft_faa`, `flight_rules`, `route`, `remarks`, `planned_alt`, `deptime`, `pilot_rating`, `military_rating`, `start_time`, `end_time`, `duration`, `duration_minutes`, `airborne_minutes`, `max_gs_kt`, `max_alt_ft`, `sample_count`, `is_circuit`, `is_zzzz`, `touches_home_region`. Unique per `(session_id, gap_segment, span_part)`. |
| `controller_session_merged` | Controller sessions with reconnects merged. Columns: `cid`, `callsign`, `merge_group`, `first_session_id`, `session_count`, `name`, `rating`, `facility`, `server`, `frequency_hz` (latest values), `start_time` (earliest `logon_time`), `end_time` (last sighting), `duration`, `duration_hours`, `is_open`. |
| `online_now` | Pilots and controllers seen within 30 seconds of the newest stored snapshot (`as_of`). Columns: `kind` (`pilot` or `controller`), `session_id`, `cid`, `callsign`, `name`, `logon_time`, `first_seen`, `last_seen`, `as_of`, then pilot fields (`departure`, `arrival`, `aircraft_icao`, `lat`, `lon`, `alt_ft`, `gs_kt`) or controller fields (`rating`, `facility`, `frequency_hz`). Relative to the newest snapshot, so check `data_freshness` to know whether that is itself stale. |
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
| `controller_session_ranked` | `controller_session_merged` with `rank_group`, `duration_minutes`, `is_ctr_fss` (callsign ends `_CTR` or `_FSS`). |
| `controller_activity_daily` | One row per controller per UTC day (session attributed to the day it started): `sessions`, `minutes`, `ctr_fss_minutes`, `rank_group`. |
| `controller_activity_summary` | Per-controller summary behind the retention panels. **UNVERIFIED**: the definitions follow Metabase screenshots; the owner's Metabase export is still needed. |
| `monday_attendance` | Per Monday since go-live, distinct pilot and controller CIDs present between 07:00Z and 12:00Z. |

## 5. Roles

| Role | Created by | Rights |
|---|---|---|
| `vatpac` (owner, from `POSTGRES_USER`) | Postgres image | Owns everything; used by `migrate`, `collector`, `backup`. |
| `grafana_ro` | `db/roles.sql` | Login; read-only by default; `statement_timeout` 30 s; SELECT on all views and the six lookup tables; no base-table access; cannot create. |
| `watchdog_ro` | `ops/migrate/migrate.sh` | Login; read-only by default; `statement_timeout` 10 s; `pg_monitor`; SELECT on `poll_log`, `network_snapshot`, `controller_unlisted_sighting` only. |

## 6. Known gaps

- The long-window and retention charts (and `controller_activity_summary`) are not verified against real data; only minutes of data existed on 07-10-2026.
