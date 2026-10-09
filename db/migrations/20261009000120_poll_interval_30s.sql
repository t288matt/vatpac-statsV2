-- migrate:up
-- ADR-024 (owner decision 09-10-2026): the poll interval is one variable, POLL_INTERVAL_SECONDS (now 30; the feed updates
-- every 15 s and every second update is read). The collector publishes it to app_setting('poll_interval_seconds') on start.
-- "Seen within the last two snapshots" is therefore 2 x that setting in online_now and in controller_session_merged.is_open
-- (it was a literal 30 seconds). No literal interval remains: until the collector has started and published the setting
-- the two windows are empty, which is also what a stopped collector should show.
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

DO $$
DECLARE def text;
BEGIN
    def := pg_get_viewdef('controller_session_merged'::regclass, true);
    -- pg_get_viewdef prints interval '30 seconds' as '00:00:30'::interval
    IF position('''00:00:30''::interval' IN def) = 0 THEN
        RAISE EXCEPTION 'controller_session_merged: expected 30 second window not found';
    END IF;
    def := replace(def, '''00:00:30''::interval', 'make_interval(secs => 2 * (SELECT value::numeric FROM app_setting WHERE key = ''poll_interval_seconds''))');
    EXECUTE 'CREATE OR REPLACE VIEW controller_session_merged AS ' || rtrim(def, '; ' || chr(10));
END $$;

-- The monitoring panel and the soak report need the cadence; grafana_ro reads views only.
CREATE OR REPLACE VIEW poll_cadence AS
SELECT value::numeric AS poll_interval_s FROM app_setting WHERE key = 'poll_interval_seconds';
COMMENT ON VIEW poll_cadence IS 'The collector poll interval in seconds (POLL_INTERVAL_SECONDS), published by the collector on start.';

-- migrate:down
DROP VIEW IF EXISTS poll_cadence;
-- controller_session_merged.is_open keeps following poll_interval_seconds after a rollback: pg_get_viewdef reformats the
-- expression, so it cannot be turned back safely, and the window stays correct as long as the setting exists (it is not
-- deleted below).
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
