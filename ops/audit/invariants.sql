-- Read-only data-integrity invariants for the live database (T30 audit, candidate for T16 check_data_integrity).
-- Run:  ( echo "SET default_transaction_read_only = on;"; cat ops/audit/invariants.sql ) | \
--       ssh vatpac-v3 'cd ~/vatpac-statsV2 && docker compose exec -T db sh -c '"'"'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X'"'"''
-- Every query returns one row: name, violations (expected 0), detail. Only SELECTs; the whole file is one repeatable-read
-- READ ONLY transaction so all figures describe the same instant. Positions are filtered to the last 3 days to stay light.
BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;

WITH w AS (SELECT now() - interval '3 days' AS since)
SELECT 'snapshot rows without an ok poll_log row' AS check_name, count(*) AS violations, NULL::text AS detail
FROM network_snapshot n, w
WHERE n.observed_at >= w.since
  AND NOT EXISTS (SELECT 1 FROM poll_log p WHERE p.status = 'ok' AND p.snapshot_ts = n.observed_at)
UNION ALL
SELECT 'ok poll_log counts differ from network_snapshot', count(*), NULL
FROM poll_log p JOIN network_snapshot n ON n.observed_at = p.snapshot_ts
WHERE p.status = 'ok' AND p.polled_at >= now() - interval '3 days'
  AND (p.pilots_seen, p.pilots_inside, p.controllers_seen, p.controllers_kept)
      IS DISTINCT FROM (n.pilots_global, n.pilots_in_region, n.controllers_global, n.controllers_in_region)
UNION ALL
SELECT 'in-region count above global count', count(*), NULL
FROM network_snapshot WHERE pilots_in_region > pilots_global OR controllers_in_region > controllers_global
UNION ALL
SELECT 'positions per snapshot differ from pilots_in_region', count(*), NULL
FROM network_snapshot n
WHERE n.observed_at >= now() - interval '3 days'
  AND n.pilots_in_region <> (SELECT count(*) FROM pilot_position p WHERE p.observed_at = n.observed_at)
UNION ALL
SELECT 'position rows at a timestamp with no network_snapshot', count(*), NULL
FROM (SELECT DISTINCT observed_at FROM pilot_position WHERE observed_at >= now() - interval '3 days') x
WHERE NOT EXISTS (SELECT 1 FROM network_snapshot n WHERE n.observed_at = x.observed_at)
UNION ALL
SELECT 'snapshot gaps over 45 s (last 3 days)', count(*), max(gap)::text
FROM (SELECT observed_at - lag(observed_at) OVER (ORDER BY observed_at) AS gap FROM network_snapshot
      WHERE observed_at >= now() - interval '3 days') g WHERE gap > interval '45 seconds'
UNION ALL
SELECT 'newest snapshot in the future or older than 5 minutes', count(*), max(observed_at)::text
FROM network_snapshot HAVING max(observed_at) > now() + interval '1 minute' OR max(observed_at) < now() - interval '5 minutes'
UNION ALL
SELECT 'pilot session first/last seen differ from first/last stored position', count(*), NULL
FROM pilot_session s
JOIN (SELECT session_id, min(observed_at) AS mn, max(observed_at) AS mx FROM pilot_position
      WHERE observed_at >= now() - interval '3 days' GROUP BY 1) m ON m.session_id = s.id
WHERE s.last_seen <> m.mx OR (s.first_seen <> m.mn AND s.first_seen >= now() - interval '3 days')
UNION ALL
SELECT 'pilot sessions with no position (active in last 3 days)', count(*), NULL
FROM pilot_session s WHERE s.last_seen >= now() - interval '3 days'
  AND NOT EXISTS (SELECT 1 FROM pilot_position p WHERE p.session_id = s.id AND p.observed_at >= now() - interval '3 days')
UNION ALL
SELECT 'identity duplicates (cid, callsign, logon_time)', count(*), NULL FROM (
  SELECT 1 FROM pilot_session GROUP BY cid, callsign, logon_time HAVING count(*) > 1) d
UNION ALL
SELECT 'positions outside plausibility caps', count(*), NULL FROM pilot_position
WHERE observed_at >= now() - interval '3 days' AND (gs_kt NOT BETWEEN 0 AND 700 OR alt_ft NOT BETWEEN -1500 AND 65000)
UNION ALL
-- The feed can restart and re-publish logon_time without its fractional seconds (seen 08-10-2026 04:08:27 UTC), which
-- creates a second session for every pilot online. Two sessions of one cid + callsign less than 2 s apart are never real.
SELECT 'pilot session pairs, same cid+callsign, logon_time within 2 s (identity flap)', count(*), min(a.first_seen)::text
FROM pilot_session a JOIN pilot_session b
  ON a.cid = b.cid AND a.callsign = b.callsign AND a.id < b.id AND abs(extract(epoch FROM a.logon_time - b.logon_time)) < 2
UNION ALL
SELECT 'controller session pairs, same cid+callsign, logon_time within 2 s (identity flap)', count(*), min(a.first_seen)::text
FROM controller_session a JOIN controller_session b
  ON a.cid = b.cid AND a.callsign = b.callsign AND a.id < b.id AND abs(extract(epoch FROM a.logon_time - b.logon_time)) < 2
UNION ALL
SELECT 'snapshots that created sessions for more than 50% of in-region pilots', count(*), NULL FROM (
  SELECT first_seen, count(*) AS n FROM pilot_session WHERE first_seen >= now() - interval '3 days' GROUP BY 1) c
JOIN network_snapshot n ON n.observed_at = c.first_seen WHERE n.pilots_in_region >= 10 AND c.n > 0.5 * n.pilots_in_region
UNION ALL
SELECT 'flight_summary rows missing for sessions older than 12 h', count(*), NULL
FROM pilot_session s WHERE s.last_seen < now() - interval '12 hours'
  AND NOT EXISTS (SELECT 1 FROM flight_summary f WHERE f.session_id = s.id)
UNION ALL
SELECT 'flight_summary sample_count differs from stored positions (final or settled sessions, last 3 days)', count(*), NULL FROM (
  SELECT f.session_id, sum(f.sample_count) AS sc,
         (SELECT count(*) FROM pilot_position p WHERE p.session_id = f.session_id AND p.observed_at >= now() - interval '3 days') AS pc
  FROM flight_summary f JOIN pilot_session s ON s.id = f.session_id
  WHERE s.last_seen < now() - interval '15 minutes' AND s.first_seen >= now() - interval '3 days' GROUP BY f.session_id) x
WHERE sc <> pc
UNION ALL
SELECT 'daily partitions missing for today..today+3', count(*), NULL FROM generate_series(0, 3) g(d)
WHERE to_regclass(format('public.pilot_position_%s', to_char((now() AT TIME ZONE 'UTC')::date + g.d, 'YYYYMMDD'))) IS NULL
UNION ALL
SELECT 'reference file hashes in poll_log differ from reference_data_change', count(*), NULL FROM (
  SELECT DISTINCT polygon_sha, whitelist_sha FROM poll_log WHERE status = 'ok' AND polled_at >= now() - interval '1 day') p
WHERE polygon_sha NOT IN (SELECT new_sha FROM reference_data_change) OR whitelist_sha NOT IN (SELECT new_sha FROM reference_data_change);

COMMIT;
