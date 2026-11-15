-- migrate:up
-- Issue 379 follow-up (owner decision 15-11-2026): "a controller has to have been seen before as lower rank in the
-- previous two months". A controller's first ever qualifying logon is never a promotion, so the view no longer emits
-- from_rank 'new' rows. Promotions to S1 are not measured (a promotion to S1 is observer to S1 and the database holds no
-- observer logons), so no row has to_rank S1. Everything else is unchanged from 20261115000100: ladder S1, S2, S3, C1, C3; only
-- logons longer than 20 minutes; a promotion is the first qualifying logon above every earlier one with a lower-rated
-- qualifying logon in the 60 days before it; a skipped rank is one promotion at the new rank; promoted_at is the start of
-- that logon. The columns are unchanged, so CREATE OR REPLACE keeps the grafana_ro grant.
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
       rank_group_of(w.prev_max) AS from_rank,
       rank_group_of(w.rating) AS to_rank
FROM w
LEFT JOIN names n ON n.cid = w.cid
WHERE w.prev_max IS NOT NULL AND w.rating > w.prev_max AND w.min_60d < w.rating;

COMMENT ON VIEW controller_promotion IS
    'Issue 379: one row per controller promotion (S1, S2, S3, C1, C3 ladder). Logons over 20 minutes only; a promotion is the first qualifying logon above every earlier one, with a lower-rated qualifying logon in the 60 days before; a controller''s first ever logon is never a promotion; a skipped rank is one promotion at the new rank. promoted_at is that logon''s start. Read by atc/atc-promotions.json.';

-- migrate:down
-- Forward-only project: no automatic rollback.
