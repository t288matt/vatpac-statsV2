-- Issue #153 (section H): a pilot whose writer batch hits the statement timeout was never fixed. The timeout rolls the whole call back, the
-- next call picks the same oldest pilots again, and the daily maintenance (15 minute timeout) is what finally clears it.
--
-- This migration changes write_finished_flights(int) in two small ways and nothing else (the method of migrations 20261112000010 and
-- 20261112000150: take the live definition, replace exactly the expected text, check each replacement happened once):
--   1. The call starts with a skip list read from the session setting vatpac.flight_writer_skip (a bigint[] literal such as '{4101,4102}';
--      empty or unset means nobody is skipped, which is today's behaviour). The collector sets it for ONE statement (set_config with
--      is_local = true) to leave out a pilot that timed out alone. The list is the function's existing `skip` variable, so a skipped pilot is
--      handled exactly like a pilot that made no progress in an earlier round of the same call.
--   2. Each round raises a NOTICE with the pilots it picked. A notice reaches the client before a later timeout rolls the transaction back,
--      so the collector can name the batch that was running when the statement was cancelled and narrow it down to one pilot.
-- Rows written, sessions marked, the pick order and the return value are unchanged when the setting is empty.
-- migrate:up
SET LOCAL lock_timeout = '5s';   -- fail fast instead of queueing behind a long panel query
DO $do$
DECLARE
    def   text := pg_get_functiondef('public.write_finished_flights(integer)'::regprocedure);
    old_s text := $a$    skip     bigint[] := '{}';$a$;
    new_s text := $a$    skip     bigint[] := COALESCE(NULLIF(current_setting('vatpac.flight_writer_skip', true), '')::bigint[], '{}');$a$;
    old_p text := $a$    EXIT WHEN pilots IS NULL;$a$;
    new_p text := $a$    EXIT WHEN pilots IS NULL;
    RAISE NOTICE 'write_finished_flights picked %', pilots;$a$;
BEGIN
    IF (length(def) - length(replace(def, old_s, ''))) <> length(old_s) OR (length(def) - length(replace(def, old_p, ''))) <> length(old_p) THEN
        RAISE EXCEPTION 'write_finished_flights does not contain each expected line exactly once; nothing changed';
    END IF;
    EXECUTE replace(replace(def, old_s, new_s), old_p, new_p);          -- CREATE OR REPLACE: same signature, owner, ACL and SET clauses
    IF EXISTS (SELECT 1 FROM pg_proc WHERE oid = 'public.write_finished_flights(integer)'::regprocedure
                  AND (prosrc !~ 'vatpac\.flight_writer_skip' OR prosrc !~ 'RAISE NOTICE ''write_finished_flights picked')) THEN
        RAISE EXCEPTION 'write_finished_flights was not re-created with the skip list';
    END IF;
END
$do$;
COMMENT ON FUNCTION write_finished_flights(int) IS
    'Writes finished flights into flight_summary (issue 89, phase 1). A FLIGHT (sessions joined by the v1 rule) is written once its own newest last_seen is flight_quiet_hours (the app_setting row; no default, a missing row raises) or more behind the newest stored snapshot; other flights of the same pilot do not matter. The airborne gap cap is app_setting.airborne_gap_cap_seconds (no default, a missing row raises). Idempotent; examines at most p_max_pilots pilots per round. The session setting vatpac.flight_writer_skip (a bigint[] literal) lists pilots to leave out of the call; each round raises a NOTICE naming the pilots it picked (issue 153). Returns the number of flights written.';

-- migrate:down
-- Forward-only project: no automatic rollback (the previous definition is in migration 20261112000150).
