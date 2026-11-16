-- migrate:up transaction:false
-- Issue 379: partial index that matches the window in the view controller_promotion exactly (PARTITION BY cid ORDER BY
-- session_start, rating, connection_id over the qualifying sessions), so the view reads the rows already in order and does not
-- sort about 5 million rows on every panel query. INCLUDE (session_end) lets the view's previous_seen_at column come from the
-- index too, so there are no random reads of the table. Built CONCURRENTLY, hence outside a transaction, so the stats loader can
-- keep writing to the table while it builds. The WHERE clause is the view's own filter (ladder ratings, longer than 20 minutes).
-- The previous migration drops any invalid leftover first, but only on its own run. If this build is cancelled or fails, it leaves an
-- INVALID index, and dbmate runs only this file again, so the plain CREATE stops with "already exists". Then run
-- DROP INDEX CONCURRENTLY IF EXISTS atc_stats_session_promotion_idx; and deploy again (docs/RUNBOOKS/deploy-to-vm.md, section 4g).
CREATE INDEX CONCURRENTLY atc_stats_session_promotion_idx
    ON atc_stats_session (cid, session_start, rating, connection_id) INCLUDE (session_end)
    WHERE rating IN (2, 3, 4, 5, 7) AND duration_seconds > 1200;

-- migrate:down
-- Forward-only project: no automatic rollback.
