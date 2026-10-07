-- migrate:up
-- Partition functions: least privilege (M7), finalise flights before a drop (M1), pinning notice (L3).
--
-- Both functions become SECURITY DEFINER with a fixed search_path and owner-only EXECUTE, so the
-- `collector` role (db/roles.sql) needs no DDL rights: it is granted EXECUTE and nothing else.

CREATE OR REPLACE FUNCTION ensure_pilot_position_partitions(days_ahead int DEFAULT 7, p_today date DEFAULT NULL)
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    today date := COALESCE(p_today, (now() AT TIME ZONE 'UTC')::date);
    d     date;
    part  text;
    n     int := 0;
BEGIN
    IF days_ahead IS NULL OR days_ahead < 0 THEN
        RAISE EXCEPTION 'days_ahead must be >= 0';
    END IF;
    FOR d IN SELECT (today + g)::date FROM generate_series(-1, days_ahead) AS g LOOP
        part := 'pilot_position_' || to_char(d, 'YYYYMMDD');
        IF to_regclass(format('public.%I', part)) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE public.%I PARTITION OF public.pilot_position FOR VALUES FROM (%L) TO (%L)',
                part,
                d::text || ' 00:00:00+00',
                (d + 1)::text || ' 00:00:00+00');
            n := n + 1;
        END IF;
    END LOOP;
    RETURN n;
END
$fn$;

-- Drops daily partitions that end before (p_today, default now()) - retention_days. Returns the number dropped.
--
-- PINNING RULE: a partition is KEPT while it holds a position of any session whose last_seen is within the
-- retention window (an open or recently active session keeps every partition it touched, because its
-- flights are still being recomputed from the raw positions). A partition pinned for longer than twice the
-- retention (a stuck session with an old position) raises a NOTICE naming the partition and up to five
-- pinning session ids; the collector logs it as a warning.
--
-- FINALISE RULE: before a partition is dropped, every session with positions in it is summarised into
-- flight_summary (final = true), and the partition is only dropped if every such session has a final
-- summary. Flights therefore survive the raw-position retention.
CREATE OR REPLACE FUNCTION drop_old_pilot_position_partitions(retention_days int, p_today date DEFAULT NULL)
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    cutoff  timestamptz;
    r       record;
    p_start timestamptz;
    p_end   timestamptz;
    pinned  bigint[];
    ids     bigint[];
    n       int := 0;
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

        EXECUTE format('DROP TABLE public.%I', r.relname);
        n := n + 1;
    END LOOP;
    RETURN n;
END
$fn$;

REVOKE ALL ON FUNCTION ensure_pilot_position_partitions(int, date) FROM PUBLIC;
REVOKE ALL ON FUNCTION drop_old_pilot_position_partitions(int, date) FROM PUBLIC;

-- Refresh every materialized view in public (concurrently when it has a unique index). There are none
-- today; this lets the unprivileged collector keep doing it if one is added later.
CREATE OR REPLACE FUNCTION refresh_materialized_views()
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    r record;
    n int := 0;
BEGIN
    FOR r IN SELECT matviewname AS name FROM pg_matviews WHERE schemaname = 'public' LOOP
        BEGIN
            EXECUTE format('REFRESH MATERIALIZED VIEW CONCURRENTLY public.%I', r.name);
        EXCEPTION WHEN object_not_in_prerequisite_state THEN
            EXECUTE format('REFRESH MATERIALIZED VIEW public.%I', r.name);
        END;
        n := n + 1;
    END LOOP;
    RETURN n;
END
$fn$;
REVOKE ALL ON FUNCTION refresh_materialized_views() FROM PUBLIC;

-- migrate:down
-- Forward-only project: no automatic rollback.
