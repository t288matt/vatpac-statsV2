-- migrate:up
SET LOCAL lock_timeout = '5s';   -- fail fast instead of queueing behind another statement
-- Issue #226: the schema has no materialised views, so refresh_materialized_views() (migration 20261007000021) refreshed nothing.
-- The collector's daily maintenance no longer calls it, and db/roles.sql no longer grants it. Drop it. IF EXISTS keeps a re-run
-- error-free. No CASCADE: nothing depends on it.
DROP FUNCTION IF EXISTS refresh_materialized_views();

-- migrate:down
-- Forward-only project: no automatic rollback.
