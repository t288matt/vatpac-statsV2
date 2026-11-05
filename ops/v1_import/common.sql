-- Issue 279 / ADR-035: shared start of load.sql and rollback.sql. psql variables set by load.sh before this file:
-- expect_db (the database the operator named), is_apply (true = commit), allow_rejects, range_end, expect_flights, expect_controllers.
-- Everything runs in ONE transaction that only ends at the very bottom of the last file (COMMIT or ROLLBACK).
BEGIN;
SET LOCAL lock_timeout = '15s';
SET LOCAL statement_timeout = '15min';

SELECT current_database() = :'expect_db' AS target_ok \gset
\if :target_ok
\else
    \echo 'REFUSING: the connected database is not the one named with --expect-db. Nothing was read or written.'
    SELECT current_database() AS connected_database, :'expect_db' AS named_database;
    DO $$ BEGIN RAISE EXCEPTION 'target database mismatch'; END $$;
\endif

\echo
\echo '== TARGET (printed before anything is written) =='
SELECT current_database() AS database, current_user AS role, current_setting('server_version') AS server_version,
       CASE WHEN :is_apply THEN 'APPLY (commits)' ELSE 'DRY RUN (rolls back, writes nothing)' END AS mode;
SELECT (SELECT count(*) FROM flight_summary WHERE session_id >= 0) AS flights_live,
       (SELECT count(*) FROM flight_summary WHERE session_id < 0)  AS flights_imported,
       (SELECT count(*) FROM controller_session WHERE id > 0)      AS controllers_live,
       (SELECT count(*) FROM controller_session WHERE id < 0)      AS controllers_imported \gset before_
SELECT :before_flights_live AS flights_live, :before_flights_imported AS flights_imported,
       :before_controllers_live AS controllers_live, :before_controllers_imported AS controllers_imported;
