-- migrate:up
-- Issue #98 (review of issue #80): drop_old_pilot_position_partitions() ran the whole job in one transaction and
-- dropped each partition as soon as it had finalised it. A DROP of a partition takes ACCESS EXCLUSIVE on the parent
-- pilot_position, and that lock is held until the transaction ends, i.e. across the finalisation of every LATER
-- partition (about 20 seconds each at the live volume, measured in docs/RUNBOOKS/db-full.md). Every collector insert
-- into pilot_position in that time waited, and failed after its 5 second lock_timeout. With one partition due per
-- night the window is short, but a catch-up run (collector stopped for days, retention lowered) would stall polling for
-- minutes.
--
-- Same rules, same result, different order:
--   1. for every partition older than the cutoff: apply the pinning rule, finalise its sessions, check they are final
--      (no lock on pilot_position beyond the ordinary row locks of the finalisation);
--   2. only then drop the partitions that passed, one after another in a short block, each with a 1 second lock
--      timeout and up to 3 attempts half a second apart. A partition that still cannot be locked is kept (NOTICE) and
--      is picked up by the next run; its finalised summaries stay committed. Previously a lock timeout raised, rolled
--      back the whole call (including the finalisation) and made the maintenance task fail.
-- The signature, the return value (number of partitions dropped), the pinning rule and its NOTICEs, the finalise rule
-- and the permissions are unchanged.
CREATE OR REPLACE FUNCTION drop_old_pilot_position_partitions(retention_days int, p_today date DEFAULT NULL)
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    cutoff   timestamptz;
    r        record;
    p_start  timestamptz;
    p_end    timestamptz;
    pinned   bigint[];
    ids      bigint[];
    to_drop  text[] := ARRAY[]::text[];
    part     text;
    attempt  int;
    n        int := 0;
BEGIN
    IF retention_days IS NULL OR retention_days < 1 THEN
        RAISE EXCEPTION 'retention_days must be >= 1';
    END IF;
    cutoff := COALESCE((p_today::text || ' 00:00:00+00')::timestamptz, now()) - make_interval(hours => retention_days * 24);
    FOR r IN
        SELECT c.relname::text AS relname
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = 'public.pilot_position'::regclass
          AND c.relname ~ '^pilot_position_[0-9]{8}$'
        ORDER BY c.relname
    LOOP
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
    END LOOP;

    IF cardinality(to_drop) > 0 THEN
        PERFORM set_config('lock_timeout', '1s', true);       -- transaction-local: the session value returns afterwards
        FOREACH part IN ARRAY to_drop LOOP
            FOR attempt IN 1..3 LOOP
                BEGIN
                    EXECUTE format('DROP TABLE public.%I', part);
                    n := n + 1;
                    EXIT;
                EXCEPTION WHEN lock_not_available THEN
                    IF attempt = 3 THEN
                        RAISE NOTICE 'partition % kept: could not lock it (readers or writers busy); retried by the next run', part;
                    ELSE
                        PERFORM pg_sleep(0.5);
                    END IF;
                END;
            END LOOP;
        END LOOP;
    END IF;
    RETURN n;
END
$fn$;
REVOKE ALL ON FUNCTION drop_old_pilot_position_partitions(int, date) FROM PUBLIC;

-- migrate:down
-- Forward-only project: no automatic rollback (the previous definition is in migration 20261007000021).
