-- migrate:up
SET LOCAL lock_timeout = '5s';   -- fail fast instead of queueing behind a long panel query
-- Issue #331: a first-timer is not someone whose EARLIER logon already reached the 60 kt airborne test. A flight is written about 8 hours
-- after a pilot's last connection ends, so a pilot who flew, dropped and logged on again inside that time has no flight row yet and was
-- alerted as "never flown". ops_never_flew_alert now also requires, for a first-timer, that no earlier session of the same cid has a
-- max_gs_kt of 60 or more (the same test as "not airborne yet"). One lookup on the unique index (cid, callsign, logon_time), per
-- candidate cid only. Same columns, so CREATE OR REPLACE keeps the grant. The panel's rule (pilot_never_flew_90d) is untouched.
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
       AND NOT EXISTS (SELECT 1 FROM pilot_session e
                        WHERE e.cid = c.cid AND e.logon_time < c.logon_time AND e.max_gs_kt >= 60)
       AND LENGTH(COALESCE(NULLIF(c.session_name, ''), c.cid::text)) >= 4
       AND SUBSTRING(COALESCE(NULLIF(c.session_name, ''), c.cid::text) FROM LENGTH(COALESCE(NULLIF(c.session_name, ''), c.cid::text)) - 3 FOR 1) = 'Y');

COMMENT ON VIEW ops_never_flew_alert IS
  'One row per logon (cid and logon_time) that is online now, has a plan filed, is not airborne yet (logon max groundspeed under 60 kt) and is a repeat non-flier (a row in pilot_never_flew_90d) or a first-timer (no finished flight and no earlier logon that reached 60 kt) with a Y name. kind is repeat or first_timer; flights_90d the finished flights in 90 days; logon_number the pilot''s logons on record up to this one. Reads pilot_session, so a reconnect with a new logon time is its own row. For the watchdog role only; grafana_ro has no access. Issue 331.';

-- migrate:down
-- Forward-only project: no automatic rollback (the previous definition is in migration 20261116000400).
