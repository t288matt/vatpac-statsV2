-- migrate:up
SET LOCAL lock_timeout = '5s';   -- fail fast instead of queueing behind a long panel query
-- Issue #89 phase 5 (issue #141, design docs/DESIGN_89_FINAL.md sections 1.2, 4, 7.2, 7.4 to 7.6): remove the old flight machinery
-- and give the flight table its final name. DEPLOYED (listed in the live schema_migrations, checked 21-10-2026). FORWARD-ONLY AND DESTRUCTIVE: after this the
-- pieces table, both refresh functions and flight_legacy are gone and the only way back is the pre-deploy dump
-- (docs/RUNBOOKS/restore.md). Deploy only after phases 1, 3 and 4 are live and the second comparison run (ops/compare_flights_89.sql,
-- which reads the pieces and so must be run BEFORE this migration) is clean.
--
--   1. DROP VIEW flight_legacy, DROP VIEW flight_part, DROP FUNCTION refresh_flight_summary(interval) and
--      refresh_flight_summary_sessions(bigint[], boolean), DROP TABLE flight_summary (the PIECES: gap_segment, span_part, final).
--      No CASCADE anywhere: an object that still depends on one of them (monday_attendance, a panel view, a forgotten function
--      view) makes the migration fail and roll back, which is the wanted outcome. Order: flight_legacy (reads flight_part), flight_part
--      (reads the table), the functions (read the table), the table.
--   2. ALTER TABLE flight_record RENAME TO flight_summary, plus the primary key, the CHECK constraint and the four indexes, so the
--      names follow the table. The key and the four indexes get the names a fresh `CREATE TABLE flight_summary` would give; the inline CHECK on end_time was auto-named `flight_record_end_time_check` (a fresh create would say `flight_summary_end_time_check`) and is renamed to `flight_summary_check`, which the tests pin. The old table's same-named objects were renamed aside first.
--      Owner, ACL (watchdog_ro, via ops/migrate/migrate.sh), comments and the dependent views (flight, by OID) follow the table.
--   3. PL/pgSQL and SQL function bodies store table names as text, so they are re-created with the word flight_record replaced by
--      flight_summary and NOTHING ELSE changed: write_finished_flights(int), unreconciled_partition_sessions(timestamptz, timestamptz)
--      and drop_old_pilot_position_partitions(int, date) (phase 4 gate logic, SECURITY DEFINER, search_path = public, pg_temp). The
--      definition is taken from the live catalogue with pg_get_functiondef and rewritten, instead of being copied here, so the logic is
--      exactly the deployed phase 1b and phase 4 text (the tests compare before and after). Every other function that still names
--      flight_record is caught the same way, and the migration refuses to finish if any body, view or comment still names an old object.
--   4. The 5-minute collector job now calls write_finished_flights(50) itself (collector/src/vatpac/maintenance.py): the collector role
--      is granted EXECUTE on it here (and in db/roles.sql, which the migrate job applies right after) in the same deploy. refresh_flight_summary
--      used to be the only caller.
--
-- Also gone with the pieces table: its foreign key to pilot_session (ON DELETE CASCADE). The flight table has no foreign key, so deleting
-- a session no longer deletes its flights: ADR-026 (amended by ADR-030): delete them by session_ids.

-- 0. Pre-checks: the phase 1 table must exist, and phase 3 must have moved every reader off the pieces (otherwise stop now).
DO $do$
BEGIN
    IF to_regclass('public.flight_record') IS NULL THEN
        RAISE EXCEPTION 'phase 5 needs public.flight_record (phase 1); nothing changed';
    END IF;
    IF to_regclass('public.flight_summary') IS NULL OR to_regclass('public.flight_part') IS NULL THEN
        RAISE EXCEPTION 'phase 5 expects the old pieces table flight_summary and the view flight_part; nothing changed';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'public.flight_summary'::regclass AND attname = 'final' AND NOT attisdropped) IS NOT TRUE THEN
        RAISE EXCEPTION 'public.flight_summary is not the old pieces table (no column final); refusing to drop it';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'public.flight' ::regclass AND attname IN ('gap_segment', 'span_part', 'is_in_progress') AND NOT attisdropped) THEN
        RAISE EXCEPTION 'view flight still has the old piece columns: phase 3 (migration 20261013000200) is not applied; nothing changed';
    END IF;
END
$do$;

-- LOCK ORDER (Opus 5.5 review, 13-10-2026). DROP TABLE of the pieces table removes its foreign key to pilot_session, which takes
-- ACCESS EXCLUSIVE on pilot_session until the transaction ends, and the collector writes pilot_session on every poll (its own lock timeout
-- is 5 s). Any statement that can WAIT (the renames of flight_record wait for a Grafana panel that is reading the flight view) must
-- therefore come BEFORE that lock, and the DROP of the pieces table must be the very last statement. So: views and functions first (they
-- touch no table the collector writes), the pieces table is only RENAMED aside (a lock on that table alone), then every rename and
-- re-creation, then the self-check, then the one DROP that locks pilot_session, followed only by the commit.

-- 1. Drop the old views and functions (no CASCADE) and move the pieces table out of the way.
DROP VIEW IF EXISTS flight_legacy;
DROP VIEW flight_part;
DROP FUNCTION IF EXISTS refresh_flight_summary(interval);
DROP FUNCTION IF EXISTS refresh_flight_summary_sessions(bigint[], boolean);
ALTER TABLE flight_summary RENAME TO flight_summary_old_p5;                 -- locks only the pieces table; frees the name
ALTER TABLE flight_summary_old_p5 RENAME CONSTRAINT flight_summary_pkey TO flight_summary_old_p5_pkey;   -- renames the key index too
ALTER INDEX IF EXISTS flight_summary_start_time_idx RENAME TO flight_summary_old_p5_start_time_idx;
ALTER INDEX IF EXISTS flight_summary_end_time_idx   RENAME TO flight_summary_old_p5_end_time_idx;

-- 2. Rename the finished-flights table, the primary key, the CHECK constraint and the four indexes (may wait for readers of the
--    flight view, up to the lock timeout; nothing the collector writes is locked yet).
ALTER TABLE flight_record RENAME TO flight_summary;
DO $do$
DECLARE
    c record;
BEGIN
    FOR c IN SELECT conname, contype FROM pg_constraint
              WHERE conrelid = 'public.flight_summary'::regclass AND contype IN ('p', 'c') AND conname LIKE 'flight\_record\_%' ESCAPE '\'
    LOOP
        EXECUTE format('ALTER TABLE public.flight_summary RENAME CONSTRAINT %I TO %I',
                       c.conname, CASE c.contype WHEN 'p' THEN 'flight_summary_pkey' ELSE 'flight_summary_check' END);
    END LOOP;
END
$do$;
ALTER INDEX IF EXISTS flight_record_cid_idx        RENAME TO flight_summary_cid_idx;
ALTER INDEX IF EXISTS flight_record_start_time_idx RENAME TO flight_summary_start_time_idx;
ALTER INDEX IF EXISTS flight_record_end_time_idx   RENAME TO flight_summary_end_time_idx;
ALTER INDEX IF EXISTS flight_record_logon_time_idx RENAME TO flight_summary_logon_time_idx;

-- 3. Re-create every function body that names flight_record, changing only that word; fix the comments that name it.
DO $do$
DECLARE
    f   record;
    def text;
    r   record;
BEGIN
    FOR f IN SELECT p.oid, p.proname, pg_get_function_identity_arguments(p.oid) AS args
               FROM pg_proc p
              WHERE p.pronamespace = 'public'::regnamespace AND p.prokind = 'f' AND p.prosrc ~ '\mflight_record\M'
              ORDER BY p.proname
    LOOP
        def := pg_get_functiondef(f.oid);
        def := regexp_replace(def, '\mflight_record\M', 'flight_summary', 'g');
        EXECUTE def;                                  -- CREATE OR REPLACE: same signature, owner, ACL and SET clauses
        RAISE NOTICE 'phase 5: re-created function %(%)', f.proname, f.args;
    END LOOP;
    -- the three functions the design names must have been among them
    FOR r IN SELECT n FROM unnest(ARRAY['write_finished_flights', 'unreconciled_partition_sessions']) AS n
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND proname = r.n AND prosrc ~ '\mflight_summary\M') THEN
            RAISE EXCEPTION 'function % was not re-created over flight_summary', r.n;
        END IF;
    END LOOP;
    -- comments (functions, tables, views, columns, indexes)
    FOR r IN SELECT d.objoid, d.classoid, d.objsubid, d.description
               FROM pg_description d
              WHERE d.description ~ '\mflight_record\M'
                AND d.classoid IN ('pg_proc'::regclass, 'pg_class'::regclass)
    LOOP
        IF r.classoid = 'pg_proc'::regclass THEN
            EXECUTE format('COMMENT ON FUNCTION %s IS %L', r.objoid::regprocedure,
                           regexp_replace(r.description, '\mflight_record\M', 'flight_summary', 'g'));
        ELSIF r.objsubid = 0 THEN
            EXECUTE format('COMMENT ON %s %s IS %L',
                           CASE (SELECT relkind FROM pg_class WHERE oid = r.objoid) WHEN 'v' THEN 'VIEW' WHEN 'i' THEN 'INDEX' ELSE 'TABLE' END,
                           r.objoid::regclass,
                           regexp_replace(r.description, '\mflight_record\M', 'flight_summary', 'g'));
        ELSE
            EXECUTE format('COMMENT ON COLUMN %s.%I IS %L', r.objoid::regclass,
                           (SELECT attname FROM pg_attribute WHERE attrelid = r.objoid AND attnum = r.objsubid),
                           regexp_replace(r.description, '\mflight_record\M', 'flight_summary', 'g'));
        END IF;
    END LOOP;
END
$do$;
COMMENT ON TABLE flight_summary IS
    'One row per finished flight, written once by write_finished_flights() (issue 89; built in phase 1 under a temporary name, renamed flight_summary in phase 5). The only flight store: the charts read it through the flight view. Primary key (session_id, start_time); session_ids lists every session of the flight. No foreign key to pilot_session: delete a session''s flights by session_ids (ADR-026, ADR-030).';

-- 4. The collector now calls the writer itself. A grant given here as well as in db/roles.sql so the window between this migration
--    and the roles step (or a manual dbmate up) cannot leave the 5-minute job without EXECUTE.
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'collector') THEN
        GRANT EXECUTE ON FUNCTION write_finished_flights(int) TO collector;
    END IF;
END
$do$;

-- 5. Self-check BEFORE the last statement: nothing names an old object (functions, view definitions, comments), the new names exist.
DO $do$
DECLARE
    bad text;
BEGIN
    SELECT string_agg(p.proname, ', ') INTO bad
      FROM pg_proc p
     WHERE p.pronamespace = 'public'::regnamespace
       AND (p.prosrc ~ '\mflight_record\M' OR p.prosrc ~ '\m(flight_part|flight_legacy|refresh_flight_summary(_sessions)?)\M');
    IF bad IS NOT NULL THEN RAISE EXCEPTION 'functions still name an old object: %', bad; END IF;
    SELECT string_agg(c.relname, ', ') INTO bad
      FROM pg_class c
     WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'v'
       AND pg_get_viewdef(c.oid) ~ '\m(flight_record|flight_part|flight_legacy)\M';
    IF bad IS NOT NULL THEN RAISE EXCEPTION 'views still name an old object: %', bad; END IF;
    SELECT string_agg(d.objoid::regclass::text, ', ') INTO bad
      FROM pg_description d
     WHERE d.classoid = 'pg_class'::regclass AND d.objoid <> 'public.flight_summary_old_p5'::regclass AND d.description ~ '\m(flight_record|flight_part|flight_legacy|refresh_flight_summary(_sessions)?)\M';
    IF bad IS NOT NULL THEN RAISE EXCEPTION 'comments still name an old object: %', bad; END IF;
    SELECT string_agg(p.oid::regprocedure::text, ', ') INTO bad
      FROM pg_description d JOIN pg_proc p ON p.oid = d.objoid AND d.classoid = 'pg_proc'::regclass
     WHERE d.description ~ '\m(flight_record|flight_part|flight_legacy|refresh_flight_summary(_sessions)?)\M';
    IF bad IS NOT NULL THEN RAISE EXCEPTION 'function comments still name an old object: %', bad; END IF;
    IF to_regclass('public.flight_record') IS NOT NULL OR to_regclass('public.flight_part') IS NOT NULL
       OR to_regclass('public.flight_legacy') IS NOT NULL THEN
        RAISE EXCEPTION 'an old relation is still present';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid = 'public.flight_summary'::regclass AND attname IN ('final', 'gap_segment', 'span_part') AND NOT attisdropped) THEN
        RAISE EXCEPTION 'flight_summary still has piece columns';
    END IF;
    IF to_regclass('public.flight_summary_pkey') IS NULL OR to_regclass('public.flight_summary_cid_idx') IS NULL
       OR to_regclass('public.flight_summary_start_time_idx') IS NULL OR to_regclass('public.flight_summary_end_time_idx') IS NULL
       OR to_regclass('public.flight_summary_logon_time_idx') IS NULL THEN
        RAISE EXCEPTION 'a renamed flight_summary index is missing';
    END IF;
END
$do$;

-- 6. LAST statement: drop the old pieces table (takes ACCESS EXCLUSIVE on pilot_session for the rest of the transaction, i.e. the commit).
-- Opus 5.5 recheck (13-10-2026): a poll write queued behind this lock finished in 3.5 s with the 5 s timeout above, right at the poll's own
-- 5 s limit. A 2 s timeout here makes the migration fail (whole transaction rolled back, safe to retry) instead of making a poll wait.
SET LOCAL lock_timeout = '2s';
DROP TABLE flight_summary_old_p5;

-- migrate:down
-- Forward-only project: no automatic rollback. Recovery is only the pre-deploy dump (docs/RUNBOOKS/restore.md); the pieces
-- table and its refresh functions are not recreated by any later migration.
