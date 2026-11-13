-- migrate:up
-- Issue 202: drop_old_pilot_position_partitions no longer reconciles the same day three times.
--   Before: step 1 reconciled up to 3 passing days, step 2 re-checked each again with no lock, step 3 re-checked the drop target a
--   third time under the lock, although only one day is dropped per call (the other two were recomputed from scratch on the next call).
--   Now: the function walks the due days oldest first and for each day that is neither pinned nor refused it goes straight to the lock
--   and drops it; the unlocked second check is gone because the drop repeats the whole gate under the lock, the only check that
--   guarantees anything. The walk ends at the first day dropped, so later days are not read in that call. Refused, pinned, busy and
--   changed days are passed over and never block a newer due day (as before: only passing days counted).
--   The pinned test now starts from the recently seen sessions (pilot_session_last_seen_idx) and probes the day by the primary key
--   (session_id, observed_at) instead of joining a scan of the day to pilot_session. Same result, same pinned ids.
--   Unchanged: at most ONE partition dropped per call, the NOWAIT lock with three attempts, the VP001 refusal, the notices, the grants
--   (CREATE OR REPLACE keeps them), the refusal-table clean-up and the return value (0 or 1).
CREATE OR REPLACE FUNCTION drop_old_pilot_position_partitions(retention_days int, p_today date DEFAULT NULL)
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
    old_lock_timeout text := current_setting('lock_timeout');
    cutoff       timestamptz;
    r            record;
    p_start      timestamptz;
    p_end        timestamptz;
    pinned       bigint[];
    n_bad        bigint;
    bad_ids      bigint[];
    bad_reasons  text;
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

    -- 1. each due day, oldest first: the pinning rule, then the reconciliation with the written flights (plain reads, no lock), then
    --    the drop below. The walk ends at the first day dropped, so a day after it is never read in this call.
    FOR r IN
        SELECT c.relname::text AS relname
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = 'public.pilot_position'::regclass
          AND c.relname ~ '^pilot_position_[0-9]{8}$'
        ORDER BY c.relname
    LOOP
        p_start := to_date(right(r.relname, 8), 'YYYYMMDD')::timestamp AT TIME ZONE 'UTC';
        p_end   := p_start + interval '24 hours';
        CONTINUE WHEN p_end > cutoff;

        SELECT array_agg(s.id ORDER BY s.id) INTO pinned
          FROM public.pilot_session s
         WHERE s.last_seen >= cutoff
           AND EXISTS (SELECT 1 FROM public.pilot_position p
                        WHERE p.session_id = s.id AND p.observed_at >= p_start AND p.observed_at < p_end);
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

        -- 2. the drop, with the whole gate repeated under the lock. A day that is busy or changed is left for the next call and the
        --    walk goes on to the next due day (a busy day must not starve the next); once one day is dropped the walk ends.
        part := r.relname;
        PERFORM set_config('lock_timeout', '1s', true);
        done := false;
        attempts := 3;                                             -- no parent lock is held yet (one drop per call), so sleeping between tries is free
        FOR attempt IN 1..attempts LOOP
            BEGIN
                EXECUTE format('LOCK TABLE public.%I IN ACCESS EXCLUSIVE MODE NOWAIT', part);
                -- With the partition locked nothing can be inserted into it any more. Rebuild everything from the partition itself
                -- and repeat the whole gate: processed, covered and counted, then (last, nearest the drop) the pinning rule.
                IF EXISTS (SELECT 1 FROM public.unreconciled_partition_sessions(p_start, p_end)) THEN
                    RAISE EXCEPTION 'partition % changed since it was checked', part USING ERRCODE = 'VP001';
                END IF;
                IF EXISTS (SELECT 1 FROM public.pilot_session s
                            WHERE s.last_seen >= cutoff
                              AND EXISTS (SELECT 1 FROM public.pilot_position p
                                           WHERE p.session_id = s.id AND p.observed_at >= p_start AND p.observed_at < p_end)) THEN
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
        PERFORM set_config('lock_timeout', old_lock_timeout, true);
        EXIT WHEN n > 0;         -- ONE drop per call: the DROP leaves the parent table ACCESS EXCLUSIVE locked until the call ends, so no
                                 -- further day is re-checked (a full day read) or locked behind it; the caller pauses and calls again
        IF NOT done THEN
            RAISE NOTICE 'DROP REFUSED: partition % kept: could not lock it (readers or writers busy); retried by the next run', part;
            INSERT INTO public.drop_gate_refusal (part_name, detail)
                 VALUES (part, 'could not lock it (readers or writers busy)')
            ON CONFLICT (part_name) DO UPDATE SET last_refused = now(), detail = EXCLUDED.detail;
        END IF;
    END LOOP;
    RETURN n;
END
$fn$;
COMMENT ON FUNCTION drop_old_pilot_position_partitions(int, date) IS
    'Drops daily pilot_position partitions older than the retention, only when every position in them is processed by the flight writer and covered by a written flight (issue 89 phase 4, unreconciled_partition_sessions). Fails safe: any doubt keeps the partition with a NOTICE starting DROP REFUSED. Pinning rule unchanged. Walks the due days oldest first to the first droppable one (issue 202), repeats the whole gate under the lock, and drops at most one per call (the collector repeats the call); returns the number dropped (0 or 1).';

-- migrate:down
-- Forward-only project: no automatic rollback (the previous definition is in migration 20261013000210, with the table name of 20261013000300).
