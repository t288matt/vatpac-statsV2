-- Issue 279 / ADR-035: staging tables and the text rules. All objects are TEMP (gone at COMMIT or ROLLBACK).
-- The column names and order of v1_flight and v1_controller are exactly the header of the frozen CSVs (COPY ... HEADER MATCH).
CREATE TEMP TABLE v1_flight (
    id bigint PRIMARY KEY, cid bigint, callsign text, departure text, arrival text, logon_time timestamptz, name text,
    server text, flight_rules text, route text, deptime text, aircraft_type text, aircraft_faa text, planned_altitude text,
    pilot_rating int, military_rating int, time_online_minutes int, total_enroute_time_minutes int, completion_time timestamptz
) ON COMMIT DROP;
CREATE TEMP TABLE v1_controller (
    id bigint PRIMARY KEY, cid bigint, callsign text, name text, session_start_time timestamptz, session_end_time timestamptz,
    rating int, facility int, server text, frequencies_used jsonb
) ON COMMIT DROP;
CREATE TEMP TABLE v1_whitelist_raw (line text) ON COMMIT DROP;

-- The collector's text rule (model._s): tab and newline become a space, control characters are dropped, blank becomes NULL.
CREATE FUNCTION pg_temp.v1_clean(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN r ~ '^\s*$' THEN NULL ELSE r END
    FROM (SELECT regexp_replace(regexp_replace(t, '[\t\n]+', ' ', 'g'), '[\x01-\x08\x0b-\x1f\x7f-\x9f]', '', 'g') AS r) s
$$;
-- The collector's caps (model.cap_text): 2000 characters for every text field; callsign, departure and arrival also 500 UTF-8 bytes.
CREATE FUNCTION pg_temp.v1_text(t text) RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT left(pg_temp.v1_clean(t), 2000) $$;
CREATE FUNCTION pg_temp.v1_cap_indexed(t text) RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE r text := left(t, 2000);
BEGIN
    IF r IS NULL THEN RETURN NULL; END IF;
    r := left(r, 500);
    WHILE octet_length(r) > 500 LOOP r := left(r, length(r) - 1); END LOOP;
    RETURN r;
END $$;
-- The collector's frequency rule (model.parse_frequency_hz) applied to the first usable element of v1's list: MHz x 1,000,000 cut to a
-- whole number; zero, junk and 199.998 (the feed's "no frequency") are skipped; NULL when no element is usable.
CREATE FUNCTION pg_temp.v1_first_freq(f jsonb) RETURNS bigint LANGUAGE sql IMMUTABLE AS $$
    SELECT hz FROM (
        SELECT e.n, CASE WHEN e.v ~ '^\s*[0-9]{1,6}(\.[0-9]{1,9})?\s*$' THEN trunc(e.v::numeric * 1000000)::bigint END AS hz
        FROM jsonb_array_elements_text(CASE WHEN jsonb_typeof(f) = 'array' THEN f ELSE '[]'::jsonb END) WITH ORDINALITY AS e(v, n)) q
    WHERE hz > 0 AND hz <> 199998000 ORDER BY n LIMIT 1
$$;
