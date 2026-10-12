-- Comparison A for issue #138 (design 5.2 A): old flight view against flight_record.
-- READ ONLY: SELECT / WITH only. Run by the owner through run_comparisons.py, or by hand with psql:
--   psql -X -v ON_ERROR_STOP=1 -v from_ts='2026-10-07 00:00+00' -v to_ts='2026-10-20 00:00+00' -f ...
-- (hand running: one statement at a time, each begins at its "-- @@QUERY" line.)
--
-- Parameters (psql variables, quoted with :'name'):
--   from_ts   only flights starting at or after this instant (use go-live, 2026-10-07 21:38+00, or later)
--   to_ts     only flights starting before this instant (use a time at least 9 h before the newest data)
--
-- Scope (design 5.2 A): pilots fully processed, that is NO session of the pilot with
-- flown_upto IS NULL OR last_seen > flown_upto. Old side: NOT is_in_progress.
-- Run it on a restore of a live dump where possible: a_independent scans pilot_position.
--
-- Shared columns (everything both sides hold): session_id, start_time, end_time, cid, callsign,
-- departure, arrival, logon_time, name, home_icao, server, aircraft_icao, aircraft_faa, flight_rules,
-- route, remarks, planned_alt, deptime, pilot_rating, military_rating, airborne_minutes, max_gs_kt,
-- max_alt_ft, sample_count. Join key: (session_id, start_time), the key of flight_record.
--
-- Reason codes follow design 5.2 A "Expected differences" 1 to 7. Anything labelled UNEXPECTED is a bug
-- until explained: stop and report it on #138.

-- @@QUERY a_summary
-- Totals on both sides for the same scope. Every number should be equal except rows touched by
-- reasons E1, E2, E3 (counts) and E5 (airborne).
WITH scope AS (
  SELECT cid FROM pilot_session
  GROUP BY cid
  HAVING bool_and(flown_upto IS NOT NULL AND last_seen <= flown_upto)
),
o AS (
  SELECT f.* FROM flight f
  WHERE NOT f.is_in_progress AND f.start_time >= :'from_ts'::timestamptz AND f.start_time < :'to_ts'::timestamptz
    AND f.cid IN (SELECT cid FROM scope)
),
n AS (
  SELECT r.* FROM flight_record r
  WHERE r.start_time >= :'from_ts'::timestamptz AND r.start_time < :'to_ts'::timestamptz
    AND r.cid IN (SELECT cid FROM scope)
)
SELECT 'old_flight_view' AS side, count(*) AS flights, count(DISTINCT cid) AS pilots,
       coalesce(sum(sample_count), 0) AS sum_sample_count,
       coalesce(sum(airborne_minutes), 0) AS sum_airborne_minutes
FROM o
UNION ALL
SELECT 'flight_record', count(*), count(DISTINCT cid), coalesce(sum(sample_count), 0),
       coalesce(sum(airborne_minutes), 0)
FROM n
UNION ALL
SELECT 'pilots_in_scope', (SELECT count(*) FROM scope), NULL, NULL, NULL
UNION ALL
SELECT 'pilots_excluded_unprocessed', count(DISTINCT cid), NULL, NULL, NULL
FROM pilot_session
WHERE cid NOT IN (SELECT cid FROM scope);

-- @@QUERY a_differences
-- Every difference, one row each, with a reason column. Rows that are identical on all shared columns
-- are not listed (their count is in a_summary: flights minus the rows here).
WITH scope AS (
  SELECT cid FROM pilot_session
  GROUP BY cid
  HAVING bool_and(flown_upto IS NOT NULL AND last_seen <= flown_upto)
),
o AS (
  SELECT f.session_id, f.start_time, f.end_time, f.cid, f.callsign, f.departure, f.arrival, f.logon_time,
         f.name, f.home_icao, f.server, f.aircraft_icao, f.aircraft_faa, f.flight_rules, f.route, f.remarks,
         f.planned_alt, f.deptime, f.pilot_rating, f.military_rating, f.airborne_minutes, f.max_gs_kt,
         f.max_alt_ft, f.sample_count
  FROM flight f
  WHERE NOT f.is_in_progress AND f.start_time >= :'from_ts'::timestamptz AND f.start_time < :'to_ts'::timestamptz
    AND f.cid IN (SELECT cid FROM scope)
),
n AS (
  SELECT r.session_id, r.start_time, r.end_time, r.cid, r.callsign, r.departure, r.arrival, r.logon_time,
         r.name, r.home_icao, r.server, r.aircraft_icao, r.aircraft_faa, r.flight_rules, r.route, r.remarks,
         r.planned_alt, r.deptime, r.pilot_rating, r.military_rating, r.airborne_minutes, r.max_gs_kt,
         r.max_alt_ft, r.sample_count, r.session_ids
  FROM flight_record r
  WHERE r.start_time >= :'from_ts'::timestamptz AND r.start_time < :'to_ts'::timestamptz
    AND r.cid IN (SELECT cid FROM scope)
),
j AS (
  SELECT coalesce(n.cid, o.cid) AS cid,
         coalesce(n.session_id, o.session_id) AS session_id,
         coalesce(n.start_time, o.start_time) AS start_time,
         CASE WHEN o.session_id IS NULL THEN 'new_only'
              WHEN n.session_id IS NULL THEN 'old_only'
              ELSE 'both' END AS presence,
         ARRAY_REMOVE(ARRAY[
           CASE WHEN o.end_time          IS DISTINCT FROM n.end_time          THEN 'end_time' END,
           CASE WHEN o.cid               IS DISTINCT FROM n.cid               THEN 'cid' END,
           CASE WHEN o.callsign          IS DISTINCT FROM n.callsign          THEN 'callsign' END,
           CASE WHEN o.departure         IS DISTINCT FROM n.departure         THEN 'departure' END,
           CASE WHEN o.arrival           IS DISTINCT FROM n.arrival           THEN 'arrival' END,
           CASE WHEN o.logon_time        IS DISTINCT FROM n.logon_time        THEN 'logon_time' END,
           CASE WHEN o.name              IS DISTINCT FROM n.name              THEN 'name' END,
           CASE WHEN o.home_icao         IS DISTINCT FROM n.home_icao         THEN 'home_icao' END,
           CASE WHEN o.server            IS DISTINCT FROM n.server            THEN 'server' END,
           CASE WHEN o.aircraft_icao     IS DISTINCT FROM n.aircraft_icao     THEN 'aircraft_icao' END,
           CASE WHEN o.aircraft_faa      IS DISTINCT FROM n.aircraft_faa      THEN 'aircraft_faa' END,
           CASE WHEN o.flight_rules      IS DISTINCT FROM n.flight_rules      THEN 'flight_rules' END,
           CASE WHEN o.route             IS DISTINCT FROM n.route             THEN 'route' END,
           CASE WHEN o.remarks           IS DISTINCT FROM n.remarks           THEN 'remarks' END,
           CASE WHEN o.planned_alt       IS DISTINCT FROM n.planned_alt       THEN 'planned_alt' END,
           CASE WHEN o.deptime           IS DISTINCT FROM n.deptime           THEN 'deptime' END,
           CASE WHEN o.pilot_rating      IS DISTINCT FROM n.pilot_rating      THEN 'pilot_rating' END,
           CASE WHEN o.military_rating   IS DISTINCT FROM n.military_rating   THEN 'military_rating' END,
           CASE WHEN o.airborne_minutes  IS DISTINCT FROM n.airborne_minutes  THEN 'airborne_minutes' END,
           CASE WHEN o.max_gs_kt         IS DISTINCT FROM n.max_gs_kt         THEN 'max_gs_kt' END,
           CASE WHEN o.max_alt_ft        IS DISTINCT FROM n.max_alt_ft        THEN 'max_alt_ft' END,
           CASE WHEN o.sample_count      IS DISTINCT FROM n.sample_count      THEN 'sample_count' END
         ], NULL) AS differing_columns,
         o.end_time AS old_end_time, n.end_time AS new_end_time,
         o.sample_count AS old_sample_count, n.sample_count AS new_sample_count,
         o.airborne_minutes AS old_airborne, n.airborne_minutes AS new_airborne,
         o.departure AS old_departure, o.arrival AS old_arrival,
         n.departure AS new_departure, n.arrival AS new_arrival,
         n.session_ids AS new_session_ids
  FROM o FULL JOIN n ON n.session_id = o.session_id AND n.start_time = o.start_time
),
d AS (SELECT * FROM j WHERE presence <> 'both' OR cardinality(differing_columns) > 0),
ctx AS (
  SELECT d.*,
    -- sessions this row covers: the new row's sessions, or the old row's first session
    coalesce(d.new_session_ids, ARRAY[d.session_id]) AS covered,
    -- E1: a covered session was split in 8-hour parts on the old side
    EXISTS (SELECT 1 FROM flight_part p WHERE p.cid = d.cid AND p.span_part > 0
              AND p.session_id = ANY (coalesce(d.new_session_ids, ARRAY[d.session_id]))) AS has_span_part,
    -- E2: a covered session was cut by a 120-minute silence on the old side
    EXISTS (SELECT 1 FROM flight_part p WHERE p.cid = d.cid AND p.gap_segment > 1
              AND p.session_id = ANY (coalesce(d.new_session_ids, ARRAY[d.session_id]))) AS has_gap_segment,
    -- E3 (and join-grouping differences): this row's first session is inside ANOTHER row of the other side
    CASE WHEN d.presence = 'old_only' THEN EXISTS (
           SELECT 1 FROM flight_record r WHERE r.cid = d.cid AND d.session_id = ANY (r.session_ids)
              AND NOT (r.session_id = d.session_id AND r.start_time = d.start_time))
         WHEN d.presence = 'new_only' THEN EXISTS (
           SELECT 1 FROM flight f2 WHERE f2.cid = d.cid AND NOT f2.is_in_progress
              AND f2.session_id = ANY (d.new_session_ids)
              AND NOT (f2.session_id = d.session_id AND f2.start_time = d.start_time))
         ELSE false END AS regrouped,
    -- E6: one session in two NEW flights with different airports
    EXISTS (SELECT 1 FROM flight_record r2 WHERE r2.cid = d.cid AND r2.session_id = d.session_id
              AND (r2.departure, r2.arrival) IS DISTINCT FROM (d.new_departure, d.new_arrival)
              AND d.new_departure IS NOT NULL) AS session_in_two_airport_flights,
    -- number of old pieces (for the E5 rounding allowance)
    (SELECT count(*) FROM flight_part p WHERE p.cid = d.cid
        AND p.session_id = ANY (coalesce(d.new_session_ids, ARRAY[d.session_id]))
        AND p.start_time <= coalesce(d.new_end_time, d.old_end_time)
        AND p.end_time >= d.start_time) AS old_pieces,
    -- E7: stored positions in the new flight's range, only evaluated when sample_count differs
    CASE WHEN d.presence = 'both' AND 'sample_count' = ANY (d.differing_columns) THEN
      (SELECT count(*) FROM pilot_position pp
         WHERE pp.session_id = ANY (d.new_session_ids)
           AND pp.observed_at >= d.start_time AND pp.observed_at <= d.new_end_time)
    END AS stored_positions
  FROM d
)
SELECT
  ctx.cid, ctx.session_id, ctx.start_time, ctx.presence, ctx.differing_columns,
  ctx.old_end_time, ctx.new_end_time, ctx.old_sample_count, ctx.new_sample_count,
  ctx.old_airborne, ctx.new_airborne, ctx.old_departure, ctx.old_arrival, ctx.new_departure, ctx.new_arrival,
  CASE
    WHEN ctx.has_span_part THEN 'E1 session split in 8-hour parts on the old side'
    WHEN ctx.has_gap_segment THEN 'E2 session cut by a 120-minute silence on the old side'
    WHEN ctx.regrouped THEN 'E3 sessions grouped differently (join by logon_time versus first position)'
    WHEN ctx.presence = 'both' AND ctx.session_in_two_airport_flights THEN 'E6 session returned after being written with a changed plan'
    WHEN ctx.presence = 'both' AND ctx.stored_positions IS NOT NULL AND ctx.old_sample_count < ctx.stored_positions
         AND ctx.new_sample_count = ctx.stored_positions THEN 'E7 stale old pieces (old count below stored positions)'
    WHEN ctx.presence = 'both'
         AND ctx.differing_columns <@ ARRAY['name','home_icao','server','aircraft_icao','aircraft_faa','flight_rules',
             'route','remarks','planned_alt','deptime','pilot_rating','military_rating'] THEN 'E4 plan or name fields (frozen at write; route latest, deptime maximum)'
    WHEN ctx.presence = 'both' AND ctx.differing_columns = ARRAY['airborne_minutes']
         AND abs(ctx.old_airborne - ctx.new_airborne) <= 0.01 * greatest(ctx.old_pieces, 1) THEN 'E5 airborne rounding (rounded once, not per piece)'
    WHEN ctx.presence = 'both' AND ctx.differing_columns <@ ARRAY['name','home_icao','server','aircraft_icao','aircraft_faa','flight_rules',
             'route','remarks','planned_alt','deptime','pilot_rating','military_rating','airborne_minutes']
         AND abs(coalesce(ctx.old_airborne, 0) - coalesce(ctx.new_airborne, 0)) <= 0.01 * greatest(ctx.old_pieces, 1)
         THEN 'E4+E5 plan fields and airborne rounding'
    ELSE 'UNEXPECTED'
  END AS reason
FROM ctx
ORDER BY ctx.cid, ctx.start_time, ctx.session_id;

-- @@QUERY a_independent
-- Independent of the writer's code: sum(sample_count) of flight_record against the count of raw positions
-- at or before flown_upto of sessions that pass the flight gate (plan live on the session). Positions are
-- bounded by from_ts / to_ts, so use a window that starts at or before the first flight in scope.
-- Expected equal. A difference means a position is uncovered (writer fault, or a returning session whose
-- plan was filed late, E6) or a plan changed after the flight was written.
WITH scope AS (
  SELECT cid FROM pilot_session
  GROUP BY cid
  HAVING bool_and(flown_upto IS NOT NULL AND last_seen <= flown_upto)
)
SELECT
  (SELECT coalesce(sum(sample_count), 0) FROM flight_record r
     WHERE r.cid IN (SELECT cid FROM scope)
       AND r.start_time >= :'from_ts'::timestamptz AND r.start_time < :'to_ts'::timestamptz) AS flight_record_sample_sum,
  (SELECT count(*) FROM pilot_position p
     JOIN pilot_session s ON s.id = p.session_id
     WHERE s.cid IN (SELECT cid FROM scope)
       AND p.observed_at >= :'from_ts'::timestamptz AND p.observed_at < :'to_ts'::timestamptz
       AND p.observed_at <= s.flown_upto
       AND NULLIF(upper(btrim(s.departure, E' \t\r\n')), '') IS NOT NULL
       AND NULLIF(upper(btrim(s.arrival,   E' \t\r\n')), '') IS NOT NULL) AS gate_positions_processed;

-- @@QUERY a_duplicates
-- Sanity: flight_record must hold each raw position at most once. Flights of one pilot that overlap in
-- time and share a session are not expected (a returning unit starts after flown_upto).
SELECT a.cid, a.session_id AS a_session_id, a.start_time AS a_start, a.end_time AS a_end,
       b.session_id AS b_session_id, b.start_time AS b_start, b.end_time AS b_end
FROM flight_record a
JOIN flight_record b
  ON b.cid = a.cid
 AND (b.session_id, b.start_time) > (a.session_id, a.start_time)
 AND b.session_ids && a.session_ids
 AND b.start_time <= a.end_time AND b.end_time >= a.start_time
WHERE a.start_time >= :'from_ts'::timestamptz AND a.start_time < :'to_ts'::timestamptz
ORDER BY a.cid, a.start_time;
