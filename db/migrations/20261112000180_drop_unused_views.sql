-- migrate:up
-- Issue #227: drop two views that no dashboard, script or collector code reads. near_miss_controllers was a plain
-- SELECT of controller_unlisted_sighting (the watchdog reads the table directly); controller_activity_daily was only
-- named in existence lists. Nothing depends on either view. grafana_ro had SELECT only through the all-views grant
-- in db/roles.sql, so nothing else needs changing.
DROP VIEW IF EXISTS near_miss_controllers;
DROP VIEW IF EXISTS controller_activity_daily;

-- migrate:down
-- Forward-only project: no automatic rollback.
