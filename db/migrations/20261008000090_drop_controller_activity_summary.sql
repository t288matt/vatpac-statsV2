-- migrate:up
-- Owner decision 08-10-2026 (tracker T31): remove the unused, unverified view controller_activity_summary.
-- No dashboard reads it; the retention panels use controller_summaries. grafana_ro had SELECT only through the
-- all-views grant in db/roles.sql, so nothing else needs changing. Nothing depends on this view.
DROP VIEW IF EXISTS controller_activity_summary;

-- migrate:down
-- Recreates the view exactly as defined in 20261007000010_analytics_views.sql. After a rollback run db/roles.sql
-- (or the grant it contains) so grafana_ro can read the restored view.

-- Per-controller summary behind the retention panels. UNVERIFIED: column meanings follow the
-- Metabase screenshots; the owner's Metabase export is still needed to confirm the definitions.
--   first_recent_session / last_recent_session / recent_sessions / hours_last_30d /
--   longest_session_minutes describe sessions that started in the last 30 days.
--   gap_days_before_recent = days between the end of the session before the first recent
--   session and the start of that first recent session (NULL if there is no earlier session).
CREATE OR REPLACE VIEW controller_activity_summary AS
WITH s AS (
  SELECT cid, name, rank_group, session_start, session_end, duration_minutes,
         lag(session_end) OVER (PARTITION BY cid ORDER BY session_start) AS prev_end
  FROM controller_session_ranked
), overall AS (
  SELECT cid,
         (array_agg(name ORDER BY session_start DESC))[1] AS name,
         (array_agg(rank_group ORDER BY session_start DESC))[1] AS rank_group,
         max(session_start) AS last_session,
         count(*) AS sessions_total
  FROM s
  GROUP BY cid
), recent AS (
  SELECT cid,
         count(*) AS recent_sessions,
         round((sum(duration_minutes) / 60.0)::numeric, 1) AS hours_last_30d,
         round(max(duration_minutes)::numeric, 0) AS longest_session_minutes,
         min(session_start) AS first_recent_session,
         max(session_start) AS last_recent_session,
         (array_agg(prev_end ORDER BY session_start))[1] AS prev_end_before_recent
  FROM s
  WHERE session_start >= now() - interval '30 days'
  GROUP BY cid
)
SELECT o.cid, o.name, o.rank_group, o.last_session, o.sessions_total,
       COALESCE(r.recent_sessions, 0) AS recent_sessions,
       COALESCE(r.hours_last_30d, 0) AS hours_last_30d,
       r.longest_session_minutes,
       r.first_recent_session, r.last_recent_session,
       CASE WHEN r.prev_end_before_recent IS NOT NULL
            THEN round((extract(epoch FROM (r.first_recent_session - r.prev_end_before_recent)) / 86400.0)::numeric, 1)
       END AS gap_days_before_recent
FROM overall o
LEFT JOIN recent r ON r.cid = o.cid;
