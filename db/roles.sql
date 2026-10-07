-- One-shot, idempotent role setup. Run by the migrate job AFTER `dbmate up`, as the owner role:
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -v grafana_ro_password="$GRAFANA_RO_PASSWORD" -f db/roles.sql
--
-- Safe to re-run (every deploy): it re-applies the grants so views added by later migrations are
-- picked up. grafana_ro gets SELECT on views, materialized views and lookup tables only; it has no
-- access to base tables, cannot write, and has a statement timeout.

\if :{?grafana_ro_password}
\else
    DO $$ BEGIN RAISE EXCEPTION 'psql variable grafana_ro_password is not set'; END $$;
\endif

SELECT format('CREATE ROLE grafana_ro LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'grafana_ro_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro')
\gexec

SELECT format('ALTER ROLE grafana_ro PASSWORD %L', :'grafana_ro_password')
\gexec

ALTER ROLE grafana_ro SET default_transaction_read_only = on;
ALTER ROLE grafana_ro SET statement_timeout = '30s';

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM grafana_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM grafana_ro;
REVOKE CREATE ON SCHEMA public FROM grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;

-- Lookups.
GRANT SELECT ON controller_rating, facility, pilot_rating, airline, aircraft_type, region_map TO grafana_ro;

-- Every view and materialized view in public is intended for Grafana.
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT schemaname, viewname AS name FROM pg_views WHERE schemaname = 'public'
        UNION ALL
        SELECT schemaname, matviewname FROM pg_matviews WHERE schemaname = 'public'
    LOOP
        EXECUTE format('GRANT SELECT ON %I.%I TO grafana_ro', r.schemaname, r.name);
    END LOOP;
END
$$;
