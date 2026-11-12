-- migrate:up
-- Issues #200 and #238, OWNER DECISIONS 12-11-2026: the airborne gap cap (airborne_gap_cap_seconds, 75 s = 2.5 x the 30 s poll interval, ADR-024) and
-- the flight quiet hours (flight_quiet_hours, 8) each have ONE source, the app_setting row. No number for either is written in SQL, shell or watchdog code.
--
--   1. setting_num(key): the one reader. It returns the numeric value and RAISES when the row is missing or is not a number. There is no
--      built-in fallback: a missing row stops the flight writer with a clear error (the watchdog then alerts) instead of quietly
--      producing flights with a guessed number. It runs with the caller's rights and nobody but the owner (and the SECURITY DEFINER
--      writer and wrapper below) may execute it.
--   2. write_finished_flights(): re-created with the two fallbacks (8 hours, 40 seconds) removed. The live definition (migration 20261012000200 as
--      renamed over flight_summary by migration 20261013000300) is taken from the catalogue with pg_get_functiondef and only those two
--      expressions are replaced, each checked to occur exactly once, so every other line stays exactly as deployed. A non-numeric
--      flight_quiet_hours still raises (now with the message of setting_num, same error class), and a value below 2.5 still raises the
--      existing message.
--   3. The stale seed: migration 20261007000001 seeded airborne_gap_cap_seconds = 40 (the value for the old 15 s polling). The collector rewrites the
--      row with the derived value at every start (maintenance.py sync_settings), so production holds 75. A row that still holds the seeded 40 is
--      removed here, so a fresh database has NO gap cap until the collector has started and the writer raises instead of using 40. A row with any other
--      value (production: 75) is not touched. flight_quiet_hours (seeded 8 by migration 20261012000190) is not published by the collector, so its row stays.
--   4. ops_setting_num, ops_setting and ops_pending_flight: for the watchdog role (watchdog_ro). ops_setting_num is a SECURITY DEFINER wrapper
--      of setting_num that answers for the three settings above only (a view calls its functions with the rights of the role that queries it,
--      so watchdog_ro needs EXECUTE on this one function; the grant is in ops/migrate/migrate.sh, with the SELECT on the two views).
--      ops_setting shows the three settings. ops_pending_flight is the ONE definition of the pending-flight (quiet hours) rule: the unprocessed
--      sessions grouped into flights by the v1 join rule, with the flight's newest last_seen and whether it is quiet, measured against the
--      newest stored snapshot (capped at now() + 5 minutes) with the setting read here. ops/audit/invariants.sql and ops/backfill_flights.sh read
--      it instead of their own copies. db/roles.sql keeps views named ops_* away from grafana_ro.
--
-- ops_pending_flight keeps the grouping of the audit and backfill copies it replaces (session first_seen and last_seen), not the writer's
-- position times, which differ by seconds; the writer additionally starts a new flight for a session that returns after being processed and
-- only writes a flight whose sessions it could lock. Both are rare and only make the view a slight superset of what the writer will write.

CREATE FUNCTION setting_num(p_key text)
RETURNS numeric
LANGUAGE plpgsql
STABLE
SET search_path = public, pg_temp
AS $f$
DECLARE
    v text;
    n numeric;
BEGIN
    SELECT value INTO v FROM app_setting WHERE key = p_key;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'setting % is missing from app_setting; there is no built-in default (the collector writes airborne_gap_cap_seconds at every start; flight_quiet_hours is set by the owner)', p_key
            USING ERRCODE = 'no_data_found';
    END IF;
    BEGIN
        n := v::numeric;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION 'setting % is not a number: %', p_key, v USING ERRCODE = 'invalid_text_representation';
    END;
    IF n IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric) THEN
        RAISE EXCEPTION 'setting % is not a finite number: %', p_key, v USING ERRCODE = 'invalid_text_representation';
    END IF;
    RETURN n;
END
$f$;
REVOKE ALL ON FUNCTION setting_num(text) FROM PUBLIC;
COMMENT ON FUNCTION setting_num(text) IS
    'The one reader of numeric settings in app_setting (issues 200, 238). Raises when the row is missing or not a finite number; there is no default. Runs with the caller''s rights; EXECUTE is not granted to any role (the SECURITY DEFINER writer and ops_setting_num call it as the owner).';

CREATE FUNCTION ops_setting_num(p_key text)
RETURNS numeric
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $f$
BEGIN
    IF p_key IS NULL OR p_key NOT IN ('flight_quiet_hours', 'airborne_gap_cap_seconds', 'flight_quiet_check_grace_minutes') THEN
        RAISE EXCEPTION 'setting % is not available through ops_setting_num', p_key USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN setting_num(p_key);
END
$f$;
REVOKE ALL ON FUNCTION ops_setting_num(text) FROM PUBLIC;
COMMENT ON FUNCTION ops_setting_num(text) IS
    'setting_num for the three flight settings only (flight_quiet_hours, airborne_gap_cap_seconds, flight_quiet_check_grace_minutes); any other key is refused. SECURITY DEFINER so the watchdog role, which has no right on app_setting, can read the two through the ops_* views. EXECUTE is granted to watchdog_ro only (ops/migrate/migrate.sh).';

DELETE FROM app_setting WHERE key = 'airborne_gap_cap_seconds' AND value = '40';

-- Owner decision 12-11-2026 ("30 mins"): the watchdog's overdue check fires at flight_quiet_hours + this many minutes, so the writer's own
-- run interval is not mistaken for a stopped writer. Its own setting row, read through setting_num; the owner may change it.
INSERT INTO app_setting (key, value) VALUES ('flight_quiet_check_grace_minutes', '30') ON CONFLICT (key) DO NOTHING;

CREATE VIEW ops_setting AS
SELECT k AS key, ops_setting_num(k) AS value
  FROM unnest(ARRAY['flight_quiet_hours', 'airborne_gap_cap_seconds', 'flight_quiet_check_grace_minutes']) AS k;
COMMENT ON VIEW ops_setting IS
    'The settings that production numbers depend on (flight_quiet_hours in hours, airborne_gap_cap_seconds in seconds, flight_quiet_check_grace_minutes in minutes), read through ops_setting_num (setting_num): a missing or non-numeric row raises. For the watchdog role only; grafana_ro has no access.';

CREATE VIEW ops_pending_flight AS
WITH g AS (
    SELECT s.cid, s.id, s.callsign, s.logon_time, s.first_seen, s.last_seen,
           NULLIF(upper(btrim(s.departure, E' \t\r\n')), '') AS dep, NULLIF(upper(btrim(s.arrival, E' \t\r\n')), '') AS arr
    FROM pilot_session s WHERE s.flown_upto IS NULL OR s.last_seen > s.flown_upto),
  o AS (
    SELECT g.*, max(g.last_seen) OVER (PARTITION BY g.cid, g.callsign, g.dep, g.arr ORDER BY g.first_seen, g.id
                                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end
    FROM g WHERE g.dep IS NOT NULL AND g.arr IS NOT NULL),
  c AS (
    SELECT o.*, sum(CASE WHEN o.prev_end IS NULL OR o.logon_time - o.prev_end > interval '120 minutes' THEN 1 ELSE 0 END)
                  OVER (PARTITION BY o.cid, o.callsign, o.dep, o.arr ORDER BY o.first_seen, o.id ROWS UNBOUNDED PRECEDING) AS chain
    FROM o),
  grp AS (
    SELECT c.cid, c.callsign, c.dep AS departure, c.arr AS arrival, max(c.last_seen) AS newest_seen, count(*) AS session_count
      FROM c GROUP BY c.cid, c.callsign, c.dep, c.arr, c.chain
    UNION ALL
    SELECT g.cid, g.callsign, g.dep, g.arr, g.last_seen, 1 FROM g WHERE g.dep IS NULL OR g.arr IS NULL),
  ref AS (SELECT CASE WHEN max(observed_at) IS NOT NULL THEN LEAST(max(observed_at), now() + interval '5 minutes') END AS t FROM network_snapshot),
  cfg AS (SELECT make_interval(secs => (ops_setting_num('flight_quiet_hours') * 3600)::double precision) AS quiet,
                 make_interval(secs => (ops_setting_num('flight_quiet_check_grace_minutes') * 60)::double precision) AS grace)
SELECT grp.cid, grp.callsign, grp.departure, grp.arrival, grp.session_count, grp.newest_seen,
       (grp.newest_seen <= ref.t - cfg.quiet) AS is_quiet,
       (grp.newest_seen <= ref.t - cfg.quiet - cfg.grace) AS is_overdue
  FROM grp CROSS JOIN ref CROSS JOIN cfg;
COMMENT ON VIEW ops_pending_flight IS
    'The one definition of the pending-flight rule (issues 200, 238): one row per flight (v1 groups of unprocessed sessions: same cid, callsign, departure and arrival, a session joins when its logon_time is within 120 minutes of the latest earlier last_seen; a session without a plan is its own row) with the flight''s newest last_seen and is_quiet, true when that is flight_quiet_hours or more behind the newest stored snapshot (capped at now() + 5 minutes), the writer''s rule. Reads the setting through ops_setting_num (setting_num), so a missing row raises; without any snapshot nothing is quiet. is_overdue is the same test with flight_quiet_check_grace_minutes added: what the watchdog invariant checks. Not for Grafana.';

DO $do$
DECLARE
    def   text := pg_get_functiondef('public.write_finished_flights(integer)'::regprocedure);
    old_q text := $a$COALESCE((SELECT value::numeric FROM app_setting WHERE key = 'flight_quiet_hours'), 8)$a$;
    new_q text := $a$setting_num('flight_quiet_hours')$a$;
    old_g text := $a$COALESCE((SELECT value::numeric FROM app_setting WHERE key = 'airborne_gap_cap_seconds'), 40)$a$;
    new_g text := $a$setting_num('airborne_gap_cap_seconds')$a$;
BEGIN
    IF (length(def) - length(replace(def, old_q, ''))) <> length(old_q) OR (length(def) - length(replace(def, old_g, ''))) <> length(old_g) THEN
        RAISE EXCEPTION 'write_finished_flights does not contain each setting fallback exactly once; nothing changed';
    END IF;
    EXECUTE replace(replace(def, old_q, new_q), old_g, new_g);          -- CREATE OR REPLACE: same signature, owner, ACL and SET clauses
    IF EXISTS (SELECT 1 FROM pg_proc WHERE oid = 'public.write_finished_flights(integer)'::regprocedure
                  AND (prosrc ~ 'app_setting' OR prosrc !~ 'setting_num\(''flight_quiet_hours''\)' OR prosrc !~ 'setting_num\(''airborne_gap_cap_seconds''\)')) THEN
        RAISE EXCEPTION 'write_finished_flights was not re-created over setting_num';
    END IF;
END
$do$;
REVOKE ALL ON FUNCTION write_finished_flights(int) FROM PUBLIC;
COMMENT ON FUNCTION write_finished_flights(int) IS
    'Writes finished flights into flight_summary (issue 89, phase 1). A FLIGHT (sessions joined by the v1 rule) is written once its own newest last_seen is flight_quiet_hours (the app_setting row; no default, a missing row raises) or more behind the newest stored snapshot; other flights of the same pilot do not matter. The airborne gap cap is app_setting.airborne_gap_cap_seconds (no default, a missing row raises). Idempotent; examines at most p_max_pilots pilots per round. Returns the number of flights written.';

-- migrate:down
-- Forward-only project: no automatic rollback.
