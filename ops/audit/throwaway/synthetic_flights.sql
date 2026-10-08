-- Synthetic flight edge cases for refresh_flight_summary (gap == 120 min, 120 min + 1 s, exact 8 h span, 40 s cap, gs 59/60, NULL gs).
-- Run on a throwaway Postgres with the migrations applied, then compare with: python ops/audit/recompute_flights.py DIR
-- (DIR holds pilot_position.csv and flight_summary.csv exported by the two \copy lines at the end).
\set ON_ERROR_STOP on
-- THROWAWAY DATABASE ONLY. This file TRUNCATEs and INSERTs. It refuses to run unless the database name ends in t30b or _test.
DO $guard$ BEGIN
  IF current_database() !~ '(t30b|_test)$' THEN RAISE EXCEPTION 'refusing to run against database %', current_database(); END IF;
END $guard$;
TRUNCATE pilot_position, pilot_session, flight_summary RESTART IDENTITY CASCADE;
-- sessions 1..8, all "now-ish" today so partitions exist (today and +1)
INSERT INTO pilot_session (cid, callsign, logon_time, name, first_seen, last_seen, departure, arrival)
SELECT 6000+g, 'SYN'||g, date_trunc('day', now()), 'Syn '||g, date_trunc('day', now()), date_trunc('day', now()) + interval '23 hours', 'YSSY','YMML' FROM generate_series(1,8) g;
-- t0 = start of today UTC
-- S1: continuous 20 h (15 s steps), alternating gs 250 / 59 / 60 to hit the airborne boundary
INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg)
SELECT date_trunc('day', now()) + (n*15) * interval '1 second', 1, -30, 150, 1000, CASE n%3 WHEN 0 THEN 250 WHEN 1 THEN 59 ELSE 60 END, 90 FROM generate_series(0, 20*240) n;
-- S2: gap of exactly 120 min (no split) then more samples
INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg)
SELECT date_trunc('day', now()) + t * interval '1 second', 2, -30, 150, 1000, 200, 90 FROM unnest(ARRAY[0,15,30,7230,7245,7260]) t;
-- S3: gap of 120 min + 1 s (split)
INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg)
SELECT date_trunc('day', now()) + t * interval '1 second', 3, -30, 150, 1000, 200, 90 FROM unnest(ARRAY[0,15,30,7231,7246,7261]) t;
-- S4: gaps 15, 30, 40, 41, 60 s, then 119 min, all moving fast (cap 40 s per pair, pair over 120 min counts zero)
INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg)
SELECT date_trunc('day', now()) + t * interval '1 second', 4, -30, 150, 1000, 300, 90 FROM unnest(ARRAY[0,15,45,85,126,186,186+7140,186+7140+7201]) t;
-- S5: span boundary: samples at exactly 8 h and 8 h + 1 s, then 16 h and 16 h + 1 s (continuous otherwise via 60-min steps)
INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg)
SELECT date_trunc('day', now()) + t * interval '1 second', 5, -30, 150, 1000, 100, 90 FROM unnest(ARRAY[0,3600,7200,10800,14400,18000,21600,25200,28800,28801,32400,36000,39600,43200,46800,50400,54000,57600,57601,61200]) t;
-- S6: slow (gs 0-59) everywhere: airborne must be 0
INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg)
SELECT date_trunc('day', now()) + (n*15) * interval '1 second', 6, -30, 150, 0, 59, 90 FROM generate_series(0,40) n;
-- S7: single sample
INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) VALUES (date_trunc('day', now()) + interval '1 hour', 7, -30, 150, 0, 300, 90);
-- S8: NULL groundspeed samples in the middle (cap-rejected values) and a pair with gs 700
INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg)
SELECT date_trunc('day', now()) + t * interval '1 second', 8, -30, 150, 1000, g, 90 FROM (VALUES (0,250),(15,NULL),(30,250),(45,700),(60,250)) v(t,g);
SELECT refresh_flight_summary(NULL);
\copy (SELECT session_id, to_char(observed_at at time zone 'utc','YYYY-MM-DD HH24:MI:SS.US') observed_at, lat, lon, alt_ft, gs_kt, hdg FROM pilot_position ORDER BY 1,2) TO '/tmp/synth_pos.csv' CSV HEADER
\copy (SELECT * FROM flight_summary ORDER BY 1,2,3) TO '/tmp/synth_fs.csv' CSV HEADER
