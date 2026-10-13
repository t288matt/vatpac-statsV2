-- migrate:up
SET LOCAL lock_timeout = '5s';   -- fail fast instead of queueing behind a long panel query; the renames and replaces below take short ACCESS EXCLUSIVE locks on views
-- Issue #89 phase 3 (issue #139, design docs/DESIGN_89_FINAL.md sections 1.2, 4, 7.2 and 7.5): the readers move from the pieces
-- table to flight_record, the table write_finished_flights() fills once per finished flight. Views only: no table is touched,
-- nothing is dropped, the writer, the pieces refresh and flown_upto are untouched.
--
--   flight           re-created as a THIN view over flight_record (finished flights only; no merge, no join to pilot_session, no
--                    read of pilot_position or flight_summary at query time). The columns gap_segment, span_part and
--                    is_in_progress are gone: there are no pieces and nothing is shown in progress any more.
--   flight_legacy    the old merging view, kept under a new name until phase 5 for the old-against-new comparison
--                    (ops/compare_flights_89.sql, ops/compare_views_89_phase3.sql). NO panel may read it
--                    (grafana/validate_dashboards.py refuses it). The old view is RENAMED rather than dropped and copied, so its
--                    body, its dependencies and its columns (including is_in_progress) are exactly what they were.
--   flight_ext       same columns and definition, without WHERE NOT is_in_progress (the flights are finished by construction).
--   movements, cohorts  unchanged definitions; they read flight_ext, so they follow it. Only the comments change.
--   monday_attendance   pilot side: count(DISTINCT cid) of flight rows overlapping 07:00 to 12:00Z, by the standard rule (design
--                    section 4), instead of its own read of flight_summary. Controller side and columns unchanged.
--
-- Deploy order: phase 1 live, the writer's backfill finished and the phase 1 exit gate (#138, comparisons A and B) passed BEFORE
-- this migration, because from here on the charts show only what flight_record holds. Not yet deployed.
-- Forward-only: recover by a forward migration that restores the old definitions (nothing is deleted) or from the dump.

-- 1. Rename the old merging view. Its dependents (flight_ext, monday_attendance and through flight_ext movements and cohorts)
--    follow the rename until step 3 re-points them.
ALTER VIEW flight RENAME TO flight_legacy;
COMMENT ON VIEW flight_legacy IS
    'The merging flight view as it was before issue 89 phase 3 (pieces from flight_summary joined to pilot_session, merged across reconnects, with is_in_progress). Kept until phase 5 only to compare the old and the new readers; no panel may read it.';

-- 2. The thin view. Columns, order and types are the old ones minus gap_segment, span_part and is_in_progress, so flight_ext and
--    every panel that reads flight keep working. is_circuit, is_zzzz and touches_home_region are the old expressions; the home
--    prefixes are read live from app_setting, as before.
CREATE VIEW flight AS
WITH cfg AS (
    SELECT COALESCE(
               (SELECT array_agg(upper(trim(p)))
                FROM app_setting a, unnest(string_to_array(a.value, ',')) AS p
                WHERE a.key = 'home_icao_prefixes' AND trim(p) <> ''),
               ARRAY['Y']) AS home_prefixes
)
SELECT
    r.session_id,
    r.cid, r.callsign, r.logon_time, r.name, r.home_icao, r.server,
    r.departure, r.arrival,
    r.aircraft_icao, r.aircraft_faa, r.flight_rules, r.route, r.remarks, r.planned_alt, r.deptime,
    r.pilot_rating, r.military_rating,
    r.start_time, r.end_time,
    r.end_time - r.start_time AS duration,
    round(extract(epoch FROM r.end_time - r.start_time) / 60.0, 1) AS duration_minutes,
    r.airborne_minutes, r.max_gs_kt, r.max_alt_ft, r.sample_count,
    (r.departure = r.arrival) AS is_circuit,
    ('ZZZZ' IN (r.departure, r.arrival)) AS is_zzzz,
    EXISTS (
        SELECT 1 FROM cfg c, unnest(c.home_prefixes) AS hp
        WHERE left(r.departure, length(hp)) = hp OR left(r.arrival, length(hp)) = hp) AS touches_home_region
FROM flight_record r;
COMMENT ON VIEW flight IS
    'One row per finished flight, read from flight_record (issue 89 phase 3): written once by write_finished_flights() when the pilot has been quiet for flight_quiet_hours (default 8) of stored data, so a flight appears about 8 hours after the pilot''s last connection ends and nothing is shown in progress. Reconnects of one flight are already joined (v1 rule: same cid, callsign, departure and arrival, logon within 120 minutes of the previous last position). session_id is the id of the flight''s first session. Plan fields are frozen at write time.';

-- 3. flight_ext: the same view without the in-progress filter. CREATE OR REPLACE keeps the grants and re-points the view from
--    flight_legacy to the new flight; every output column has the same name, type and position.
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
COMMENT ON VIEW flight_ext IS
    'Finished flights, wide view over flight with the derived fields the charts need. Every row is a finished flight (flight_record is written only after the pilot has been quiet for 8 hours), so there is no in-progress filter (issue 89 phase 3; it replaced ADR-025''s mechanism).';
COMMENT ON VIEW movements IS
    'Departures (at flight start) and arrivals (at flight end) at home-region airports, of finished flights (rows of flight_record). Counts every flight-gate session, including sessions that never became airborne (airborne_minutes = 0).';
COMMENT ON VIEW cohorts IS
    'Joining cohorts (groups of 100,000 CIDs) over finished flights (rows of flight_record).';

-- 4. monday_attendance: the pilot side reads the standard flight view (design section 4): distinct CIDs of finished flights that
--    overlap 07:00 to 12:00Z. A flight joined across a reconnect covers the gap, so a pilot who reconnected during a flight is
--    present throughout. Same columns, same types, same controller side as before.
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
), pil AS (
  SELECT w.monday_date, count(DISTINCT f.cid) AS n
  FROM win w
  JOIN flight f ON f.start_time < w.win_end AND f.end_time >= w.win_start
  GROUP BY w.monday_date
), ctl AS (
  SELECT w.monday_date, count(DISTINCT c.cid) AS n
  FROM win w
  JOIN controller_session_merged c ON c.start_time < w.win_end AND c.end_time >= w.win_start
  GROUP BY w.monday_date
)
SELECT w.monday_date, w.win_start, w.win_end,
       COALESCE(p.n, 0) AS pilot_cids,
       COALESCE(c.n, 0) AS controller_cids
FROM win w
LEFT JOIN pil p ON p.monday_date = w.monday_date
LEFT JOIN ctl c ON c.monday_date = w.monday_date;
COMMENT ON VIEW monday_attendance IS
    'Distinct pilot CIDs (finished flights of flight_record overlapping 07:00 to 12:00 UTC, the same rule as every other flight count) and controller CIDs present on each Monday since go-live. A Monday is complete about 8 hours after the last pilot in the window disconnects.';

-- 5. A view created here has no grant of its own: db/roles.sql grants SELECT on every view to grafana_ro, and ops/migrate/migrate.sh
--    runs it right after the migrations. Grant now as well when the role already exists, so the window between the two steps (and
--    a manual `dbmate up`) cannot leave the dashboards unable to read `flight`. Same grant roles.sql gives; nothing new is exposed.
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro') THEN
        GRANT SELECT ON flight TO grafana_ro;       -- flight_legacy keeps the grant it had (a rename keeps privileges)
    END IF;
END
$do$;

-- migrate:down
-- Forward-only project: no automatic rollback. To go back, restore the pre-deploy dump (docs/RUNBOOKS/restore.md) or write a
-- forward migration that renames flight_legacy back and re-creates flight_ext and monday_attendance over it.
