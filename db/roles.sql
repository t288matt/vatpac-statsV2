-- One-shot, idempotent role setup. Run by the migrate job AFTER `dbmate up`, as the owner role:
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
--        -v grafana_ro_password="$GRAFANA_RO_PASSWORD" \
--        -v collector_password="$COLLECTOR_DB_PASSWORD" \
--        -v backup_password="$BACKUP_DB_PASSWORD" -f db/roles.sql
--
-- Safe to re-run (every deploy): it re-applies the grants so objects added by later migrations are
-- picked up. Roles:
--   grafana_ro  SELECT on views, materialized views and lookup tables only; no base tables, no writes,
--               statement timeout, at most 10 connections.
--   collector   the application role: DML on the tables the collector writes, USAGE on sequences, EXECUTE
--               on the maintenance functions. No DDL (the partition functions are SECURITY DEFINER), no
--               access to flight_summary or views, not a superuser.
--   backup_ro   pg_read_all_data (pg_dump); read-only.

\if :{?grafana_ro_password}
\else
    DO $$ BEGIN RAISE EXCEPTION 'psql variable grafana_ro_password is not set'; END $$;
\endif
\if :{?collector_password}
\else
    DO $$ BEGIN RAISE EXCEPTION 'psql variable collector_password is not set'; END $$;
\endif
\if :{?backup_password}
\else
    DO $$ BEGIN RAISE EXCEPTION 'psql variable backup_password is not set'; END $$;
\endif

-- ---------------------------------------------------------------------------------------------
-- grafana_ro
-- ---------------------------------------------------------------------------------------------
SELECT format('CREATE ROLE grafana_ro LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'grafana_ro_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro')
\gexec

SELECT format('ALTER ROLE grafana_ro PASSWORD %L', :'grafana_ro_password')
\gexec

ALTER ROLE grafana_ro CONNECTION LIMIT 10;
ALTER ROLE grafana_ro SET default_transaction_read_only = on;
ALTER ROLE grafana_ro SET statement_timeout = '30s';
ALTER ROLE grafana_ro SET work_mem = '16MB';

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

-- ---------------------------------------------------------------------------------------------
-- collector
-- ---------------------------------------------------------------------------------------------
SELECT format('CREATE ROLE collector LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'collector_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'collector')
\gexec

SELECT format('ALTER ROLE collector PASSWORD %L', :'collector_password')
\gexec

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM collector;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM collector;
REVOKE CREATE ON SCHEMA public FROM collector;
GRANT USAGE ON SCHEMA public TO collector;

-- Data tables the collector writes (and reads back: upserts, RETURNING, retention trim).
GRANT SELECT, INSERT, UPDATE, DELETE ON
    pilot_session, pilot_position, controller_session, controller_rating_event,
    controller_frequency_event, network_snapshot, poll_log, controller_unlisted_sighting,
    reference_data_change, app_setting
TO collector;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO collector;

-- Maintenance functions. The partition functions, the flight_summary refresh and the materialized view
-- refresh are SECURITY DEFINER (owner rights), so the collector needs no DDL or table rights for them.
-- Not granted: refresh_flight_summary_sessions (internal helper).
GRANT EXECUTE ON FUNCTION
    ensure_pilot_position_partitions(int, date),
    drop_old_pilot_position_partitions(int, date),
    refresh_flight_summary(interval),
    refresh_materialized_views()
TO collector;

-- ---------------------------------------------------------------------------------------------
-- backup_ro
-- ---------------------------------------------------------------------------------------------
SELECT format('CREATE ROLE backup_ro LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'backup_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'backup_ro')
\gexec

SELECT format('ALTER ROLE backup_ro PASSWORD %L', :'backup_password')
\gexec

GRANT pg_read_all_data TO backup_ro;
ALTER ROLE backup_ro SET default_transaction_read_only = on;
