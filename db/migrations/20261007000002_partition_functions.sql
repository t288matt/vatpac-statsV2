-- migrate:up
-- Daily partition management for pilot_position (SPEC sections 6 and 7).

-- Creates partitions for yesterday .. today+days_ahead (UTC). Returns the number created.
-- p_today defaults to the current UTC date (the argument exists so tests can pin the date).
CREATE OR REPLACE FUNCTION ensure_pilot_position_partitions(days_ahead int DEFAULT 7, p_today date DEFAULT NULL)
RETURNS int
LANGUAGE plpgsql
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

-- Drops daily partitions that end before (p_today, default now()) - retention_days, unless the partition holds
-- positions of a session whose last_seen is within the retention window. Returns the number dropped.
CREATE OR REPLACE FUNCTION drop_old_pilot_position_partitions(retention_days int, p_today date DEFAULT NULL)
RETURNS int
LANGUAGE plpgsql
AS $fn$
DECLARE
    cutoff  timestamptz;
    r       record;
    p_start timestamptz;
    p_end   timestamptz;
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
        CONTINUE WHEN EXISTS (
            SELECT 1
            FROM pilot_position p
            JOIN pilot_session s ON s.id = p.session_id
            WHERE p.observed_at >= p_start AND p.observed_at < p_end
              AND s.last_seen >= cutoff);
        EXECUTE format('DROP TABLE public.%I', r.relname);
        n := n + 1;
    END LOOP;
    RETURN n;
END
$fn$;

-- Partitions must exist before the first insert.
SELECT ensure_pilot_position_partitions(7);

-- migrate:down
-- Forward-only project: no automatic rollback.
