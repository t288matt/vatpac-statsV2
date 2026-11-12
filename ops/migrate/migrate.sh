#!/bin/sh
# Apply db/migrations with dbmate, then roles from env passwords. Idempotent: safe on every
# `docker compose up`.
#   - grafana_ro, collector, backup_ro, watchdog_ro : created and granted by /db/roles.sql (passwords passed as
#     psql variables grafana_ro_password, collector_password, backup_password, watchdog_password)
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

echo "migrate: applying /db/roles.sql (grafana_ro, collector, backup_ro, watchdog_ro)"
psql -X -q -v ON_ERROR_STOP=1   -v grafana_ro_password="$GRAFANA_RO_PASSWORD"   -v collector_password="$COLLECTOR_DB_PASSWORD"   -v backup_password="$BACKUP_DB_PASSWORD"   -v watchdog_password="$WATCHDOG_DB_PASSWORD"   -f /db/roles.sql

echo "migrate: done"
