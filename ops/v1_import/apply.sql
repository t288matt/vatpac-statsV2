-- Issue 279 / ADR-035: map the staged v1 rows, insert them, check the result, then COMMIT (apply) or ROLLBACK (dry run).
-- Runs after common.sql, stage.sql and the COPY blocks load.sh streams in. The text rules are the collector's (stage.sql).

\echo
\echo '== STAGED =='
SELECT (SELECT count(*) FROM v1_flight) AS flights_in_file, :expect_flights AS flights_expected,
       (SELECT count(*) FROM v1_controller) AS controllers_in_file, :expect_controllers AS controllers_expected,
       (SELECT count(*) FROM v1_flight) = :expect_flights AND (SELECT count(*) FROM v1_controller) = :expect_controllers AS counts_ok \gset staged_
SELECT :staged_flights_in_file AS flights_in_file, :staged_flights_expected AS flights_expected,
       :staged_controllers_in_file AS controllers_in_file, :staged_controllers_expected AS controllers_expected;
\if :staged_counts_ok
\else
    DO $$ BEGIN RAISE EXCEPTION 'the CSV row counts differ from EXPECTED_COUNTS: the extract is incomplete or the wrong files were given'; END $$;
\endif

-- Flights. reason is NULL for a row that can be loaded; otherwise the FIRST rule it breaks (nothing is dropped without a reason).
CREATE TEMP TABLE v1_flight_x ON COMMIT DROP AS
SELECT x.*,
       CASE WHEN x.cid IS NULL THEN 'cid is null'
            WHEN x.callsign IS NULL THEN 'callsign is blank'
            WHEN x.departure IS NULL THEN 'departure is blank'
            WHEN x.arrival IS NULL THEN 'arrival is blank'
            WHEN x.logon_time IS NULL THEN 'logon_time is null'
            WHEN x.completion_time IS NULL THEN 'completion_time is null'
            WHEN x.completion_time >= :'range_end'::timestamptz THEN 'completion_time is outside the range'
            WHEN x.time_online_minutes IS NULL OR x.time_online_minutes NOT BETWEEN 0 AND 1000000 THEN 'time_online_minutes is null, negative or absurd'
            WHEN x.enroute < 0 THEN 'total_enroute_time_minutes is negative'
       END AS reason,
       EXISTS (SELECT 1 FROM flight_summary s WHERE s.session_id = x.session_id) AS already_loaded
FROM (
    SELECT f.id AS v1_id, -f.id AS session_id, f.cid,
           pg_temp.v1_cap_indexed(regexp_replace(pg_temp.v1_clean(f.callsign), '^\s+|\s+$', '', 'g')) AS callsign,
           pg_temp.v1_cap_indexed(upper(btrim(pg_temp.v1_clean(f.departure), E' \t\r\n'))) AS departure,
           pg_temp.v1_cap_indexed(upper(btrim(pg_temp.v1_clean(f.arrival), E' \t\r\n'))) AS arrival,
           date_trunc('second', f.logon_time) AS logon_time, f.completion_time, f.time_online_minutes,
           f.total_enroute_time_minutes AS enroute,
           CASE WHEN f.time_online_minutes BETWEEN 0 AND 1000000 THEN f.completion_time - make_interval(mins => f.time_online_minutes) END AS start_time,
           pg_temp.v1_text(f.name) AS name,
           substring(regexp_replace(pg_temp.v1_text(f.name), '\s+$', '') from '\s([A-Z]{4})$') AS home_icao,
           pg_temp.v1_text(f.server) AS server,
           upper(btrim(pg_temp.v1_text(f.aircraft_type))) AS aircraft_icao,
           pg_temp.v1_text(f.aircraft_faa) AS aircraft_faa, pg_temp.v1_text(f.flight_rules) AS flight_rules,
           pg_temp.v1_text(f.route) AS route, pg_temp.v1_text(f.planned_altitude) AS planned_alt, pg_temp.v1_text(f.deptime) AS deptime,
           f.pilot_rating, f.military_rating,
           CASE WHEN f.total_enroute_time_minutes IS NOT NULL AND f.time_online_minutes IS NOT NULL
                THEN least(f.total_enroute_time_minutes, f.time_online_minutes) END AS airborne_minutes
    FROM v1_flight f) x;

-- Controllers. The callsign must be on the v3 whitelist (the collector would never have stored it otherwise).
CREATE TEMP TABLE v1_whitelist ON COMMIT DROP AS
SELECT DISTINCT btrim(split_part(line, ',', 1), E' \t\r') AS callsign FROM v1_whitelist_raw
WHERE btrim(line, E' \t\r') <> '' AND btrim(line, E' \t\r') !~ '^#';
CREATE TEMP TABLE v1_controller_x ON COMMIT DROP AS
SELECT z.*,
       EXISTS (SELECT 1 FROM controller_session s WHERE s.id = z.id) AS already_loaded
FROM (
    SELECT y.*,
           CASE WHEN y.cid IS NULL THEN 'cid is null'
                WHEN y.callsign IS NULL THEN 'callsign is blank'
                WHEN y.logon_time IS NULL THEN 'session_start_time is null'
                WHEN y.last_seen IS NULL THEN 'session_end_time is null'
                WHEN y.last_seen < y.first_seen THEN 'session_end_time is before session_start_time'
                WHEN y.logon_time >= :'range_end'::timestamptz THEN 'session_start_time is outside the range'
                WHEN NOT EXISTS (SELECT 1 FROM v1_whitelist w WHERE w.callsign = y.callsign) THEN 'callsign is not on the v3 whitelist'
                WHEN row_number() OVER (PARTITION BY y.cid, y.callsign, y.logon_time ORDER BY y.v1_id) > 1
                     THEN 'same cid, callsign and whole-second logon as an earlier row of the file'
                WHEN EXISTS (SELECT 1 FROM controller_session s WHERE s.cid = y.cid AND s.callsign = y.callsign
                                                                  AND s.logon_time = y.logon_time AND s.id <> y.id)
                     THEN 'cid, callsign and logon_time are held by another session in the table'
           END AS reason
    FROM (
        SELECT c.id AS v1_id, -c.id AS id, c.cid,
               pg_temp.v1_cap_indexed(regexp_replace(pg_temp.v1_clean(c.callsign), '^\s+|\s+$', '', 'g')) AS callsign,
               date_trunc('second', c.session_start_time) AS logon_time, date_trunc('second', c.session_start_time) AS first_seen,
               c.session_end_time AS last_seen, pg_temp.v1_text(c.name) AS name, c.rating, c.facility, pg_temp.v1_text(c.server) AS server,
               pg_temp.v1_first_freq(c.frequencies_used) AS frequency_hz
        FROM v1_controller c) y) z;

\echo
\echo '== RESULT BEFORE WRITING =='
SELECT 'flights' AS what, count(*) AS in_file, count(*) FILTER (WHERE reason IS NOT NULL) AS rejected,
       count(*) FILTER (WHERE reason IS NULL AND already_loaded) AS already_loaded,
       count(*) FILTER (WHERE reason IS NULL AND NOT already_loaded) AS to_insert FROM v1_flight_x
UNION ALL
SELECT 'controllers', count(*), count(*) FILTER (WHERE reason IS NOT NULL), count(*) FILTER (WHERE reason IS NULL AND already_loaded),
       count(*) FILTER (WHERE reason IS NULL AND NOT already_loaded) FROM v1_controller_x;
SELECT 'flights' AS what, reason, count(*) AS rows, min(v1_id) AS first_v1_id FROM v1_flight_x WHERE reason IS NOT NULL GROUP BY reason
UNION ALL
SELECT 'controllers', reason, count(*), min(v1_id) FROM v1_controller_x WHERE reason IS NOT NULL GROUP BY reason ORDER BY 1, 2;
SELECT (SELECT count(*) FROM v1_flight_x WHERE reason IS NOT NULL) + (SELECT count(*) FROM v1_controller_x WHERE reason IS NOT NULL) AS rejects,
       (SELECT count(*) FROM v1_flight_x x JOIN flight_summary s ON s.session_id = x.session_id
         WHERE x.reason IS NULL AND (s.start_time, s.end_time, s.cid, s.callsign, s.departure, s.arrival, s.airborne_minutes)
               IS DISTINCT FROM (x.start_time, x.completion_time, x.cid, x.callsign, x.departure, x.arrival, x.airborne_minutes))
       + (SELECT count(*) FROM v1_controller_x x JOIN controller_session s ON s.id = x.id
         WHERE x.reason IS NULL AND (s.cid, s.callsign, s.logon_time, s.last_seen, s.frequency_hz)
               IS DISTINCT FROM (x.cid, x.callsign, x.logon_time, x.last_seen, x.frequency_hz)) AS already_loaded_but_different \gset
SELECT :rejects AS rejects, :already_loaded_but_different AS already_loaded_but_different;
SELECT ((:rejects > 0 OR :already_loaded_but_different > 0) AND NOT :allow_rejects) AS stop \gset
\if :stop
    \echo 'STOP: rejected rows exist, or rows already loaded differ from the file (already_loaded_but_different above). Nothing was written.'
    \echo 'Read the reasons above; --allow-rejects loads the rest and leaves already-loaded rows as they are.'
    DO $$ BEGIN RAISE EXCEPTION 'rejected rows, or already-loaded rows that differ from the file; nothing written'; END $$;
\endif

\echo
\echo '== WRITING (timed) =='
\timing on
WITH ins AS (
    INSERT INTO flight_summary (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, logon_time, name,
                                home_icao, server, aircraft_icao, aircraft_faa, flight_rules, route, remarks, planned_alt, deptime,
                                pilot_rating, military_rating, airborne_minutes, max_gs_kt, max_alt_ft, sample_count)
    SELECT x.session_id, x.start_time, x.completion_time, x.cid, x.callsign, x.departure, x.arrival, ARRAY[x.session_id], x.logon_time, x.name,
           x.home_icao, x.server, x.aircraft_icao, x.aircraft_faa, x.flight_rules, x.route, NULL, x.planned_alt, x.deptime,
           x.pilot_rating, x.military_rating, x.airborne_minutes, NULL, NULL, 0
    FROM v1_flight_x x WHERE x.reason IS NULL AND NOT x.already_loaded
    ON CONFLICT DO NOTHING RETURNING 1)
SELECT count(*) AS flights_inserted FROM ins \gset
WITH ins AS (
    INSERT INTO controller_session (id, cid, callsign, logon_time, name, rating, facility, server, frequency_hz, visual_range, first_seen, last_seen)
    SELECT x.id, x.cid, x.callsign, x.logon_time, x.name, x.rating, x.facility, x.server, x.frequency_hz, NULL, x.first_seen, x.last_seen
    FROM v1_controller_x x WHERE x.reason IS NULL AND NOT x.already_loaded
    ON CONFLICT DO NOTHING RETURNING 1)
SELECT count(*) AS controllers_inserted FROM ins \gset
\timing off

-- Checks: every loadable row is now in the table; every live row that existed at the start is still there (the live count may GROW: the collector and the writer keep
-- committing while this transaction runs, and this script only ever inserts negative ids); the imported total is what we expect.
SELECT (SELECT count(*) FROM v1_flight_x x WHERE x.reason IS NULL AND NOT EXISTS (SELECT 1 FROM flight_summary s WHERE s.session_id = x.session_id))
     + (SELECT count(*) FROM v1_controller_x x WHERE x.reason IS NULL AND NOT EXISTS (SELECT 1 FROM controller_session s WHERE s.id = x.id)) AS missing,
       (SELECT count(*) FROM flight_summary WHERE session_id >= 0) AS flights_live, (SELECT count(*) FROM flight_summary WHERE session_id < 0) AS flights_imported,
       (SELECT count(*) FROM controller_session WHERE id > 0) AS controllers_live, (SELECT count(*) FROM controller_session WHERE id < 0) AS controllers_imported,
       (SELECT count(*) FROM v1_live_flights b WHERE NOT EXISTS (SELECT 1 FROM flight_summary s WHERE s.session_id = b.session_id AND s.start_time = b.start_time)) + (SELECT count(*) FROM v1_live_controllers b WHERE NOT EXISTS (SELECT 1 FROM controller_session s WHERE s.id = b.id)) AS live_missing \gset after_
SELECT :after_missing = 0 AND :after_live_missing = 0
       AND :after_flights_imported = :before_flights_imported + :flights_inserted
       AND :after_controllers_imported = :before_controllers_imported + :controllers_inserted AS checks_ok \gset
\echo
\echo '== AFTER =='
SELECT :flights_inserted AS flights_inserted, :controllers_inserted AS controllers_inserted, :after_missing AS loadable_rows_missing,
       :after_flights_live AS flights_live, :after_flights_imported AS flights_imported,
       :after_controllers_live AS controllers_live, :after_controllers_imported AS controllers_imported, :'checks_ok' AS checks_ok;
\if :checks_ok
\else
    DO $$ BEGIN RAISE EXCEPTION 'post-load checks failed (a loadable row is missing, or a live row is gone); nothing committed'; END $$;
\endif
\echo 'Imported rows by month (flights by end_time, controllers by logon_time, UTC):'
SELECT to_char(date_trunc('month', m), 'MM-YYYY') AS month, sum(f) AS flights, sum(c) AS controllers FROM (
    SELECT end_time AT TIME ZONE 'UTC' AS m, 1 AS f, 0 AS c FROM flight_summary WHERE session_id < 0
    UNION ALL SELECT logon_time AT TIME ZONE 'UTC', 0, 1 FROM controller_session WHERE id < 0) t GROUP BY date_trunc('month', m) ORDER BY date_trunc('month', m);

\if :is_apply
    \echo 'APPLY: committing.'
    COMMIT;
    ANALYZE flight_summary;
    ANALYZE controller_session;
    \echo 'Done. To undo: sh ops/v1_import/load.sh --rollback --apply --expect-db NAME'
\else
    \echo 'DRY RUN: rolling back. Nothing was written.'
    ROLLBACK;
\endif
