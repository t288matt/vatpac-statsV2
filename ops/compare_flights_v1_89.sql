-- Issue #89 phase 1 exit gate (#138), comparison (B), step 2 of 2: flight_record against v1's flight summaries. READ ONLY on v3.
-- The independent check (the writer's author did not write v1). Run on the v3 database or a restore of a live dump, as a read-only session:
--   psql -X -v ON_ERROR_STOP=1 -d DATABASE -v v1_csv=v1_flight_summaries.csv -f ops/compare_flights_v1_89.sql
-- after step 1 (ops/compare_flights_v1_export.sql) produced the CSV. The temp table below is dropped when psql exits; nothing in v3 changes.
--
-- Window of docs/DESIGN_89_FINAL.md 1.4 (times when BOTH systems were polling): flights logging on from 08-10-2026 07:52 UTC, ending
-- before 10-10-2026 18:00 UTC, excluding any that overlap the joint power cut of 09-10-2026 15:30 to 15:50 UTC. Edit the four literals in
-- `win` below for another window.
--
-- EXPECTED for that window (887 v1 summaries): 867 v3 flights, 864 exact 1-to-1 matches, 0 v1 flights split in two by v3, 23 v1-only
-- flights (all earlier plans before an amendment: same pilot and callsign in v3 with a different departure or arrival) and 3 v3-only
-- flights. Any change in these numbers is a bug: stop and report it on issue #138. Airborne minutes are NOT compared (decided difference).
--
-- How a v1 summary is classified (v1 key: cid, callsign, departure, arrival; its logon_time is truncated to whole seconds, E8):
--   exact    : exactly one v3 flight with that key logged on in [v1 logon, v1 completion] and its logon_time equals the v1 one;
--   split    : two or more v3 flights with that key logged on in [v1 logon, v1 completion];
--   v1-only  : no v3 flight with that key in that interval; "amendment candidate" if v3 has a flight of the same pilot and callsign
--              with the same logon second but another departure or arrival;
--   other    : anything else (one v3 flight in the interval whose logon_time differs): listed, to be explained by hand.
-- v3-only: a v3 flight no v1 summary claims.
-- NOT run against real v1 data by its author: only against synthetic rows (tests/integration/test_compare_flights.py). The matching
-- rules above are the author's reading of the owner's comparison of 12-10-2026; if the owner's own matching differs, say so on #138.
CREATE TEMP TABLE v1_summary (
    cid bigint, callsign text, departure text, arrival text, logon_time timestamptz, completion_time timestamptz);
\copy v1_summary FROM :'v1_csv' CSV HEADER
WITH win AS (
    SELECT timestamptz '2026-10-08 07:52:00+00' AS w_start, timestamptz '2026-10-10 18:00:00+00' AS w_end,
           timestamptz '2026-10-09 15:30:00+00' AS cut_start, timestamptz '2026-10-09 15:50:00+00' AS cut_end
),
v3 AS (
    SELECT f.session_id, f.cid, f.callsign, f.departure, f.arrival, date_trunc('second', f.logon_time) AS logon_s, f.start_time, f.end_time
      FROM flight_record f, win w
     WHERE f.logon_time >= w.w_start AND f.end_time < w.w_end AND NOT (f.start_time < w.cut_end AND f.end_time > w.cut_start)
),
v1 AS (
    SELECT row_number() OVER () AS rid, s.cid, s.callsign, upper(btrim(s.departure)) AS departure, upper(btrim(s.arrival)) AS arrival,
           date_trunc('second', s.logon_time) AS logon_s, s.completion_time
      FROM v1_summary s, win w
     WHERE s.logon_time >= w.w_start AND s.logon_time < w.w_end
       AND NOT (s.logon_time >= w.cut_start AND s.logon_time < w.cut_end)
),
claim AS (                                          -- which v3 flights fall inside which v1 summary
    SELECT v1.rid, v3.session_id, v3.start_time, (v3.logon_s = v1.logon_s) AS same_logon
      FROM v1 JOIN v3 ON v3.cid = v1.cid AND v3.callsign = v1.callsign AND v3.departure = v1.departure AND v3.arrival = v1.arrival
                     AND v3.logon_s >= v1.logon_s AND v3.logon_s <= v1.completion_time
),
per AS (
    SELECT v1.rid, v1.cid, v1.callsign, v1.departure, v1.arrival, v1.logon_s,
           count(c.session_id) AS n_inside, count(*) FILTER (WHERE c.same_logon) AS n_exact
      FROM v1 LEFT JOIN claim c ON c.rid = v1.rid
     GROUP BY v1.rid, v1.cid, v1.callsign, v1.departure, v1.arrival, v1.logon_s
),
classed AS (
    SELECT p.*,
           CASE WHEN p.n_inside = 1 AND p.n_exact = 1 THEN 'exact'
                WHEN p.n_inside >= 2 THEN 'split'
                WHEN p.n_inside = 0 THEN 'v1-only'
                ELSE 'other' END AS class,
           (p.n_inside = 0 AND EXISTS (SELECT 1 FROM v3 WHERE v3.cid = p.cid AND v3.callsign = p.callsign AND v3.logon_s = p.logon_s
                                        AND (v3.departure, v3.arrival) <> (p.departure, p.arrival))) AS amendment_candidate
      FROM per p
),
v3_only AS (
    SELECT v3.* FROM v3 WHERE NOT EXISTS (SELECT 1 FROM claim c WHERE c.session_id = v3.session_id AND c.start_time = v3.start_time)
)
SELECT ord, metric, value FROM (
    SELECT 1 AS ord, 'v1 summaries in the window'::text AS metric, (SELECT count(*) FROM v1) AS value
    UNION ALL SELECT 2, 'v3 flights in the window (expected 867 on the design window)', (SELECT count(*) FROM v3)
    UNION ALL SELECT 3, 'exact 1-to-1 matches (expected 864)', (SELECT count(*) FROM classed WHERE class = 'exact')
    UNION ALL SELECT 4, 'v1 flights split in two or more by v3 (expected 0)', (SELECT count(*) FROM classed WHERE class = 'split')
    UNION ALL SELECT 5, 'v1-only flights (expected 23)', (SELECT count(*) FROM classed WHERE class = 'v1-only')
    UNION ALL SELECT 6, '  of which an amendment candidate (same pilot, callsign and logon, other departure or arrival; expected all)',
                        (SELECT count(*) FROM classed WHERE class = 'v1-only' AND amendment_candidate)
    UNION ALL SELECT 7, 'v1 flights classed other (to explain by hand; expected 0)', (SELECT count(*) FROM classed WHERE class = 'other')
    UNION ALL SELECT 8, 'v3-only flights (expected 3)', (SELECT count(*) FROM v3_only)
) m
UNION ALL
SELECT 100 + row_number() OVER (), class || (CASE WHEN amendment_candidate THEN ' (amendment candidate)' ELSE '' END) || ': cid ' || cid || ' '
       || callsign || ' ' || departure || '-' || arrival || ' logon ' || logon_s::text, 1
  FROM classed WHERE class <> 'exact'
UNION ALL
SELECT 300 + row_number() OVER (), 'v3-only: cid ' || cid || ' ' || callsign || ' ' || departure || '-' || arrival || ' logon ' || logon_s::text, 1
  FROM v3_only
ORDER BY 1;
