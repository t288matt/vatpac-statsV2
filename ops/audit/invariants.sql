-- Read-only data-integrity invariants for the live database (T30 audit, candidate for T16 check_data_integrity).
-- Run:  ( echo "SET default_transaction_read_only = on;"; cat ops/audit/invariants.sql ) | \
--       ssh vatpac-v3 'cd ~/vatpac-statsV2 && docker compose exec -T db sh -c '"'"'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X'"'"''
-- Every invariant returns exactly one row: name, violations (expected 0), detail. The watchdog (ops/watchdog/integrity.py) runs this
-- file every 72 hours and fails if a row is missing, so keep each invariant as one SELECT of the form: name literal, count(*), detail. Only SELECTs; the whole file is one repeatable-read
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
  -- network_snapshot.pilots_in_region is the pilots actually stored, so it is compared with poll_log.pilots_kept
  -- (a row without pilots_kept falls back to pilots_inside; pilots_inside counts every pilot in the polygon, rejected ones too; they differ only when a record was skipped)
  AND (p.pilots_seen, coalesce(p.pilots_kept, p.pilots_inside), p.controllers_seen, p.controllers_kept)
      IS DISTINCT FROM (n.pilots_global, n.pilots_in_region, n.controllers_global, n.controllers_in_region)
UNION ALL
SELECT 'in-region count above global count', count(*), NULL
FROM network_snapshot WHERE pilots_in_region > pilots_global OR controllers_in_region > controllers_global
UNION ALL
-- (one grouped pass over the positions; a per-snapshot subquery took 22 s on live data because no index starts with observed_at)
SELECT 'positions per snapshot differ from pilots_in_region', count(*), NULL
FROM network_snapshot n
LEFT JOIN (SELECT observed_at, count(*) AS c FROM pilot_position WHERE observed_at >= now() - interval '3 days' GROUP BY 1) p
  ON p.observed_at = n.observed_at
WHERE n.observed_at >= now() - interval '3 days' AND n.pilots_in_region <> coalesce(p.c, 0)
UNION ALL
SELECT 'position rows at a timestamp with no network_snapshot', count(*), NULL
FROM (SELECT DISTINCT observed_at FROM pilot_position WHERE observed_at >= now() - interval '3 days') x
WHERE NOT EXISTS (SELECT 1 FROM network_snapshot n WHERE n.observed_at = x.observed_at)
UNION ALL
-- (5 minutes, the watchdog's own page threshold for a stale poll. The first version said 45 s, which was right for 15 s polling
-- but flagged ordinary 30 s polling (a feed tick missed: 45.0002 s) and every restart; shorter gaps are the soak report's job.)
SELECT 'snapshot gaps over 5 minutes (last 3 days)', count(*), max(gap)::text
FROM (SELECT observed_at - lag(observed_at) OVER (ORDER BY observed_at) AS gap FROM network_snapshot
      WHERE observed_at >= now() - interval '3 days') g WHERE gap > interval '5 minutes'
UNION ALL
-- (always returns its row: count 0 when the newest snapshot is fresh, or when there is no snapshot yet)
SELECT 'newest snapshot in the future or older than 5 minutes', count(*), max(observed_at)::text
FROM (SELECT max(observed_at) AS observed_at FROM network_snapshot) m
WHERE observed_at > now() + interval '1 minute' OR observed_at < now() - interval '5 minutes'
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
  AND n.observed_at > (SELECT min(observed_at) FROM network_snapshot)  -- the very first snapshot of a database creates every session
UNION ALL
-- Issue 89 phase 1: the flight writer (write_finished_flights, table flight_summary). A flight is written once THE FLIGHT has had no
-- position for the quiet period (owner decision 12-10-2026: quiet per flight, not per pilot). "Quiet" is measured against the
-- newest stored snapshot, as the writer does, never the clock. The flights below are the v1 groups of unprocessed sessions (same
-- cid, callsign, departure, arrival; a session joins when its logon_time is within 120 minutes of the latest earlier last_seen),
-- built from session times (the writer uses position times, which differ by seconds). A session without a plan is its own group.
-- The 9 hours are the default quiet period of 8 hours plus 1; watchdog_ro cannot read app_setting, so if the owner changes
-- flight_quiet_hours this literal must follow (docs/CONFIG_MAP.md).
SELECT 'flights quiet longer than the quiet period plus 1 hour with unprocessed positions', count(*), NULL FROM (
WITH g AS (
    SELECT s.cid, s.id, s.callsign, s.logon_time, s.first_seen, s.last_seen,
           NULLIF(upper(btrim(s.departure, E' \t\r\n')), '') AS dep, NULLIF(upper(btrim(s.arrival, E' \t\r\n')), '') AS arr
    FROM pilot_session s WHERE s.flown_upto IS NULL OR s.last_seen > s.flown_upto),
  o AS (
    SELECT g.*, max(g.last_seen) OVER (PARTITION BY g.cid, g.callsign, g.dep, g.arr ORDER BY g.first_seen, g.id
                                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end
    FROM g WHERE g.dep IS NOT NULL AND g.arr IS NOT NULL),
  c AS (
    SELECT o.*, sum(CASE WHEN o.prev_end IS NULL OR o.logon_time - o.prev_end > interval '120 minutes' THEN 1 ELSE 0 END)
                  OVER (PARTITION BY o.cid, o.callsign, o.dep, o.arr ORDER BY o.first_seen, o.id ROWS UNBOUNDED PRECEDING) AS chain
    FROM o),
  grp AS (
    SELECT max(c.last_seen) AS newest, count(*) AS n FROM c GROUP BY c.cid, c.callsign, c.dep, c.arr, c.chain
    UNION ALL SELECT g.last_seen, 1 FROM g WHERE g.dep IS NULL OR g.arr IS NULL)
  SELECT 1 FROM grp WHERE grp.newest <= (SELECT max(observed_at) FROM network_snapshot) - interval '9 hours') q
UNION ALL
SELECT 'flights written in the last 3 days whose sample_count differs from the stored positions of their sessions', count(*), NULL FROM (
  SELECT f.session_id, f.start_time, f.sample_count,
         (SELECT count(*) FROM pilot_position p
           WHERE p.session_id = ANY (f.session_ids) AND p.observed_at BETWEEN f.start_time AND f.end_time) AS pc
  FROM flight_summary f WHERE f.written_at >= now() - interval '3 days' AND f.sample_count > 0) x
WHERE sample_count <> pc
UNION ALL
-- Per flight too: sessions of a flight whose newest last_seen is more than 2 days behind the newest snapshot and that is still
-- unprocessed. A pilot who flew earlier and is online again in a DIFFERENT flight is not counted.
SELECT 'sessions ended more than 2 days ago with unprocessed positions', count(*), NULL FROM (
WITH g AS (
    SELECT s.cid, s.id, s.callsign, s.logon_time, s.first_seen, s.last_seen,
           NULLIF(upper(btrim(s.departure, E' \t\r\n')), '') AS dep, NULLIF(upper(btrim(s.arrival, E' \t\r\n')), '') AS arr
    FROM pilot_session s WHERE s.flown_upto IS NULL OR s.last_seen > s.flown_upto),
  o AS (
    SELECT g.*, max(g.last_seen) OVER (PARTITION BY g.cid, g.callsign, g.dep, g.arr ORDER BY g.first_seen, g.id
                                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end
    FROM g WHERE g.dep IS NOT NULL AND g.arr IS NOT NULL),
  c AS (
    SELECT o.*, sum(CASE WHEN o.prev_end IS NULL OR o.logon_time - o.prev_end > interval '120 minutes' THEN 1 ELSE 0 END)
                  OVER (PARTITION BY o.cid, o.callsign, o.dep, o.arr ORDER BY o.first_seen, o.id ROWS UNBOUNDED PRECEDING) AS chain
    FROM o),
  grp AS (
    SELECT max(c.last_seen) AS newest, count(*) AS n FROM c GROUP BY c.cid, c.callsign, c.dep, c.arr, c.chain
    UNION ALL SELECT g.last_seen, 1 FROM g WHERE g.dep IS NULL OR g.arr IS NULL)
  SELECT generate_series(1, grp.n) FROM grp WHERE grp.newest < (SELECT max(observed_at) FROM network_snapshot) - interval '2 days') q
UNION ALL
-- Issue 89 phase 4: the raw-position drop gate (drop_old_pilot_position_partitions) keeps a day when a position is processed but lies in
-- no written flight. This is the same condition seen from the session side, so the owner hears of it long before a partition is
-- due (the NOTICE "DROP REFUSED" in the collector log is the other half). Only sessions that still have raw positions, ended 2 to 30
-- days before the newest snapshot, with a flight plan (departure and arrival non-empty, normalised as the writer does).
SELECT 'processed flight-plan sessions in no written flight (ended 2 to 30 days ago)', count(*), NULL
FROM pilot_session s
WHERE s.flown_upto >= s.last_seen
  AND s.last_seen < (SELECT max(observed_at) FROM network_snapshot) - interval '2 days'
  AND s.last_seen >= (SELECT max(observed_at) FROM network_snapshot) - interval '30 days'
  AND NULLIF(upper(btrim(s.departure, E' \t\r\n')), '') IS NOT NULL
  AND NULLIF(upper(btrim(s.arrival,   E' \t\r\n')), '') IS NOT NULL
  AND EXISTS (SELECT 1 FROM pilot_position p WHERE p.session_id = s.id AND p.observed_at >= s.first_seen AND p.observed_at <= s.last_seen)
  AND s.id NOT IN (SELECT unnest(f.session_ids) FROM flight_summary f
                    WHERE f.end_time >= (SELECT max(observed_at) FROM network_snapshot) - interval '45 days')
UNION ALL
-- Issue 89 phase 4: a day of raw positions that the drop gate has refused for more than 3 days (the table is written by
-- drop_old_pilot_position_partitions and emptied when the day reconciles or is dropped). Covers a refusal at any age, e.g. at day 90 to 180.
SELECT 'raw-position days kept by the deletion check for more than 3 days', count(*), min(part_name)
FROM drop_gate_refusal WHERE first_refused < now() - interval '3 days'
UNION ALL
SELECT 'daily partitions missing for today..today+3', count(*), NULL FROM generate_series(0, 3) g(d)
WHERE to_regclass(format('public.pilot_position_%s', to_char((now() AT TIME ZONE 'UTC')::date + g.d, 'YYYYMMDD'))) IS NULL
UNION ALL
SELECT 'reference file hashes in poll_log differ from reference_data_change', count(*), NULL FROM (
  SELECT DISTINCT polygon_sha, whitelist_sha FROM poll_log WHERE status = 'ok' AND polled_at >= now() - interval '1 day') p
WHERE polygon_sha NOT IN (SELECT new_sha FROM reference_data_change) OR whitelist_sha NOT IN (SELECT new_sha FROM reference_data_change);

COMMIT;
