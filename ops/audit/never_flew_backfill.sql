-- Backfill for the alert on pilots who log on, file a plan and never get airborne (issue 331): how many alerts the last 30 days
-- WOULD have raised, so the owner reads the volume before the dry-run switch is turned off. SELECT only: it writes nothing.
-- Run it by hand as the read-only watchdog login (it has the rights on the three tables read below), never inside the watchdog:
--   docker compose exec -T -e PGPASSWORD="$WATCHDOG_DB_PASSWORD" db psql -X -v ON_ERROR_STOP=1 -U watchdog_ro -d "$POSTGRES_DB" < ops/audit/never_flew_backfill.sql
-- (a shell where those variables are set; the role's 10 s statement limit is lifted for this session by the SET below.)
-- Result: one table of (section, item, value): alerts per day (UTC, every day of the window, zeros included), the total, distinct
-- pilots, first-timer alerts, repeat alerts, and the 10 pilots with the most alerts (CID only, no names).
--
-- It is an APPROXIMATION of the live check (view ops_never_flew_alert + watchdog), applying the same rule per logon (a pilot_session
-- row: CID + logon time, so a reconnect is its own logon and counted once): a plan filed (departure and arrival present after
-- norm_code), a Y name (fourth character from the end), not airborne by the same 60 kt test, and either a first-timer (no finished
-- flight) or on the 90-day panel rule (finished flights in the last 90 days, none with airborne time), judged on flight history that
-- ended before the logon. Where it can differ from what the live check would have sent:
--  1. The live check looks once a minute at who is online now. Here a logon counts when it had no sample at 60 kt or more until at
--     least min_ground_s seconds after it was first seen (positions kept for POSITION_RETENTION_DAYS, 180 by default), or never had
--     one. A pilot who took off between two checks, or a position the feed missed, can differ.
--  2. The plan: only the final plan is stored, and its filing time is not. A plan is taken as filed from the first sighting, so a
--     pilot who filed late counts here but may not have alerted live. This leans towards more alerts, not fewer.
--  3. History: flights are written about 8 hours after a pilot's last connection ends (flight_quiet_hours), so at the moment of a
--     logon the live check did not yet see a flight finished a few hours before. Here every flight that ended before the logon is
--     seen. Pilots can therefore look less like first-timers here than they did live.
--  4. The name for the Y test is today's stored name: the latest flight name before the logon for the 90-day rule, the session name
--     for a first-timer. A name changed since is not reproduced.
--  5. Online now is not tested (the logon is in the past); the first sighting stands in for "online".
--  6. Retention: a session whose positions were deleted counts as never airborne. Inside 30 days with 180 days kept this should not happen.
SET statement_timeout = '120s';
WITH params AS (
  SELECT now() - interval '30 days' AS since,
         interval '60 seconds' AS min_ground_s   -- the watchdog cycle: shorter on the ground than this would not have been seen
),
s AS (  -- logons in the window with a plan filed
  SELECT ps.id, ps.cid, ps.logon_time, ps.first_seen, NULLIF(ps.name, '') AS session_name
  FROM pilot_session ps, params p
  WHERE ps.logon_time >= p.since
    AND norm_code(ps.departure) IS NOT NULL
    AND norm_code(ps.arrival) IS NOT NULL
),
c AS (  -- each logon with its first airborne sample (60 kt or more) and its flight history as it stood before the logon
  SELECT s.id, s.cid, s.logon_time, s.session_name,
         air.t AS first_airborne, h.all_n, h.earlier_flown, h.n90, h.air90, h.flight_name
  FROM s
  CROSS JOIN params p
  LEFT JOIN LATERAL (
    SELECT min(pp.observed_at) AS t
    FROM pilot_position pp
    WHERE pp.session_id = s.id AND pp.observed_at >= p.since AND pp.gs_kt >= 60
  ) air ON true
  LEFT JOIN LATERAL (
    SELECT count(*) AS all_n,
           -- an earlier logon of this pilot that reached 60 kt counts as flown even before its flight is written (as the alert view)
           (SELECT count(*) FROM pilot_session e WHERE e.cid = s.cid AND e.logon_time < s.logon_time AND e.max_gs_kt >= 60) AS earlier_flown,
           count(*) FILTER (WHERE f.airborne_minutes IS NOT NULL AND f.end_time >= s.logon_time - interval '90 days') AS n90,
           count(*) FILTER (WHERE f.airborne_minutes > 0 AND f.end_time >= s.logon_time - interval '90 days') AS air90,
           (array_agg(f.name ORDER BY f.logon_time DESC NULLS LAST)
              FILTER (WHERE f.name IS NOT NULL AND f.name <> '' AND f.airborne_minutes IS NOT NULL
                        AND f.end_time >= s.logon_time - interval '90 days'))[1] AS flight_name
    FROM flight_summary f
    WHERE f.cid = s.cid AND f.end_time < s.logon_time
  ) h ON true
  WHERE air.t IS NULL OR air.t - s.first_seen >= p.min_ground_s
),
k AS (  -- the kind of alert, if any; the Y test is the panel's: at least 4 characters, the 4th from the end is Y
  SELECT c.cid, c.logon_time,
         CASE WHEN c.n90 > 0 AND c.air90 = 0
                   AND length(COALESCE(c.flight_name, c.cid::text)) >= 4
                   AND substring(COALESCE(c.flight_name, c.cid::text) FROM length(COALESCE(c.flight_name, c.cid::text)) - 3 FOR 1) = 'Y'
              THEN 'repeat'
              WHEN c.all_n = 0 AND c.earlier_flown = 0
                   AND length(COALESCE(c.session_name, c.cid::text)) >= 4
                   AND substring(COALESCE(c.session_name, c.cid::text) FROM length(COALESCE(c.session_name, c.cid::text)) - 3 FOR 1) = 'Y'
              THEN 'first_timer'
         END AS kind
  FROM c
),
a AS (SELECT * FROM k WHERE kind IS NOT NULL),
days AS (
  SELECT d::date AS day
  FROM params p, generate_series((p.since AT TIME ZONE 'UTC')::date, (now() AT TIME ZONE 'UTC')::date, interval '1 day') d
),
top10 AS (SELECT cid, count(*) AS n FROM a GROUP BY cid ORDER BY n DESC, cid LIMIT 10)
SELECT section, item, value FROM (
  SELECT 1 AS o, d.day::text AS sort, 'alerts per day' AS section, to_char(d.day, 'DD-MM-YYYY') AS item,
         (SELECT count(*) FROM a WHERE (a.logon_time AT TIME ZONE 'UTC')::date = d.day) AS value
  FROM days d
  UNION ALL SELECT 2, '', 'total', 'alerts in 30 days', count(*) FROM a
  UNION ALL SELECT 3, '', 'distinct pilots', 'pilots', count(DISTINCT cid) FROM a
  UNION ALL SELECT 4, '', 'by kind', 'first-timer alerts', count(*) FROM a WHERE kind = 'first_timer'
  UNION ALL SELECT 5, '', 'by kind', 'repeat alerts', count(*) FROM a WHERE kind = 'repeat'
  UNION ALL SELECT 6, lpad((1000000 - n)::text, 8, '0') || cid::text, 'top 10 pilots by alerts', 'CID ' || cid::text, n FROM top10
) r
ORDER BY o, sort;
