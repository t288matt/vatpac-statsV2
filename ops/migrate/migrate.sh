#!/bin/sh
# Apply db/migrations with dbmate, then roles from env passwords. Idempotent: safe on every
# `docker compose up`.
#   - grafana_ro  : created and granted by /db/roles.sql (password passed as a psql variable)
#   - watchdog_ro : created here (pg_monitor + SELECT on the few tables the watchdog reads)
# Passwords never leave this container's environment and are not echoed.
set -eu

: "${POSTGRES_USER:?}" "${POSTGRES_PASSWORD:?}" "${POSTGRES_DB:?}"
: "${GRAFANA_RO_PASSWORD:?}" "${WATCHDOG_DB_PASSWORD:?}"

export DATABASE_URL="postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}?sslmode=disable"
export PGHOST=db PGUSER="$POSTGRES_USER" PGPASSWORD="$POSTGRES_PASSWORD" PGDATABASE="$POSTGRES_DB"

echo "migrate: applying migrations from /db/migrations"
dbmate --migrations-dir /db/migrations --no-dump-schema --wait --wait-timeout 60s up

echo "migrate: applying /db/roles.sql (grafana_ro)"
psql -X -q -v ON_ERROR_STOP=1 -v grafana_ro_password="$GRAFANA_RO_PASSWORD" -f /db/roles.sql

echo "migrate: creating watchdog_ro"
psql -X -q -v ON_ERROR_STOP=1 -v wd_pw="$WATCHDOG_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE watchdog_ro LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'wd_pw')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'watchdog_ro') \gexec
SELECT format('ALTER ROLE watchdog_ro PASSWORD %L', :'wd_pw') \gexec
ALTER ROLE watchdog_ro SET default_transaction_read_only = on;
ALTER ROLE watchdog_ro SET statement_timeout = '10s';
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM watchdog_ro;
GRANT USAGE ON SCHEMA public TO watchdog_ro;
GRANT pg_monitor TO watchdog_ro;
SELECT format('GRANT SELECT ON %I TO watchdog_ro', t)
  FROM unnest(ARRAY['poll_log', 'network_snapshot', 'controller_unlisted_sighting']) AS t
 WHERE to_regclass('public.' || t) IS NOT NULL \gexec
SQL

echo "migrate: done"
