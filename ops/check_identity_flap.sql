-- Read-only identity-flap check (T36, feeds T16). Run: psql -X -v ON_ERROR_STOP=1 -f ops/check_identity_flap.sql
-- Expect every count to be 0. A session is identified by (cid, callsign, whole-second logon_time). If the feed
-- changes how it writes logon_time again, one connection becomes two sessions that differ by a little. The CHECK
-- constraints make a fractional logon_time impossible; this catches a shift of whole seconds as well.
SELECT 'pilot session pairs, same cid+callsign, logon_time within 2 s' AS check, count(*) AS bad,
       min(a.first_seen)::text AS first_seen
  FROM pilot_session a JOIN pilot_session b
    ON a.cid = b.cid AND a.callsign = b.callsign AND a.id < b.id
   AND abs(extract(epoch FROM a.logon_time - b.logon_time)) < 2
UNION ALL
SELECT 'controller session pairs, same cid+callsign, logon_time within 2 s', count(*), min(a.first_seen)::text
  FROM controller_session a JOIN controller_session b
    ON a.cid = b.cid AND a.callsign = b.callsign AND a.id < b.id
   AND abs(extract(epoch FROM a.logon_time - b.logon_time)) < 2
UNION ALL
SELECT 'sessions with a fractional logon_time', count(*), NULL
  FROM (SELECT logon_time FROM pilot_session UNION ALL SELECT logon_time FROM controller_session) s
 WHERE logon_time <> date_trunc('second', logon_time);
