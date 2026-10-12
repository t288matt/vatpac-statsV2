-- Issue #89 phase 1 exit gate (#138), comparison (A): the new table flight_record against today's `flight` view.
-- READ ONLY, ONE statement. The owner runs it with psql on the live database or, better, on a restore of a live dump:
--   ( echo "SET default_transaction_read_only = on;"; cat ops/compare_flights_89.sql ) | psql -X -v ON_ERROR_STOP=1 -d <database>
-- Repeat it just before phase 5 with `flight_legacy` in place of `flight` (the one word to change is marked below).
--
-- This is the author's code checking the author's code (the writer was built from the same SQL as the view): it proves the MOVE,
-- not the rule. The rule is checked by comparison (B) against v1, ops/compare_flights_v1_89.sql.
--
-- Scope: pilots fully processed (no session with flown_upto NULL or last_seen > flown_upto); old side NOT is_in_progress.
-- Output: rows of (metric, value). Read them top to bottom:
--   * "rows only in old" / "rows only in new" are the EXCEPT ALL differences on every shared column. Anything not explained by
--     the "explained" lines below is a bug: stop and report it on issue #138. The first 50 differing rows follow as JSON.
--   * Expected differences (docs/DESIGN_89_FINAL.md 5.2): (1) a session over 8 hours is two or more old rows and one new row;
--     (2) a session cut by a 120 minute silence (none live so far); (3) a join decided by logon_time instead of the first
--     position; (4) plan fields: the old view reads them live, the new table froze them at write time; (5) airborne rounding,
--     at most 0.01 per extra old piece; (6) a session that came back after being processed; (7) stale old pieces.
--   * The independent line: the sum of sample_count in the new table must equal the number of stored positions at or before
--     flown_upto of sessions that pass the flight gate (positions already dropped make it smaller; there are none yet).
WITH done AS (
    SELECT cid FROM pilot_session GROUP BY cid
    HAVING bool_and(flown_upto IS NOT NULL AND last_seen <= flown_upto)
),
old AS (
    SELECT session_id, cid, callsign, departure, arrival, logon_time, start_time, end_time, sample_count, airborne_minutes,
           max_gs_kt, max_alt_ft, name, home_icao, server, aircraft_icao, aircraft_faa, flight_rules, route, remarks,
           planned_alt, deptime, pilot_rating, military_rating
      FROM flight                                 -- <- flight_legacy for the run before phase 5
     WHERE NOT is_in_progress AND cid IN (SELECT cid FROM done)
),
new AS (
    SELECT session_id, cid, callsign, departure, arrival, logon_time, start_time, end_time, sample_count, airborne_minutes,
           max_gs_kt, max_alt_ft, name, home_icao, server, aircraft_icao, aircraft_faa, flight_rules, route, remarks,
           planned_alt, deptime, pilot_rating, military_rating
      FROM flight_record WHERE cid IN (SELECT cid FROM done)
),
only_old AS (SELECT * FROM old EXCEPT ALL SELECT * FROM new),
only_new AS (SELECT * FROM new EXCEPT ALL SELECT * FROM old),
long_sessions AS (                                -- explained (1) and (2): new rows built from a session the old table split
    SELECT n.session_id, n.start_time
      FROM only_new n
     WHERE EXISTS (SELECT 1 FROM flight_summary s WHERE s.session_id = n.session_id AND s.span_part > 0)
),
silence_sessions AS (
    SELECT n.session_id, n.start_time
      FROM only_new n
     WHERE EXISTS (SELECT 1 FROM flight_summary s WHERE s.session_id = n.session_id AND s.gap_segment > 1)
),
positions AS (
    SELECT count(*) AS n
      FROM pilot_position p
      JOIN pilot_session s ON s.id = p.session_id
     WHERE p.observed_at <= s.flown_upto
       AND NULLIF(btrim(s.departure, E' \t\r\n'), '') IS NOT NULL AND NULLIF(btrim(s.arrival, E' \t\r\n'), '') IS NOT NULL
       AND s.cid IN (SELECT cid FROM done)
)
SELECT ord, metric, value FROM (
    SELECT 1 AS ord, 'pilots fully processed (scope)'::text AS metric, (SELECT count(*) FROM done)::numeric AS value
    UNION ALL SELECT 2, 'pilots NOT fully processed (outside scope, still waiting for the writer)',
                        (SELECT count(DISTINCT cid) FROM pilot_session) - (SELECT count(*) FROM done)
    UNION ALL SELECT 3, 'old rows (flight view, finished)', (SELECT count(*) FROM old)
    UNION ALL SELECT 4, 'new rows (flight_record)', (SELECT count(*) FROM new)
    UNION ALL SELECT 5, 'old sum(sample_count)', (SELECT COALESCE(sum(sample_count), 0) FROM old)
    UNION ALL SELECT 6, 'new sum(sample_count)', (SELECT COALESCE(sum(sample_count), 0) FROM new)
    UNION ALL SELECT 7, 'old sum(airborne_minutes)', (SELECT COALESCE(sum(airborne_minutes), 0) FROM old)
    UNION ALL SELECT 8, 'new sum(airborne_minutes)', (SELECT COALESCE(sum(airborne_minutes), 0) FROM new)
    UNION ALL SELECT 9, 'rows only in old (EXCEPT ALL, all shared columns)', (SELECT count(*) FROM only_old)
    UNION ALL SELECT 10, 'rows only in new (EXCEPT ALL, all shared columns)', (SELECT count(*) FROM only_new)
    UNION ALL SELECT 11, 'explained: new rows from a session the old table cut at 8 hours (span_part > 0)', (SELECT count(*) FROM long_sessions)
    UNION ALL SELECT 12, 'explained: new rows from a session the old table cut at a 120 minute silence (gap_segment > 1)', (SELECT count(*) FROM silence_sessions)
    UNION ALL SELECT 13, 'independent: stored positions at or before flown_upto of gate-passing sessions', (SELECT n FROM positions)
    UNION ALL SELECT 14, 'independent: new sum(sample_count) minus those positions (expect 0, negative if positions were dropped)',
                        (SELECT COALESCE(sum(sample_count), 0) FROM new) - (SELECT n FROM positions)
    UNION ALL SELECT 15, 'carried-over flights without positions (sample_count = 0, expected 0 live)', (SELECT count(*) FROM flight_record WHERE sample_count = 0)
) m
UNION ALL
SELECT 100 + row_number() OVER (), 'only in old: ' || row_to_json(o)::text, 1 FROM (SELECT * FROM only_old ORDER BY cid, start_time LIMIT 50) o
UNION ALL
SELECT 200 + row_number() OVER (), 'only in new: ' || row_to_json(n)::text, 1 FROM (SELECT * FROM only_new ORDER BY cid, start_time LIMIT 50) n
ORDER BY 1;
