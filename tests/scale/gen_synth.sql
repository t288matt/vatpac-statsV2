-- synthetic flight history. psql vars: nflights, days. Throwaway DB only.
\set ON_ERROR_STOP on
SELECT setseed(0.42);
-- refuse to run anywhere but a throwaway database: this script TRUNCATEs the real tables
SELECT current_database() !~ '_test$' AS refuse \gset
\if :refuse
    \echo 'REFUSING: database name must end in _test'
    DO $$ BEGIN RAISE EXCEPTION 'gen_synth.sql only runs on a database whose name ends in _test'; END $$;
\endif
TRUNCATE pilot_position, pilot_session, network_snapshot RESTART IDENTITY CASCADE;
CREATE TEMP TABLE airports(i serial, icao text, w int);
INSERT INTO airports(icao,w) VALUES ('YSSY',30),('YMML',28),('YBBN',14),('YPPH',10),('YPAD',6),('YBCG',4),('YSCB',4),('YMHB',2),('YBCS',3),('YPDN',2),('NZAA',3),('NZWN',2),('WSSS',2),('VHHH',2),('KLAX',3),('EGLL',3),('YSRI',2),('YMAV',2),('YMEN',2),('YSBK',2);
CREATE TEMP TABLE airport_pool AS SELECT icao, generate_series(1,w) FROM airports;
CREATE TEMP TABLE acft(i serial, t text); INSERT INTO acft(t) VALUES ('B738'),('A320'),('B744'),('A333'),('B77W'),('C172'),('A388'),('E190'),('DH8D'),('F18S'),('C130'),('B789');
CREATE TEMP TABLE pre(i serial, p text); INSERT INTO pre(p) VALUES ('QFA'),('VOZ'),('JST'),('UAE'),('SIA'),('RXA'),('ANZ'),('CPA'),('VH-'),('RAAF');
CREATE TEMP TABLE ap AS SELECT array_agg(icao) a FROM airport_pool;

CREATE TEMP TABLE base AS
WITH g AS (
  SELECT i, 1000000 + floor((:nflights / 5.0) * power(random(), 2))::bigint AS cid,
         random() r_dep, random() r_arr, random() r_kind, random() r_rc, random() r_dur, random() r_t, random() r_rc2
  FROM generate_series(1, :nflights) i)
SELECT g.i, g.cid,
       (SELECT p FROM pre WHERE pre.i = 1 + (g.cid % 10)) || ((g.cid * 7 + (g.i % 2)) % 1000) AS callsign,
       CASE WHEN r_kind < 0.01 THEN NULL ELSE ap.a[1 + floor(r_dep * array_length(ap.a,1))::int] END AS dep,
       CASE WHEN r_kind < 0.03 THEN 'ZZZZ'
            WHEN r_kind < 0.07 THEN ap.a[1 + floor(r_dep * array_length(ap.a,1))::int]
            ELSE ap.a[1 + floor(r_arr * array_length(ap.a,1))::int] END AS arr,
       now() - make_interval(secs => r_t * :days * 86400) AS st,
       CASE WHEN r_kind > 0.985 THEN 500 + floor(r_dur*900) ELSE 20 + floor(r_dur * 280) END AS dur_min,
       r_rc, r_rc2
FROM g, ap;
-- flights whose dep is NULL keep a blank-ish plan too: mix of ' ' and NULL
UPDATE base SET dep = ' ' WHERE dep IS NULL AND i % 2 = 0;

CREATE TEMP TABLE sess_raw AS
SELECT i, 0 AS rk, cid, callsign, dep, arr, st, dur_min, r_rc FROM base
UNION ALL  -- 8% reconnect: gap 0 to 100 min (merge) or, for a quarter of them, 125 to 300 min (no merge), a few exactly 120
SELECT i, 1, cid, callsign, dep, arr,
       st + make_interval(mins => dur_min::int) + make_interval(mins => CASE WHEN r_rc < 0.02 THEN 125 + floor(r_rc2*175)::int
                                                                             WHEN r_rc < 0.0225 THEN 120
                                                                             ELSE floor(r_rc2*100)::int END),
       20 + floor(r_rc2 * 120), r_rc2
FROM base WHERE r_rc < 0.08
UNION ALL  -- 1% a second reconnect right after the first
SELECT i, 2, cid, callsign, dep, arr,
       st + make_interval(mins => (dur_min + 20 + 20 + floor(r_rc2*120))::int + 15), 30, r_rc2
FROM base WHERE r_rc < 0.01
UNION ALL  -- 3% changed plan: same pilot and callsign, new arrival, within 60 minutes (a different flight)
SELECT i, 3, cid, callsign, dep, 'YSSY', st + make_interval(mins => (dur_min + 30)::int), 40, r_rc2
FROM base WHERE r_rc BETWEEN 0.5 AND 0.53 AND arr <> 'YSSY';

CREATE TEMP TABLE sess AS
SELECT row_number() OVER (ORDER BY st, i, rk) AS sid, *,
       CASE WHEN i % 100 = 7 THEN 2 WHEN i % 33 = 5 THEN 1 ELSE 0 END AS shape   -- 2: span split (needs long), 1: gap split
FROM (SELECT DISTINCT ON (cid, callsign, date_trunc('second', st)) * FROM sess_raw ORDER BY cid, callsign, date_trunc('second', st), i, rk) d;

INSERT INTO pilot_session (id, cid, callsign, logon_time, name, home_icao, server, first_seen, last_seen,
        departure, arrival, aircraft_icao, aircraft_faa, flight_rules, route, remarks, planned_alt, deptime, max_gs_kt, max_alt_ft, pilot_rating, military_rating)
SELECT sid, cid, callsign, date_trunc('second', st), 'Pilot ' || cid,
       (SELECT icao FROM airports WHERE airports.i = 1 + (cid % 20)), 'SYDNEY',
       st,
       st + make_interval(mins => dur_min::int) + CASE WHEN shape = 1 THEN interval '130 minutes' ELSE interval '0' END,
       dep, arr, (SELECT t FROM acft WHERE acft.i = 1 + ((cid + sid) % 12)), 'H-SDE3GRY/L', CASE WHEN sid % 5 = 0 THEN 'V' ELSE 'I' END, 'DCT', 
       CASE WHEN sid % 40 = 0 THEN 'RAAFVIRTUAL.ORG SEL/ABCD' WHEN sid % 9 = 0 THEN 'PBN/A1 SEL/KLMN' ELSE NULL END, '35000', '1200',
       250 + (sid % 300), 20000 + (sid % 20000), 0, 0
FROM sess;
SELECT setval('pilot_session_id_seq', (SELECT max(id) FROM pilot_session));

-- flight_summary rows
INSERT INTO flight_summary (session_id, gap_segment, span_part, cid, start_time, end_time, sample_count, airborne_minutes, max_gs_kt, max_alt_ft, final)
SELECT sid, 1, 0, cid, st, st + make_interval(mins => (dur_min / CASE WHEN shape = 1 THEN 2 ELSE 1 END)::int),
       (dur_min * 4)::bigint, round((dur_min * 0.8)::numeric, 2), 250 + (sid % 300), 20000 + (sid % 20000), true
FROM sess WHERE shape <> 2
UNION ALL  -- second gap segment for gap-split sessions
SELECT sid, 2, 0, cid, st + make_interval(mins => (dur_min / 2)::int + 130), st + make_interval(mins => dur_min::int + 130),
       (dur_min * 2)::bigint, round((dur_min * 0.4)::numeric, 2), 200, 18000, true
FROM sess WHERE shape = 1
UNION ALL  -- 8 hour span parts for long sessions (3 parts)
SELECT sid, 1, k, cid, st + make_interval(mins => k * 480), st + make_interval(mins => LEAST((k + 1) * 480, dur_min::int)),
       1000, round(400::numeric, 2), 300, 36000, true
FROM sess, generate_series(0, 2) k WHERE shape = 2 AND k * 480 < dur_min;
-- placeholder rows: sessions with no positions (about 1%), as the migration created for pre-partition history
DELETE FROM flight_summary WHERE session_id IN (SELECT sid FROM sess WHERE sid % 97 = 0);
INSERT INTO flight_summary (session_id, gap_segment, span_part, cid, start_time, end_time, sample_count, airborne_minutes, max_gs_kt, max_alt_ft, final)
SELECT ps.id, 0, 0, ps.cid, ps.first_seen, ps.last_seen, 0, NULL, ps.max_gs_kt, ps.max_alt_ft, true FROM pilot_session ps WHERE ps.id % 97 = 0;
-- a few flights in progress (online now)
INSERT INTO network_snapshot VALUES (now() - interval '5 seconds', 1, 1, 1, 1);
UPDATE pilot_session SET last_seen = now() - interval '5 seconds' WHERE id % 700 = 1 AND last_seen > now() - interval '3 days' OR id % 7000 = 1;
ANALYZE;
SELECT (SELECT count(*) FROM pilot_session) sessions, (SELECT count(*) FROM flight_summary) fs_rows, (SELECT count(*) FROM flight) flights;
