-- Issue #89 phase 3 (#139): the five switched views, OLD against NEW, on the same live data. READ ONLY, ONE statement.
-- Run it AFTER migration 20261013000200 is deployed (it needs `flight_legacy`, the renamed old merging view), on the live database or,
-- better, on a restore of a live dump:
--   ( echo "SET default_transaction_read_only = on;"; cat ops/compare_views_89_phase3.sql ) | psql -X -v ON_ERROR_STOP=1 -d <database>
-- NOT YET DEPLOYED: it describes the phase 3 branch (p3-switch-views).
--
-- What is compared (design docs/DESIGN_89_FINAL.md section 7.5: "comparison (A) still passes against flight_legacy", and every
-- panel keeps its result):
--   flight      NEW `flight`      against OLD `flight_legacy` (finished flights only: NOT is_in_progress), 29 shared columns
--   flight_ext  NEW `flight_ext`  against the OLD definition of flight_ext (migration 20261009000140) evaluated over flight_legacy
--   movements   NEW `movements`   against the OLD definition (migration 20261007000010) over the old flight_ext
--   cohorts     NEW `cohorts`     against the OLD definition over the old flight_ext
--   monday      NEW `monday_attendance` against the OLD definition (migration 20261007000020, which read flight_summary and
--               pilot_session): compared Monday by Monday, for ALL pilots (the view cannot be scoped)
-- Scope for flight, flight_ext, movements and cohorts: pilots fully processed by the writer (no session with flown_upto NULL or
-- last_seen > flown_upto), exactly as ops/compare_flights_89.sql. The old side shows a pilot the writer has not written yet; the
-- new side does not, so only fully processed pilots can be compared.
--
-- Output: rows of (metric, value) in the order below. Every "only in" line must be 0 except for the EXPECTED differences of design
-- section 5.2, which the same pilots show here as in ops/compare_flights_89.sql: (1) a session over 8 hours is two or more old rows
-- and one new row, so flight, flight_ext, movements and cohorts differ for it in the same way (movements: 2 departures + 2 arrivals
-- become 1 + 1); (2) a silence of over 120 minutes inside one connection (none live so far); (3) a join decided by logon_time;
-- (4) plan fields frozen at write time (the old view reads them live); (5) airborne rounding of at most 0.01 per extra old piece.
-- Anything else is a bug: stop and report it on issue #139. The first 50 differing flight rows follow as JSON.
-- The Monday lines compare all pilots: a Monday that is not yet complete (pilots still unwritten, up to about 8 hours after the last
-- pilot in the window disconnects) reads LOWER in the new view; expect that on the latest Monday only.
WITH done AS (
    SELECT cid FROM pilot_session GROUP BY cid
    HAVING bool_and(flown_upto IS NOT NULL AND last_seen <= flown_upto)
),
old_flight AS (
    SELECT session_id, cid, callsign, logon_time, name, home_icao, server, departure, arrival, aircraft_icao, aircraft_faa,
           flight_rules, route, remarks, planned_alt, deptime, pilot_rating, military_rating, start_time, end_time, duration,
           duration_minutes, airborne_minutes, max_gs_kt, max_alt_ft, sample_count, is_circuit, is_zzzz, touches_home_region
      FROM flight_legacy
     WHERE NOT is_in_progress AND cid IN (SELECT cid FROM done)
),
new_flight AS (
    SELECT session_id, cid, callsign, logon_time, name, home_icao, server, departure, arrival, aircraft_icao, aircraft_faa,
           flight_rules, route, remarks, planned_alt, deptime, pilot_rating, military_rating, start_time, end_time, duration,
           duration_minutes, airborne_minutes, max_gs_kt, max_alt_ft, sample_count, is_circuit, is_zzzz, touches_home_region
      FROM flight
     WHERE cid IN (SELECT cid FROM done)
),
flight_only_old AS (SELECT * FROM old_flight EXCEPT ALL SELECT * FROM new_flight),
flight_only_new AS (SELECT * FROM new_flight EXCEPT ALL SELECT * FROM old_flight),
old_ext AS (                                      -- flight_ext exactly as migration 20261009000140 defined it, over the old view
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
      FROM flight_legacy f
      LEFT JOIN airline a ON a.prefix = CASE WHEN f.callsign ~ '^[A-Z]{3}[0-9]' THEN left(f.callsign, 3) END
      LEFT JOIN aircraft_type t ON t.icao = upper(btrim(f.aircraft_icao))
     WHERE NOT f.is_in_progress AND f.cid IN (SELECT cid FROM done)
),
new_ext AS (SELECT * FROM flight_ext WHERE cid IN (SELECT cid FROM done)),
ext_only_old AS (SELECT * FROM old_ext EXCEPT ALL SELECT * FROM new_ext),
ext_only_new AS (SELECT * FROM new_ext EXCEPT ALL SELECT * FROM old_ext),
old_mov AS (                                      -- movements as migration 20261007000010 defined it, over the old flight_ext
    SELECT departure AS airport, 'departure'::text AS movement_type, start_time AS movement_time, session_id, cid, callsign
      FROM old_ext WHERE dep_is_home AND NOT is_zzzz
    UNION ALL
    SELECT arrival, 'arrival', end_time, session_id, cid, callsign
      FROM old_ext WHERE arr_is_home AND NOT is_zzzz
),
new_mov AS (SELECT * FROM movements WHERE cid IN (SELECT cid FROM done)),
mov_only_old AS (SELECT * FROM old_mov EXCEPT ALL SELECT * FROM new_mov),
mov_only_new AS (SELECT * FROM new_mov EXCEPT ALL SELECT * FROM old_mov),
old_coh AS (
    SELECT session_id, cid, cid_group,
           (cid_group * 100000)::text || '-' || (cid_group * 100000 + 99999)::text AS cohort,
           start_time, end_time
      FROM old_ext WHERE cid_group > 0
),
new_coh AS (SELECT * FROM cohorts WHERE cid IN (SELECT cid FROM done)),
coh_only_old AS (SELECT * FROM old_coh EXCEPT ALL SELECT * FROM new_coh),
coh_only_new AS (SELECT * FROM new_coh EXCEPT ALL SELECT * FROM old_coh),
old_mon AS (                                      -- monday_attendance as migration 20261007000020 defined it (pieces + plan gate)
    WITH mondays AS (
        SELECT d::date AS monday_date
          FROM generate_series(date_trunc('week', (SELECT go_live FROM data_since) AT TIME ZONE 'UTC'),
                               date_trunc('week', now() AT TIME ZONE 'UTC'), interval '1 week') AS d
    ), win AS (
        SELECT monday_date,
               (monday_date + time '07:00') AT TIME ZONE 'UTC' AS win_start,
               (monday_date + time '12:00') AT TIME ZONE 'UTC' AS win_end
          FROM mondays
    ), pil AS (
        SELECT w.monday_date, count(DISTINCT fs.cid) AS n
          FROM win w
          JOIN flight_summary fs ON fs.start_time < w.win_end AND fs.end_time >= w.win_start
          JOIN pilot_session ps ON ps.id = fs.session_id
         WHERE btrim(ps.departure, E' \t\r\n') <> '' AND btrim(ps.arrival, E' \t\r\n') <> ''
         GROUP BY w.monday_date
    ), ctl AS (
        SELECT w.monday_date, count(DISTINCT c.cid) AS n
          FROM win w
          JOIN controller_session_merged c ON c.start_time < w.win_end AND c.end_time >= w.win_start
         GROUP BY w.monday_date
    )
    SELECT w.monday_date, w.win_start, w.win_end, COALESCE(p.n, 0) AS pilot_cids, COALESCE(c.n, 0) AS controller_cids
      FROM win w LEFT JOIN pil p ON p.monday_date = w.monday_date LEFT JOIN ctl c ON c.monday_date = w.monday_date
),
mon_diff AS (
    SELECT o.monday_date, o.pilot_cids AS old_pilot_cids, n.pilot_cids AS new_pilot_cids,
           o.controller_cids AS old_controller_cids, n.controller_cids AS new_controller_cids
      FROM old_mon o JOIN monday_attendance n USING (monday_date)
     WHERE (o.pilot_cids, o.controller_cids, o.win_start, o.win_end) IS DISTINCT FROM (n.pilot_cids, n.controller_cids, n.win_start, n.win_end)
)
SELECT ord, metric, value FROM (
    SELECT 1 AS ord, 'pilots fully processed (scope)'::text AS metric, (SELECT count(*) FROM done)::numeric AS value
    UNION ALL SELECT 2, 'flight: old rows (flight_legacy, finished)', (SELECT count(*) FROM old_flight)
    UNION ALL SELECT 3, 'flight: new rows (flight)', (SELECT count(*) FROM new_flight)
    UNION ALL SELECT 4, 'flight: rows only in old (EXCEPT ALL, 29 shared columns)', (SELECT count(*) FROM flight_only_old)
    UNION ALL SELECT 5, 'flight: rows only in new', (SELECT count(*) FROM flight_only_new)
    UNION ALL SELECT 6, 'flight_ext: old rows', (SELECT count(*) FROM old_ext)
    UNION ALL SELECT 7, 'flight_ext: new rows', (SELECT count(*) FROM new_ext)
    UNION ALL SELECT 8, 'flight_ext: rows only in old (EXCEPT ALL, 31 columns)', (SELECT count(*) FROM ext_only_old)
    UNION ALL SELECT 9, 'flight_ext: rows only in new', (SELECT count(*) FROM ext_only_new)
    UNION ALL SELECT 10, 'movements: old rows', (SELECT count(*) FROM old_mov)
    UNION ALL SELECT 11, 'movements: new rows', (SELECT count(*) FROM new_mov)
    UNION ALL SELECT 12, 'movements: rows only in old', (SELECT count(*) FROM mov_only_old)
    UNION ALL SELECT 13, 'movements: rows only in new', (SELECT count(*) FROM mov_only_new)
    UNION ALL SELECT 14, 'cohorts: old rows', (SELECT count(*) FROM old_coh)
    UNION ALL SELECT 15, 'cohorts: new rows', (SELECT count(*) FROM new_coh)
    UNION ALL SELECT 16, 'cohorts: rows only in old', (SELECT count(*) FROM coh_only_old)
    UNION ALL SELECT 17, 'cohorts: rows only in new', (SELECT count(*) FROM coh_only_new)
    UNION ALL SELECT 18, 'monday_attendance: Mondays compared (all pilots)', (SELECT count(*) FROM old_mon)
    UNION ALL SELECT 19, 'monday_attendance: Mondays whose pilot or controller count differs (expect 0, or the latest Monday while pilots are unwritten)', (SELECT count(*) FROM mon_diff)
    UNION ALL SELECT 20, 'monday_attendance: Mondays where NEW is HIGHER than old (expect 0; only a join decided by logon_time, expected difference 3, can do this)',
                         (SELECT count(*) FROM mon_diff WHERE new_pilot_cids > old_pilot_cids)
) m
UNION ALL
SELECT 100 + row_number() OVER (), 'flight only in old: ' || row_to_json(o)::text, 1 FROM (SELECT * FROM flight_only_old ORDER BY cid, start_time LIMIT 50) o
UNION ALL
SELECT 200 + row_number() OVER (), 'flight only in new: ' || row_to_json(n)::text, 1 FROM (SELECT * FROM flight_only_new ORDER BY cid, start_time LIMIT 50) n
UNION ALL
SELECT 300 + row_number() OVER (), 'monday differs: ' || row_to_json(d)::text, 1 FROM (SELECT * FROM mon_diff ORDER BY monday_date) d
ORDER BY 1;
