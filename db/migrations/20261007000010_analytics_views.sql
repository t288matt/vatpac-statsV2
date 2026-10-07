-- migrate:up
-- grafana/views.sql
-- Views and helper functions that the Grafana dashboards read. Written by the analytics agent for the
-- lead to turn into a migration (forward-only, idempotent: CREATE OR REPLACE throughout).
--
-- NOT executed against PostgreSQL by the analytics agent (no database was available); only parsed
-- with sqlglot (dialect postgres). Apply to a scratch database and run the integration test
-- "every provisioned panel query runs" before relying on it.
--
-- The migration must also GRANT SELECT on every object created here, and on `flight`,
-- `controller_session_merged`, `online_now`, `data_freshness` and the lookup tables, to the
-- read-only role `grafana_ro` (and EXECUTE on the two icao_* functions and rank_group_of, which
-- PostgreSQL grants to PUBLIC by default).
--
-- CONTRACT with the data-engineer's views. Checked against db/migrations/20261007000004_views.sql
-- (column names confirmed with grafana/check_columns.py; the 4 views there are not redefined here).
-- Everything else in this file is built on these columns, so a mismatch only needs fixing in
-- `flight_ext` and `controller_session_ranked`:
--   flight (one row per flight after the gap/span split and the flight gate):
--     session_id, cid, callsign, name, home_icao, server, departure, arrival (both normalised,
--     upper/trim), aircraft_icao, flight_rules, remarks, start_time, end_time, airborne_minutes,
--     max_gs_kt, max_alt_ft, is_circuit, is_zzzz
--   controller_session_merged (reconnects within 5 minutes merged):
--     cid, callsign, name, rating, facility, server, start_time, end_time
-- TABLE created by db/migrations/20261007000001_core_schema.sql (confirmed, not in SPEC section 6):
--   app_setting(key text PRIMARY KEY, value text), holding home_icao_prefixes = 'Y' (comma separated).
--   If it does not exist, replace the body of `home_prefix` with a constant list.
--
-- All times are timestamptz (UTC). Views that use now() are evaluated at query time.

-- ---------------------------------------------------------------------------------------------
-- Settings and helper functions
-- ---------------------------------------------------------------------------------------------

CREATE OR REPLACE VIEW home_prefix AS
SELECT upper(btrim(p)) AS prefix
FROM app_setting s
CROSS JOIN LATERAL regexp_split_to_table(s.value, ',') AS p
WHERE s.key = 'home_icao_prefixes'
  AND btrim(p) <> ''
UNION ALL
SELECT 'Y'::text
WHERE NOT EXISTS (SELECT 1 FROM app_setting WHERE key = 'home_icao_prefixes');

CREATE OR REPLACE FUNCTION icao_is_home(code text) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (
    SELECT 1 FROM home_prefix h WHERE starts_with(upper(btrim(code)), h.prefix)
  )
$$;

CREATE OR REPLACE FUNCTION icao_region(code text) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(
    (SELECT r.region
       FROM region_map r
      WHERE starts_with(upper(btrim(code)), r.prefix)
      ORDER BY length(r.prefix) DESC
      LIMIT 1),
    'Unknown')
$$;

-- Rating group used by the v1 charts (1 and 6 are shown as OBS and C2; v1 charts left them out).
CREATE OR REPLACE FUNCTION rank_group_of(rating int) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE
           WHEN rating = 1 THEN 'OBS'
           WHEN rating = 2 THEN 'S1'
           WHEN rating = 3 THEN 'S2'
           WHEN rating = 4 THEN 'S3'
           WHEN rating = 5 THEN 'C1'
           WHEN rating = 6 THEN 'C2'
           WHEN rating = 7 THEN 'C3'
           WHEN rating >= 8 THEN 'I1+'
           ELSE 'Unknown'
         END
$$;

-- ---------------------------------------------------------------------------------------------
-- Network and monitoring (thin views so grafana_ro never reads a base table)
-- ---------------------------------------------------------------------------------------------

CREATE OR REPLACE VIEW network_history AS
SELECT observed_at, pilots_in_region, controllers_in_region, pilots_global, controllers_global
FROM network_snapshot;

CREATE OR REPLACE VIEW data_since AS
SELECT min(observed_at) AS go_live
FROM network_snapshot;

CREATE OR REPLACE VIEW poll_history AS
SELECT id, polled_at, status, snapshot_ts, duration_ms, http_status,
       pilots_seen, pilots_inside, pilots_with_plan, pilots_kept,
       controllers_seen, controllers_kept, rejected_values, error
FROM poll_log;

CREATE OR REPLACE VIEW near_miss_controllers AS
SELECT callsign, cid, first_seen, last_seen, polls_seen
FROM controller_unlisted_sighting;

-- Age of the newest record in each source. Rows: network_snapshot, pilot_session,
-- controller_session, poll_log_ok.
CREATE OR REPLACE VIEW monitor_freshness AS
SELECT x.source, x.newest_at,
       extract(epoch FROM (now() - x.newest_at)) AS age_seconds
FROM (
  SELECT 'network_snapshot'::text AS source, max(observed_at) AS newest_at FROM network_snapshot
  UNION ALL
  SELECT 'pilot_session', max(last_seen) FROM pilot_session
  UNION ALL
  SELECT 'controller_session', max(last_seen) FROM controller_session
  UNION ALL
  SELECT 'poll_log_ok', max(polled_at) FROM poll_log WHERE status = 'ok'
) x;

-- ---------------------------------------------------------------------------------------------
-- Who is online now. "Online" = seen within the last two snapshots (SPEC section 5) and, as a
-- guard against a dead collector, within the last 5 minutes of wall-clock time.
-- ---------------------------------------------------------------------------------------------

-- Latest known position of every pilot session seen in the last 3 hours (used by the departure board).
CREATE OR REPLACE VIEW pilot_latest AS
SELECT ps.id AS session_id, ps.cid, ps.callsign, ps.name, ps.home_icao, ps.server,
       upper(btrim(ps.departure)) AS departure, upper(btrim(ps.arrival)) AS arrival,
       upper(btrim(ps.aircraft_icao)) AS aircraft_icao, ps.flight_rules, ps.deptime,
       ps.logon_time, ps.last_seen,
       p.observed_at AS position_time, p.lat, p.lon, p.alt_ft, p.gs_kt,
       (ps.last_seen >= (SELECT min(s.observed_at)
                           FROM (SELECT observed_at FROM network_snapshot
                                  ORDER BY observed_at DESC LIMIT 2) s)
        AND ps.last_seen >= now() - interval '5 minutes') AS is_online
FROM pilot_session ps
CROSS JOIN LATERAL (
  SELECT pp.observed_at, pp.lat, pp.lon, pp.alt_ft, pp.gs_kt
  FROM pilot_position pp
  WHERE pp.session_id = ps.id
    AND pp.observed_at >= now() - interval '3 hours'
  ORDER BY pp.observed_at DESC
  LIMIT 1
) p
WHERE ps.last_seen >= now() - interval '3 hours';

CREATE OR REPLACE VIEW pilot_online_now AS
SELECT l.*, icao_region(l.home_icao) AS region
FROM pilot_latest l
WHERE l.is_online;

CREATE OR REPLACE VIEW controller_online_now AS
SELECT cs.id AS session_id, cs.cid, cs.callsign, cs.name, cs.rating,
       rank_group_of(cs.rating) AS rank_group, cs.facility, cs.server,
       cs.frequency_hz, round(cs.frequency_hz / 1000000.0, 3) AS frequency_mhz,
       cs.logon_time, cs.last_seen,
       round(extract(epoch FROM (cs.last_seen - cs.logon_time)) / 3600.0, 2) AS hours_online
FROM controller_session cs
WHERE cs.last_seen >= (SELECT min(s.observed_at)
                         FROM (SELECT observed_at FROM network_snapshot
                                ORDER BY observed_at DESC LIMIT 2) s)
  AND cs.last_seen >= now() - interval '5 minutes';

-- ---------------------------------------------------------------------------------------------
-- Flights: one wide view over `flight` with the derived fields the charts need.
-- ---------------------------------------------------------------------------------------------

CREATE OR REPLACE VIEW flight_ext AS
SELECT f.session_id, f.cid, f.callsign, f.name, f.home_icao, f.server,
       f.departure, f.arrival,
       f.departure || '-' || f.arrival AS route_label,
       upper(btrim(f.aircraft_icao)) AS aircraft_icao,
       f.flight_rules, f.remarks,
       f.start_time, f.end_time,
       extract(epoch FROM (f.end_time - f.start_time)) / 60.0 AS duration_minutes,
       f.airborne_minutes, f.max_gs_kt, f.max_alt_ft,
       f.is_circuit, f.is_zzzz,
       icao_is_home(f.departure) AS dep_is_home,
       icao_is_home(f.arrival) AS arr_is_home,
       icao_is_home(f.home_icao) AS pilot_is_local,
       icao_region(f.home_icao) AS pilot_region,
       (f.cid / 100000)::int AS cid_group,
       CASE WHEN f.callsign ~ '^[A-Z]{3}[0-9]' THEN left(f.callsign, 3) END AS airline_prefix,
       a.name AS airline_name,
       t.category AS aircraft_category,
       COALESCE(t.is_military, false) AS is_military,
       (f.remarks ~* 'SEL/') AS has_selcal,
       (f.remarks IS NOT NULL AND btrim(f.remarks) <> '') AS has_remarks
FROM flight f
LEFT JOIN airline a
       ON a.prefix = CASE WHEN f.callsign ~ '^[A-Z]{3}[0-9]' THEN left(f.callsign, 3) END
LEFT JOIN aircraft_type t
       ON t.icao = upper(btrim(f.aircraft_icao));

-- Departures (at flight start) and arrivals (at flight end) at home-region airports.
CREATE OR REPLACE VIEW movements AS
SELECT departure AS airport, 'departure'::text AS movement_type, start_time AS movement_time,
       session_id, cid, callsign
FROM flight_ext
WHERE dep_is_home AND NOT is_zzzz
UNION ALL
SELECT arrival, 'arrival', end_time, session_id, cid, callsign
FROM flight_ext
WHERE arr_is_home AND NOT is_zzzz;

-- Joining cohorts: groups of 100,000 CIDs (group 0 excluded, as in v1).
CREATE OR REPLACE VIEW cohorts AS
SELECT session_id, cid, cid_group,
       (cid_group * 100000)::text || '-' || (cid_group * 100000 + 99999)::text AS cohort,
       start_time, end_time
FROM flight_ext
WHERE cid_group > 0;

-- ---------------------------------------------------------------------------------------------
-- Controllers
-- ---------------------------------------------------------------------------------------------

CREATE OR REPLACE VIEW controller_session_ranked AS
SELECT m.cid, m.callsign, m.name, m.rating,
       rank_group_of(m.rating) AS rank_group,
       m.facility, m.server,
       m.start_time AS session_start, m.end_time AS session_end,
       extract(epoch FROM (m.end_time - m.start_time)) / 60.0 AS duration_minutes,
       (m.callsign ~ '_(CTR|FSS)$') AS is_ctr_fss
FROM controller_session_merged m;

-- One row per controller per UTC day (a session is attributed to the day it started).
CREATE OR REPLACE VIEW controller_activity_daily AS
SELECT (session_start AT TIME ZONE 'UTC')::date AS activity_date,
       cid,
       (array_agg(name ORDER BY session_start DESC))[1] AS name,
       rank_group_of(max(rating)) AS rank_group,
       count(*) AS sessions,
       round(sum(duration_minutes)::numeric, 1) AS minutes,
       round(COALESCE(sum(duration_minutes) FILTER (WHERE is_ctr_fss), 0)::numeric, 1) AS ctr_fss_minutes
FROM controller_session_ranked
GROUP BY (session_start AT TIME ZONE 'UTC')::date, cid;

-- Per-controller summary behind the retention panels. UNVERIFIED: column meanings follow the
-- Metabase screenshots; the owner's Metabase export is still needed to confirm the definitions.
--   first_recent_session / last_recent_session / recent_sessions / hours_last_30d /
--   longest_session_minutes describe sessions that started in the last 30 days.
--   gap_days_before_recent = days between the end of the session before the first recent
--   session and the start of that first recent session (NULL if there is no earlier session).
CREATE OR REPLACE VIEW controller_activity_summary AS
WITH s AS (
  SELECT cid, name, rank_group, session_start, session_end, duration_minutes,
         lag(session_end) OVER (PARTITION BY cid ORDER BY session_start) AS prev_end
  FROM controller_session_ranked
), overall AS (
  SELECT cid,
         (array_agg(name ORDER BY session_start DESC))[1] AS name,
         (array_agg(rank_group ORDER BY session_start DESC))[1] AS rank_group,
         max(session_start) AS last_session,
         count(*) AS sessions_total
  FROM s
  GROUP BY cid
), recent AS (
  SELECT cid,
         count(*) AS recent_sessions,
         round((sum(duration_minutes) / 60.0)::numeric, 1) AS hours_last_30d,
         round(max(duration_minutes)::numeric, 0) AS longest_session_minutes,
         min(session_start) AS first_recent_session,
         max(session_start) AS last_recent_session,
         (array_agg(prev_end ORDER BY session_start))[1] AS prev_end_before_recent
  FROM s
  WHERE session_start >= now() - interval '30 days'
  GROUP BY cid
)
SELECT o.cid, o.name, o.rank_group, o.last_session, o.sessions_total,
       COALESCE(r.recent_sessions, 0) AS recent_sessions,
       COALESCE(r.hours_last_30d, 0) AS hours_last_30d,
       r.longest_session_minutes,
       r.first_recent_session, r.last_recent_session,
       CASE WHEN r.prev_end_before_recent IS NOT NULL
            THEN round((extract(epoch FROM (r.first_recent_session - r.prev_end_before_recent)) / 86400.0)::numeric, 1)
       END AS gap_days_before_recent
FROM overall o
LEFT JOIN recent r ON r.cid = o.cid;

-- ---------------------------------------------------------------------------------------------
-- Monday event: distinct CIDs present at any time between 0700Z and 1200Z on each Monday since go-live.
-- ---------------------------------------------------------------------------------------------

CREATE OR REPLACE VIEW monday_attendance AS
WITH mondays AS (
  SELECT d::date AS monday_date
  FROM generate_series(
         date_trunc('week', (SELECT go_live FROM data_since) AT TIME ZONE 'UTC'),
         date_trunc('week', now() AT TIME ZONE 'UTC'),
         interval '1 week') AS d
), win AS (
  SELECT monday_date,
         (monday_date + time '07:00') AT TIME ZONE 'UTC' AS win_start,
         (monday_date + time '12:00') AT TIME ZONE 'UTC' AS win_end
  FROM mondays
)
SELECT w.monday_date, w.win_start, w.win_end,
       (SELECT count(DISTINCT f.cid) FROM flight f
         WHERE f.start_time < w.win_end AND f.end_time >= w.win_start) AS pilot_cids,
       (SELECT count(DISTINCT c.cid) FROM controller_session_merged c
         WHERE c.start_time < w.win_end AND c.end_time >= w.win_start) AS controller_cids
FROM win w;

-- migrate:down
-- forward-only: views and helper functions are recreated with CREATE OR REPLACE
