-- migrate:up
-- Issue #366: drop the temporary compatibility view that migration 20261114000210 left under the old table name. Only the previous
-- collector and watchdog used it; both are replaced. The table controller_unlisted_callsign, its rows and its grants are not touched.
SET LOCAL lock_timeout = '5s';
DROP VIEW IF EXISTS controller_unlisted_sighting;

-- migrate:down
-- Forward-only project: no automatic rollback.
