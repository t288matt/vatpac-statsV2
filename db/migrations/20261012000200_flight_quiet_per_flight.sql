-- migrate:up
-- Issue #89, OWNER DECISION 12-10-2026: a flight is written once THE FLIGHT ITSELF has had no position for flight_quiet_hours (8) of
-- stored data, not once the whole pilot has been quiet. Replaces write_finished_flights() from migration 20261012000190 (same signature,
-- same grants); nothing else changes: same table, same v1 join rule (same cid, callsign, departure, arrival; a new connection joins
-- when its logon_time is within 120 minutes of the latest earlier last position; no 8 hour parts; no cut inside a connection),
-- same airborne rule, same locking (the pilot's unprocessed sessions are locked and only those ids are used), same skip-not-wait
-- advisory lock, same conflict handling, same 5 minute cap on a future snapshot.
--
-- Why: on the live system (deploy of phase 1) 141 pilots who were online again within the last 8 hours held back ALL their older
-- finished flights, some two days old, because the quiet test looked at the pilot's newest session. A pilot who flew yesterday and is
-- flying again now has a finished flight from yesterday; it must not wait for today's flight to end.
--
-- Rules of the new test, in plain words:
--   1. Per pilot, take the unprocessed sessions and group them into flights by the v1 join rule (exactly as before).
--   2. A flight (group) is written when ITS newest last_seen is flight_quiet_hours or more behind the newest stored snapshot
--      (capped at now() + 5 minutes). It does not matter that the same pilot is online in a later, different flight.
--   3. A flight that is not quiet yet is left untouched: nothing written, its sessions keep flown_upto unchanged. A later, still
--      active flight never blocks or changes an earlier quiet flight, and an earlier quiet flight never waits for a later one.
--   4. A later connection of the same key whose logon_time is within 120 minutes of the previous last position JOINS the flight (v1),
--      so a flight that is still being continued by a reconnect is not quiet: nothing is written for it until the whole joined flight is.
--   5. A session that forms no flight (no plan, or no positions) is marked when it is itself quiet.
-- Worked example: a pilot flies YSSY-YMML 08:00-10:00, disconnects, and flies YMML-YSSY 17:00-19:00. At 18:30 the first flight has
-- been quiet 8.5 h, so it is written; the second is still in progress and is left alone. At 03:00 the next day the second is written.
-- Under the old per-pilot rule nothing would have been written until 03:00.
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
    idle     bigint[];
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

    -- A pilot who made no progress in a round (a key conflict, logged; or only flights that are not quiet yet) keeps the oldest
    -- last_seen and would be picked first on every call, so the call loops: such pilots are left out of the next pick of this call
    -- (at most 10 rounds), and the rest progress. The cap p_max_pilots counts the pilots examined in a round.
    LOOP
    tries := tries + 1;
    -- Pick the pilots that may have a finished flight: their OLDEST unprocessed session is already quiet. (Whether a flight is quiet
    -- is decided per flight below; this is only the cheap necessary test.) Longest quiet first.
    SELECT array_agg(x.cid) INTO pilots FROM (
        SELECT ps.cid
          FROM pilot_session ps
         WHERE (ps.flown_upto IS NULL OR ps.last_seen > ps.flown_upto) AND ps.cid <> ALL (skip)
         GROUP BY ps.cid
        HAVING ref - min(ps.last_seen) >= quiet
         ORDER BY min(ps.last_seen), ps.cid
         LIMIT p_max_pilots) x;
    EXIT WHEN pilots IS NULL;

    -- Lock ALL their unprocessed sessions (in id order, quiet or not) and remember exactly which ones. The rest of the call uses only
    -- these ids: the groups and the quiet decision per group are computed over this one locked set, and a session inserted after this
    -- point (FOR UPDATE does not block an INSERT) can be neither written nor marked. The values read after the lock are stable because
    -- the collector's update of a locked session waits.
    SELECT array_agg(l.id) INTO locked FROM (
        SELECT ps.id FROM pilot_session ps
         WHERE ps.cid = ANY (pilots) AND (ps.flown_upto IS NULL OR ps.last_seen > ps.flown_upto)
         ORDER BY ps.id FOR UPDATE) l;
    EXIT WHEN locked IS NULL;
    SELECT min(COALESCE(ps.flown_upto, ps.first_seen)) - interval '1 day', max(ps.last_seen) INTO lb, ub
      FROM pilot_session ps
     WHERE ps.id = ANY (locked);

    WITH s AS (                                    -- the locked, unprocessed sessions of the picked pilots
        SELECT ps.id, ps.cid, ps.callsign, ps.logon_time, ps.last_seen, ps.flown_upto,
               ps.name, ps.home_icao, ps.server, ps.aircraft_icao, ps.aircraft_faa, ps.flight_rules, ps.route,
               ps.remarks, ps.planned_alt, ps.deptime, ps.pilot_rating, ps.military_rating,
               NULLIF(upper(btrim(ps.departure, E' \t\r\n')), '') AS dep_n,
               NULLIF(upper(btrim(ps.arrival,   E' \t\r\n')), '') AS arr_n
          FROM pilot_session ps
         WHERE ps.id = ANY (locked)
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
               sum(c.cnt)::bigint AS sample_count,
               max(c.last_seen) AS newest_seen          -- the flight's newest last_seen: what the quiet test looks at
          FROM chained c
         GROUP BY c.cid, c.callsign, c.dep_n, c.arr_n, c.chain
    ),
    quiet_flights AS (                             -- QUIET PER FLIGHT: only a flight with no position for flight_quiet_hours is written
        SELECT f.* FROM flights f WHERE ref - f.newest_seen >= quiet
    ),
    conflict AS (                                  -- a flight key that already exists means positions were written but not marked: a bug
        SELECT DISTINCT f.cid
          FROM quiet_flights f
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
          FROM quiet_flights f
         WHERE f.cid NOT IN (SELECT cid FROM conflict)
        RETURNING cid
    ),
    marks AS (                                     -- sessions to mark: those of the flights written now, and the sessions that form no
        SELECT x.id, x.cid                         -- flight (no plan, or no positions) once they are quiet themselves.
          FROM (SELECT unnest(q.session_ids) AS id, q.cid FROM quiet_flights q
                UNION
                SELECT s.id, s.cid FROM s
                 WHERE s.id NOT IN (SELECT g.id FROM gated g) AND ref - s.last_seen >= quiet) x
         WHERE x.cid NOT IN (SELECT cid FROM conflict)
    ),
    mark AS (                                      -- a flight that is not quiet yet keeps its sessions unprocessed (flown_upto untouched)
        UPDATE pilot_session ps SET flown_upto = ps.last_seen
         WHERE ps.id IN (SELECT m.id FROM marks m)
        RETURNING ps.cid
    )
    SELECT (SELECT count(*) FROM ins), (SELECT array_agg(cid) FROM conflict),
           (SELECT array_agg(p) FROM unnest(pilots) p
             WHERE p NOT IN (SELECT cid FROM ins) AND p NOT IN (SELECT cid FROM mark))
      INTO n, bad, idle;

    total := total + n;
    IF bad IS NOT NULL THEN
        RAISE WARNING 'write_finished_flights: % pilot(s) skipped, a flight with the same key already exists but its sessions were not marked (cids %)',
            cardinality(bad), bad;
    END IF;
    EXIT WHEN idle IS NULL OR tries >= 10;
    skip := skip || idle;
    bad := NULL;
    idle := NULL;
    END LOOP;
    RETURN total;
END
$fn$;
REVOKE ALL ON FUNCTION write_finished_flights(int) FROM PUBLIC;
COMMENT ON FUNCTION write_finished_flights(int) IS
    'Writes finished flights into flight_record (issue 89, phase 1). A FLIGHT (sessions joined by the v1 rule) is written once its own newest last_seen is flight_quiet_hours (default 8) or more behind the newest stored snapshot; other flights of the same pilot do not matter. Idempotent; examines at most p_max_pilots pilots per round. Returns the number of flights written.';

-- migrate:down
-- Forward-only project: no automatic rollback.
