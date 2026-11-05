-- Issue 279 / ADR-035: remove the imported rows. The predicate is the whole definition of "imported": flight_summary.session_id < 0
-- and controller_session.id < 0 (live ids come from bigserials and are positive). Runs after common.sql.
-- The two event tables are not touched: they hold no rows for imported sessions, and a row there would make the DELETE fail on its
-- foreign key (stop and look, do not clear it).
\echo
\echo '== ROLLBACK OF THE v1 IMPORT =='
WITH d AS (DELETE FROM flight_summary WHERE session_id < 0 RETURNING 1) SELECT count(*) AS flights_deleted FROM d \gset
WITH d AS (DELETE FROM controller_session WHERE id < 0 RETURNING 1) SELECT count(*) AS controllers_deleted FROM d \gset
SELECT (SELECT count(*) FROM flight_summary WHERE session_id < 0) + (SELECT count(*) FROM controller_session WHERE id < 0) AS left_over,
       (SELECT count(*) FROM flight_summary WHERE session_id >= 0) AS flights_live, (SELECT count(*) FROM controller_session WHERE id > 0) AS controllers_live,
       (SELECT count(*) FROM v1_live_flights b WHERE NOT EXISTS (SELECT 1 FROM flight_summary s WHERE s.session_id = b.session_id AND s.start_time = b.start_time)) + (SELECT count(*) FROM v1_live_controllers b WHERE NOT EXISTS (SELECT 1 FROM controller_session s WHERE s.id = b.id)) AS live_missing \gset after_
SELECT :flights_deleted AS flights_deleted, :controllers_deleted AS controllers_deleted, :after_left_over AS left_over,
       :after_flights_live AS flights_live, :after_controllers_live AS controllers_live;
SELECT :after_left_over = 0 AND :after_live_missing = 0 AS checks_ok \gset
\if :checks_ok
\else
    DO $$ BEGIN RAISE EXCEPTION 'rollback checks failed; nothing committed'; END $$;
\endif
\if :is_apply
    \echo 'APPLY: committing the rollback.'
    COMMIT;
    ANALYZE flight_summary;
    ANALYZE controller_session;
\else
    \echo 'DRY RUN: rolling back the rollback. Nothing was changed.'
    ROLLBACK;
\endif
