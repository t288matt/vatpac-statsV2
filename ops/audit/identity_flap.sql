-- Identity-flap check (T36): the ONE definition of "two sessions of one cid and callsign less than 2 s apart".
-- Read by the watchdog (every 60 s, window 24 h) and by `ops/deploy.sh --verify` (whole history), and runnable by hand:
--   docker compose exec -T db sh -c 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < ops/audit/identity_flap.sql
-- Result: one row per table (check, bad, first_seen); every `bad` should be 0. first_seen is the earlier session of the
-- earliest pair. A session is identified by (cid, callsign, whole-second logon_time). If the feed changes how it writes
-- logon_time again, one connection becomes two sessions that differ by a little. The CHECK constraints make a fractional
-- logon_time impossible, so only a shift of whole seconds can still show up here.
-- Window: the custom setting vatpac.flap_window_hours limits the check to sessions seen in the last N hours (so an alert
-- clears once the pair ages out). Unset, empty or 0 means the whole history. The watchdog sets it before running this file.
-- Cheap every cycle: the window is ONE lower bound (`since`, computed once) applied as a plain range on last_seen, so with
-- a window the planner reads only recent rows through pilot_session_last_seen_idx and controller_session_last_seen_idx
-- instead of walking all history. (A `hours = 0 OR ...` filter would hide the range from the planner.)
-- The two rows of the same rule in ops/audit/invariants.sql cannot include this file (that pack is one UNION query), so
-- tests/test_flap_check_copies.py fails if their text drifts from this one.
WITH w AS (
  SELECT CASE WHEN h.hours = 0 THEN '-infinity'::timestamptz ELSE now() - make_interval(hours => h.hours) END AS since
  FROM (SELECT coalesce(nullif(current_setting('vatpac.flap_window_hours', true), ''), '0')::int AS hours) h)
SELECT 'pilot session pairs, same cid+callsign, logon_time within 2 s (identity flap)' AS check, count(*) AS bad,
       min(a.first_seen) AS first_seen
FROM pilot_session a JOIN pilot_session b
  ON a.cid = b.cid AND a.callsign = b.callsign AND a.id < b.id AND abs(extract(epoch FROM a.logon_time - b.logon_time)) < 2
WHERE b.last_seen > (SELECT since FROM w)
UNION ALL
SELECT 'controller session pairs, same cid+callsign, logon_time within 2 s (identity flap)', count(*), min(a.first_seen)
FROM controller_session a JOIN controller_session b
  ON a.cid = b.cid AND a.callsign = b.callsign AND a.id < b.id AND abs(extract(epoch FROM a.logon_time - b.logon_time)) < 2
WHERE b.last_seen > (SELECT since FROM w);
