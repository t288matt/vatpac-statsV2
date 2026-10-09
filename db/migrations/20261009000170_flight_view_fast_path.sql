-- migrate:up
-- Issue #89 (review of issue #80): the `flight` view ran two window functions and about twenty ordered array_agg
-- calls over EVERY row of flight_part on every query, and history is kept forever (ADR-026), so every panel got
-- slower as the database grew (measured: about 2 s per panel and 12 s for the movements panels at one year of
-- history, so about 10 s and 60 s at five years, past grafana_ro's 30 s statement_timeout). See
-- docs/FLIGHT_VIEW_SCALING.md for the numbers.
--
-- The merge rule only ever joins parts of DIFFERENT sessions that share (cid, callsign, departure, arrival), and only
-- about one flight in ten has such a partner. Parts of one session are never joined (every part is its own flight).
-- So the view is now two branches:
--   * parts whose (cid, callsign, departure, arrival) group holds parts of ONE session only: each part is its own
--     flight, read straight from flight_part with no sorting, windowing or array_agg;
--   * parts in groups that hold more than one session: the unchanged chain computation, over those rows only.
-- Nothing is stored, so nothing can go stale: the plan fields, the home-region setting and is_in_progress are still
-- read live. The result is row for row identical to the previous definition (same columns, order, types and values,
-- proven by tests/integration/test_flight_view_equivalence.py and the measurements in docs/FLIGHT_VIEW_SCALING.md).
CREATE OR REPLACE VIEW flight AS
WITH multi AS (
    -- groups in which a merge is possible: parts of more than one session
    SELECT g.cid, g.callsign, g.departure, g.arrival
    FROM flight_part g
    GROUP BY g.cid, g.callsign, g.departure, g.arrival
    HAVING min(g.session_id) <> max(g.session_id)
),
solo AS (
    -- one session in the group: every part is a flight by itself, exactly what the chain computation returns for it
    SELECT p.session_id, p.gap_segment, p.span_part, p.cid, p.callsign, p.logon_time, p.name, p.home_icao, p.server,
           p.departure, p.arrival, p.aircraft_icao, p.aircraft_faa, p.flight_rules, p.route, p.remarks, p.planned_alt,
           p.deptime, p.pilot_rating, p.military_rating, p.start_time, p.end_time,
           p.end_time - p.start_time AS duration,
           round(extract(epoch FROM p.end_time - p.start_time) / 60.0, 1) AS duration_minutes,
           p.airborne_minutes, p.max_gs_kt, p.max_alt_ft, p.sample_count::bigint AS sample_count,
           p.is_circuit, p.is_zzzz, p.touches_home_region,
           (p.session_id IN (SELECT o.session_id FROM online_now o WHERE o.kind = 'pilot')) AS is_in_progress
    FROM flight_part p
    LEFT JOIN multi m
           ON m.cid = p.cid AND m.callsign = p.callsign AND m.departure = p.departure AND m.arrival = p.arrival
    WHERE m.cid IS NULL
),
ordered AS (
    SELECT p.*,
           lag(p.session_id) OVER w AS prev_session,
           max(p.end_time) OVER (PARTITION BY p.cid, p.callsign, p.departure, p.arrival
                                 ORDER BY p.start_time, p.session_id, p.gap_segment, p.span_part
                                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end
    FROM flight_part p
    JOIN multi m
      ON m.cid = p.cid AND m.callsign = p.callsign AND m.departure = p.departure AND m.arrival = p.arrival
    WINDOW w AS (PARTITION BY p.cid, p.callsign, p.departure, p.arrival
                 ORDER BY p.start_time, p.session_id, p.gap_segment, p.span_part)
),
flagged AS (
    SELECT o.*,
           CASE WHEN o.prev_end IS NOT NULL
                 AND o.prev_session <> o.session_id
                 AND o.start_time - o.prev_end <= interval '120 minutes'
                THEN 0 ELSE 1 END AS is_new
    FROM ordered o
),
chained AS (
    SELECT f.*,
           sum(f.is_new) OVER (PARTITION BY f.cid, f.callsign, f.departure, f.arrival
                               ORDER BY f.start_time, f.session_id, f.gap_segment, f.span_part
                               ROWS UNBOUNDED PRECEDING) AS chain
    FROM flagged f
),
merged AS (
SELECT
    (array_agg(c.session_id ORDER BY c.start_time, c.session_id, c.gap_segment, c.span_part))[1] AS session_id,
    (array_agg(c.gap_segment ORDER BY c.start_time, c.session_id, c.gap_segment, c.span_part))[1] AS gap_segment,
    (array_agg(c.span_part ORDER BY c.start_time, c.session_id, c.gap_segment, c.span_part))[1] AS span_part,
    c.cid, c.callsign,
    min(c.logon_time) AS logon_time,
    (array_agg(c.name ORDER BY c.end_time DESC))[1] AS name,
    (array_agg(c.home_icao ORDER BY c.end_time DESC))[1] AS home_icao,
    (array_agg(c.server ORDER BY c.end_time DESC))[1] AS server,
    c.departure, c.arrival,
    (array_agg(c.aircraft_icao ORDER BY c.end_time DESC))[1] AS aircraft_icao,
    (array_agg(c.aircraft_faa ORDER BY c.end_time DESC))[1] AS aircraft_faa,
    (array_agg(c.flight_rules ORDER BY c.end_time DESC))[1] AS flight_rules,
    (array_agg(c.route ORDER BY c.end_time DESC))[1] AS route,
    (array_agg(c.remarks ORDER BY c.end_time DESC))[1] AS remarks,
    (array_agg(c.planned_alt ORDER BY c.end_time DESC))[1] AS planned_alt,
    (array_agg(c.deptime ORDER BY c.end_time DESC))[1] AS deptime,
    (array_agg(c.pilot_rating ORDER BY c.end_time DESC))[1] AS pilot_rating,
    (array_agg(c.military_rating ORDER BY c.end_time DESC))[1] AS military_rating,
    min(c.start_time) AS start_time,
    max(c.end_time) AS end_time,
    max(c.end_time) - min(c.start_time) AS duration,
    round(extract(epoch FROM max(c.end_time) - min(c.start_time)) / 60.0, 1) AS duration_minutes,
    sum(c.airborne_minutes) AS airborne_minutes,
    max(c.max_gs_kt) AS max_gs_kt,
    max(c.max_alt_ft) AS max_alt_ft,
    sum(c.sample_count)::bigint AS sample_count,
    bool_or(c.is_circuit) AS is_circuit,
    bool_or(c.is_zzzz) AS is_zzzz,
    bool_or(c.touches_home_region) AS touches_home_region,
    bool_or(c.session_id IN (SELECT o.session_id FROM online_now o WHERE o.kind = 'pilot')) AS is_in_progress
FROM chained c
GROUP BY c.cid, c.callsign, c.departure, c.arrival, c.chain
)
SELECT * FROM solo
UNION ALL
SELECT * FROM merged;
COMMENT ON VIEW flight IS
    'One row per flight. Parts of different sessions (reconnects) with the same cid, callsign, departure and arrival are merged when the gap is 120 minutes or less (v1 rule, issue 35). is_in_progress is true while a session of the flight is online now (issue 38). Per-session parts are in flight_part. Groups with one session skip the chain computation (issue 89).';

-- migrate:down
-- Restores the single-branch definition of migration 20261009000130.
CREATE OR REPLACE VIEW flight AS
WITH ordered AS (
    SELECT p.*,
           lag(p.session_id) OVER w AS prev_session,
           max(p.end_time) OVER (PARTITION BY p.cid, p.callsign, p.departure, p.arrival
                                 ORDER BY p.start_time, p.session_id, p.gap_segment, p.span_part
                                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end
    FROM flight_part p
    WINDOW w AS (PARTITION BY p.cid, p.callsign, p.departure, p.arrival
                 ORDER BY p.start_time, p.session_id, p.gap_segment, p.span_part)
),
flagged AS (
    SELECT o.*,
           CASE WHEN o.prev_end IS NOT NULL
                 AND o.prev_session <> o.session_id
                 AND o.start_time - o.prev_end <= interval '120 minutes'
                THEN 0 ELSE 1 END AS is_new
    FROM ordered o
),
chained AS (
    SELECT f.*,
           sum(f.is_new) OVER (PARTITION BY f.cid, f.callsign, f.departure, f.arrival
                               ORDER BY f.start_time, f.session_id, f.gap_segment, f.span_part
                               ROWS UNBOUNDED PRECEDING) AS chain
    FROM flagged f
)
SELECT
    (array_agg(c.session_id ORDER BY c.start_time, c.session_id, c.gap_segment, c.span_part))[1] AS session_id,
    (array_agg(c.gap_segment ORDER BY c.start_time, c.session_id, c.gap_segment, c.span_part))[1] AS gap_segment,
    (array_agg(c.span_part ORDER BY c.start_time, c.session_id, c.gap_segment, c.span_part))[1] AS span_part,
    c.cid, c.callsign,
    min(c.logon_time) AS logon_time,
    (array_agg(c.name ORDER BY c.end_time DESC))[1] AS name,
    (array_agg(c.home_icao ORDER BY c.end_time DESC))[1] AS home_icao,
    (array_agg(c.server ORDER BY c.end_time DESC))[1] AS server,
    c.departure, c.arrival,
    (array_agg(c.aircraft_icao ORDER BY c.end_time DESC))[1] AS aircraft_icao,
    (array_agg(c.aircraft_faa ORDER BY c.end_time DESC))[1] AS aircraft_faa,
    (array_agg(c.flight_rules ORDER BY c.end_time DESC))[1] AS flight_rules,
    (array_agg(c.route ORDER BY c.end_time DESC))[1] AS route,
    (array_agg(c.remarks ORDER BY c.end_time DESC))[1] AS remarks,
    (array_agg(c.planned_alt ORDER BY c.end_time DESC))[1] AS planned_alt,
    (array_agg(c.deptime ORDER BY c.end_time DESC))[1] AS deptime,
    (array_agg(c.pilot_rating ORDER BY c.end_time DESC))[1] AS pilot_rating,
    (array_agg(c.military_rating ORDER BY c.end_time DESC))[1] AS military_rating,
    min(c.start_time) AS start_time,
    max(c.end_time) AS end_time,
    max(c.end_time) - min(c.start_time) AS duration,
    round(extract(epoch FROM max(c.end_time) - min(c.start_time)) / 60.0, 1) AS duration_minutes,
    sum(c.airborne_minutes) AS airborne_minutes,
    max(c.max_gs_kt) AS max_gs_kt,
    max(c.max_alt_ft) AS max_alt_ft,
    sum(c.sample_count)::bigint AS sample_count,
    bool_or(c.is_circuit) AS is_circuit,
    bool_or(c.is_zzzz) AS is_zzzz,
    bool_or(c.touches_home_region) AS touches_home_region,
    bool_or(c.session_id IN (SELECT o.session_id FROM online_now o WHERE o.kind = 'pilot')) AS is_in_progress
FROM chained c
GROUP BY c.cid, c.callsign, c.departure, c.arrival, c.chain;
COMMENT ON VIEW flight IS
    'One row per flight. Parts of different sessions (reconnects) with the same cid, callsign, departure and arrival are merged when the gap is 120 minutes or less (v1 rule, issue 35). is_in_progress is true while a session of the flight is online now (issue 38). Per-session parts are in flight_part.';
