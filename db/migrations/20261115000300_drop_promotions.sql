-- migrate:up
-- Issue 388: the promotions view and its two rating-history tables are removed; promotions will be worked out again under issue 387.
-- The view goes first because it reads both tables. Nothing else depends on them; grafana_ro had SELECT on the view only
-- (db/roles.sql grants it on every view by query, so no grant change is needed).
DROP VIEW IF EXISTS controller_promotion;
DROP TABLE IF EXISTS controller_rating_first_seen;
DROP TABLE IF EXISTS controller_rating_backfill;

-- migrate:down
-- Forward-only project: no automatic rollback.
