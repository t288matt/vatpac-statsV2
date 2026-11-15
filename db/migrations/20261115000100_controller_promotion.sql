-- migrate:up
-- Issue 379: the view behind the ATC promotions dashboard. One row per promotion, defined once here.
-- Reads controller_summaries (merged sessions) only. Window functions only: no DISTINCT ON, no date literals.
--
-- The rules (owner decisions 15-11-2026 in issue 379):
--   1. Ladder: ratings 2 (S1), 3 (S2), 4 (S3), 5 (C1), 7 (C3). Observer, C2, instructor and supervisor ratings are ignored,
--      so a C3/I3 flip is not a promotion.
--   2. Only logons longer than 20 minutes (session_duration_minutes > 20) count, everywhere in the rule.
--   3. A promotion is the first qualifying logon at a rating above every earlier qualifying logon of the same CID. A drop and
--      return is therefore never a promotion, and each rank counts once per controller.
--   4. It needs an earlier qualifying logon at a LOWER rating no more than 60 days before it (start to start). A longer gap
--      is not counted (kept at 60 days by the owner).
--   5. A first ever qualifying logon at S1 is a promotion to S1 (from_rank 'new'); a first ever logon above S1 is not.
--   6. A skipped rank is one promotion, at the new rank (from_rank is the highest rank held before).
--   7. promoted_at is the start of that first qualifying logon at the new rating.
-- name is the latest non-blank name seen for the CID in any session, or NULL (the dashboard then shows the CID).
CREATE OR REPLACE VIEW controller_promotion AS
WITH q AS (
    SELECT cid, rating, session_start_time AS at
    FROM controller_summaries
    WHERE rating IN (2, 3, 4, 5, 7) AND session_duration_minutes > 20
),
w AS (
    SELECT cid, rating, at,
           max(rating) OVER (PARTITION BY cid ORDER BY at, rating ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_max,
           min(rating) OVER (PARTITION BY cid ORDER BY at RANGE BETWEEN interval '60 days' PRECEDING AND CURRENT ROW) AS min_60d
    FROM q
),
names AS (
    SELECT cid, (array_agg(btrim(name) ORDER BY session_start_time DESC) FILTER (WHERE btrim(name) <> ''))[1] AS name
    FROM controller_summaries
    GROUP BY cid
)
SELECT w.cid,
       n.name,
       w.at AS promoted_at,
       CASE WHEN w.prev_max IS NULL THEN 'new' ELSE rank_group_of(w.prev_max) END AS from_rank,
       rank_group_of(w.rating) AS to_rank
FROM w
LEFT JOIN names n ON n.cid = w.cid
WHERE (w.prev_max IS NULL AND w.rating = 2)
   OR (w.prev_max IS NOT NULL AND w.rating > w.prev_max AND w.min_60d < w.rating);

COMMENT ON VIEW controller_promotion IS
    'Issue 379: one row per controller promotion (S1, S2, S3, C1, C3 ladder). Logons over 20 minutes only; a promotion is the first qualifying logon above every earlier one, with a lower-rated qualifying logon in the 60 days before; a first S1 logon is a promotion from new; a skipped rank is one promotion at the new rank. promoted_at is that logon''s start. Read by atc/atc-promotions.json.';

-- migrate:down
-- Forward-only project: no automatic rollback.
