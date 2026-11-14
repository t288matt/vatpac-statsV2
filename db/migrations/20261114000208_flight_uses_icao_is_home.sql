-- migrate:up
SET LOCAL lock_timeout = '5s';   -- fail fast instead of queueing behind a long panel query
-- Issue #208: the flight view no longer keeps its own copy of the home-prefix list. touches_home_region reads the prefixes from the
-- home_prefix view (the same list, the same missing-setting fallback to 'Y' and the same norm_code() normalisation that icao_is_home()
-- uses for dep_is_home and arr_is_home in flight_ext), and tests each code with norm_code() as icao_is_home() does. The list is read
-- once per query, not once per row: calling icao_is_home() per row made the column about 30 times slower at 1 million flights.
-- Same columns, same order, same types (CREATE OR REPLACE VIEW, so the grants and comments stay).
--
-- What can differ: only when the setting home_icao_prefixes exists but holds no usable prefix (an empty or blank value). The old copy then
-- fell back to 'Y'; home_prefix has no prefix in that case and the column is FALSE, as dep_is_home and arr_is_home always were. The
-- collector refuses to publish such a value, so stored data never takes that path. Airport codes in flight_summary are stored already
-- normalised and never null, so norm_code() changes nothing for them.
CREATE OR REPLACE VIEW flight AS
WITH cfg AS (
    SELECT array_agg(prefix) AS home_prefixes FROM home_prefix
)
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
    EXISTS (
        SELECT 1 FROM unnest(c.home_prefixes) AS hp
        WHERE starts_with(norm_code(r.departure), hp) OR starts_with(norm_code(r.arrival), hp)) AS touches_home_region
FROM flight_summary r
CROSS JOIN cfg c;

-- migrate:down
-- Forward-only project: no automatic rollback (the previous definition is in migration 20261112000150).
