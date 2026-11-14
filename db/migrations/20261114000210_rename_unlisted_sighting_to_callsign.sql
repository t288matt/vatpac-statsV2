-- migrate:up
-- Issue #209: the table controller_unlisted_sighting becomes controller_unlisted_callsign (owner decision 12-11-2026).
-- A rename keeps every row, the privileges (they belong to the table, not its name) and the primary key; the key index
-- carries the old name and is renamed with it. The table has no sequence.
SET LOCAL lock_timeout = '5s';   -- fail fast instead of queueing behind a long query
ALTER TABLE controller_unlisted_sighting RENAME TO controller_unlisted_callsign;
ALTER INDEX controller_unlisted_sighting_pkey RENAME TO controller_unlisted_callsign_pkey;

-- TEMPORARY, removed by a later migration once the new collector and watchdog are live. The migrate job runs before the
-- new images start, so for a short time the old collector (INSERT ... ON CONFLICT ... RETURNING xmax = 0, in the same
-- transaction as the snapshot row) and the old watchdog (SELECT) still look for the old name. Without this view the old
-- collector would fail every poll that carries an unlisted callsign. The view has the old name and one extra column,
-- xmax, because a plain view has no system columns and the old statement reads xmax to tell an insert from an update.
-- The view is auto-updatable, so the old statements work unchanged; nothing new reads it.
CREATE VIEW controller_unlisted_sighting AS SELECT t.*, t.xmax AS xmax FROM controller_unlisted_callsign t;
COMMENT ON VIEW controller_unlisted_sighting IS
    'Temporary compatibility name for controller_unlisted_callsign, for the previous collector and watchdog during a deploy (issue 209). Dropped by a later migration.';
DO $do$
BEGIN
    -- the roles are created by db/roles.sql after the migrations, so on a first install they do not exist yet; roles.sql then grants
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'collector') THEN
        GRANT SELECT, INSERT, UPDATE ON controller_unlisted_sighting TO collector;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'watchdog_ro') THEN
        GRANT SELECT ON controller_unlisted_sighting TO watchdog_ro;
    END IF;
END
$do$;

-- migrate:down
-- Forward-only project: no automatic rollback.
