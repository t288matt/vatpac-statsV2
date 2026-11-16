-- migrate:up transaction:false
-- Issue 379: clears any half-built atc_stats_session_promotion_idx before the next migration builds it. A CREATE INDEX
-- CONCURRENTLY that is cancelled or fails leaves an INVALID index, and a re-run of CREATE ... IF NOT EXISTS would then skip it and
-- keep the dead index (the planner ignores it, the loader still maintains it on every write). Its own file because dbmate sends
-- a file as one command, and a CONCURRENTLY statement cannot run inside a multi-statement command.
DROP INDEX CONCURRENTLY IF EXISTS atc_stats_session_promotion_idx;

-- migrate:down
-- Forward-only project: no automatic rollback.
