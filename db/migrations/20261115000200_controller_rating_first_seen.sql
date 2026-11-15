-- migrate:up
-- Issue 379 (next design): promotions from each member's VATSIM rating history.
-- Two small tables are filled by hand with ops/backfill_rating_history.py (from the public VATSIM member history, one member at a
-- time); no session records are kept. The view controller_promotion is replaced: same name, same five columns and types, so the
-- dashboard atc/atc-promotions.json and the read-only role are unchanged. Grafana reads the view only; the tables are not granted.
--
-- Rules (owner decisions 15-11-2026):
--   1. Ladder: ratings 2 (S1), 3 (S2), 4 (S3), 5 (C1), 7 (C3). Observer (1) may appear as an earlier rank ('OBS'). C2, instructor and
--      supervisor ratings are ignored, so a C3/I3 flip is not a promotion.
--   2. Only sessions longer than 20 minutes count (end minus start in the API data; session_duration_minutes in our own data).
--   3. The first session per rating is LEAST(the backfilled value, the earliest long session in controller_summaries at that rating).
--   4. A promotion is the first session at a rating higher than every earlier rating. A first ever session at S1 is a promotion
--      from 'new'; a first ever session above S1 is not (visitor or transfer). A skipped rank is one promotion at the new rank.
--   5. promoted_at is the start of that first session. Only CIDs with a row in controller_rating_backfill are included.
CREATE TABLE controller_rating_first_seen (
    cid                bigint      NOT NULL CHECK (cid > 0),
    rating             int         NOT NULL CHECK (rating > 0),
    first_session_start timestamptz NOT NULL,
    PRIMARY KEY (cid, rating)
);
COMMENT ON TABLE controller_rating_first_seen IS
    'Issue 379: one row per controller and rating: the start of the first session over 20 minutes at that rating in the member''s full VATSIM history. Filled by hand by ops/backfill_rating_history.py. Grafana reads the view controller_promotion, not this table.';

CREATE TABLE controller_rating_backfill (
    cid           bigint      PRIMARY KEY CHECK (cid > 0),
    backfilled_at timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE controller_rating_backfill IS
    'Issue 379: one row per controller whose VATSIM history has been loaded into controller_rating_first_seen. A controller without a row here is not counted in controller_promotion. Written in the same transaction as the rating rows by ops/backfill_rating_history.py.';

CREATE OR REPLACE VIEW controller_promotion AS
WITH own AS (
    SELECT s.cid, s.rating, min(s.session_start_time) AS at
    FROM controller_summaries s
    JOIN controller_rating_backfill b ON b.cid = s.cid
    WHERE s.rating IN (1, 2, 3, 4, 5, 7) AND s.session_duration_minutes > 20
    GROUP BY s.cid, s.rating
),
api AS (
    SELECT f.cid, f.rating, f.first_session_start AS at
    FROM controller_rating_first_seen f
    JOIN controller_rating_backfill b ON b.cid = f.cid
    WHERE f.rating IN (1, 2, 3, 4, 5, 7)
),
first_at AS (
    SELECT cid, rating, min(at) AS at
    FROM (SELECT cid, rating, at FROM own UNION ALL SELECT cid, rating, at FROM api) u
    GROUP BY cid, rating
),
w AS (
    SELECT cid, rating, at,
           max(rating) OVER (PARTITION BY cid ORDER BY at, rating ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_max
    FROM first_at
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
WHERE w.rating IN (2, 3, 4, 5, 7)
  AND ((w.prev_max IS NULL AND w.rating = 2) OR (w.prev_max IS NOT NULL AND w.rating > w.prev_max));

COMMENT ON VIEW controller_promotion IS
    'Issue 379: one row per controller promotion (S1, S2, S3, C1, C3 ladder), from each member''s full VATSIM rating history (controller_rating_first_seen, filled by hand) plus our own later sessions. Sessions over 20 minutes only; a promotion is the first session at a rating above every earlier one; a first ever S1 session is a promotion from new; a first ever session above S1 is not; a skipped rank is one promotion at the new rank. promoted_at is that session''s start. Only controllers with a row in controller_rating_backfill are included. Read by atc/atc-promotions.json.';

-- migrate:down
-- Forward-only project: no automatic rollback.
