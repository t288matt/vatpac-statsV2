-- NOTE 12-10-2026: this drill describes the drop gate BEFORE issue 89 phase 4 (it finalises the pieces table). Under the phase 4 gate (migration 20261013000210,
-- not yet deployed) nothing here is dropped until write_finished_flights() has processed the sessions; use ops/rehearse_drop_gate.sql instead.
-- Retention drill: a session wholly older than 90 days, one straddling the cutoff, one recent. Shows that dropping old daily
-- partitions keeps the flight rows and airborne minutes, and that a straddling session pins its partition until it ages out.
\set ON_ERROR_STOP on
-- THROWAWAY DATABASE ONLY. This file TRUNCATEs and INSERTs. It refuses to run unless the database name ends in t30b or _test.
DO $guard$ BEGIN
  IF current_database() !~ '(t30b|_test)$' THEN RAISE EXCEPTION 'refusing to run against database %', current_database(); END IF;
END $guard$;
\pset pager off
TRUNCATE pilot_position, pilot_session, flight_summary RESTART IDENTITY CASCADE;
-- partitions for the old synthetic days
SELECT ensure_pilot_position_partitions(0, (now() AT TIME ZONE 'utc')::date - 100);
SELECT ensure_pilot_position_partitions(0, (now() AT TIME ZONE 'utc')::date - 91);
SELECT ensure_pilot_position_partitions(0, (now() AT TIME ZONE 'utc')::date - 90);
SELECT ensure_pilot_position_partitions(0, (now() AT TIME ZONE 'utc')::date);
-- session A: wholly in the past (100 d ago 10:00-11:00), 250 kt
-- session B: straddles the 90 day cutoff (91 d ago 23:30 -> 90 d ago 00:30)
-- session C: recent (today)
INSERT INTO pilot_session (cid, callsign, logon_time, name, first_seen, last_seen, departure, arrival, aircraft_icao, remarks)
VALUES (5001,'OLDA', date_trunc('day', now()) - interval '100 days' + interval '9 hours 50 minutes', 'Old A YSSY', date_trunc('day', now()) - interval '100 days' + interval '10 hours', date_trunc('day', now()) - interval '100 days' + interval '11 hours', 'YSSY','YMML','B738','SEL/ABCD'),
       (5002,'OLDB', date_trunc('day', now()) - interval '91 days' + interval '23 hours', 'Old B YMML', date_trunc('day', now()) - interval '91 days' + interval '23 hours 30 minutes', date_trunc('day', now()) - interval '90 days' + interval '30 minutes', 'YMML','NZAA','A320',NULL),
       (5003,'NEWC', date_trunc('day', now()) + interval '1 hour', 'New C YPPH', date_trunc('day', now()) + interval '1 hour', date_trunc('day', now()) + interval '2 hours', 'YPPH','YSSY','B738',NULL);
INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg)
SELECT t, 1, -33.9, 151.2, 5000, 250, 90 FROM generate_series(date_trunc('day', now()) - interval '100 days' + interval '10 hours', date_trunc('day', now()) - interval '100 days' + interval '11 hours', interval '15 seconds') t;
INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg)
SELECT t, 2, -33.9, 151.2, 5000, 250, 90 FROM generate_series(date_trunc('day', now()) - interval '91 days' + interval '23 hours 30 minutes', date_trunc('day', now()) - interval '90 days' + interval '30 minutes', interval '15 seconds') t;
INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg)
SELECT t, 3, -33.9, 151.2, 5000, 250, 90 FROM generate_series(date_trunc('day', now()) + interval '1 hour', date_trunc('day', now()) + interval '2 hours', interval '15 seconds') t;
SELECT refresh_flight_summary(NULL);
\echo --- BEFORE drop
SELECT session_id, start_time, end_time, sample_count, airborne_minutes, final FROM flight_summary ORDER BY 1;
CREATE TEMP TABLE before AS SELECT session_id, callsign, start_time, end_time, airborne_minutes, sample_count, departure, arrival FROM flight;
SELECT count(*) flights, round(sum(airborne_minutes),2) airborne FROM flight;
\echo --- retention run 1 (today): A should be dropped, B pinned by last_seen >= cutoff
SELECT drop_old_pilot_position_partitions(90, (now() AT TIME ZONE 'utc')::date);
SELECT relname FROM pg_class WHERE relname ~ '^pilot_position_2026[0-9]{4}$' ORDER BY 1;
SELECT session_id, final FROM flight_summary ORDER BY 1;
\echo --- flight view identical after the drop? (rows that differ; expect 0)
SELECT count(*) AS rows_differing FROM (SELECT * FROM before EXCEPT SELECT session_id, callsign, start_time, end_time, airborne_minutes, sample_count, departure, arrival FROM flight) x;
\echo --- retention run 2 (one day later): B no longer pinned
SELECT drop_old_pilot_position_partitions(90, (now() AT TIME ZONE 'utc')::date + 1);
SELECT relname FROM pg_class WHERE relname ~ '^pilot_position_2026[0-9]{4}$' ORDER BY 1;
SELECT count(*) AS rows_differing FROM (SELECT * FROM before EXCEPT SELECT session_id, callsign, start_time, end_time, airborne_minutes, sample_count, departure, arrival FROM flight) x;
SELECT count(*) flights, round(sum(airborne_minutes),2) airborne FROM flight;
SELECT session_id, start_time, end_time, sample_count, airborne_minutes, final FROM flight_summary ORDER BY 1;
\echo --- a refresh after the drop must not damage final rows
SELECT refresh_flight_summary(NULL);
SELECT count(*) AS rows_differing FROM (SELECT * FROM before EXCEPT SELECT session_id, callsign, start_time, end_time, airborne_minutes, sample_count, departure, arrival FROM flight) x;
\echo --- panel-style queries over the long window after the drop
SELECT pilot_region, count(*) FROM flight_ext GROUP BY 1 ORDER BY 1;
SELECT round(sum(airborne_minutes)/60.0,2) AS airborne_hours FROM flight_ext;
SELECT * FROM cohorts ORDER BY session_id;
