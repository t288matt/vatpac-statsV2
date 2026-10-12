-- Issue #89 phase 1 exit gate (#138), comparison (B), step 1 of 2: take v1's flight summaries for the window. READ ONLY, run on the v1 box.
-- Rules for touching v1 (docs/V1_COMPARISON.md section 1): SELECT only, read-only session, 30 s timeout, bounded by time, never
-- flights_archive or transceivers, no restarts. Run it over ssh from this file with the container's own environment so no
-- credential is printed. Adapt the host and the container to the v1 box:
--   ( echo "SET default_transaction_read_only = on; SET statement_timeout = '30s';"; cat ops/compare_flights_v1_export.sql ) |
--     ssh V1HOST 'docker exec -i V1DBCONTAINER sh -c "psql -X -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" --csv"' > v1_flight_summaries.csv
-- The SET line prints "SET" lines into the CSV when --csv is used without -q: add -q to psql, or delete those lines from the file.
-- Step 2 loads that file into the v3 database (or a restore of a live dump): see ops/compare_flights_v1_89.sql.
-- Column names VERIFIED 12-10-2026 against the live v1 schema (information_schema.columns of flight_summaries, read-only):
-- cid, callsign, departure, arrival, logon_time, completion_time all exist. Not taken from Metabase.
SELECT cid, callsign, departure, arrival, logon_time, completion_time
FROM flight_summaries
WHERE logon_time >= '2026-10-08 07:52:00+00' AND logon_time < '2026-10-10 18:00:00+00'
ORDER BY logon_time;
