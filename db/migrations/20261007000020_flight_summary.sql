-- migrate:up
-- Incremental flight summary (review H1/M1).
--
-- The old `flight` view ran window functions over ALL of pilot_position on every query (12 s at 10.5M
-- rows) and lost flights when a raw-position partition was dropped. Instead:
--   * flight_summary holds one row per (session_id, gap_segment, span_part), as the old view computed it;
--   * refresh_flight_summary() recomputes it for sessions seen recently (window functions restricted to
--     those session ids), idempotently; the collector calls it every few minutes;
--   * drop_old_pilot_position_partitions() finalises the sessions of a partition before dropping it
--     (migration 21), so flights survive retention;
--   * `flight` becomes a thin view: flight_summary joined to pilot_session (live plan fields).
-- SPEC section 5 definitions are unchanged: 120 minute gap split (exact), 8 hour span split by buckets
-- from the start of each gap segment, airborne = gs >= 60 kt at the earlier sample with each gap capped
-- at app_setting.airborne_gap_cap_seconds (pairs more than 120 minutes apart count as zero), flight gate
-- = departure and arrival non-empty after btrim of space/tab/CR/LF (applied in the view, not the table).

CREATE TABLE IF NOT EXISTS flight_summary (
    session_id       bigint      NOT NULL REFERENCES pilot_session (id) ON DELETE CASCADE,
    gap_segment      bigint      NOT NULL,
    span_part        int         NOT NULL,
    cid              bigint      NOT NULL,
    start_time       timestamptz NOT NULL,
    end_time         timestamptz NOT NULL,
    sample_count     bigint      NOT NULL,
    airborne_minutes numeric,
    max_gs_kt        int,
    max_alt_ft       int,
    final            boolean     NOT NULL DEFAULT false,
    computed_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, gap_segment, span_part)
);
COMMENT ON TABLE flight_summary IS
    'Per (session, gap segment, 8 h part) flight aggregates computed from pilot_position. final = the session is dead and its raw positions are fully summarised (safe to drop the partitions). Maintained by refresh_flight_summary().';
CREATE INDEX IF NOT EXISTS flight_summary_start_time_idx ON flight_summary (start_time);
CREATE INDEX IF NOT EXISTS flight_summary_end_time_idx   ON flight_summary (end_time);

-- Recompute the summary for the given sessions. Sessions already final are skipped (their raw
-- positions may be partly dropped). Upsert only touches rows whose values changed; segments that no
-- longer exist are removed. Returns the number of rows inserted, changed or removed.
CREATE OR REPLACE FUNCTION refresh_flight_summary_sessions(p_ids bigint[], p_final boolean DEFAULT false)
RETURNS int
LANGUAGE plpgsql
SET search_path = public
AS $fn$
DECLARE
    gap_cap numeric;
    ids     bigint[];
    lb      timestamptz;
    n       int := 0;
BEGIN
    IF p_ids IS NULL OR cardinality(p_ids) = 0 THEN
        RETURN 0;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtext('vatpac.flight_summary_refresh'));
    gap_cap := COALESCE((SELECT value::numeric FROM app_setting WHERE key = 'airborne_gap_cap_seconds'), 40);
    SELECT array_agg(ps.id), min(ps.first_seen) - interval '1 day'
      INTO ids, lb
      FROM pilot_session ps
     WHERE ps.id = ANY (p_ids)
       AND NOT EXISTS (SELECT 1 FROM flight_summary f WHERE f.session_id = ps.id AND f.final);
    IF ids IS NULL THEN
        RETURN 0;
    END IF;

    WITH samples AS (
        SELECT p.session_id, p.observed_at, p.gs_kt, p.alt_ft,
               p.observed_at - lag(p.observed_at) OVER w AS gap,
               CASE
                   WHEN p.gs_kt >= 60
                    AND lead(p.observed_at) OVER w IS NOT NULL
                    AND lead(p.observed_at) OVER w - p.observed_at <= interval '120 minutes'
                   THEN LEAST(extract(epoch FROM lead(p.observed_at) OVER w - p.observed_at), gap_cap)
                   ELSE 0
               END AS air_s
        FROM pilot_position p
        WHERE p.session_id = ANY (ids) AND p.observed_at >= lb
        WINDOW w AS (PARTITION BY p.session_id ORDER BY p.observed_at)
    ),
    gap_seg AS (
        SELECT s.*,
               sum(CASE WHEN s.gap IS NULL OR s.gap > interval '120 minutes' THEN 1 ELSE 0 END)
                   OVER (PARTITION BY s.session_id ORDER BY s.observed_at) AS gap_segment
        FROM samples s
    ),
    span_seg AS (
        SELECT g.*,
               GREATEST(
                   ceil(extract(epoch FROM g.observed_at
                            - min(g.observed_at) OVER (PARTITION BY g.session_id, g.gap_segment)) / 28800.0)::int - 1,
                   0) AS span_part
        FROM gap_seg g
    ),
    agg AS (
        SELECT session_id, gap_segment::bigint AS gap_segment, span_part,
               min(observed_at) AS start_time, max(observed_at) AS end_time,
               count(*) AS sample_count,
               round((sum(air_s) / 60.0)::numeric, 2) AS airborne_minutes,
               max(gs_kt) AS max_gs_kt, max(alt_ft) AS max_alt_ft
        FROM span_seg
        GROUP BY session_id, gap_segment, span_part
    ),
    up AS (
        INSERT INTO flight_summary AS fs
               (session_id, gap_segment, span_part, cid, start_time, end_time, sample_count,
                airborne_minutes, max_gs_kt, max_alt_ft, final, computed_at)
        SELECT a.session_id, a.gap_segment, a.span_part, ps.cid, a.start_time, a.end_time, a.sample_count,
               a.airborne_minutes, a.max_gs_kt, a.max_alt_ft, p_final, now()
        FROM agg a
        JOIN pilot_session ps ON ps.id = a.session_id
        ON CONFLICT (session_id, gap_segment, span_part) DO UPDATE
           SET start_time = EXCLUDED.start_time, end_time = EXCLUDED.end_time,
               sample_count = EXCLUDED.sample_count, airborne_minutes = EXCLUDED.airborne_minutes,
               max_gs_kt = EXCLUDED.max_gs_kt, max_alt_ft = EXCLUDED.max_alt_ft,
               final = EXCLUDED.final, computed_at = EXCLUDED.computed_at
         WHERE (fs.start_time, fs.end_time, fs.sample_count, fs.airborne_minutes, fs.max_gs_kt, fs.max_alt_ft, fs.final)
               IS DISTINCT FROM
               (EXCLUDED.start_time, EXCLUDED.end_time, EXCLUDED.sample_count, EXCLUDED.airborne_minutes,
                EXCLUDED.max_gs_kt, EXCLUDED.max_alt_ft, EXCLUDED.final)
        RETURNING 1
    ),
    del AS (
        DELETE FROM flight_summary d
         WHERE d.session_id = ANY (ids)
           AND NOT EXISTS (SELECT 1 FROM agg a
                            WHERE a.session_id = d.session_id AND a.gap_segment = d.gap_segment
                              AND a.span_part = d.span_part)
        RETURNING 1
    )
    SELECT (SELECT count(*) FROM up) + (SELECT count(*) FROM del) INTO n;
    RETURN n;
END
$fn$;
REVOKE ALL ON FUNCTION refresh_flight_summary_sessions(bigint[], boolean) FROM PUBLIC;

-- Recompute the summary for every non-final session with last_seen within p_since of now (default 12 h).
-- p_since NULL = every session whose last_seen is on or after the oldest existing partition (used by the
-- backfill below and by tests). Cheap enough to run every 5 minutes; idempotent.
CREATE OR REPLACE FUNCTION refresh_flight_summary(p_since interval DEFAULT '12 hours')
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    cutoff timestamptz;
BEGIN
    IF p_since IS NULL THEN
        SELECT (to_date(min(right(c.relname, 8)), 'YYYYMMDD')::text || ' 00:00:00+00')::timestamptz
          INTO cutoff
          FROM pg_inherits i
          JOIN pg_class c ON c.oid = i.inhrelid
         WHERE i.inhparent = 'public.pilot_position'::regclass
           AND c.relname ~ '^pilot_position_[0-9]{8}$';
        IF cutoff IS NULL THEN
            RETURN 0;
        END IF;
    ELSE
        cutoff := now() - p_since;
    END IF;
    RETURN refresh_flight_summary_sessions(
        ARRAY(SELECT ps.id FROM pilot_session ps WHERE ps.last_seen >= cutoff), false);
END
$fn$;
REVOKE ALL ON FUNCTION refresh_flight_summary(interval) FROM PUBLIC;

-- Backfill from the raw positions that exist now.
SELECT refresh_flight_summary(NULL);

-- Sessions whose raw positions were already dropped before this migration (last_seen before the oldest
-- partition) keep one placeholder row, exactly as the old view returned them: start = first_seen,
-- end = last_seen, airborne_minutes NULL, sample_count 0.
INSERT INTO flight_summary (session_id, gap_segment, span_part, cid, start_time, end_time, sample_count,
                            airborne_minutes, max_gs_kt, max_alt_ft, final)
SELECT ps.id, 0, 0, ps.cid, ps.first_seen, ps.last_seen, 0, NULL, ps.max_gs_kt, ps.max_alt_ft, true
FROM pilot_session ps
WHERE ps.last_seen < (SELECT (to_date(min(right(c.relname, 8)), 'YYYYMMDD')::text || ' 00:00:00+00')::timestamptz
                        FROM pg_inherits i
                        JOIN pg_class c ON c.oid = i.inhrelid
                       WHERE i.inhparent = 'public.pilot_position'::regclass
                         AND c.relname ~ '^pilot_position_[0-9]{8}$')
  AND NOT EXISTS (SELECT 1 FROM flight_summary f WHERE f.session_id = ps.id)
ON CONFLICT DO NOTHING;

-- `flight`: thin view, same columns and types as before (CREATE OR REPLACE keeps flight_ext, movements,
-- cohorts and every dependant working). Plan fields come live from pilot_session, so a late flight plan
-- or a corrected airport code applies immediately; only the numbers lag by the refresh interval.
CREATE OR REPLACE VIEW flight AS
WITH cfg AS (
    SELECT COALESCE(
               (SELECT array_agg(upper(trim(p)))
                FROM app_setting a, unnest(string_to_array(a.value, ',')) AS p
                WHERE a.key = 'home_icao_prefixes' AND trim(p) <> ''),
               ARRAY['Y']) AS home_prefixes
),
norm AS (
    -- btrim with an explicit set: plain trim() strips spaces only, not tabs or line breaks.
    SELECT ps.*, NULLIF(upper(btrim(ps.departure, E' \t\r\n')), '') AS dep_n,
           NULLIF(upper(btrim(ps.arrival, E' \t\r\n')), '') AS arr_n
    FROM pilot_session ps
)
SELECT
    n.id AS session_id, a.gap_segment, a.span_part,
    n.cid, n.callsign, n.logon_time, n.name, n.home_icao, n.server,
    n.dep_n AS departure, n.arr_n AS arrival,
    n.aircraft_icao, n.aircraft_faa, n.flight_rules, n.route, n.remarks, n.planned_alt, n.deptime,
    n.pilot_rating, n.military_rating,
    a.start_time, a.end_time,
    a.end_time - a.start_time AS duration,
    round(extract(epoch FROM a.end_time - a.start_time) / 60.0, 1) AS duration_minutes,
    a.airborne_minutes, a.max_gs_kt, a.max_alt_ft, a.sample_count,
    (n.dep_n = n.arr_n) AS is_circuit,
    ('ZZZZ' IN (n.dep_n, n.arr_n)) AS is_zzzz,
    EXISTS (
        SELECT 1 FROM cfg c, unnest(c.home_prefixes) AS hp
        WHERE left(n.dep_n, length(hp)) = hp OR left(n.arr_n, length(hp)) = hp) AS touches_home_region
FROM flight_summary a
JOIN norm n ON n.id = a.session_id
WHERE n.dep_n IS NOT NULL AND n.arr_n IS NOT NULL;
COMMENT ON VIEW flight IS
    'Flights = flight_summary joined to pilot_session, with the flight gate (departure and arrival non-empty after btrim/upper). Numbers lag live data by the refresh interval (collector: every 5 minutes).';

-- monday_attendance: one join per side instead of a correlated subquery per week.
CREATE OR REPLACE VIEW monday_attendance AS
WITH mondays AS (
  SELECT d::date AS monday_date
  FROM generate_series(
         date_trunc('week', (SELECT go_live FROM data_since) AT TIME ZONE 'UTC'),
         date_trunc('week', now() AT TIME ZONE 'UTC'),
         interval '1 week') AS d
), win AS (
  SELECT monday_date,
         (monday_date + time '07:00') AT TIME ZONE 'UTC' AS win_start,
         (monday_date + time '12:00') AT TIME ZONE 'UTC' AS win_end
  FROM mondays
), pil AS (
  SELECT w.monday_date, count(DISTINCT fs.cid) AS n
  FROM win w
  JOIN flight_summary fs ON fs.start_time < w.win_end AND fs.end_time >= w.win_start
  JOIN pilot_session ps ON ps.id = fs.session_id
  WHERE btrim(ps.departure, E' \t\r\n') <> '' AND btrim(ps.arrival, E' \t\r\n') <> ''
  GROUP BY w.monday_date
), ctl AS (
  SELECT w.monday_date, count(DISTINCT c.cid) AS n
  FROM win w
  JOIN controller_session_merged c ON c.start_time < w.win_end AND c.end_time >= w.win_start
  GROUP BY w.monday_date
)
SELECT w.monday_date, w.win_start, w.win_end,
       COALESCE(p.n, 0) AS pilot_cids,
       COALESCE(c.n, 0) AS controller_cids
FROM win w
LEFT JOIN pil p ON p.monday_date = w.monday_date
LEFT JOIN ctl c ON c.monday_date = w.monday_date;

-- migrate:down
-- Forward-only project: no automatic rollback.
