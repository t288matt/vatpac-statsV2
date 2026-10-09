-- migrate:up
-- Owner decision 09-10-2026 (issue #35, tracker T41): the pilot sessions of one flight are merged the way v1 merged them.
-- v1 (app/services/session_selector.py) grouped raw rows by callsign + CID + departure + arrival, ignoring the logon
-- time, and started a new flight only after a gap of more than 120 minutes. A pilot who disconnected and reconnected
-- with the same callsign and the same airports was therefore still one flight.
-- v3 identifies a session by (cid, callsign, logon_time), so every reconnect is a new session. `flight_part` keeps the
-- old per-session, per-gap, per-8-hour rows; `flight` now joins parts of DIFFERENT sessions with the same
-- (cid, callsign, departure, arrival) when the next part starts no more than 120 minutes after everything before it
-- ended. Parts of one session are never joined (the 8 hour span split stays as decided). A changed plan (different
-- airports) is a different flight, as in v1. The merged row keeps the columns and types of the old view; session_id,
-- gap_segment, span_part and logon_time are those of the first part, plan and pilot fields come from the latest part.
CREATE OR REPLACE VIEW flight_part AS
WITH cfg AS (
    SELECT COALESCE(
               (SELECT array_agg(upper(trim(p)))
                FROM app_setting a, unnest(string_to_array(a.value, ',')) AS p
                WHERE a.key = 'home_icao_prefixes' AND trim(p) <> ''),
               ARRAY['Y']) AS home_prefixes
),
norm AS (
    -- btrim with an explicit set: plain trim() strips spaces only, not tabs or line breaks.
    SELECT ps.*, NULLIF(upper(btrim(ps.departure, E' \t\r\n')), '') AS dep_n,
           NULLIF(upper(btrim(ps.arrival, E' \t\r\n')), '') AS arr_n
    FROM pilot_session ps
)
SELECT
    n.id AS session_id, a.gap_segment, a.span_part,
    n.cid, n.callsign, n.logon_time, n.name, n.home_icao, n.server,
    n.dep_n AS departure, n.arr_n AS arrival,
    n.aircraft_icao, n.aircraft_faa, n.flight_rules, n.route, n.remarks, n.planned_alt, n.deptime,
    n.pilot_rating, n.military_rating,
    a.start_time, a.end_time,
    a.end_time - a.start_time AS duration,
    round(extract(epoch FROM a.end_time - a.start_time) / 60.0, 1) AS duration_minutes,
    a.airborne_minutes, a.max_gs_kt, a.max_alt_ft, a.sample_count,
    (n.dep_n = n.arr_n) AS is_circuit,
    ('ZZZZ' IN (n.dep_n, n.arr_n)) AS is_zzzz,
    EXISTS (
        SELECT 1 FROM cfg c, unnest(c.home_prefixes) AS hp
        WHERE left(n.dep_n, length(hp)) = hp OR left(n.arr_n, length(hp)) = hp) AS touches_home_region
FROM flight_summary a
JOIN norm n ON n.id = a.session_id
WHERE n.dep_n IS NOT NULL AND n.arr_n IS NOT NULL;

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
    bool_or(c.touches_home_region) AS touches_home_region
FROM chained c
GROUP BY c.cid, c.callsign, c.departure, c.arrival, c.chain;
COMMENT ON VIEW flight IS
    'One row per flight. Parts of different sessions (reconnects) with the same cid, callsign, departure and arrival are merged when the gap is 120 minutes or less (v1 rule, issue 35). Per-session parts are in flight_part.';
COMMENT ON VIEW flight_part IS
    'One row per (session, gap segment, 8 h part) with the flight gate applied. flight merges these across reconnects.';

-- migrate:down
-- Restores the per-session view. flight_part is dropped after flight no longer reads it.
CREATE OR REPLACE VIEW flight AS
WITH cfg AS (
    SELECT COALESCE(
               (SELECT array_agg(upper(trim(p)))
                FROM app_setting a, unnest(string_to_array(a.value, ',')) AS p
                WHERE a.key = 'home_icao_prefixes' AND trim(p) <> ''),
               ARRAY['Y']) AS home_prefixes
),
norm AS (
    -- btrim with an explicit set: plain trim() strips spaces only, not tabs or line breaks.
    SELECT ps.*, NULLIF(upper(btrim(ps.departure, E' \t\r\n')), '') AS dep_n,
           NULLIF(upper(btrim(ps.arrival, E' \t\r\n')), '') AS arr_n
    FROM pilot_session ps
)
SELECT
    n.id AS session_id, a.gap_segment, a.span_part,
    n.cid, n.callsign, n.logon_time, n.name, n.home_icao, n.server,
    n.dep_n AS departure, n.arr_n AS arrival,
    n.aircraft_icao, n.aircraft_faa, n.flight_rules, n.route, n.remarks, n.planned_alt, n.deptime,
    n.pilot_rating, n.military_rating,
    a.start_time, a.end_time,
    a.end_time - a.start_time AS duration,
    round(extract(epoch FROM a.end_time - a.start_time) / 60.0, 1) AS duration_minutes,
    a.airborne_minutes, a.max_gs_kt, a.max_alt_ft, a.sample_count,
    (n.dep_n = n.arr_n) AS is_circuit,
    ('ZZZZ' IN (n.dep_n, n.arr_n)) AS is_zzzz,
    EXISTS (
        SELECT 1 FROM cfg c, unnest(c.home_prefixes) AS hp
        WHERE left(n.dep_n, length(hp)) = hp OR left(n.arr_n, length(hp)) = hp) AS touches_home_region
FROM flight_summary a
JOIN norm n ON n.id = a.session_id
WHERE n.dep_n IS NOT NULL AND n.arr_n IS NOT NULL;
DROP VIEW IF EXISTS flight_part;
