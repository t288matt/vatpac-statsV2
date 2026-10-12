-- migrate:up
SET LOCAL lock_timeout = '5s';   -- fail fast instead of queueing behind a long query with ACCESS EXCLUSIVE wanted on pilot_session
-- Apply at a quiet moment (pilot_session is small, a few thousand rows, so the plain CREATE INDEX below takes milliseconds; the repo's
-- CONCURRENTLY pattern of migrations 22 and 23 needs its own file with transaction:false and is not worth it for this table): ALTER TABLE and CREATE INDEX on pilot_session take locks and migrate.sh sets no lock_timeout.
-- Issue #89 phase 1 (issue #137, design docs/DESIGN_89_FINAL.md, sections 1.3 to 1.6, 2 and 5): flights are written ONCE, as
-- one row, when the pilot has been quiet for 8 hours. This migration only ADDS: the table flight_record, the column
-- pilot_session.flown_upto, the writer write_finished_flights(), the setting flight_quiet_hours, and one guarded call at the end
-- of refresh_flight_summary. Nothing reads flight_record yet (the `flight` view is unchanged until phase 3); today's pieces
-- table keeps running side by side. The backfill is NOT done here (it must stay quick): the writer fills the table by itself in
-- batches of pilots, or ops/backfill_flight_record.sh does it sooner.

-- 1. "this session's positions up to this time have been processed by the writer" (bookkeeping for the writer and, in phase 4,
--    for the raw-data drop gate).
ALTER TABLE pilot_session ADD COLUMN IF NOT EXISTS flown_upto timestamptz;
COMMENT ON COLUMN pilot_session.flown_upto IS
    'Positions of this session up to this time have been processed by write_finished_flights() (written into flight_record, or the session has no flight). NULL = never processed. A session with last_seen > flown_upto has unprocessed positions.';
-- The writer's work list: only sessions with something left to write.
CREATE INDEX IF NOT EXISTS pilot_session_unwritten_idx ON pilot_session (cid)
    WHERE flown_upto IS NULL OR last_seen > flown_upto;

-- 2. The quiet period, changeable without a migration. The collector's sync_settings does not touch this key.
INSERT INTO app_setting (key, value) VALUES ('flight_quiet_hours', '8') ON CONFLICT (key) DO NOTHING;

-- 3. The table: one row per finished flight, written once, never updated. Renamed flight_summary in phase 5.
CREATE TABLE IF NOT EXISTS flight_record (
    session_id       bigint      NOT NULL,   -- id of the flight's FIRST session (panels use it as the flight id)
    start_time       timestamptz NOT NULL,   -- first position
    end_time         timestamptz NOT NULL CHECK (end_time >= start_time),   -- last position
    cid              bigint      NOT NULL,   -- the pilot
    callsign         text        NOT NULL,
    departure        text        NOT NULL,   -- upper(btrim): the flight gate passed
    arrival          text        NOT NULL,
    session_ids      bigint[]    NOT NULL,   -- every session in the flight
    logon_time       timestamptz NOT NULL,   -- earliest logon of its sessions
    name text, home_icao text, server text, aircraft_icao text, aircraft_faa text, flight_rules text,
    route text, remarks text, planned_alt text, deptime text, pilot_rating int, military_rating int,
    airborne_minutes numeric,                -- NULL only for carried-over flights without raw positions
    max_gs_kt        int,
    max_alt_ft       int,
    sample_count     bigint      NOT NULL,
    written_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, start_time)
);
COMMENT ON TABLE flight_record IS
    'One row per finished flight, written once by write_finished_flights() when the pilot has been quiet for flight_quiet_hours (default 8) of stored data, and never changed. A flight is one session, or several sessions of the same cid, callsign, departure and arrival joined across reconnects (v1 rule). Plan fields are frozen at write time. Nothing reads it before phase 3 of issue 89.';
CREATE INDEX IF NOT EXISTS flight_record_cid_idx        ON flight_record (cid);
CREATE INDEX IF NOT EXISTS flight_record_start_time_idx ON flight_record (start_time);
CREATE INDEX IF NOT EXISTS flight_record_end_time_idx   ON flight_record (end_time);
CREATE INDEX IF NOT EXISTS flight_record_logon_time_idx ON flight_record (logon_time);

-- 4. The writer (design section 2). One transaction, one writer at a time, idempotent.
--    quiet  : a pilot is quiet when (newest stored snapshot) - (newest last_seen of their unprocessed sessions) >= the setting.
--             Measured against network_snapshot, not now(): a stopped collector or a feed outage cannot make a live pilot look quiet.
--    rule   : exactly v1's join. Same cid, callsign, departure, arrival; a unit (one session) joins the flight when its logon_time
--             is no more than 120 minutes after the latest last position of all earlier units of that key; no cut inside a
--             connection; no 8 hour parts. A unit coming back after its session was processed starts its own flight.
--    airborne: v3's measured-gap rule, copied from refresh_flight_summary_sessions (the last position counts zero; a pair more than
--             120 minutes apart counts zero; each gap capped at app_setting.airborne_gap_cap_seconds).
CREATE OR REPLACE FUNCTION write_finished_flights(p_max_pilots int DEFAULT 500)
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
    quiet_h  numeric;
    quiet    interval;
    ref      timestamptz;
    gap_cap  numeric;
    pilots   bigint[];
    lb       timestamptz;
    n        int := 0;
    bad      bigint[];
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
    -- which blocks; the manager's decision of 12-10-2026, see docs/CONFORMANCE_89_PHASE1.md); the running call does the work.
    IF NOT pg_try_advisory_xact_lock(hashtext('vatpac.flight_writer')) THEN
        RETURN 0;
    END IF;

    quiet_h := COALESCE((SELECT value::numeric FROM app_setting WHERE key = 'flight_quiet_hours'), 8);
    IF quiet_h < 2.5 THEN
        RAISE EXCEPTION 'flight_quiet_hours is % but must be at least 2.5 (the 120 minute join rule needs a longer quiet period); nothing written', quiet_h;
    END IF;
    quiet := make_interval(secs => (quiet_h * 3600)::double precision);
    gap_cap := COALESCE((SELECT value::numeric FROM app_setting WHERE key = 'airborne_gap_cap_seconds'), 40);

    SELECT max(observed_at) INTO ref FROM network_snapshot;
    IF ref IS NULL THEN
        RETURN 0;                                  -- cold start: nothing stored, nothing is quiet
    END IF;
    ref := LEAST(ref, now() + interval '5 minutes');   -- a bogus future feed timestamp must not make everyone quiet

    -- A pilot skipped because of a key conflict (a bug case, logged) keeps the oldest last_seen and would be picked first on every
    -- call, so the call loops: pilots skipped in this call are left out of the next pick (at most 10 rounds), and the rest progress.
    LOOP
    tries := tries + 1;
    -- Pick the quiet pilots, longest quiet first.
    SELECT array_agg(x.cid) INTO pilots FROM (
        SELECT ps.cid
          FROM pilot_session ps
         WHERE (ps.flown_upto IS NULL OR ps.last_seen > ps.flown_upto) AND ps.cid <> ALL (skip)
         GROUP BY ps.cid
        HAVING ref - max(ps.last_seen) >= quiet
         ORDER BY max(ps.last_seen), ps.cid
         LIMIT p_max_pilots) x;
    EXIT WHEN pilots IS NULL;

    -- Lock their unprocessed sessions (in id order) and remember exactly which ones; the rest of the call uses only these ids, so a
    -- session inserted after this point (FOR UPDATE does not block an INSERT) can be neither written nor marked. Then re-check
    -- the quiet test over ALL the pilot's unprocessed sessions, new ones included: a pilot who reconnected meanwhile drops out.
    SELECT array_agg(l.id) INTO locked FROM (
        SELECT ps.id FROM pilot_session ps
         WHERE ps.cid = ANY (pilots) AND (ps.flown_upto IS NULL OR ps.last_seen > ps.flown_upto)
         ORDER BY ps.id FOR UPDATE) l;
    SELECT array_agg(x.cid) INTO pilots FROM (
        SELECT ps.cid FROM pilot_session ps
         WHERE ps.cid = ANY (pilots) AND (ps.flown_upto IS NULL OR ps.last_seen > ps.flown_upto)
         GROUP BY ps.cid
        HAVING ref - max(ps.last_seen) >= quiet) x;
    EXIT WHEN pilots IS NULL;
    SELECT min(COALESCE(ps.flown_upto, ps.first_seen)) - interval '1 day', max(ps.last_seen) INTO lb, ub
      FROM pilot_session ps
     WHERE ps.id = ANY (locked) AND ps.cid = ANY (pilots);

    WITH s AS (                                    -- the locked, unprocessed sessions of the quiet pilots
        SELECT ps.id, ps.cid, ps.callsign, ps.logon_time, ps.last_seen, ps.flown_upto,
               ps.name, ps.home_icao, ps.server, ps.aircraft_icao, ps.aircraft_faa, ps.flight_rules, ps.route,
               ps.remarks, ps.planned_alt, ps.deptime, ps.pilot_rating, ps.military_rating,
               NULLIF(upper(btrim(ps.departure, E' \t\r\n')), '') AS dep_n,
               NULLIF(upper(btrim(ps.arrival,   E' \t\r\n')), '') AS arr_n
          FROM pilot_session ps
         WHERE ps.id = ANY (locked) AND ps.cid = ANY (pilots)
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
               sum(c.cnt)::bigint AS sample_count
          FROM chained c
         GROUP BY c.cid, c.callsign, c.dep_n, c.arr_n, c.chain
    ),
    conflict AS (                                  -- a flight key that already exists means positions were written but not marked: a bug
        SELECT DISTINCT f.cid
          FROM flights f
          JOIN flight_record r ON r.session_id = f.session_id AND r.start_time = f.start_time
    ),
    ins AS (
        INSERT INTO flight_record
               (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, logon_time,
                name, home_icao, server, aircraft_icao, aircraft_faa, flight_rules, route, remarks, planned_alt,
                deptime, pilot_rating, military_rating, airborne_minutes, max_gs_kt, max_alt_ft, sample_count)
        SELECT f.session_id, f.start_time, f.end_time, f.cid, f.callsign, f.departure, f.arrival, f.session_ids, f.logon_time,
               f.name, f.home_icao, f.server, f.aircraft_icao, f.aircraft_faa, f.flight_rules, f.route, f.remarks, f.planned_alt,
               f.deptime, f.pilot_rating, f.military_rating, f.airborne_minutes, f.max_gs_kt, f.max_alt_ft, f.sample_count
          FROM flights f
         WHERE f.cid NOT IN (SELECT cid FROM conflict)
        RETURNING 1
    ),
    mark AS (                                      -- every locked session of the written pilots, with or without a plan or positions
        UPDATE pilot_session ps SET flown_upto = ps.last_seen
         WHERE ps.id IN (SELECT s.id FROM s)
           AND ps.cid NOT IN (SELECT cid FROM conflict)
        RETURNING 1
    )
    SELECT (SELECT count(*) FROM ins), (SELECT array_agg(cid) FROM conflict) INTO n, bad;

    total := total + n;
    EXIT WHEN bad IS NULL OR tries >= 10;
    RAISE WARNING 'write_finished_flights: % pilot(s) skipped, a flight with the same key already exists but its sessions were not marked (cids %)',
        cardinality(bad), bad;
    skip := skip || bad;
    bad := NULL;
    END LOOP;
    IF bad IS NOT NULL THEN
        RAISE WARNING 'write_finished_flights: % pilot(s) skipped, a flight with the same key already exists but its sessions were not marked (cids %)',
            cardinality(bad), bad;
    END IF;
    RETURN total;
END
$fn$;
REVOKE ALL ON FUNCTION write_finished_flights(int) FROM PUBLIC;
COMMENT ON FUNCTION write_finished_flights(int) IS
    'Writes finished flights into flight_record (issue 89, phase 1). A pilot is quiet when the newest stored snapshot is flight_quiet_hours (default 8) or more after their newest last_seen. v1 join rule. Idempotent; at most p_max_pilots pilots per call. Returns the number of flights written.';

-- 5. refresh_flight_summary: unchanged except that it now ends with a guarded call of the writer, so a writer fault cannot stop
--    the pieces refresh. (Body of migration 20, plus the block before RETURN.) No Python change and no new grant: the call runs
--    with the definer's rights.
CREATE OR REPLACE FUNCTION refresh_flight_summary(p_since interval DEFAULT '12 hours')
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
DECLARE
    cutoff timestamptz;
    n      int;
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
    n := refresh_flight_summary_sessions(
        ARRAY(SELECT ps.id FROM pilot_session ps WHERE ps.last_seen >= cutoff), false);
    -- The collector runs this with statement_timeout = 20 s. EXCEPTION WHEN OTHERS does not catch query_canceled (57014), so a
    -- slow writer batch would roll back the pieces refresh too; it is named explicitly. The batch is small (50 pilots) so the
    -- 5 minute job stays well inside the timeout; ops/backfill_flight_record.sh runs first and does the bulk with 500.
    BEGIN
        PERFORM write_finished_flights(50);
    EXCEPTION
        WHEN query_canceled THEN
            RAISE WARNING 'write_finished_flights cancelled (statement timeout?): %', SQLERRM;
        WHEN OTHERS THEN
            RAISE WARNING 'write_finished_flights failed: % (%)', SQLERRM, SQLSTATE;
    END;
    RETURN n;
END
$fn$;
REVOKE ALL ON FUNCTION refresh_flight_summary(interval) FROM PUBLIC;

-- 6. Carry-over (design 5.3). Placeholder rows (sample_count = 0) exist only for sessions older than the oldest partition when
--    migration 20 ran; on the live database the expected count is ZERO. If any exist, copy their flights once from the old view
--    (airborne_minutes NULL) and mark their sessions, so the writer never tries to rebuild them from positions that are gone.
-- carry-over:begin
DO $do$
BEGIN
    -- Skipped when nothing needs carrying (the expected live case): the full evaluation of the flight view is not paid for.
    IF EXISTS (SELECT 1 FROM flight_summary WHERE sample_count = 0) THEN
    INSERT INTO flight_record
           (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, logon_time,
            name, home_icao, server, aircraft_icao, aircraft_faa, flight_rules, route, remarks, planned_alt,
            deptime, pilot_rating, military_rating, airborne_minutes, max_gs_kt, max_alt_ft, sample_count)
    SELECT f.session_id, f.start_time, f.end_time, f.cid, f.callsign, f.departure, f.arrival, ARRAY[f.session_id], f.logon_time,
           f.name, f.home_icao, f.server, f.aircraft_icao, f.aircraft_faa, f.flight_rules, f.route, f.remarks, f.planned_alt,
           f.deptime, f.pilot_rating, f.military_rating, NULL, f.max_gs_kt, f.max_alt_ft, 0
      FROM flight f
     WHERE f.sample_count = 0
    ON CONFLICT DO NOTHING;
    UPDATE pilot_session ps SET flown_upto = ps.last_seen
     WHERE ps.id IN (SELECT fs.session_id FROM flight_summary fs WHERE fs.sample_count = 0)
       AND ps.flown_upto IS NULL;
    END IF;
END
$do$;
-- carry-over:end

-- migrate:down
-- Forward-only project: no automatic rollback.
