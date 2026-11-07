-- migrate:up
SET LOCAL lock_timeout = '5s';   -- fail fast instead of queueing behind a long panel query
-- Issue #285: the Departure board showed one flight several times (live on 07-11-2026: DLH1112 x3, ETD455 x3, BAW16 x2; 17 cid and
-- callsign pairs). pilot_latest returned one row per pilot_session, i.e. per logon, so a reconnect or relog gave a second row for the
-- same flight, sometimes ON GROUND and LOGGED OFF at once. Owner decision 06-11-2026 and 07-11-2026: fix it in the view, never by
-- hiding rows in the panel query.
--
-- New rule: of the rows the view already returned, keep one per (cid, callsign): the highest last_seen, ties broken by the highest
-- session id (DISTINCT ON). Only an older session of the same cid and callsign can disappear; every column, its order and its type
-- is unchanged, and so is the 3 hour window, the position lookup and is_online (CREATE OR REPLACE VIEW, so the grants stay).
-- pilot_online_now reads this view and every panel on it counts DISTINCT cid, so an online-now count changes only where one pilot
-- had two online sessions of one callsign. online_now and network_history do not read this view and are not touched.
CREATE OR REPLACE VIEW pilot_latest AS
SELECT DISTINCT ON (ps.cid, ps.callsign)
       ps.id AS session_id, ps.cid, ps.callsign, ps.name, ps.home_icao, ps.server,
       upper(btrim(ps.departure)) AS departure, upper(btrim(ps.arrival)) AS arrival,
       upper(btrim(ps.aircraft_icao)) AS aircraft_icao, ps.flight_rules, ps.deptime,
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
ORDER BY ps.cid, ps.callsign, ps.last_seen DESC, ps.id DESC;

COMMENT ON VIEW pilot_latest IS
  'Latest known position of each flight (cid and callsign) seen in the last 3 hours: the most recent logon only (highest last_seen, then highest session id). Issue 285.';

-- migrate:down
-- Forward-only project: no automatic rollback (the previous definition is in migration 20261007000010).
