-- migrate:up
-- Issue #109. online_now looked up each online pilot's newest position with a plain LEFT JOIN. Postgres cannot estimate
-- how many sessions are online (the window comes from another table), so on many daily partitions it picks a hash join
-- that reads every position row of every partition: about 25 s at 240 partitions, against 6 ms with the lookup below.
-- LEFT JOIN LATERAL (... LIMIT 1) forces a nested loop with one primary-key probe per online pilot; the other partitions
-- are pruned at run time from ps.last_seen. (session_id, observed_at) is the primary key, so at most one row matches and
-- the result is identical to the plain join (same columns, order and types; dependents such as flight are untouched).
CREATE OR REPLACE VIEW online_now AS
WITH ref AS (
    SELECT max(observed_at) AS as_of
    FROM network_snapshot
    HAVING max(observed_at) > now() - interval '5 minutes'
)
SELECT 'pilot'::text AS kind, ps.id AS session_id, ps.cid, ps.callsign, ps.name, ps.logon_time,
       ps.first_seen, ps.last_seen, ref.as_of,
       ps.departure, ps.arrival, ps.aircraft_icao,
       pos.lat, pos.lon, pos.alt_ft, pos.gs_kt,
       NULL::int AS rating, NULL::int AS facility, NULL::bigint AS frequency_hz
FROM ref
JOIN pilot_session ps ON ps.last_seen > ref.as_of - make_interval(secs => 2 * (SELECT value::numeric FROM app_setting WHERE key = 'poll_interval_seconds'))
LEFT JOIN LATERAL (
    SELECT p.lat, p.lon, p.alt_ft, p.gs_kt
    FROM pilot_position p
    WHERE p.session_id = ps.id AND p.observed_at = ps.last_seen
    LIMIT 1  -- at most one row (primary key); LIMIT keeps the lookup a nested loop whatever the row estimates
) pos ON true
UNION ALL
SELECT 'controller'::text, cs.id, cs.cid, cs.callsign, cs.name, cs.logon_time,
       cs.first_seen, cs.last_seen, ref.as_of,
       NULL, NULL, NULL,
       NULL, NULL, NULL, NULL,
       cs.rating, cs.facility, cs.frequency_hz
FROM ref
JOIN controller_session cs ON cs.last_seen > ref.as_of - make_interval(secs => 2 * (SELECT value::numeric FROM app_setting WHERE key = 'poll_interval_seconds'));

-- migrate:down
-- Back to the plain LEFT JOIN of 20261009000120.
CREATE OR REPLACE VIEW online_now AS
WITH ref AS (
    SELECT max(observed_at) AS as_of
    FROM network_snapshot
    HAVING max(observed_at) > now() - interval '5 minutes'
)
SELECT 'pilot'::text AS kind, ps.id AS session_id, ps.cid, ps.callsign, ps.name, ps.logon_time,
       ps.first_seen, ps.last_seen, ref.as_of,
       ps.departure, ps.arrival, ps.aircraft_icao,
       pos.lat, pos.lon, pos.alt_ft, pos.gs_kt,
       NULL::int AS rating, NULL::int AS facility, NULL::bigint AS frequency_hz
FROM ref
JOIN pilot_session ps ON ps.last_seen > ref.as_of - make_interval(secs => 2 * (SELECT value::numeric FROM app_setting WHERE key = 'poll_interval_seconds'))
LEFT JOIN pilot_position pos ON pos.session_id = ps.id AND pos.observed_at = ps.last_seen
UNION ALL
SELECT 'controller'::text, cs.id, cs.cid, cs.callsign, cs.name, cs.logon_time,
       cs.first_seen, cs.last_seen, ref.as_of,
       NULL, NULL, NULL,
       NULL, NULL, NULL, NULL,
       cs.rating, cs.facility, cs.frequency_hz
FROM ref
JOIN controller_session cs ON cs.last_seen > ref.as_of - make_interval(secs => 2 * (SELECT value::numeric FROM app_setting WHERE key = 'poll_interval_seconds'));
