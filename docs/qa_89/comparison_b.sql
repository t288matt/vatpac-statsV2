-- Comparison B for issue #138 (design 5.2 B): flight_record against v1 flight_summaries.
-- READ ONLY, two separate databases, so two queries: b_v3 runs on v3, b_v1 runs on v1. The runner exports
-- both to CSV and matches them in Python (no cross-database join is possible).
-- v1 rules (docs/V1_COMPARISON.md section 1): SELECT only, read-only session, 30 s statement timeout,
-- every query bounded by time, never touch flights_archive or transceivers. This file touches only
-- flight_summaries, bounded by logon_time and completion_time.
--
-- Parameters (design 1.4 "Evidence" window; the owner supplies the exact values):
--   from_ts   '2026-10-08 07:52:00+00'  flights logging on before this are excluded (v1 not polling earlier)
--   to_ts     '2026-10-10 18:00:00+00'  flights ending at or after this are excluded
--   gap_from  '2026-10-09 15:30:00+00'  joint gap (power cut): flights overlapping gap_from..gap_to excluded
--   gap_to    '2026-10-09 15:50:00+00'
--
-- Assumptions about v1 (not documented in the repo, check before relying on them):
--   flight_summaries has cid, callsign, departure, arrival, logon_time, completion_time, id.
--   completion_time is treated as the flight's end for the window cut and the gap exclusion. If v1 sets it
--   later (it summarises after the newest row is 8 h old), widen to_ts by 9 h on the v1 side only.

-- @@QUERY b_v3
SELECT r.cid, r.callsign, r.departure, r.arrival,
       date_trunc('second', r.logon_time) AS logon_time,
       r.start_time, r.end_time, r.session_id,
       cardinality(r.session_ids) AS session_count, r.sample_count
FROM flight_record r
WHERE r.logon_time >= :'from_ts'::timestamptz
  AND r.end_time   <  :'to_ts'::timestamptz
  AND NOT (r.start_time < :'gap_to'::timestamptz AND r.end_time >= :'gap_from'::timestamptz)
ORDER BY r.cid, r.callsign, r.logon_time;

-- @@QUERY b_v1
SELECT s.id AS v1_id, s.cid, s.callsign, s.departure, s.arrival,
       date_trunc('second', s.logon_time) AS logon_time,
       s.completion_time
FROM flight_summaries s
WHERE s.logon_time      >= :'from_ts'::timestamptz
  AND s.logon_time      <  :'to_ts'::timestamptz
  AND s.completion_time >= :'from_ts'::timestamptz
  AND s.completion_time <  :'to_ts'::timestamptz
  AND NOT (s.logon_time < :'gap_to'::timestamptz AND s.completion_time >= :'gap_from'::timestamptz)
ORDER BY s.cid, s.callsign, s.logon_time;
