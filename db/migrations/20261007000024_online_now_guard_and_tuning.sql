-- migrate:up
-- L5: online_now respects a wall-clock guard. L2: pilot_session storage tuning. M12: movements comment.

-- online_now: pilots and controllers seen within the last 2 snapshots, relative to the newest stored
-- snapshot (as_of), but only while that snapshot is itself within 5 minutes of now(); if the collector
-- is dead the view is empty instead of showing the last pilots online forever. Same columns as before.
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
JOIN pilot_session ps ON ps.last_seen > ref.as_of - interval '30 seconds'
LEFT JOIN pilot_position pos ON pos.session_id = ps.id AND pos.observed_at = ps.last_seen
UNION ALL
SELECT 'controller'::text, cs.id, cs.cid, cs.callsign, cs.name, cs.logon_time,
       cs.first_seen, cs.last_seen, ref.as_of,
       NULL, NULL, NULL,
       NULL, NULL, NULL, NULL,
       cs.rating, cs.facility, cs.frequency_hz
FROM ref
JOIN controller_session cs ON cs.last_seen > ref.as_of - interval '30 seconds';

-- pilot_session is updated on every poll for every pilot online: leave room for HOT updates and vacuum
-- it more often than the default 20% dead-tuple threshold.
ALTER TABLE pilot_session SET (fillfactor = 70, autovacuum_vacuum_scale_factor = 0.02);

COMMENT ON VIEW movements IS
    'Departures (at flight start) and arrivals (at flight end) at home-region airports. Counts every flight-gate session, including sessions that never became airborne (airborne_minutes = 0); SPEC decision pending on excluding them.';

-- migrate:down
-- Forward-only project: no automatic rollback.
