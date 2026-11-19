-- migrate:up
SET LOCAL lock_timeout = '5s';   -- fail fast instead of queueing behind a long panel query
-- Issue #406: the panel "Pilots who logged on but never flew (90 days)" (pilots/pilot-onboarding.json, card 180) sorted by
-- total_flights DESC, name, so a pilot who had just qualified for the first time (one flight) was buried wherever their name fell
-- alphabetically. New entries looked like the list had gone stale even though it was current; only the sort order hid them.
--
-- Fix (owner decision 19-11-2026, Option A): add last_qualifying_flight, computed inside the view's own pilot_enroute_stats CTE,
-- which already filters completion_time >= NOW() - INTERVAL '90 days', so the new column shares the view's existing 90-day window
-- rather than a separate unfiltered lookup. The panel now sorts by this column instead (migration in the same pull request).
-- CREATE OR REPLACE VIEW can only append a column at the end, so every existing column keeps its name, type and position; the
-- watchdog's ops_never_flew_alert names its columns explicitly (SELECT v.cid, v.name, v.total_flights FROM pilot_never_flew_90d v ...,
-- migration 20261116000400), so it does not see the new column, and CREATE OR REPLACE VIEW keeps the view's existing grants because
-- the column list it already had is unchanged, only extended.
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
    SUM(time_online_minutes) as total_time_online_minutes,
    MAX(completion_time) as last_qualifying_flight
  FROM flight_summaries
  WHERE cid IS NOT NULL
  AND completion_time IS NOT NULL
  AND completion_time >= NOW() - INTERVAL '90 days'
  GROUP BY cid
  HAVING COUNT(*) > 0
)
SELECT cid, name, total_flights, flights_with_zero_enroute_time,
       avg_time_online_minutes, min_time_online_minutes, max_time_online_minutes, total_time_online_minutes,
       last_qualifying_flight
FROM pilot_enroute_stats
WHERE flights_with_enroute_time = 0
AND LENGTH(name) >= 4
AND SUBSTRING(name FROM LENGTH(name) - 3 FOR 1) = 'Y';

COMMENT ON VIEW pilot_never_flew_90d IS
  'Pilots with a finished flight in the last 90 days and none with airborne time, whose most recent non-empty name (the CID when there is none) has a Y as its fourth character from the end (the panel Pilots who logged on but never flew, 90 days). One row per cid. Columns: cid, name, total_flights, flights_with_zero_enroute_time, avg_time_online_minutes, min_time_online_minutes, max_time_online_minutes, total_time_online_minutes, last_qualifying_flight (the most recent completion_time inside this view''s own 90-day window, issue 406, so the panel can sort newly qualified pilots first without a separate unfiltered lookup). No row limit and no ordering: the panel adds its own ordering, percentage and limit, and the watchdog alert filters it by CID. Migrations 20261116000400 (issue 331), 20261119000100 (issue 406).';

-- migrate:down
-- Forward-only project: no automatic rollback (the previous definition is in migration 20261116000410).
