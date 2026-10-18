-- migrate:up
-- Removes the near-miss controller whitelist-gap detector: the owner decided it is not needed (18-10-2026).
-- Drops the near_miss_controllers view (created in db/migrations/20261007000010_analytics_views.sql) and the
-- controller_unlisted_sighting table it read (created in db/migrations/20261007000001_core_schema.sql), which
-- the collector wrote to and the watchdog alerted from. db/roles.sql no longer grants the collector role
-- rights on this table.

DROP VIEW IF EXISTS near_miss_controllers;
DROP TABLE IF EXISTS controller_unlisted_sighting;

-- migrate:down
-- Forward-only project: no automatic rollback.
