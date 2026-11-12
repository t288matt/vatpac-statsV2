-- One-shot, idempotent role setup. Run by the migrate job AFTER `dbmate up`, as the owner role:
--
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
--        -v grafana_ro_password="$GRAFANA_RO_PASSWORD" \
--        -v collector_password="$COLLECTOR_DB_PASSWORD" \
--        -v backup_password="$BACKUP_DB_PASSWORD" \
--        -v watchdog_password="$WATCHDOG_DB_PASSWORD" -f db/roles.sql
--
-- Safe to re-run (every deploy): it re-applies the grants so objects added by later migrations are
-- picked up. Roles:
--   grafana_ro  SELECT on views, materialized views and lookup tables only; no base tables, no writes,
--               statement timeout, at most 10 connections.
--   collector   the application role: SELECT, INSERT, UPDATE on the tables the collector writes (DELETE on poll_log only), USAGE on sequences, EXECUTE
--               on the maintenance functions. No DDL (the partition functions are SECURITY DEFINER), no
--               table access to flight_summary (it only EXECUTEs the writer) or views, not a superuser.
--   backup_ro   pg_read_all_data (pg_dump); read-only.
--   watchdog_ro pg_monitor and SELECT on the few tables, views and the one function the watchdog reads; read-only, statement timeout.

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
\if :{?watchdog_password}
\else
    DO $$ BEGIN RAISE EXCEPTION 'psql variable watchdog_password is not set'; END $$;
\endif

-- Everything below runs in ONE transaction (issue 84). The grants are re-applied with REVOKE then GRANT on every deploy; run
-- as separate statements, a connection that checked its privileges between the two saw "permission denied" for a moment on
-- a live system. In one transaction other sessions keep seeing the old privileges until COMMIT, so there is no gap. With
-- ON_ERROR_STOP a failure leaves the transaction open and psql exits, which rolls everything back.
BEGIN;

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
-- JIT compilation adds about 1 s to a query over many partitions and gains nothing on these small result sets (issue #109).
ALTER ROLE grafana_ro SET jit = off;
-- Pin the session time zone so date_trunc / ::date in dashboard SQL (retention panels) never depend on the
-- server default (T27 item 4).
ALTER ROLE grafana_ro SET timezone = 'UTC';

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM grafana_ro;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM grafana_ro;
REVOKE CREATE ON SCHEMA public FROM grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;

-- Lookups.
GRANT SELECT ON controller_rating, facility, pilot_rating, airline, aircraft_type, region_map TO grafana_ro;

-- Every view and materialized view in public is intended for Grafana, except the views named ops_* (issues 200, 238): those are
-- read-only views for the watchdog role (the watchdog_ro section below grants them to watchdog_ro only).
DO $$
DECLARE r record;
BEGIN
    FOR r IN
        SELECT schemaname, viewname AS name FROM pg_views WHERE schemaname = 'public' AND left(viewname, 4) <> 'ops_'
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

-- Data tables the collector writes (and reads back: upserts, RETURNING). Upserts are INSERT ... ON CONFLICT DO UPDATE, so
-- they need INSERT and UPDATE; no table other than poll_log is ever deleted from by the collector (issue 95).
GRANT SELECT, INSERT, UPDATE ON
    pilot_session, pilot_position, controller_session, controller_rating_event,
    controller_frequency_event, network_snapshot, poll_log, controller_unlisted_sighting,
    reference_data_change, app_setting
TO collector;
-- The one retention trim the collector runs itself (maintenance.py trim_poll_log). Position partitions are dropped and the
-- finished flights are written by the SECURITY DEFINER writer below, which needs no DELETE right.
GRANT DELETE ON poll_log TO collector;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO collector;

-- Maintenance functions. The partition functions, the flight writer (write_finished_flights, which fills flight_summary) and the materialized view
-- refresh are SECURITY DEFINER (owner rights), so the collector needs no DDL or table rights for them.
-- Not granted: unreconciled_partition_sessions (internal helper).
GRANT EXECUTE ON FUNCTION
    ensure_pilot_position_partitions(int, date),
    drop_old_pilot_position_partitions(int, date),
    write_finished_flights(int),
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

-- ---------------------------------------------------------------------------------------------
-- watchdog_ro
-- ---------------------------------------------------------------------------------------------
-- pg_monitor + SELECT on the few tables the watchdog reads: the two session tables are for the identity-flap check, and pilot_position,
-- flight_summary (the finished flights) and reference_data_change are for the scheduled invariant sweep and archive cross-check
-- (ops/audit/invariants.sql, issue 20); the two read-only views ops_setting and ops_pending_flight and the function ops_setting_num
-- behind them show the flight settings and the pending-flight rule without a right on app_setting (issues 200 and 238).
SELECT format('CREATE ROLE watchdog_ro LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'watchdog_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'watchdog_ro')
\gexec

SELECT format('ALTER ROLE watchdog_ro PASSWORD %L', :'watchdog_password')
\gexec

ALTER ROLE watchdog_ro SET default_transaction_read_only = on;
ALTER ROLE watchdog_ro SET statement_timeout = '10s';
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM watchdog_ro;
GRANT USAGE ON SCHEMA public TO watchdog_ro;
GRANT pg_monitor TO watchdog_ro;
SELECT format('GRANT SELECT ON %I TO watchdog_ro', t)
  FROM unnest(ARRAY['poll_log', 'network_snapshot', 'controller_unlisted_sighting', 'pilot_session', 'controller_session',
                'pilot_position', 'flight_summary', 'drop_gate_refusal', 'reference_data_change', 'ops_setting', 'ops_pending_flight']) AS t
 WHERE to_regclass('public.' || t) IS NOT NULL
\gexec
-- a view calls its functions with the rights of the role that queries it, so the two ops_* views need this one function (it answers for the
-- two flight settings only and refuses any other key; setting_num itself stays with the owner)
SELECT format('GRANT EXECUTE ON FUNCTION %s TO watchdog_ro', f)
  FROM unnest(ARRAY['ops_setting_num(text)']) AS f
 WHERE to_regprocedure('public.' || f) IS NOT NULL
\gexec

COMMIT;
