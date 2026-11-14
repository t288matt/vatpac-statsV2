-- migrate:up
SET LOCAL lock_timeout = '5s';   -- fail fast instead of queueing behind a long panel query
-- Issue #208: the flight view no longer keeps its own copy of the home-prefix list. touches_home_region now asks icao_is_home(), the same
-- function flight_ext uses for dep_is_home and arr_is_home, so one rule (norm_code and the home_prefix view) answers "is this airport in
-- the home region" in every view. Same columns, same order, same types (CREATE OR REPLACE VIEW, so the grants and comments stay).
--
-- What can differ: only when the setting home_icao_prefixes exists but holds no usable prefix (an empty or blank value). The old copy then
-- fell back to 'Y'; home_prefix, and so icao_is_home(), has no prefix in that case and answers FALSE, as flight_ext always did. The
-- collector refuses to publish such a value, so stored data never takes that path. Airport codes in flight_summary are stored already
-- normalised and never null, so the normalisation inside icao_is_home() changes nothing for them.
CREATE OR REPLACE VIEW flight AS
SELECT
    r.session_id,
    r.cid, r.callsign, r.logon_time, r.name, r.home_icao, r.server,
    r.departure, r.arrival,
    r.aircraft_icao, r.aircraft_faa, r.flight_rules, r.route, r.remarks, r.planned_alt, r.deptime,
    r.pilot_rating, r.military_rating,
    r.start_time, r.end_time,
    r.end_time - r.start_time AS duration,
    round(extract(epoch FROM r.end_time - r.start_time) / 60.0, 1) AS duration_minutes,
    r.airborne_minutes, r.max_gs_kt, r.max_alt_ft, r.sample_count,
    (r.departure = r.arrival) AS is_circuit,
    ('ZZZZ' IN (r.departure, r.arrival)) AS is_zzzz,
    (icao_is_home(r.departure) OR icao_is_home(r.arrival)) AS touches_home_region
FROM flight_summary r;

-- migrate:down
-- Forward-only project: no automatic rollback (the previous definition is in migration 20261112000150).
