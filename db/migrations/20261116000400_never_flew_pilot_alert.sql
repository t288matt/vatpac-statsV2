-- migrate:up
SET LOCAL lock_timeout = '5s';   -- fail fast instead of queueing behind a long panel query
-- Issue #331: a Telegram alert for pilots who log on, file a plan and never get airborne, and the one rule that both that alert and
-- the panel "Pilots who logged on but never flew (90 days)" (pilots/pilot-onboarding.json) read, so they cannot disagree.
--
-- 1. pilot_never_flew_90d: the panel's rule moved out of the panel query, unchanged. One row per CID with at least one finished flight
--    in the last 90 days and none with airborne time, whose name has a Y as its fourth character from the end. It reads the flight
--    view exactly as the panel did; the panel now selects from this view and keeps its own ORDER BY, percentage and LIMIT, so its
--    rows are the same. The view groups by cid and has no row limit, so a filter on cid is pushed down to the flight rows of that cid.
-- 2. ops_never_flew_alert: one row per LOGON (CID + logon time) that is online now with a plan filed and not airborne yet, and is
--    either a repeat non-flier (a row in pilot_never_flew_90d) or a first-timer (no finished flight at all) with a Y name. Lapsed
--    pilots are in neither. Query shape (owner choice 16-11-2026): start from the few sessions online now, then look up only those
--    CIDs. It reads pilot_session, not pilot_latest, because pilot_latest keeps ONE row per cid, callsign and departure (migration
--    20261107000010): a reconnect to the same flight would hide the earlier logon. Every logon alerts; the 120-minute reconnect merge
--    is not used. Plain view, no stored data. For the watchdog role only (ops_*), like ops_pending_flight.
--    Online now = the test pilot_latest uses (seen in the last two snapshots and within 5 minutes). Plan filed = departure and
--    arrival both non-empty after norm_code (the rule of ops_pending_flight). Not airborne yet = the logon's highest groundspeed is
--    under 60 kt, the writer's airborne threshold (docs/DEFINITIONS.md section 3; flight_summary is written with gs_kt >= 60).

CREATE OR REPLACE VIEW pilot_never_flew_90d AS
WITH flight_summaries AS (
  -- v3 stand-in for the v1 table: one row per flight
  SELECT cid, name, logon_time,
         end_time AS completion_time,
         airborne_minutes AS total_enroute_time_minutes,
         duration_minutes AS time_online_minutes
  FROM flight
  WHERE airborne_minutes IS NOT NULL
),
pilot_enroute_stats AS (
  SELECT
    cid,
    COALESCE((ARRAY_AGG(name ORDER BY logon_time DESC NULLS LAST)
                  FILTER (WHERE name IS NOT NULL AND name <> ''))[1], cid::text) AS name,
    COUNT(*) as total_flights,
    COUNT(CASE WHEN total_enroute_time_minutes > 0 THEN 1 END) as flights_with_enroute_time,
    COUNT(CASE WHEN total_enroute_time_minutes = 0 THEN 1 END) as flights_with_zero_enroute_time,
    AVG(time_online_minutes) as avg_time_online_minutes,
    MIN(time_online_minutes) as min_time_online_minutes,
    MAX(time_online_minutes) as max_time_online_minutes,
    SUM(time_online_minutes) as total_time_online_minutes
  FROM flight_summaries
  WHERE cid IS NOT NULL
  AND completion_time IS NOT NULL
  AND completion_time >= NOW() - INTERVAL '90 days'
  GROUP BY cid
  HAVING COUNT(*) > 0
)
SELECT cid, name, total_flights, flights_with_zero_enroute_time,
       avg_time_online_minutes, min_time_online_minutes, max_time_online_minutes, total_time_online_minutes
FROM pilot_enroute_stats
WHERE flights_with_enroute_time = 0
AND LENGTH(name) >= 4
AND SUBSTRING(name FROM LENGTH(name) - 3 FOR 1) = 'Y';

COMMENT ON VIEW pilot_never_flew_90d IS
  'Pilots with a finished flight in the last 90 days and none with airborne time, whose name has a Y fourth from the end (the panel Pilots who logged on but never flew, 90 days). One row per cid. The one definition of that rule: the panel and the watchdog alert ops_never_flew_alert both read it. Issue 331.';

CREATE OR REPLACE VIEW ops_never_flew_alert AS
WITH cand AS (
  -- the few sessions online now with a plan filed and not airborne yet
  SELECT ps.id AS session_id, ps.cid, ps.callsign, ps.logon_time, ps.name AS session_name,
         norm_code(ps.departure) AS departure, norm_code(ps.arrival) AS arrival, ps.route
  FROM pilot_session ps
  WHERE ps.last_seen >= (SELECT min(s.observed_at)
                           FROM (SELECT observed_at FROM network_snapshot
                                  ORDER BY observed_at DESC LIMIT 2) s)
    AND ps.last_seen >= now() - interval '5 minutes'
    AND norm_code(ps.departure) IS NOT NULL
    AND norm_code(ps.arrival) IS NOT NULL
    AND COALESCE(ps.max_gs_kt, 0) < 60
)
SELECT c.cid,
       COALESCE(r.name, COALESCE(NULLIF(c.session_name, ''), c.cid::text)) AS name,
       CASE WHEN r.cid IS NOT NULL THEN 'repeat' ELSE 'first_timer' END AS kind,
       COALESCE(r.total_flights, 0) AS flights_90d,
       (SELECT count(*) FROM pilot_session p2 WHERE p2.cid = c.cid AND p2.logon_time <= c.logon_time) AS logon_number,
       c.session_id, c.callsign, c.logon_time, c.departure, c.arrival, c.route,
       pos.lat, pos.lon
FROM cand c
-- OFFSET 0 keeps this a real lateral subquery, so the filter on c.cid reaches the grouped view and only this pilot's flights are read
LEFT JOIN LATERAL (SELECT v.cid, v.name, v.total_flights FROM pilot_never_flew_90d v WHERE v.cid = c.cid OFFSET 0) r ON true
LEFT JOIN LATERAL (
  SELECT pp.lat, pp.lon
  FROM pilot_position pp
  WHERE pp.session_id = c.session_id
    AND pp.observed_at >= now() - interval '3 hours'
  ORDER BY pp.observed_at DESC
  LIMIT 1
) pos ON true
WHERE r.cid IS NOT NULL
   OR (NOT EXISTS (SELECT 1 FROM flight f WHERE f.cid = c.cid)
       AND LENGTH(COALESCE(NULLIF(c.session_name, ''), c.cid::text)) >= 4
       AND SUBSTRING(COALESCE(NULLIF(c.session_name, ''), c.cid::text) FROM LENGTH(COALESCE(NULLIF(c.session_name, ''), c.cid::text)) - 3 FOR 1) = 'Y');

COMMENT ON VIEW ops_never_flew_alert IS
  'One row per logon (cid and logon_time) that is online now, has a plan filed, is not airborne yet (logon max groundspeed under 60 kt) and is a repeat non-flier (a row in pilot_never_flew_90d) or a first-timer (no finished flight) with a Y name. kind is repeat or first_timer; flights_90d the finished flights in 90 days; logon_number the pilot''s logons on record up to this one. Reads pilot_session, so a reconnect with a new logon time is its own row. For the watchdog role only; grafana_ro has no access. Issue 331.';

DO $do$
BEGIN
    -- the roles are created by db/roles.sql after the migrations, so on a first install they do not exist yet; roles.sql then grants
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro') THEN
        GRANT SELECT ON pilot_never_flew_90d TO grafana_ro;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'watchdog_ro') THEN
        GRANT SELECT ON ops_never_flew_alert TO watchdog_ro;
    END IF;
END
$do$;

-- migrate:down
-- Forward-only project: no automatic rollback.
