-- migrate:up
-- ADR-024 (owner decision 09-10-2026): the collector polls every 30 s instead of 15 s (the feed still updates every 15 s;
-- every second update is read). "Seen within the last two snapshots" was a 30 second window; it is now 60 seconds in
-- online_now and in controller_session_merged.is_open. The airborne gap cap follows the cadence (40 s to 75 s, about 2.5
-- intervals). The collector writes the cap to app_setting on start; the update here keeps the two consistent before that.
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
JOIN pilot_session ps ON ps.last_seen > ref.as_of - interval '60 seconds'
LEFT JOIN pilot_position pos ON pos.session_id = ps.id AND pos.observed_at = ps.last_seen
UNION ALL
SELECT 'controller'::text, cs.id, cs.cid, cs.callsign, cs.name, cs.logon_time,
       cs.first_seen, cs.last_seen, ref.as_of,
       NULL, NULL, NULL,
       NULL, NULL, NULL, NULL,
       cs.rating, cs.facility, cs.frequency_hz
FROM ref
JOIN controller_session cs ON cs.last_seen > ref.as_of - interval '60 seconds';

DO $$
DECLARE def text;
BEGIN
    def := pg_get_viewdef('controller_session_merged'::regclass, true);
    IF position('30 seconds' IN def) = 0 THEN
        RAISE EXCEPTION 'controller_session_merged: expected 30 second window not found';
    END IF;
    def := replace(def, '30 seconds', '60 seconds');
    EXECUTE 'CREATE OR REPLACE VIEW controller_session_merged AS ' || rtrim(def, '; ' || chr(10));
END $$;

UPDATE app_setting SET value = '75' WHERE key = 'airborne_gap_cap_seconds';

-- migrate:down
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

DO $$
DECLARE def text;
BEGIN
    def := pg_get_viewdef('controller_session_merged'::regclass, true);
    def := replace(def, '60 seconds', '30 seconds');
    EXECUTE 'CREATE OR REPLACE VIEW controller_session_merged AS ' || rtrim(def, '; ' || chr(10));
END $$;

UPDATE app_setting SET value = '40' WHERE key = 'airborne_gap_cap_seconds';
