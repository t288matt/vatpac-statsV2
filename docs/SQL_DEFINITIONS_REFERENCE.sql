-- REFERENCE SNAPSHOT, NOT APPLIED. Nothing reads or runs this file; it is documentation only (issue #234).
--
-- What it is: the text of the database objects that migrations 20261009000120 and 20261013000300 rebuilt from the
-- live catalogue (pg_get_functiondef / pg_get_viewdef plus a text replace of the old flight table name with flight_summary), so
-- the migration files themselves never contain the final text. The older migrations (20261012000190, 20261012000200,
-- 20261013000210) still use the old table name; the definitions below are what a database holds after every migration up
-- to and including 20261013000300.
--
-- How it was made (21-10-2026): a throwaway PostgreSQL 16 database built from db/migrations by the production
-- migrate image (the same way CI builds its test database), then extracted with the queries at the bottom.
-- Checked against the live VM database on 21-10-2026 (read-only, md5 of the same queries): the three functions and the
-- view are identical to the text below. To check again, run the queries at the bottom there (read-only; see
-- ops/schema_dump.sql for how to run a file over ssh) and compare.
--
-- It goes stale the next time a migration changes one of these objects. Regenerate it then, and update the
-- migration number above.
--
-- Objects: write_finished_flights, drop_old_pilot_position_partitions, unreconciled_partition_sessions (functions),
-- controller_session_merged (view).

-- ============================================================ function write_finished_flights
CREATE OR REPLACE FUNCTION public.write_finished_flights(p_max_pilots integer DEFAULT 500)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
DECLARE
    quiet_h  numeric;
    quiet    interval;
    ref      timestamptz;
    gap_cap  numeric;
    pilots   bigint[];
    lb       timestamptz;
    n        int := 0;
    bad      bigint[];
    idle     bigint[];
    locked   bigint[];
    ub       timestamptz;
    skip     bigint[] := '{}';
    total    int := 0;
    tries    int := 0;
BEGIN
    IF p_max_pilots IS NULL OR p_max_pilots < 1 THEN
        RETURN 0;
    END IF;
    -- One writer at a time. A second caller does not wait: it returns 0 at once (deliberate deviation from design section 2 step 1,
    -- which blocks; the manager's decision of 12-10-2026); the running call does the work.
    IF NOT pg_try_advisory_xact_lock(hashtext('vatpac.flight_writer')) THEN
        RETURN 0;
    END IF;

    quiet_h := setting_num('flight_quiet_hours');           -- the one reader: raises when the row is missing; no default
    IF quiet_h < 2.5 THEN
        RAISE EXCEPTION 'flight_quiet_hours is % but must be at least 2.5 (the 120 minute join rule needs a longer quiet period); nothing written', quiet_h;
    END IF;
    quiet := make_interval(secs => (quiet_h * 3600)::double precision);
    gap_cap := setting_num('airborne_gap_cap_seconds');      -- 75 s = 2.5 x the 30 s poll interval; raises when the row is missing

    SELECT max(observed_at) INTO ref FROM network_snapshot;
    IF ref IS NULL THEN
        RETURN 0;                                  -- cold start: nothing stored, nothing is quiet
    END IF;
    ref := LEAST(ref, now() + interval '5 minutes');   -- a bogus future feed timestamp must not make everyone quiet

    -- A pilot who made no progress in a round (a key conflict, logged; or only flights that are not quiet yet) keeps the oldest
    -- last_seen and would be picked first on every call, so the call loops: such pilots are left out of the next pick of this call
    -- (at most 10 rounds), and the rest progress. The cap p_max_pilots counts the pilots examined in a round.
    LOOP
    tries := tries + 1;
    -- Pick the pilots that may have a finished flight: their OLDEST unprocessed session is already quiet. (Whether a flight is quiet
    -- is decided per flight below; this is only the cheap necessary test.) Longest quiet first.
    SELECT array_agg(x.cid) INTO pilots FROM (
        SELECT ps.cid
          FROM pilot_session ps
         WHERE (ps.flown_upto IS NULL OR ps.last_seen > ps.flown_upto) AND ps.cid <> ALL (skip)
         GROUP BY ps.cid
        HAVING ref - min(ps.last_seen) >= quiet
         ORDER BY min(ps.last_seen), ps.cid
         LIMIT p_max_pilots) x;
    EXIT WHEN pilots IS NULL;

    -- Lock ONLY the sessions that can be written: unprocessed, last seen at least 120 minutes before the reference time (a flight is
    -- written only when quiet for flight_quiet_hours >= 2.5 h, so every session of a writable flight is older than that). A session seen
    -- more recently is possibly live: the collector updates it on every poll, so it is never locked here (the collector's write gives up
    -- after 5 s, and it locks in feed order while this call locks in id order). SKIP LOCKED: a session the collector is updating right
    -- now is simply left out. The rest of the call uses these ids for everything that is WRITTEN or MARKED; the recent sessions are only
    -- READ, without a lock, to see that they hold a flight open (a flight with any session outside `locked` is never written).
    SELECT array_agg(l.id) INTO locked FROM (
        SELECT ps.id FROM pilot_session ps
         WHERE ps.cid = ANY (pilots) AND (ps.flown_upto IS NULL OR ps.last_seen > ps.flown_upto)
           AND ps.last_seen <= ref - interval '120 minutes'
         ORDER BY ps.id FOR UPDATE SKIP LOCKED) l;
    IF locked IS NULL THEN                         -- everything of these pilots is recent or being updated: leave them for later
        EXIT WHEN tries >= 10;
        skip := skip || pilots;
        CONTINUE;
    END IF;
    SELECT min(COALESCE(ps.flown_upto, ps.first_seen)) - interval '1 day', max(ps.last_seen) INTO lb, ub
      FROM pilot_session ps
     WHERE ps.cid = ANY (pilots) AND (ps.flown_upto IS NULL OR ps.last_seen > ps.flown_upto);

    WITH s AS (                                    -- ALL unprocessed sessions of the picked pilots: locked ones (writable) and recent ones (read only)
        SELECT ps.id, ps.cid, ps.callsign, ps.logon_time, ps.last_seen, ps.flown_upto,
               ps.name, ps.home_icao, ps.server, ps.aircraft_icao, ps.aircraft_faa, ps.flight_rules, ps.route,
               ps.remarks, ps.planned_alt, ps.deptime, ps.pilot_rating, ps.military_rating,
               NULLIF(upper(btrim(ps.departure, E' \t\r\n')), '') AS dep_n,
               NULLIF(upper(btrim(ps.arrival,   E' \t\r\n')), '') AS arr_n
          FROM pilot_session ps
         WHERE ps.cid = ANY (pilots) AND (ps.flown_upto IS NULL OR ps.last_seen > ps.flown_upto)
    ),
    samples AS (                                   -- today's airborne rule, copied word for word from the pieces refresh
        SELECT p.session_id, p.observed_at, p.gs_kt, p.alt_ft,
               CASE
                   WHEN p.gs_kt >= 60
                    AND lead(p.observed_at) OVER w IS NOT NULL
                    AND lead(p.observed_at) OVER w - p.observed_at <= interval '120 minutes'
                   THEN LEAST(extract(epoch FROM lead(p.observed_at) OVER w - p.observed_at), gap_cap)
                   ELSE 0
               END AS air_s
          FROM pilot_position p
          JOIN s ON s.id = p.session_id
         WHERE p.observed_at >= lb AND p.observed_at <= ub
           AND p.observed_at > COALESCE(s.flown_upto, '-infinity'::timestamptz)
           AND p.observed_at <= s.last_seen
        WINDOW w AS (PARTITION BY p.session_id ORDER BY p.observed_at)
    ),
    units AS (                                     -- one unit per session (no pieces)
        SELECT sa.session_id, min(sa.observed_at) AS first_pos, max(sa.observed_at) AS last_pos, count(*) AS cnt,
               max(sa.gs_kt) AS max_gs, max(sa.alt_ft) AS max_alt, sum(sa.air_s) AS air_s
          FROM samples sa
         GROUP BY sa.session_id
    ),
    gated AS (                                     -- the flight gate: departure and arrival non-empty
        SELECT s.*, u.first_pos, u.last_pos, u.cnt, u.max_gs, u.max_alt, u.air_s,
               (s.flown_upto IS NOT NULL) AS returning
          FROM s JOIN units u ON u.session_id = s.id
         WHERE s.dep_n IS NOT NULL AND s.arr_n IS NOT NULL
    ),
    ordered AS (                                   -- latest end of all EARLIER units of the same key
        SELECT g.*,
               max(g.last_pos) OVER (PARTITION BY g.cid, g.callsign, g.dep_n, g.arr_n
                                     ORDER BY g.first_pos, g.id
                                     ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end
          FROM gated g
    ),
    flagged AS (                                   -- v1: new flight unless logon_time is within 120 minutes of the previous end
        SELECT o.*,
               CASE WHEN o.prev_end IS NULL
                      OR o.returning
                      OR o.logon_time - o.prev_end > interval '120 minutes'
                    THEN 1 ELSE 0 END AS is_new
          FROM ordered o
    ),
    chained AS (
        SELECT f.*,
               sum(f.is_new) OVER (PARTITION BY f.cid, f.callsign, f.dep_n, f.arr_n
                                   ORDER BY f.first_pos, f.id ROWS UNBOUNDED PRECEDING) AS chain
          FROM flagged f
    ),
    flights AS (
        SELECT c.cid, c.callsign, c.dep_n AS departure, c.arr_n AS arrival,
               (array_agg(c.id ORDER BY c.first_pos, c.id))[1] AS session_id,
               array_agg(c.id ORDER BY c.first_pos, c.id) AS session_ids,
               min(c.first_pos) AS start_time, max(c.last_pos) AS end_time,
               min(c.logon_time) AS logon_time,
               (array_agg(c.name            ORDER BY c.last_pos DESC, c.id DESC))[1] AS name,
               (array_agg(c.home_icao       ORDER BY c.last_pos DESC, c.id DESC))[1] AS home_icao,
               (array_agg(c.server          ORDER BY c.last_pos DESC, c.id DESC))[1] AS server,
               (array_agg(c.aircraft_icao   ORDER BY c.last_pos DESC, c.id DESC))[1] AS aircraft_icao,
               (array_agg(c.aircraft_faa    ORDER BY c.last_pos DESC, c.id DESC))[1] AS aircraft_faa,
               (array_agg(c.flight_rules    ORDER BY c.last_pos DESC, c.id DESC))[1] AS flight_rules,
               (array_agg(c.route           ORDER BY c.last_pos DESC, c.id DESC) FILTER (WHERE c.route IS NOT NULL))[1] AS route,
               (array_agg(c.remarks         ORDER BY c.last_pos DESC, c.id DESC))[1] AS remarks,
               (array_agg(c.planned_alt     ORDER BY c.last_pos DESC, c.id DESC))[1] AS planned_alt,
               max(c.deptime) AS deptime,
               (array_agg(c.pilot_rating    ORDER BY c.last_pos DESC, c.id DESC))[1] AS pilot_rating,
               (array_agg(c.military_rating ORDER BY c.last_pos DESC, c.id DESC))[1] AS military_rating,
               round((sum(c.air_s) / 60.0)::numeric, 2) AS airborne_minutes,
               max(c.max_gs) AS max_gs_kt, max(c.max_alt) AS max_alt_ft,
               sum(c.cnt)::bigint AS sample_count,
               max(c.last_seen) AS newest_seen          -- the flight's newest last_seen: what the quiet test looks at
          FROM chained c
         GROUP BY c.cid, c.callsign, c.dep_n, c.arr_n, c.chain
    ),
    quiet_flights AS (                             -- QUIET PER FLIGHT: only a flight with no position for flight_quiet_hours is written
        SELECT f.* FROM flights f WHERE ref - f.newest_seen >= quiet AND f.session_ids <@ locked   -- never a flight with a session we did not lock
    ),
    conflict AS (                                  -- a flight key that already exists means positions were written but not marked: a bug
        SELECT DISTINCT f.cid
          FROM quiet_flights f
          JOIN flight_summary r ON r.session_id = f.session_id AND r.start_time = f.start_time
    ),
    ins AS (
        INSERT INTO flight_summary
               (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, logon_time,
                name, home_icao, server, aircraft_icao, aircraft_faa, flight_rules, route, remarks, planned_alt,
                deptime, pilot_rating, military_rating, airborne_minutes, max_gs_kt, max_alt_ft, sample_count)
        SELECT f.session_id, f.start_time, f.end_time, f.cid, f.callsign, f.departure, f.arrival, f.session_ids, f.logon_time,
               f.name, f.home_icao, f.server, f.aircraft_icao, f.aircraft_faa, f.flight_rules, f.route, f.remarks, f.planned_alt,
               f.deptime, f.pilot_rating, f.military_rating, f.airborne_minutes, f.max_gs_kt, f.max_alt_ft, f.sample_count
          FROM quiet_flights f
         WHERE f.cid NOT IN (SELECT cid FROM conflict)
        RETURNING cid
    ),
    marks AS (                                     -- sessions to mark: those of the flights written now, and the sessions that form no
        SELECT x.id, x.cid                         -- flight (no plan, or no positions) once they are quiet themselves.
          FROM (SELECT unnest(q.session_ids) AS id, q.cid FROM quiet_flights q
                UNION
                SELECT s.id, s.cid FROM s
                 WHERE s.id = ANY (locked) AND s.id NOT IN (SELECT g.id FROM gated g) AND ref - s.last_seen >= quiet) x
         WHERE x.id = ANY (locked) AND x.cid NOT IN (SELECT cid FROM conflict)    -- only locked sessions can ever be marked
    ),
    mark AS (                                      -- a flight that is not quiet yet keeps its sessions unprocessed (flown_upto untouched)
        UPDATE pilot_session ps SET flown_upto = ps.last_seen
         WHERE ps.id IN (SELECT m.id FROM marks m)
        RETURNING ps.cid
    )
    SELECT (SELECT count(*) FROM ins), (SELECT array_agg(cid) FROM conflict),
           (SELECT array_agg(p) FROM unnest(pilots) p
             WHERE p NOT IN (SELECT cid FROM ins) AND p NOT IN (SELECT cid FROM mark))
      INTO n, bad, idle;

    total := total + n;
    IF bad IS NOT NULL THEN
        RAISE WARNING 'write_finished_flights: % pilot(s) skipped, a flight with the same key already exists but its sessions were not marked (cids %)',
            cardinality(bad), bad;
    END IF;
    EXIT WHEN idle IS NULL OR tries >= 10;
    skip := skip || idle;
    bad := NULL;
    idle := NULL;
    END LOOP;
    RETURN total;
END
$function$

;

-- ============================================================ function drop_old_pilot_position_partitions
CREATE OR REPLACE FUNCTION public.drop_old_pilot_position_partitions(retention_days integer, p_today date DEFAULT NULL::date)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
DECLARE
    max_per_call constant int := 3;   -- candidates checked per call (a busy one must not starve the next); at most ONE is dropped
    old_lock_timeout text := current_setting('lock_timeout');
    cutoff       timestamptz;
    r            record;
    p_start      timestamptz;
    p_end        timestamptz;
    pinned       bigint[];
    n_bad        bigint;
    bad_ids      bigint[];
    bad_reasons  text;
    to_drop      text[] := ARRAY[]::text[];
    keep         text[] := ARRAY[]::text[];
    part         text;
    attempts     int;
    attempt      int;
    done         boolean;
    n            int := 0;
BEGIN
    IF retention_days IS NULL OR retention_days < 1 THEN
        RAISE EXCEPTION 'retention_days must be >= 1';
    END IF;
    cutoff := COALESCE(p_today::timestamp AT TIME ZONE 'UTC', now()) - make_interval(hours => retention_days * 24);

    -- A refusal row is cleared only when its day is dropped (below), no longer exists (dropped by hand) or is no longer due (the
    -- retention was raised). Any other keep path leaves it, or writes it, so a day kept for ever stays visible to the watchdog.
    DELETE FROM public.drop_gate_refusal d
     WHERE to_regclass(format('public.%I', d.part_name)) IS NULL
        OR to_date(right(d.part_name, 8), 'YYYYMMDD')::timestamp AT TIME ZONE 'UTC' + interval '24 hours' > cutoff;

    -- 1. pinning rule, then the reconciliation with the written flights (oldest first, at most max_per_call partitions).
    --    Plain reads only: no lock on pilot_position, no write.
    FOR r IN
        SELECT c.relname::text AS relname
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = 'public.pilot_position'::regclass
          AND c.relname ~ '^pilot_position_[0-9]{8}$'
        ORDER BY c.relname
    LOOP
        EXIT WHEN cardinality(to_drop) >= max_per_call;
        p_start := to_date(right(r.relname, 8), 'YYYYMMDD')::timestamp AT TIME ZONE 'UTC';
        p_end   := p_start + interval '24 hours';
        CONTINUE WHEN p_end > cutoff;

        SELECT array_agg(DISTINCT s.id) INTO pinned
          FROM public.pilot_position p
          JOIN public.pilot_session s ON s.id = p.session_id
         WHERE p.observed_at >= p_start AND p.observed_at < p_end
           AND s.last_seen >= cutoff;
        IF pinned IS NOT NULL THEN
            IF p_end < cutoff - make_interval(hours => retention_days * 24) THEN
                RAISE NOTICE 'partition % is pinned beyond twice the retention by session id(s) %',
                    r.relname, pinned[1:5];
            END IF;
            DELETE FROM public.drop_gate_refusal WHERE part_name = r.relname;      -- pinned is the rule working, not a refusal
            CONTINUE;
        END IF;

        SELECT count(*), (array_agg(u.session_id ORDER BY u.session_id))[1:5], string_agg(DISTINCT u.reason, '; ')
          INTO n_bad, bad_ids, bad_reasons
          FROM public.unreconciled_partition_sessions(p_start, p_end) u;
        IF n_bad > 0 THEN
            RAISE NOTICE 'DROP REFUSED: partition % kept: % session(s) or flight(s) not reconciled with the written flights (%); first ids %',
                r.relname, n_bad, bad_reasons, bad_ids;
            INSERT INTO public.drop_gate_refusal (part_name, detail)
                 VALUES (r.relname, left(n_bad || ' unreconciled: ' || bad_reasons, 500))
            ON CONFLICT (part_name) DO UPDATE SET last_refused = now(), detail = EXCLUDED.detail;
            IF p_end < cutoff - make_interval(hours => retention_days * 24) THEN
                RAISE NOTICE 'partition % is kept beyond twice the retention: the flight writer has stopped or a position is not in a written flight (see the previous notice)',
                    r.relname;
            END IF;
            CONTINUE;
        END IF;

        to_drop := to_drop || r.relname;
    END LOOP;

    -- 2. re-check each candidate with no lock held: the world may have moved while later partitions were checked
    FOREACH part IN ARRAY to_drop LOOP
        p_start := to_date(right(part, 8), 'YYYYMMDD')::timestamp AT TIME ZONE 'UTC';
        p_end   := p_start + interval '24 hours';
        IF EXISTS (SELECT 1 FROM public.pilot_position p JOIN public.pilot_session s ON s.id = p.session_id
                    WHERE p.observed_at >= p_start AND p.observed_at < p_end AND s.last_seen >= cutoff) THEN
            RAISE NOTICE 'partition % kept: a session in it was seen again inside the retention window', part;
            DELETE FROM public.drop_gate_refusal WHERE part_name = part;
            CONTINUE;
        END IF;
        SELECT count(*), (array_agg(u.session_id ORDER BY u.session_id))[1:5], string_agg(DISTINCT u.reason, '; ')
          INTO n_bad, bad_ids, bad_reasons
          FROM public.unreconciled_partition_sessions(p_start, p_end) u;
        IF n_bad > 0 THEN
            RAISE NOTICE 'DROP REFUSED: partition % kept: % session(s) or flight(s) no longer reconciled (%); first ids %',
                part, n_bad, bad_reasons, bad_ids;
            INSERT INTO public.drop_gate_refusal (part_name, detail)
                 VALUES (part, left(n_bad || ' no longer reconciled: ' || bad_reasons, 500))
            ON CONFLICT (part_name) DO UPDATE SET last_refused = now(), detail = EXCLUDED.detail;
        ELSE
            keep := keep || part;
        END IF;
    END LOOP;

    -- 3. the drops
    IF cardinality(keep) > 0 THEN
        PERFORM set_config('lock_timeout', '1s', true);
        FOREACH part IN ARRAY keep LOOP
            p_start := to_date(right(part, 8), 'YYYYMMDD')::timestamp AT TIME ZONE 'UTC';
            p_end   := p_start + interval '24 hours';
            done := false;
            attempts := 3;                                         -- no parent lock is held yet (one drop per call), so sleeping between tries is free
            FOR attempt IN 1..attempts LOOP
                BEGIN
                    EXECUTE format('LOCK TABLE public.%I IN ACCESS EXCLUSIVE MODE NOWAIT', part);
                    -- With the partition locked nothing can be inserted into it any more. Rebuild everything from the partition itself (not
                    -- from a list made before the lock: a session not on that list could have added a position since) and repeat the
                    -- whole gate: pinning, processed, covered and counted.
                    IF EXISTS (SELECT 1 FROM public.pilot_position p JOIN public.pilot_session s ON s.id = p.session_id
                                WHERE p.observed_at >= p_start AND p.observed_at < p_end AND s.last_seen >= cutoff)
                       OR EXISTS (SELECT 1 FROM public.unreconciled_partition_sessions(p_start, p_end)) THEN
                        RAISE EXCEPTION 'partition % changed since it was checked', part USING ERRCODE = 'VP001';
                    END IF;
                    EXECUTE format('DROP TABLE IF EXISTS public.%I', part);
                    DELETE FROM public.drop_gate_refusal WHERE part_name = part;
                    n := n + 1;
                    done := true;
                    EXIT;
                EXCEPTION
                    WHEN lock_not_available OR deadlock_detected THEN
                        IF attempt < attempts THEN
                            PERFORM pg_sleep(0.5);
                        END IF;
                    WHEN SQLSTATE 'VP001' THEN
                        RAISE NOTICE 'DROP REFUSED: partition % kept: it changed after it was checked (a session seen again, or a position not covered by a written flight)', part;
                        INSERT INTO public.drop_gate_refusal (part_name, detail)
                             VALUES (part, 'changed after it was checked (session seen again, or a position not covered)')
                        ON CONFLICT (part_name) DO UPDATE SET last_refused = now(), detail = EXCLUDED.detail;
                        done := true;
                        EXIT;
                    WHEN undefined_table THEN
                        done := true;                                 -- dropped meanwhile by another run
                        EXIT;
                END;
            END LOOP;
            EXIT WHEN n > 0;     -- ONE drop per call: the DROP leaves the parent table ACCESS EXCLUSIVE locked until the call ends, so no
                                 -- further day is re-checked (a full day read) or locked behind it; the caller pauses and calls again
            IF NOT done THEN
                RAISE NOTICE 'DROP REFUSED: partition % kept: could not lock it (readers or writers busy); retried by the next run', part;
                INSERT INTO public.drop_gate_refusal (part_name, detail)
                     VALUES (part, 'could not lock it (readers or writers busy)')
                ON CONFLICT (part_name) DO UPDATE SET last_refused = now(), detail = EXCLUDED.detail;
            END IF;
        END LOOP;
        PERFORM set_config('lock_timeout', old_lock_timeout, true);
    END IF;
    RETURN n;
END
$function$

;

-- ============================================================ function unreconciled_partition_sessions
CREATE OR REPLACE FUNCTION public.unreconciled_partition_sessions(p_start timestamp with time zone, p_end timestamp with time zone)
 RETURNS TABLE(session_id bigint, reason text)
 LANGUAGE sql
 STABLE SECURITY DEFINER
 SET search_path TO 'public', 'pg_temp'
AS $function$
    WITH pos AS (                                  -- the day's positions, one row per session
        SELECT p.session_id, count(*) AS n, max(p.observed_at) AS last_at
          FROM public.pilot_position p
         WHERE p.observed_at >= p_start AND p.observed_at < p_end
         GROUP BY p.session_id
    ),
    fl AS (                                        -- flights that overlap the day, one row per (flight, session)
        SELECT u.sid, f.start_time, f.end_time
          FROM public.flight_summary f
         CROSS JOIN LATERAL unnest(f.session_ids) AS u(sid)
         WHERE f.end_time >= p_start AND f.start_time < p_end
    ),
    cov AS (                                       -- positions of the day inside a written flight of their own session
        SELECT p.session_id, count(*) AS n
          FROM public.pilot_position p
         WHERE p.observed_at >= p_start AND p.observed_at < p_end
           AND EXISTS (SELECT 1 FROM fl
                        WHERE fl.sid = p.session_id AND p.observed_at BETWEEN fl.start_time AND fl.end_time)
         GROUP BY p.session_id
    )
    SELECT pos.session_id,
           CASE
               WHEN s.id IS NULL
                   THEN 'session row missing'
               WHEN s.flown_upto IS NULL OR s.flown_upto < pos.last_at
                   THEN 'positions not yet processed by the flight writer'
               WHEN NULLIF(upper(btrim(s.departure, E' \t\r\n')), '') IS NOT NULL
                AND NULLIF(upper(btrim(s.arrival,   E' \t\r\n')), '') IS NOT NULL
                AND COALESCE(cov.n, 0) < pos.n
                   THEN 'positions not inside a written flight'
           END AS reason
      FROM pos
      LEFT JOIN public.pilot_session s ON s.id = pos.session_id
      LEFT JOIN cov ON cov.session_id = pos.session_id
     WHERE s.id IS NULL
        OR s.flown_upto IS NULL OR s.flown_upto < pos.last_at
        OR (NULLIF(upper(btrim(s.departure, E' \t\r\n')), '') IS NOT NULL
            AND NULLIF(upper(btrim(s.arrival, E' \t\r\n')), '') IS NOT NULL
            AND COALESCE(cov.n, 0) < pos.n)
    UNION ALL
    -- Stored count against sample_count for every flight that overlaps the day, provided EVERY day the flight spans is still stored
    -- (a flight whose earlier day is already dropped cannot be recounted; its positions are still covered by the check above, and
    -- the earlier day was compared when it was dropped). Fail safe: any difference keeps the day.
    SELECT f.session_id, 'flight sample_count differs from its stored positions'
      FROM public.flight_summary f
     WHERE f.end_time >= p_start AND f.start_time < p_end AND f.sample_count > 0
       AND NOT EXISTS (SELECT 1 FROM generate_series((f.start_time AT TIME ZONE 'UTC')::date,
                                                      (f.end_time AT TIME ZONE 'UTC')::date, interval '1 day') d
                        WHERE to_regclass(format('public.pilot_position_%s', to_char(d, 'YYYYMMDD'))) IS NULL)
       AND f.sample_count <> (SELECT count(*) FROM public.pilot_position p
                               WHERE p.session_id = ANY (f.session_ids)
                                 AND p.observed_at BETWEEN f.start_time AND f.end_time)
$function$

;

-- ============================================================ view controller_session_merged
CREATE OR REPLACE VIEW controller_session_merged AS
 WITH ordered AS (
         SELECT cs.id,
            cs.cid,
            cs.callsign,
            cs.logon_time,
            cs.name,
            cs.rating,
            cs.facility,
            cs.server,
            cs.frequency_hz,
            cs.visual_range,
            cs.first_seen,
            cs.last_seen,
            max(cs.last_seen) OVER (PARTITION BY cs.cid, cs.callsign ORDER BY cs.first_seen, cs.id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_last
           FROM controller_session cs
        ), grouped AS (
         SELECT o.id,
            o.cid,
            o.callsign,
            o.logon_time,
            o.name,
            o.rating,
            o.facility,
            o.server,
            o.frequency_hz,
            o.visual_range,
            o.first_seen,
            o.last_seen,
            o.prev_last,
            sum(
                CASE
                    WHEN o.prev_last IS NULL OR (o.first_seen - o.prev_last) > '00:05:00'::interval THEN 1
                    ELSE 0
                END) OVER (PARTITION BY o.cid, o.callsign ORDER BY o.first_seen, o.id) AS merge_group
           FROM ordered o
        ), merged AS (
         SELECT grouped.cid,
            grouped.callsign,
            grouped.merge_group,
            min(grouped.id) AS first_session_id,
            min(grouped.logon_time) AS start_time,
            min(grouped.first_seen) AS first_seen,
            max(grouped.last_seen) AS end_time,
            count(*) AS session_count,
            (array_agg(grouped.name ORDER BY grouped.last_seen DESC))[1] AS name,
            (array_agg(grouped.rating ORDER BY grouped.last_seen DESC))[1] AS rating,
            (array_agg(grouped.facility ORDER BY grouped.last_seen DESC))[1] AS facility,
            (array_agg(grouped.server ORDER BY grouped.last_seen DESC))[1] AS server,
            (array_agg(grouped.frequency_hz ORDER BY grouped.last_seen DESC))[1] AS frequency_hz
           FROM grouped
          GROUP BY grouped.cid, grouped.callsign, grouped.merge_group
        )
 SELECT cid,
    callsign,
    merge_group,
    first_session_id,
    session_count,
    name,
    rating,
    facility,
    server,
    frequency_hz,
    start_time,
    end_time,
    end_time - start_time AS duration,
    round(EXTRACT(epoch FROM end_time - start_time) / 3600.0, 2) AS duration_hours,
    end_time > ((( SELECT max(network_snapshot.observed_at) AS max
           FROM network_snapshot)) - make_interval(secs => (2::numeric * (( SELECT app_setting.value::numeric AS value
           FROM app_setting
          WHERE app_setting.key = 'poll_interval_seconds'::text)))::double precision)) AS is_open
   FROM merged m;


-- ============================================================ queries used to produce the text above
-- SELECT pg_get_functiondef(p.oid) FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace
--    AND p.proname IN ('write_finished_flights', 'drop_old_pilot_position_partitions', 'unreconciled_partition_sessions');
-- SELECT pg_get_viewdef('controller_session_merged'::regclass, true);
