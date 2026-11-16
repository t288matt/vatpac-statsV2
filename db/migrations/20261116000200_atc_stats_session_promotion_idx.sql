-- migrate:up transaction:false
-- Issue 379: partial index that matches the window in the view controller_promotion exactly (PARTITION BY cid ORDER BY
-- session_start, rating, connection_id over the qualifying sessions), so the view reads the rows already in order and does not
-- sort about 5 million rows on every panel query. Built CONCURRENTLY, hence outside a transaction, so the stats loader can keep
-- writing to the table while it builds. The WHERE clause is the view's own filter (ladder ratings, longer than 20 minutes).
CREATE INDEX CONCURRENTLY IF NOT EXISTS atc_stats_session_promotion_idx
    ON atc_stats_session (cid, session_start, rating, connection_id)
    WHERE rating IN (2, 3, 4, 5, 7) AND duration_seconds > 1200;

-- migrate:down
-- Forward-only project: no automatic rollback.
