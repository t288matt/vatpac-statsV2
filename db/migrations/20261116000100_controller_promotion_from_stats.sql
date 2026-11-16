-- migrate:up
-- Issue 379: the view behind the ATC promotions dashboard. One row per promotion, defined once here.
-- Reads atc_stats_session (every ATC session on the network, loaded from the VATSIM API by the stats loader), which holds each
-- controller's full history. Window functions only: no DISTINCT ON, no date literals.
--
-- The rules (owner decisions in issue 379):
--   1. Ladder: ratings 2 (S1), 3 (S2), 4 (S3), 5 (C1), 7 (C3). Every other rating is ignored, so a C3/I3 flip is not a promotion.
--   2. Only sessions longer than 20 minutes (duration_seconds > 1200) count, everywhere in the rule. An open session has no
--      duration and does not count.
--   3. A promotion is the first qualifying session at a rating above every earlier qualifying session of the same CID. A drop
--      and return is never a promotion, and each rank counts once per controller. A skipped rank is one promotion at the new rank.
--   4. A first ever qualifying session counts only if it is at S1 (is_new, from_rating NULL). A first ever session above S1 is a
--      visitor or transfer and is not counted.
--   5. promoted_at is the start of that first qualifying session; previous_seen_at is the end of the latest earlier qualifying
--      session (NULL for a new S1).
-- name is the latest non-blank name in controller_summaries for the CID, else the CID as text (atc_stats_session holds no names).
-- The view runs with its owner's rights, so grafana_ro reads the view and still has no right on atc_stats_session.
CREATE OR REPLACE VIEW controller_promotion AS
WITH q AS (
    SELECT cid, rating, session_start, session_end,
           max(rating) OVER w AS prev_max,
           max(session_end) OVER w AS prev_end
    FROM atc_stats_session
    WHERE rating IN (2, 3, 4, 5, 7) AND duration_seconds > 1200
    WINDOW w AS (PARTITION BY cid ORDER BY session_start, rating, connection_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
),
names AS (
    SELECT cid, (array_agg(btrim(name) ORDER BY session_start_time DESC) FILTER (WHERE btrim(name) <> ''))[1] AS name
    FROM controller_summaries
    GROUP BY cid
)
SELECT q.cid,
       coalesce(n.name, q.cid::text)                               AS name,
       CASE WHEN q.prev_max IS NULL THEN NULL ELSE rank_group_of(q.prev_max) END AS from_rating,
       rank_group_of(q.rating)                                     AS to_rating,
       q.rating::int                                               AS to_rating_id,
       q.session_start                                             AS promoted_at,
       q.prev_end                                                  AS previous_seen_at,
       q.prev_max IS NULL                                          AS is_new
FROM q
LEFT JOIN names n ON n.cid = q.cid
WHERE (q.prev_max IS NULL AND q.rating = 2)
   OR (q.prev_max IS NOT NULL AND q.rating > q.prev_max);

COMMENT ON VIEW controller_promotion IS
    'Issue 379: one row per controller promotion (S1, S2, S3, C1, C3 ladder), from atc_stats_session (full network history). Sessions over 20 minutes only; a promotion is the first qualifying session at a rating above every earlier qualifying one; a first ever qualifying session counts only at S1 (is_new, from_rating NULL); a skipped rank is one promotion at the new rank. promoted_at is that session''s start. name is the latest known name, else the CID. Read by atc/atc-promotions.json.';

-- migrate:down
-- Forward-only project: no automatic rollback.
