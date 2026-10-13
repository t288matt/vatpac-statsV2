#!/bin/sh
# Apply db/migrations with dbmate, then roles from env passwords. Idempotent: safe on every
# `docker compose up`.
#   - grafana_ro, collector, backup_ro : created and granted by /db/roles.sql (passwords passed as
#     psql variables grafana_ro_password, collector_password, backup_password)
#   - watchdog_ro : created here (pg_monitor + SELECT on the few tables the watchdog reads; the two session tables are
#     for the identity-flap check, and pilot_position, flight_summary (the finished flights) and reference_data_change are for the scheduled
#     invariant sweep and archive cross-check, ops/audit/invariants.sql, issue 20)
# This job is the only place the owner/superuser password is used for schema work.
# Passwords never leave this container's environment and are not echoed.
set -eu

: "${POSTGRES_USER:?}" "${POSTGRES_PASSWORD:?}" "${POSTGRES_DB:?}"
: "${GRAFANA_RO_PASSWORD:?}" "${WATCHDOG_DB_PASSWORD:?}"
: "${COLLECTOR_DB_PASSWORD:?}" "${BACKUP_DB_PASSWORD:?}"

export DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}?sslmode=disable"
export PGHOST=db PGUSER="$POSTGRES_USER" PGPASSWORD="$POSTGRES_PASSWORD" PGDATABASE="$POSTGRES_DB"

echo "migrate: applying migrations from /db/migrations"
dbmate --migrations-dir /db/migrations --no-dump-schema --wait --wait-timeout 60s up

echo "migrate: applying /db/roles.sql (grafana_ro, collector, backup_ro)"
psql -X -q -v ON_ERROR_STOP=1   -v grafana_ro_password="$GRAFANA_RO_PASSWORD"   -v collector_password="$COLLECTOR_DB_PASSWORD"   -v backup_password="$BACKUP_DB_PASSWORD"   -f /db/roles.sql

echo "migrate: creating watchdog_ro"
psql -X -q -v ON_ERROR_STOP=1 -v wd_pw="$WATCHDOG_DB_PASSWORD" <<'SQL'
BEGIN;
SELECT format('CREATE ROLE watchdog_ro LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'wd_pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'watchdog_ro') \gexec
SELECT format('ALTER ROLE watchdog_ro PASSWORD %L', :'wd_pw') \gexec
ALTER ROLE watchdog_ro SET default_transaction_read_only = on;
ALTER ROLE watchdog_ro SET statement_timeout = '10s';
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM watchdog_ro;
GRANT USAGE ON SCHEMA public TO watchdog_ro;
GRANT pg_monitor TO watchdog_ro;
SELECT format('GRANT SELECT ON %I TO watchdog_ro', t)
  FROM unnest(ARRAY['poll_log', 'network_snapshot', 'controller_unlisted_sighting', 'pilot_session', 'controller_session',
                'pilot_position', 'flight_summary', 'drop_gate_refusal', 'reference_data_change']) AS t
 WHERE to_regclass('public.' || t) IS NOT NULL \gexec
COMMIT;
SQL

echo "migrate: done"
