-- migrate:up
-- T36 / ADR-021: a session is identified by (cid, callsign, logon_time) and logon_time is now kept to WHOLE
-- SECONDS (truncated, never rounded; the fraction is dropped on purpose). The feed sometimes sends the
-- fraction and sometimes not (after a feed restart at about 04:08 UTC on 08-10-2026 every online connection
-- lost it), which stored 31 pilot and 3 controller sessions twice.
--
-- Run at deploy time BEFORE the new collector starts (compose stops the old one first), as the owner role,
-- in one transaction (dbmate). Steps:
--   1. Safety: list every foreign key that references pilot_session or controller_session and refuse to
--      continue if one is not handled below (so a table added later cannot be orphaned silently).
--   2. Merge twin sessions (same cid, callsign and whole-second logon_time). The survivor is the row with the
--      lowest id (the earliest first_seen). Children are re-pointed to it, its timestamps and maxima are
--      combined, and its other attributes take the latest non-null value (the collector's own rule).
--   3. Rebuild flight_summary for every merged pilot session, once, with refresh_flight_summary_sessions.
--   4. Truncate every stored logon_time to whole seconds (essential: without it every session online at
--      deploy time would be stored a second time by the new collector).
--   5. Add CHECK constraints so a fractional logon_time can never be stored again.
-- On a database with no twins and no fractions steps 2 and 3 do nothing, step 4 changes no row and step 5 is
-- skipped when the constraints exist: running it again is harmless.

DO $mig$
DECLARE
    fk       record;
    n_groups int;
    n_losers int;
    ids      bigint[];
BEGIN
    -- 1. every foreign key that references a session table (the FKs of a partitioned table's partitions are
    --    clones with conparentid <> 0 and are skipped)
    FOR fk IN
        SELECT c.conname::text AS conname,
               replace(c.conrelid::regclass::text, 'public.', '') AS child,
               replace(c.confrelid::regclass::text, 'public.', '') AS parent
          FROM pg_constraint c
         WHERE c.contype = 'f' AND c.conparentid = 0
           AND c.confrelid IN ('public.pilot_session'::regclass, 'public.controller_session'::regclass)
         ORDER BY 2, 1
    LOOP
        RAISE NOTICE 'foreign key % : % -> %', fk.conname, fk.child, fk.parent;
        IF (fk.child, fk.parent) NOT IN (
               ('pilot_position', 'pilot_session'), ('flight_summary', 'pilot_session'),
               ('controller_rating_event', 'controller_session'), ('controller_frequency_event', 'controller_session')) THEN
            RAISE EXCEPTION 'unhandled foreign key % from % to %: extend migration 20261008000100 before running it',
                fk.conname, fk.child, fk.parent;
        END IF;
    END LOOP;

    -- 2a. pilot twins
    CREATE TEMP TABLE _p_grp ON COMMIT DROP AS
    SELECT id, min(id) OVER w AS survivor, count(*) OVER w AS n
      FROM pilot_session
    WINDOW w AS (PARTITION BY cid, callsign, date_trunc('second', logon_time));
    DELETE FROM _p_grp WHERE n = 1;
    SELECT count(DISTINCT survivor), count(*) - count(DISTINCT survivor) INTO n_groups, n_losers FROM _p_grp;
    RAISE NOTICE 'pilot twin groups: %, duplicate sessions to merge: %', n_groups, n_losers;

    IF n_groups > 0 THEN
        -- A final flight_summary row means raw positions may already be dropped, so the summary cannot be
        -- rebuilt from them. Twins this old do not arise from a precision flip; stop rather than lose data.
        IF EXISTS (SELECT 1 FROM flight_summary f JOIN _p_grp g ON g.id = f.session_id WHERE f.final) THEN
            RAISE EXCEPTION 'a twin pilot session already has a final flight_summary row; merge it by hand';
        END IF;

        UPDATE pilot_session p SET
            first_seen = m.first_seen, last_seen = m.last_seen, max_gs_kt = m.max_gs_kt, max_alt_ft = m.max_alt_ft,
            name = m.name, home_icao = m.home_icao, server = m.server, departure = m.departure,
            arrival = m.arrival, aircraft_icao = m.aircraft_icao, aircraft_faa = m.aircraft_faa,
            flight_rules = m.flight_rules, route = m.route, remarks = m.remarks, planned_alt = m.planned_alt,
            deptime = m.deptime, plan_changed_at = m.plan_changed_at, pilot_rating = m.pilot_rating,
            military_rating = m.military_rating
          FROM (
            SELECT g.survivor AS id,
                   min(s.first_seen) AS first_seen, max(s.last_seen) AS last_seen,
                   max(s.max_gs_kt) AS max_gs_kt, max(s.max_alt_ft) AS max_alt_ft,
                   (array_agg(s.name ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.name IS NOT NULL))[1] AS name,
                   (array_agg(s.home_icao ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.home_icao IS NOT NULL))[1] AS home_icao,
                   (array_agg(s.server ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.server IS NOT NULL))[1] AS server,
                   (array_agg(s.departure ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.departure IS NOT NULL))[1] AS departure,
                   (array_agg(s.arrival ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.arrival IS NOT NULL))[1] AS arrival,
                   (array_agg(s.aircraft_icao ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.aircraft_icao IS NOT NULL))[1] AS aircraft_icao,
                   (array_agg(s.aircraft_faa ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.aircraft_faa IS NOT NULL))[1] AS aircraft_faa,
                   (array_agg(s.flight_rules ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.flight_rules IS NOT NULL))[1] AS flight_rules,
                   (array_agg(s.route ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.route IS NOT NULL))[1] AS route,
                   (array_agg(s.remarks ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.remarks IS NOT NULL))[1] AS remarks,
                   (array_agg(s.planned_alt ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.planned_alt IS NOT NULL))[1] AS planned_alt,
                   (array_agg(s.deptime ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.deptime IS NOT NULL))[1] AS deptime,
                   (array_agg(s.plan_changed_at ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.plan_changed_at IS NOT NULL))[1] AS plan_changed_at,
                   (array_agg(s.pilot_rating ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.pilot_rating IS NOT NULL))[1] AS pilot_rating,
                   (array_agg(s.military_rating ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.military_rating IS NOT NULL))[1] AS military_rating
              FROM _p_grp g JOIN pilot_session s ON s.id = g.id
             GROUP BY g.survivor) m
         WHERE p.id = m.id;

        -- flight_summary of every member goes (rebuilt below); positions are re-pointed to the survivor. If two
        -- members hold a position at the same instant (same snapshot), the lowest session id keeps it.
        DELETE FROM flight_summary f USING _p_grp g WHERE f.session_id = g.id;
        DELETE FROM pilot_position p USING _p_grp l
         WHERE p.session_id = l.id
           AND EXISTS (SELECT 1 FROM pilot_position q JOIN _p_grp l2 ON l2.id = q.session_id
                        WHERE l2.survivor = l.survivor AND l2.id < l.id AND q.observed_at = p.observed_at);
        UPDATE pilot_position p SET session_id = l.survivor FROM _p_grp l WHERE p.session_id = l.id AND l.id <> l.survivor;
        DELETE FROM pilot_session s USING _p_grp l WHERE s.id = l.id AND l.id <> l.survivor;

        -- 3. one rebuild per merged flight (not final: live sessions keep refreshing, dead ones are finalised
        --    when their partition is dropped, like any other session)
        ids := ARRAY(SELECT DISTINCT survivor FROM _p_grp);
        PERFORM refresh_flight_summary_sessions(ids, false);
    END IF;

    -- 2b. controller twins (rating and frequency are the latest session's values, name and server the latest
    --     non-null, exactly as the collector's upsert does)
    CREATE TEMP TABLE _c_grp ON COMMIT DROP AS
    SELECT id, min(id) OVER w AS survivor, count(*) OVER w AS n
      FROM controller_session
    WINDOW w AS (PARTITION BY cid, callsign, date_trunc('second', logon_time));
    DELETE FROM _c_grp WHERE n = 1;
    SELECT count(DISTINCT survivor), count(*) - count(DISTINCT survivor) INTO n_groups, n_losers FROM _c_grp;
    RAISE NOTICE 'controller twin groups: %, duplicate sessions to merge: %', n_groups, n_losers;

    IF n_groups > 0 THEN
        UPDATE controller_session c SET
            first_seen = m.first_seen, last_seen = m.last_seen, name = m.name, server = m.server,
            rating = m.rating, facility = m.facility, frequency_hz = m.frequency_hz, visual_range = m.visual_range
          FROM (
            SELECT g.survivor AS id,
                   min(s.first_seen) AS first_seen, max(s.last_seen) AS last_seen,
                   (array_agg(s.name ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.name IS NOT NULL))[1] AS name,
                   (array_agg(s.server ORDER BY s.last_seen DESC, s.id DESC) FILTER (WHERE s.server IS NOT NULL))[1] AS server,
                   (array_agg(s.rating ORDER BY s.last_seen DESC, s.id DESC))[1] AS rating,
                   (array_agg(s.facility ORDER BY s.last_seen DESC, s.id DESC))[1] AS facility,
                   (array_agg(s.frequency_hz ORDER BY s.last_seen DESC, s.id DESC))[1] AS frequency_hz,
                   (array_agg(s.visual_range ORDER BY s.last_seen DESC, s.id DESC))[1] AS visual_range
              FROM _c_grp g JOIN controller_session s ON s.id = g.id
             GROUP BY g.survivor) m
         WHERE c.id = m.id;

        DELETE FROM controller_rating_event e USING _c_grp l
         WHERE e.session_id = l.id
           AND EXISTS (SELECT 1 FROM controller_rating_event q JOIN _c_grp l2 ON l2.id = q.session_id
                        WHERE l2.survivor = l.survivor AND l2.id < l.id AND q.observed_at = e.observed_at);
        UPDATE controller_rating_event e SET session_id = l.survivor FROM _c_grp l WHERE e.session_id = l.id AND l.id <> l.survivor;
        DELETE FROM controller_frequency_event e USING _c_grp l
         WHERE e.session_id = l.id
           AND EXISTS (SELECT 1 FROM controller_frequency_event q JOIN _c_grp l2 ON l2.id = q.session_id
                        WHERE l2.survivor = l.survivor AND l2.id < l.id AND q.observed_at = e.observed_at);
        UPDATE controller_frequency_event e SET session_id = l.survivor FROM _c_grp l WHERE e.session_id = l.id AND l.id <> l.survivor;
        DELETE FROM controller_session s USING _c_grp l WHERE s.id = l.id AND l.id <> l.survivor;
    END IF;

    -- 4. whole seconds everywhere (after the merge, so no unique violation is possible)
    UPDATE pilot_session SET logon_time = date_trunc('second', logon_time)
     WHERE logon_time <> date_trunc('second', logon_time);
    GET DIAGNOSTICS n_groups = ROW_COUNT;
    RAISE NOTICE 'pilot_session logon_time truncated on % rows', n_groups;
    UPDATE controller_session SET logon_time = date_trunc('second', logon_time)
     WHERE logon_time <> date_trunc('second', logon_time);
    GET DIAGNOSTICS n_groups = ROW_COUNT;
    RAISE NOTICE 'controller_session logon_time truncated on % rows', n_groups;
END
$mig$;

-- 5. A fractional logon_time can no longer be stored. The test is written on the UTC wall-clock value
--    (timezone() with a constant zone is immutable), so it does not depend on the session time zone.
DO $mig$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'pilot_session_logon_whole_second'
                      AND conrelid = 'public.pilot_session'::regclass) THEN
        ALTER TABLE pilot_session ADD CONSTRAINT pilot_session_logon_whole_second
            CHECK ((logon_time AT TIME ZONE 'UTC') = date_trunc('second', logon_time AT TIME ZONE 'UTC'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'controller_session_logon_whole_second'
                      AND conrelid = 'public.controller_session'::regclass) THEN
        ALTER TABLE controller_session ADD CONSTRAINT controller_session_logon_whole_second
            CHECK ((logon_time AT TIME ZONE 'UTC') = date_trunc('second', logon_time AT TIME ZONE 'UTC'));
    END IF;
END
$mig$;

-- migrate:down
-- Only the constraints can be undone. The merge of twin sessions and the truncation of logon_time to whole
-- seconds CANNOT be reversed: the fractional seconds are gone for good (dropped on purpose, ADR-021) and the
-- merged duplicate rows are not restored. To go back, restore the pre-deploy dump (docs/RUNBOOKS/restore.md).
ALTER TABLE pilot_session DROP CONSTRAINT IF EXISTS pilot_session_logon_whole_second;
ALTER TABLE controller_session DROP CONSTRAINT IF EXISTS controller_session_logon_whole_second;
