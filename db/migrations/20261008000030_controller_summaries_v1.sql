-- migrate:up
-- Controller retention panels (T13): the v1 Metabase queries read a table called controller_summaries
-- with these five columns. This view exposes the same names over v3's merged controller sessions, so
-- the recovered v1 SQL is ported verbatim (owner's Metabase export, 08-10-2026).
--
-- Known difference from v1: v1 stored one summary row per session as its own collector produced them;
-- v3 merges a reconnect of the same cid+callsign within 5 minutes into one session
-- (controller_session_merged), so a few sessions that v1 counted twice are one here.
CREATE OR REPLACE VIEW controller_summaries AS
SELECT cid,
       name,
       rating,
       session_start    AS session_start_time,
       duration_minutes AS session_duration_minutes
FROM controller_session_ranked;

COMMENT ON VIEW controller_summaries IS
    'v1-compatible controller sessions (cid, name, rating, session_start_time, session_duration_minutes) over controller_session_merged. Used by the atc-retention dashboard so the recovered v1 queries run unchanged.';

-- migrate:down
-- Forward-only project: no automatic rollback.
