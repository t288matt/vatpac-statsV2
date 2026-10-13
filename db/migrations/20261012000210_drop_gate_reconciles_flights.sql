-- migrate:up
-- Issue #89 phase 4 (#140): the raw-position drop gate reconciles against WRITTEN flights.
-- Design: docs/DESIGN_89_FINAL.md section 3 (branch opus-89); requirement-to-test table: docs/CONFORMANCE_89_PHASE4.md.
-- NOT YET DEPLOYED: written on branch p4-drop-gate, not merged, not run on the VM or the live database.
--
-- Until now a daily partition of pilot_position was dropped only after the pieces table (flight_summary) had been
-- "finalised" for every session in it (refresh_flight_summary_sessions(ids, true), the final flag). That rule belongs to the
-- pieces, which issue 89 retires. From this migration the gate asks instead whether the flights are safely WRITTEN:
-- a partition may be dropped only when, for every session with positions in it,
--   1. processed : the session's flown_upto is at or after its last position in the partition (the flight writer has been
--                  through all of them); and
--   2. covered   : if the session passes the flight gate (departure and arrival non-empty, normalised as the writer does),
--                  each of its positions in the partition lies inside the [start_time, end_time] of a written flight that
--                  lists the session in session_ids. A session without a plan has no flight and needs only item 1; and
--   3. counted   : each written flight that lies wholly inside the day has sample_count equal to the positions stored for it.
-- Anything else keeps the partition (fail safe: data deleted here is irreplaceable) with a NOTICE that starts "DROP REFUSED",
-- which the collector logs as a WARNING (maintenance.drop_old_partitions); the watchdog's integrity sweep reports sessions that are
-- processed but in no written flight (ops/audit/invariants.sql).
-- The pinning rule is unchanged: a session seen inside the retention window keeps its partitions. The old finalisation step and the
-- final re-checks are gone: this function no longer calls refresh_flight_summary_sessions and no longer reads flight_summary.
--
-- One new internal function, unreconciled_partition_sessions(p_start, p_end): the reconciliation as a read-only query, so the drop
-- function and the owner's rehearsal (docs/RUNBOOKS/drop-gate-rehearsal.md) use the same code. It is set-based: one aggregate per
-- session over the day, one range semi-join against the flights that overlap the day (served by the start_time and end_time
-- indexes), never a probe per position. It is SECURITY DEFINER, not granted to anyone (REVOKE FROM PUBLIC), like the other helpers.
--
-- Unchanged: the signature, the return value (partitions dropped in this call), at most 10 partitions per call, the lock-friendly
-- order of migration 20261009000170 (no parent lock while checking; LOCK ... NOWAIT per partition; DROP with a 1 s lock timeout; retries
-- only before the first drop), the restore of lock_timeout, the permissions. No writer advisory lock is taken (design section 3,
-- reviewer point 4): the gate is monotone, because an old partition receives no inserts, flown_upto only rises and written flights
-- are never removed, so a writer running at the same moment can only turn a "keep" into a later "drop".

CREATE OR REPLACE FUNCTION unreconciled_partition_sessions(p_start timestamptz, p_end timestamptz)
RETURNS TABLE (session_id bigint, reason text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
    WITH pos AS (                                  -- the day's positions, one row per session
        SELECT p.session_id, count(*) AS n, max(p.observed_at) AS last_at
          FROM pilot_position p
         WHERE p.observed_at >= p_start AND p.observed_at < p_end
         GROUP BY p.session_id
    ),
    fl AS (                                        -- flights that overlap the day, one row per (flight, session)
        SELECT u.sid, f.start_time, f.end_time
          FROM flight_record f
         CROSS JOIN LATERAL unnest(f.session_ids) AS u(sid)
         WHERE f.end_time >= p_start AND f.start_time < p_end
    ),
    cov AS (                                       -- positions of the day inside a written flight of their own session
        SELECT p.session_id, count(*) AS n
          FROM pilot_position p
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
      LEFT JOIN pilot_session s ON s.id = pos.session_id
      LEFT JOIN cov ON cov.session_id = pos.session_id
     WHERE s.id IS NULL
        OR s.flown_upto IS NULL OR s.flown_upto < pos.last_at
        OR (NULLIF(upper(btrim(s.departure, E' \t\r\n')), '') IS NOT NULL
            AND NULLIF(upper(btrim(s.arrival, E' \t\r\n')), '') IS NOT NULL
            AND COALESCE(cov.n, 0) < pos.n)
    UNION ALL
    -- Flights wholly inside the day: the stored count must equal the row's sample_count. A flight that crosses midnight is not
    -- counted here (the other half may be in a partition already dropped); its positions are covered by the check above.
    SELECT f.session_id, 'flight sample_count differs from its stored positions'
      FROM flight_record f
     WHERE f.start_time >= p_start AND f.end_time < p_end AND f.sample_count > 0
       AND f.sample_count <> (SELECT count(*) FROM pilot_position p
                               WHERE p.session_id = ANY (f.session_ids)
                                 AND p.observed_at >= p_start AND p.observed_at < p_end
                                 AND p.observed_at BETWEEN f.start_time AND f.end_time)
$fn$;
REVOKE ALL ON FUNCTION unreconciled_partition_sessions(timestamptz, timestamptz) FROM PUBLIC;
COMMENT ON FUNCTION unreconciled_partition_sessions(timestamptz, timestamptz) IS
    'Issue 89 phase 4: the sessions (and flights) that stop a day of pilot_position from being dropped, with the reason. No rows = every position of the day is processed and covered by a written flight, and the flights wholly inside the day have the right sample_count. Read-only; used by drop_old_pilot_position_partitions and the drop-gate rehearsal.';

CREATE OR REPLACE FUNCTION drop_old_pilot_position_partitions(retention_days int, p_today date DEFAULT NULL)
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    max_per_call constant int := 10;
    old_lock_timeout text := current_setting('lock_timeout');
    cutoff       timestamptz;
    r            record;
    p_start      timestamptz;
    p_end        timestamptz;
    pinned       bigint[];
    ids          bigint[];
    n_bad        bigint;
    bad_ids      bigint[];
    bad_reasons  text;
    cand         jsonb := '{}'::jsonb;
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
    cutoff := COALESCE((p_today::text || ' 00:00:00+00')::timestamptz, now()) - make_interval(hours => retention_days * 24);

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
        p_start := (to_date(right(r.relname, 8), 'YYYYMMDD')::text || ' 00:00:00+00')::timestamptz;
        p_end   := p_start + interval '24 hours';
        CONTINUE WHEN p_end > cutoff;

        SELECT array_agg(DISTINCT s.id) INTO pinned
          FROM pilot_position p
          JOIN pilot_session s ON s.id = p.session_id
         WHERE p.observed_at >= p_start AND p.observed_at < p_end
           AND s.last_seen >= cutoff;
        IF pinned IS NOT NULL THEN
            IF p_end < cutoff - make_interval(hours => retention_days * 24) THEN
                RAISE NOTICE 'partition % is pinned beyond twice the retention by session id(s) %',
                    r.relname, pinned[1:5];
            END IF;
            CONTINUE;
        END IF;

        SELECT count(*), (array_agg(u.session_id ORDER BY u.session_id))[1:5], string_agg(DISTINCT u.reason, '; ')
          INTO n_bad, bad_ids, bad_reasons
          FROM unreconciled_partition_sessions(p_start, p_end) u;
        IF n_bad > 0 THEN
            RAISE NOTICE 'DROP REFUSED: partition % kept: % session(s) or flight(s) not reconciled with the written flights (%); first ids %',
                r.relname, n_bad, bad_reasons, bad_ids;
            IF p_end < cutoff - make_interval(hours => retention_days * 24) THEN
                RAISE NOTICE 'partition % is kept beyond twice the retention: the flight writer has stopped or a position is not in a written flight (see the previous notice)',
                    r.relname;
            END IF;
            CONTINUE;
        END IF;

        ids := ARRAY(SELECT DISTINCT p.session_id FROM pilot_position p
                      WHERE p.observed_at >= p_start AND p.observed_at < p_end);
        to_drop := to_drop || r.relname;
        cand := cand || jsonb_build_object(r.relname, to_jsonb(ids));
    END LOOP;

    -- 2. re-check each candidate with no lock held: the world may have moved while later partitions were checked
    FOREACH part IN ARRAY to_drop LOOP
        p_start := (to_date(right(part, 8), 'YYYYMMDD')::text || ' 00:00:00+00')::timestamptz;
        p_end   := p_start + interval '24 hours';
        IF EXISTS (SELECT 1 FROM pilot_position p JOIN pilot_session s ON s.id = p.session_id
                    WHERE p.observed_at >= p_start AND p.observed_at < p_end AND s.last_seen >= cutoff) THEN
            RAISE NOTICE 'partition % kept: a session in it was seen again inside the retention window', part;
            CONTINUE;
        END IF;
        SELECT count(*), (array_agg(u.session_id ORDER BY u.session_id))[1:5], string_agg(DISTINCT u.reason, '; ')
          INTO n_bad, bad_ids, bad_reasons
          FROM unreconciled_partition_sessions(p_start, p_end) u;
        IF n_bad > 0 THEN
            RAISE NOTICE 'DROP REFUSED: partition % kept: % session(s) or flight(s) no longer reconciled (%); first ids %',
                part, n_bad, bad_reasons, bad_ids;
        ELSE
            keep := keep || part;
        END IF;
    END LOOP;

    -- 3. the drops
    IF cardinality(keep) > 0 THEN
        PERFORM set_config('lock_timeout', '1s', true);
        FOREACH part IN ARRAY keep LOOP
            ids := ARRAY(SELECT jsonb_array_elements_text(cand -> part)::bigint);
            p_start := (to_date(right(part, 8), 'YYYYMMDD')::text || ' 00:00:00+00')::timestamptz;
            p_end   := p_start + interval '24 hours';
            done := false;
            attempts := CASE WHEN n = 0 THEN 3 ELSE 1 END;         -- sleeping is only free while no parent lock is held
            FOR attempt IN 1..attempts LOOP
                BEGIN
                    EXECUTE format('LOCK TABLE public.%I IN ACCESS EXCLUSIVE MODE NOWAIT', part);
                    -- cheap, by session id (one index lookup each, the partition is locked): item 1 of the gate for the
                    -- last position of each session in this day, plus the pinning rule. Written flights are never removed
                    -- and flown_upto only rises, so item 2 cannot turn from true to false and is not repeated here.
                    IF (SELECT count(*) FROM pilot_session s WHERE s.id = ANY (ids)) <> cardinality(ids)
                       OR EXISTS (SELECT 1 FROM pilot_session s
                                   WHERE s.id = ANY (ids)
                                     AND (s.last_seen >= cutoff
                                          OR s.flown_upto IS NULL
                                          OR s.flown_upto < (SELECT max(p.observed_at) FROM pilot_position p
                                                              WHERE p.session_id = s.id
                                                                AND p.observed_at >= p_start AND p.observed_at < p_end))) THEN
                        RAISE EXCEPTION 'partition % changed since it was checked', part USING ERRCODE = 'VP001';
                    END IF;
                    EXECUTE format('DROP TABLE IF EXISTS public.%I', part);
                    n := n + 1;
                    done := true;
                    EXIT;
                EXCEPTION
                    WHEN lock_not_available OR deadlock_detected THEN
                        IF attempt < attempts THEN
                            PERFORM pg_sleep(0.5);
                        END IF;
                    WHEN SQLSTATE 'VP001' THEN
                        RAISE NOTICE 'DROP REFUSED: partition % kept: a session in it was seen again or has positions after its processed marker', part;
                        done := true;
                        EXIT;
                    WHEN undefined_table THEN
                        done := true;                                 -- dropped meanwhile by another run
                        EXIT;
                END;
            END LOOP;
            IF NOT done THEN
                RAISE NOTICE 'partition % kept: could not lock it (readers or writers busy); retried by the next run', part;
            END IF;
        END LOOP;
        PERFORM set_config('lock_timeout', old_lock_timeout, true);
    END IF;
    RETURN n;
END
$fn$;
REVOKE ALL ON FUNCTION drop_old_pilot_position_partitions(int, date) FROM PUBLIC;
COMMENT ON FUNCTION drop_old_pilot_position_partitions(int, date) IS
    'Drops daily pilot_position partitions older than the retention, only when every position in them is processed by the flight writer and covered by a written flight (issue 89 phase 4, unreconciled_partition_sessions). Fails safe: any doubt keeps the partition with a NOTICE starting DROP REFUSED. Pinning rule unchanged. At most 10 per call; returns the number dropped.';

-- migrate:down
-- Forward-only project: no automatic rollback (the previous definition is in migration 20261009000170).
