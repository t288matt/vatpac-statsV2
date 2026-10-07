-- migrate:up
-- Core views. Grafana reads views only (grafana_ro, see db/roles.sql). Views run with the
-- owner's privileges, so grafana_ro needs no access to the base tables.

-- flight: pilot sessions split on a >120 minute sample gap or >8 hour span, with the flight gate
-- (departure and arrival non-empty after UPPER/TRIM) applied.
--  * Gap split is exact. The 8 hour span split uses fixed 8 hour buckets measured from the start
--    of each gap segment (an approximation of "restart after 8 h" that avoids a recursive query).
--  * airborne_minutes = sum of gaps between consecutive samples where gs >= 60 kt at the earlier
--    sample, each gap capped at app_setting.airborne_gap_cap_seconds (default 40); pairs more than
--    120 minutes apart are not consecutive and count as zero.
--  * Sessions whose raw positions have all been dropped by retention (last_seen before the oldest
--    remaining partition) are still returned from pilot_session (start=first_seen, end=last_seen,
--    airborne_minutes NULL, sample_count 0) so long-term flight counts survive.
CREATE OR REPLACE VIEW flight AS
WITH cfg AS (
    SELECT
        COALESCE((SELECT value::numeric FROM app_setting WHERE key = 'airborne_gap_cap_seconds'), 40) AS gap_cap_s,
        COALESCE(
            (SELECT array_agg(upper(trim(p)))
             FROM app_setting a, unnest(string_to_array(a.value, ',')) AS p
             WHERE a.key = 'home_icao_prefixes' AND trim(p) <> ''),
            ARRAY['Y']) AS home_prefixes,
        (SELECT (to_date(min(right(c.relname, 8)), 'YYYYMMDD')::text || ' 00:00:00+00')::timestamptz
         FROM pg_inherits i
         JOIN pg_class c ON c.oid = i.inhrelid
         WHERE i.inhparent = 'public.pilot_position'::regclass
           AND c.relname ~ '^pilot_position_[0-9]{8}$') AS horizon
),
samples AS (
    SELECT p.session_id, p.observed_at, p.gs_kt, p.alt_ft,
           p.observed_at - lag(p.observed_at) OVER w AS gap,
           CASE
               WHEN p.gs_kt >= 60
                AND lead(p.observed_at) OVER w IS NOT NULL
                AND lead(p.observed_at) OVER w - p.observed_at <= interval '120 minutes'
               THEN LEAST(extract(epoch FROM lead(p.observed_at) OVER w - p.observed_at), cfg.gap_cap_s)
               ELSE 0
           END AS air_s
    FROM pilot_position p
    CROSS JOIN cfg
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
parts AS (
    SELECT * FROM agg
    UNION ALL
    SELECT ps.id, 0::bigint, 0, ps.first_seen, ps.last_seen, 0::bigint, NULL::numeric, ps.max_gs_kt, ps.max_alt_ft
    FROM pilot_session ps
    CROSS JOIN cfg
    WHERE ps.last_seen < cfg.horizon
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
FROM parts a
JOIN norm n ON n.id = a.session_id
WHERE n.dep_n IS NOT NULL AND n.arr_n IS NOT NULL;

-- controller_session_merged: reconnects of the same cid+callsign within 5 minutes of the previous
-- last sighting are merged. start_time = earliest logon_time, end_time = last sighting.
-- is_open is derived: last seen within the last 2 snapshots (never written).
CREATE OR REPLACE VIEW controller_session_merged AS
WITH ordered AS (
    SELECT cs.*,
           max(cs.last_seen) OVER (PARTITION BY cs.cid, cs.callsign ORDER BY cs.first_seen, cs.id
                                   ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_last
    FROM controller_session cs
),
grouped AS (
    SELECT o.*,
           sum(CASE WHEN o.prev_last IS NULL OR o.first_seen - o.prev_last > interval '5 minutes' THEN 1 ELSE 0 END)
               OVER (PARTITION BY o.cid, o.callsign ORDER BY o.first_seen, o.id) AS merge_group
    FROM ordered o
),
merged AS (
    SELECT cid, callsign, merge_group,
           min(id) AS first_session_id,
           min(logon_time) AS start_time,
           min(first_seen) AS first_seen,
           max(last_seen) AS end_time,
           count(*) AS session_count,
           (array_agg(name ORDER BY last_seen DESC))[1] AS name,
           (array_agg(rating ORDER BY last_seen DESC))[1] AS rating,
           (array_agg(facility ORDER BY last_seen DESC))[1] AS facility,
           (array_agg(server ORDER BY last_seen DESC))[1] AS server,
           (array_agg(frequency_hz ORDER BY last_seen DESC))[1] AS frequency_hz
    FROM grouped
    GROUP BY cid, callsign, merge_group
)
SELECT m.cid, m.callsign, m.merge_group::bigint AS merge_group, m.first_session_id, m.session_count,
       m.name, m.rating, m.facility, m.server, m.frequency_hz,
       m.start_time, m.end_time,
       m.end_time - m.start_time AS duration,
       round(extract(epoch FROM m.end_time - m.start_time) / 3600.0, 2) AS duration_hours,
       (m.end_time > (SELECT max(observed_at) FROM network_snapshot) - interval '30 seconds') AS is_open
FROM merged m;

-- online_now: pilots and controllers seen within the last 2 snapshots, relative to the newest
-- stored snapshot (as_of). Check data_freshness to know whether as_of itself is stale.
CREATE OR REPLACE VIEW online_now AS
WITH ref AS (SELECT max(observed_at) AS as_of FROM network_snapshot)
SELECT 'pilot'::text AS kind, ps.id AS session_id, ps.cid, ps.callsign, ps.name, ps.logon_time,
       ps.first_seen, ps.last_seen, ref.as_of,
       ps.departure, ps.arrival, ps.aircraft_icao,
       pos.lat, pos.lon, pos.alt_ft, pos.gs_kt,
       NULL::int AS rating, NULL::int AS facility, NULL::bigint AS frequency_hz
FROM ref
JOIN pilot_session ps ON ps.last_seen > ref.as_of - interval '30 seconds'
LEFT JOIN pilot_position pos ON pos.session_id = ps.id AND pos.observed_at = ps.last_seen
UNION ALL
SELECT 'controller'::text, cs.id, cs.cid, cs.callsign, cs.name, cs.logon_time,
       cs.first_seen, cs.last_seen, ref.as_of,
       NULL, NULL, NULL,
       NULL, NULL, NULL, NULL,
       cs.rating, cs.facility, cs.frequency_hz
FROM ref
JOIN controller_session cs ON cs.last_seen > ref.as_of - interval '30 seconds';

-- data_freshness: one row describing how current the data is (and where history starts).
CREATE OR REPLACE VIEW data_freshness AS
SELECT
    now() AS checked_at,
    sn.latest_snapshot_ts,
    round(extract(epoch FROM now() - sn.latest_snapshot_ts)::numeric, 1) AS snapshot_age_seconds,
    sn.data_since,
    lo.last_ok_poll_at,
    round(extract(epoch FROM now() - lo.last_ok_poll_at)::numeric, 1) AS last_ok_age_seconds,
    pl.ok_last_hour, pl.skipped_last_hour, pl.error_last_hour
FROM (SELECT max(observed_at) AS latest_snapshot_ts, min(observed_at) AS data_since FROM network_snapshot) sn
CROSS JOIN (SELECT max(polled_at) AS last_ok_poll_at FROM poll_log WHERE status = 'ok') lo
CROSS JOIN (
    SELECT count(*) FILTER (WHERE status = 'ok') AS ok_last_hour,
           count(*) FILTER (WHERE status = 'skipped_unchanged') AS skipped_last_hour,
           count(*) FILTER (WHERE status = 'error') AS error_last_hour
    FROM poll_log WHERE polled_at > now() - interval '1 hour') pl;

-- migrate:down
-- Forward-only project: no automatic rollback.
