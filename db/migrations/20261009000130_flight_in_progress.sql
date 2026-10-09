-- migrate:up
-- Issue #38 (tracker T45): tell completed flights from flights still in progress. `flight` gets one new LAST column,
-- is_in_progress: true when any session that belongs to the (merged) flight is online now, by the same definition as the
-- online_now view (seen within the last two snapshots, newest snapshot not older than 5 minutes). All existing columns,
-- their order and types are unchanged, so every panel and view that reads flight keeps working; no panel is changed here.
-- When the collector is stopped or stale, online_now is empty and every flight reads as not in progress.
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

-- migrate:down
-- A view cannot lose a column through CREATE OR REPLACE, and dropping flight would also drop flight_ext and
-- monday_attendance. The extra last column is harmless, so the down step leaves the view as it is. To go back past
-- this migration, restore the pre-deploy dump (docs/RUNBOOKS/restore.md); the down step of migration 110 would fail
-- while the column exists.
SELECT 1;
