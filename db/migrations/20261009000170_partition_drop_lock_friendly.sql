-- migrate:up
-- Issue #98 (review of issue #80): drop_old_pilot_position_partitions() ran the whole job in one transaction and
-- dropped each partition as soon as it had finalised it. A DROP of a partition takes ACCESS EXCLUSIVE on the parent
-- pilot_position (taken BEFORE it waits for the partition itself), and that lock is held until the transaction ends,
-- i.e. across the finalisation of every LATER partition (about 20 seconds each at the live volume, measured in
-- docs/RUNBOOKS/db-full.md). Every collector insert into pilot_position in that time waited, and failed after its 5
-- second lock_timeout. With one partition due per night the window is short, but a catch-up run (collector stopped for
-- days, retention lowered) stopped polling for minutes. A busy partition (a long reader) made it worse: the DROP
-- waited for it while holding the parent lock.
--
-- Same rules, same result, different order and a cap per call:
--   1. for the oldest eligible partitions (at most 10 per call): apply the pinning rule, finalise their sessions, check
--      they are final. No lock on pilot_position beyond the ordinary row locks of the finalisation.
--   2. re-check every partition that passed, with no lock held: still no session seen inside the retention window
--      (a session reactivated meanwhile pins it), every session still has a final summary, and no position is newer
--      than the final summary of its session (a late position). A partition that fails is kept with a NOTICE.
--   3. drop the partitions that passed, one after another in a short block. Per partition: LOCK ... NOWAIT first (a
--      reader or writer on the partition means "busy": the parent is NOT touched, so nobody queues behind us), then a
--      cheap re-check of pinning and finalisation by session id while the partition is locked, then DROP ... IF EXISTS
--      with a 1 second lock timeout (it only waits for in-flight writer transactions on the parent). Until the first
--      drop has succeeded a busy partition is retried up to 3 times, half a second apart (no parent lock is held then,
--      so sleeping is free). After the first success the parent is locked until the call ends, so there is no sleeping
--      and no waiting: a busy partition is skipped at once. A skipped partition is kept with a NOTICE and picked up by
--      the next run.
-- The collector calls the function repeatedly until it drops nothing (maintenance.drop_old_partitions), so a catch-up
-- is a series of short transactions and the parent lock ends with each call. The function no longer raises on a busy
-- partition, so the caller's commit keeps the finalised summaries (before, the lock timeout raised and rolled the whole
-- call back, finalisation included). The signature, the return value (number of partitions dropped in this call), the
-- pinning rule and its NOTICEs, the finalise rule and the permissions are unchanged. lock_timeout is restored at the end.
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

    -- 1. pinning rule and finalisation (oldest first, at most max_per_call partitions)
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

        ids := ARRAY(SELECT DISTINCT p.session_id FROM pilot_position p
                      WHERE p.observed_at >= p_start AND p.observed_at < p_end);
        PERFORM refresh_flight_summary_sessions(ids, true);
        IF EXISTS (SELECT 1 FROM unnest(ids) AS i(id)
                    WHERE NOT EXISTS (SELECT 1 FROM flight_summary f WHERE f.session_id = i.id AND f.final)) THEN
            RAISE NOTICE 'partition % kept: some of its sessions could not be summarised', r.relname;
            CONTINUE;
        END IF;

        to_drop := to_drop || r.relname;
        cand := cand || jsonb_build_object(r.relname, to_jsonb(ids));
    END LOOP;

    -- 2. re-check each candidate with no lock held: the world may have moved while later partitions were finalised
    FOREACH part IN ARRAY to_drop LOOP
        p_start := (to_date(right(part, 8), 'YYYYMMDD')::text || ' 00:00:00+00')::timestamptz;
        p_end   := p_start + interval '24 hours';
        IF EXISTS (SELECT 1 FROM pilot_position p JOIN pilot_session s ON s.id = p.session_id
                    WHERE p.observed_at >= p_start AND p.observed_at < p_end AND s.last_seen >= cutoff) THEN
            RAISE NOTICE 'partition % kept: a session in it was seen again inside the retention window', part;
        ELSIF EXISTS (SELECT 1 FROM pilot_position p
                       WHERE p.observed_at >= p_start AND p.observed_at < p_end
                         AND NOT EXISTS (SELECT 1 FROM flight_summary f WHERE f.session_id = p.session_id AND f.final)) THEN
            RAISE NOTICE 'partition % kept: a session in it no longer has a final summary', part;
        ELSIF EXISTS (SELECT 1 FROM pilot_position p
                       WHERE p.observed_at >= p_start AND p.observed_at < p_end
                         AND p.observed_at > (SELECT max(f.end_time) FROM flight_summary f
                                               WHERE f.session_id = p.session_id AND f.final)) THEN
            RAISE NOTICE 'partition % kept: a position newer than the final summary of its session arrived', part;
        ELSE
            keep := keep || part;
        END IF;
    END LOOP;

    -- 3. the drops
    IF cardinality(keep) > 0 THEN
        PERFORM set_config('lock_timeout', '1s', true);
        FOREACH part IN ARRAY keep LOOP
            ids := ARRAY(SELECT jsonb_array_elements_text(cand -> part)::bigint);
            done := false;
            attempts := CASE WHEN n = 0 THEN 3 ELSE 1 END;         -- sleeping is only free while no parent lock is held
            FOR attempt IN 1..attempts LOOP
                BEGIN
                    EXECUTE format('LOCK TABLE public.%I IN ACCESS EXCLUSIVE MODE NOWAIT', part);
                    IF EXISTS (SELECT 1 FROM pilot_session s WHERE s.id = ANY (ids) AND s.last_seen >= cutoff)
                       OR EXISTS (SELECT 1 FROM unnest(ids) AS i(id)
                                   WHERE NOT EXISTS (SELECT 1 FROM flight_summary f WHERE f.session_id = i.id AND f.final)) THEN
                        RAISE EXCEPTION 'partition % changed since it was finalised', part USING ERRCODE = 'VP001';
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
                        RAISE NOTICE 'partition % kept: a session in it was seen again or lost its final summary', part;
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

-- migrate:down
-- Forward-only project: no automatic rollback (the previous definition is in migration 20261007000021).
