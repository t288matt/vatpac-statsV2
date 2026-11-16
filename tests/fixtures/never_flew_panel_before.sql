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
SELECT
  cid,
  name,
  total_flights,
  flights_with_zero_enroute_time,
  ROUND(flights_with_zero_enroute_time * 100.0 / total_flights, 2) as zero_enroute_percentage,
  ROUND(avg_time_online_minutes, 2) as avg_time_online_minutes,
  min_time_online_minutes,
  max_time_online_minutes,
  total_time_online_minutes
FROM pilot_enroute_stats
WHERE flights_with_enroute_time = 0
AND LENGTH(name) >= 4
AND SUBSTRING(name FROM LENGTH(name) - 3 FOR 1) = 'Y'
ORDER BY total_flights DESC, name
LIMIT 200
