-- migrate:up
SET LOCAL lock_timeout = '5s';   -- fail fast instead of queueing behind a long panel query
-- Issue #207, OWNER DECISION 12-11-2026: every view and function that normalises an ICAO text code now calls norm_code() (migration
-- 20261112000100). The strong rule wins where the old copies disagreed. Every view keeps its columns, their order and their types
-- (CREATE OR REPLACE VIEW, so the grants and comments stay), and every function keeps its signature, owner, rights and SET clauses.
--
-- What can differ in output (the old spaces-only copies against the strong rule): a code with a tab, carriage return or line feed at an
-- end is now trimmed (it was kept), and a code that is empty after trimming is now NULL (it was ''). The collector already strips
-- every end of these fields before it stores them, so on stored data the two rules agree; the comparison on seeded awkward data is in the
-- pull request. Affected: pilot_latest (departure, arrival, aircraft_icao), flight_ext (aircraft_icao), home_prefix, icao_is_home,
-- icao_region (a blank code is NULL, which matches no prefix: still FALSE and 'Unknown') and the home prefixes read by the flight view.
-- No change: the flight writer, the drop gate and ops_pending_flight already used this exact rule; they now call the function.

-- 1. Home prefixes and the two helpers that read them.
CREATE OR REPLACE VIEW home_prefix AS
SELECT norm_code(p) AS prefix
FROM app_setting s
CROSS JOIN LATERAL regexp_split_to_table(s.value, ',') AS p
WHERE s.key = 'home_icao_prefixes'
  AND norm_code(p) IS NOT NULL
UNION ALL
SELECT 'Y'::text
WHERE NOT EXISTS (SELECT 1 FROM app_setting WHERE key = 'home_icao_prefixes');

CREATE OR REPLACE FUNCTION icao_is_home(code text) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (
    SELECT 1 FROM home_prefix h WHERE starts_with(norm_code(code), h.prefix)
  )
$$;

CREATE OR REPLACE FUNCTION icao_region(code text) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(
    (SELECT r.region
       FROM region_map r
      WHERE starts_with(norm_code(code), r.prefix)
      ORDER BY length(r.prefix) DESC
      LIMIT 1),
    'Unknown')
$$;

-- 2. flight: the home prefixes of the touches_home_region test (same setting, same rule as home_prefix). Same columns as before.
CREATE OR REPLACE VIEW flight AS
WITH cfg AS (
    SELECT COALESCE(
               (SELECT array_agg(norm_code(p))
                FROM app_setting a, unnest(string_to_array(a.value, ',')) AS p
                WHERE a.key = 'home_icao_prefixes' AND norm_code(p) IS NOT NULL),
               ARRAY['Y']) AS home_prefixes
)
SELECT
    r.session_id,
    r.cid, r.callsign, r.logon_time, r.name, r.home_icao, r.server,
    r.departure, r.arrival,
    r.aircraft_icao, r.aircraft_faa, r.flight_rules, r.route, r.remarks, r.planned_alt, r.deptime,
    r.pilot_rating, r.military_rating,
    r.start_time, r.end_time,
    r.end_time - r.start_time AS duration,
    round(extract(epoch FROM r.end_time - r.start_time) / 60.0, 1) AS duration_minutes,
    r.airborne_minutes, r.max_gs_kt, r.max_alt_ft, r.sample_count,
    (r.departure = r.arrival) AS is_circuit,
    ('ZZZZ' IN (r.departure, r.arrival)) AS is_zzzz,
    EXISTS (
        SELECT 1 FROM cfg c, unnest(c.home_prefixes) AS hp
        WHERE left(r.departure, length(hp)) = hp OR left(r.arrival, length(hp)) = hp) AS touches_home_region
FROM flight_summary r;

-- 3. flight_ext: the normalised aircraft type, in the select list and in the aircraft_type join.
CREATE OR REPLACE VIEW flight_ext AS
SELECT f.session_id, f.cid, f.callsign, f.name, f.home_icao, f.server,
       f.departure, f.arrival,
       f.departure || '-' || f.arrival AS route_label,
       norm_code(f.aircraft_icao) AS aircraft_icao,
       f.flight_rules, f.remarks,
       f.start_time, f.end_time,
       extract(epoch FROM (f.end_time - f.start_time)) / 60.0 AS duration_minutes,
       f.airborne_minutes, f.max_gs_kt, f.max_alt_ft,
       f.is_circuit, f.is_zzzz,
       icao_is_home(f.departure) AS dep_is_home,
       icao_is_home(f.arrival) AS arr_is_home,
       icao_is_home(f.home_icao) AS pilot_is_local,
       icao_region(f.home_icao) AS pilot_region,
       (f.cid / 100000)::int AS cid_group,
       CASE WHEN f.callsign ~ '^[A-Z]{3}[0-9]' THEN left(f.callsign, 3) END AS airline_prefix,
       a.name AS airline_name,
       t.category AS aircraft_category,
       COALESCE(t.is_military, false) AS is_military,
       (f.remarks ~* 'SEL/') AS has_selcal,
       (f.remarks IS NOT NULL AND btrim(f.remarks) <> '') AS has_remarks
FROM flight f
LEFT JOIN airline a
       ON a.prefix = CASE WHEN f.callsign ~ '^[A-Z]{3}[0-9]' THEN left(f.callsign, 3) END
LEFT JOIN aircraft_type t
       ON t.icao = norm_code(f.aircraft_icao);

-- 4. pilot_latest: departure, arrival and aircraft type, and the one-row-per-flight key. Everything else is as migration 20261107000010.
CREATE OR REPLACE VIEW pilot_latest AS
SELECT DISTINCT ON (ps.cid, ps.callsign, norm_code(ps.departure))
       ps.id AS session_id, ps.cid, ps.callsign, ps.name, ps.home_icao, ps.server,
       norm_code(ps.departure) AS departure, norm_code(ps.arrival) AS arrival,
       norm_code(ps.aircraft_icao) AS aircraft_icao, ps.flight_rules, ps.deptime,
       ps.logon_time, ps.last_seen,
       p.observed_at AS position_time, p.lat, p.lon, p.alt_ft, p.gs_kt,
       (ps.last_seen >= (SELECT min(s.observed_at)
                           FROM (SELECT observed_at FROM network_snapshot
                                  ORDER BY observed_at DESC LIMIT 2) s)
        AND ps.last_seen >= now() - interval '5 minutes') AS is_online
FROM pilot_session ps
CROSS JOIN LATERAL (
  SELECT pp.observed_at, pp.lat, pp.lon, pp.alt_ft, pp.gs_kt
  FROM pilot_position pp
  WHERE pp.session_id = ps.id
    AND pp.observed_at >= now() - interval '3 hours'
  ORDER BY pp.observed_at DESC
  LIMIT 1
) p
WHERE ps.last_seen >= now() - interval '3 hours'
ORDER BY ps.cid, ps.callsign, norm_code(ps.departure), ps.last_seen DESC, ps.id DESC;

-- 5. ops_pending_flight (the pending-flight rule of issues 200 and 238): the same rule, now through the function. watchdog_ro calls
--    norm_code with the rights of the role that queries the view; norm_code is executable by PUBLIC, so no grant is needed.
CREATE OR REPLACE VIEW ops_pending_flight AS
WITH g AS (
    SELECT s.cid, s.id, s.callsign, s.logon_time, s.first_seen, s.last_seen,
           norm_code(s.departure) AS dep, norm_code(s.arrival) AS arr
    FROM pilot_session s WHERE s.flown_upto IS NULL OR s.last_seen > s.flown_upto),
  o AS (
    SELECT g.*, max(g.last_seen) OVER (PARTITION BY g.cid, g.callsign, g.dep, g.arr ORDER BY g.first_seen, g.id
                                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_end
    FROM g WHERE g.dep IS NOT NULL AND g.arr IS NOT NULL),
  c AS (
    SELECT o.*, sum(CASE WHEN o.prev_end IS NULL OR o.logon_time - o.prev_end > interval '120 minutes' THEN 1 ELSE 0 END)
                  OVER (PARTITION BY o.cid, o.callsign, o.dep, o.arr ORDER BY o.first_seen, o.id ROWS UNBOUNDED PRECEDING) AS chain
    FROM o),
  grp AS (
    SELECT c.cid, c.callsign, c.dep AS departure, c.arr AS arrival, max(c.last_seen) AS newest_seen, count(*) AS session_count
      FROM c GROUP BY c.cid, c.callsign, c.dep, c.arr, c.chain
    UNION ALL
    SELECT g.cid, g.callsign, g.dep, g.arr, g.last_seen, 1 FROM g WHERE g.dep IS NULL OR g.arr IS NULL),
  ref AS (SELECT CASE WHEN max(observed_at) IS NOT NULL THEN LEAST(max(observed_at), now() + interval '5 minutes') END AS t FROM network_snapshot),
  cfg AS (SELECT make_interval(secs => (ops_setting_num('flight_quiet_hours') * 3600)::double precision) AS quiet,
                 make_interval(secs => (ops_setting_num('flight_quiet_check_grace_minutes') * 60)::double precision) AS grace)
SELECT grp.cid, grp.callsign, grp.departure, grp.arrival, grp.session_count, grp.newest_seen,
       (grp.newest_seen <= ref.t - cfg.quiet) AS is_quiet,
       (grp.newest_seen <= ref.t - cfg.quiet - cfg.grace) AS is_overdue
  FROM grp CROSS JOIN ref CROSS JOIN cfg;

-- 6. The flight writer and the drop gate. Their live definitions are taken from the catalogue with pg_get_functiondef and only the
--    copies of the rule are replaced, each checked to occur exactly the expected number of times, so every other line stays exactly as
--    deployed (the method of migration 20261112000010). CREATE OR REPLACE with the same signature keeps owner, ACL, SET clauses and comment.
DO $do$
DECLARE
    def   text;
    fn    text;
    i     int;
    -- function, text to replace (dollar-quoted, as written in the source), replacement, expected occurrences
    pairs text[][] := ARRAY[
        ['write_finished_flights(integer)',
         $a$NULLIF(upper(btrim(ps.departure, E' \t\r\n')), '')$a$, 'norm_code(ps.departure)', '1'],
        ['write_finished_flights(integer)',
         $a$NULLIF(upper(btrim(ps.arrival,   E' \t\r\n')), '')$a$, 'norm_code(ps.arrival)', '1'],
        ['unreconciled_partition_sessions(timestamptz,timestamptz)',
         $a$NULLIF(upper(btrim(s.departure, E' \t\r\n')), '')$a$, 'public.norm_code(s.departure)', '2'],
        ['unreconciled_partition_sessions(timestamptz,timestamptz)',
         $a$NULLIF(upper(btrim(s.arrival,   E' \t\r\n')), '')$a$, 'public.norm_code(s.arrival)', '1'],
        ['unreconciled_partition_sessions(timestamptz,timestamptz)',
         $a$NULLIF(upper(btrim(s.arrival, E' \t\r\n')), '')$a$, 'public.norm_code(s.arrival)', '1']
    ];
BEGIN
    FOREACH fn IN ARRAY ARRAY['write_finished_flights(integer)', 'unreconciled_partition_sessions(timestamptz,timestamptz)'] LOOP
        def := pg_get_functiondef(('public.' || fn)::regprocedure);
        FOR i IN 1 .. array_length(pairs, 1) LOOP
            CONTINUE WHEN pairs[i][1] <> fn;
            IF (length(def) - length(replace(def, pairs[i][2], ''))) <> length(pairs[i][2]) * pairs[i][4]::int THEN
                RAISE EXCEPTION '% does not contain % exactly % time(s); nothing changed', fn, pairs[i][2], pairs[i][4];
            END IF;
            def := replace(def, pairs[i][2], pairs[i][3]);
        END LOOP;
        EXECUTE def;
        IF EXISTS (SELECT 1 FROM pg_proc WHERE oid = ('public.' || fn)::regprocedure AND prosrc ~* 'btrim') THEN
            RAISE EXCEPTION '% still holds a copy of the rule', fn;
        END IF;
    END LOOP;
END
$do$;

-- migrate:down
-- Forward-only project: no automatic rollback (the previous definitions are in the migrations named above).
