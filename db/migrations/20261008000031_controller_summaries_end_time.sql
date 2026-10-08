-- migrate:up
-- Cards 94 and 95 (when people are online) read callsign and session_end_time from v1's
-- controller_summaries table. Append both columns to the compatibility view; CREATE OR REPLACE
-- VIEW only allows new columns at the end, so the first five stay as they were.
CREATE OR REPLACE VIEW controller_summaries AS
SELECT cid,
       name,
       rating,
       session_start    AS session_start_time,
       duration_minutes AS session_duration_minutes,
       callsign,
       session_end      AS session_end_time
FROM controller_session_ranked;

-- migrate:down
-- Forward-only project: no automatic rollback.
