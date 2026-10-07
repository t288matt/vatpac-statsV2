#!/bin/bash
# Prove the newest backup restores: restore into a scratch database, sanity-check, drop it.
# Exit non-zero on any failure. Result is written to $BACKUP_DIR/.last_restore_test.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
SCRATCH="vatpac_restore_test"
RESULT="${BACKUP_DIR}/.last_restore_test"
LIVE_DB="$PGDATABASE"

newest="$(find "$BACKUP_DIR" -maxdepth 1 -name '*.dump' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"
if [ -z "$newest" ]; then
  echo "FAIL no backup found in $BACKUP_DIR" | tee "$RESULT"
  exit 1
fi

fail() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) FAIL $1 ($newest)" | tee "$RESULT"; exit 1; }

restore.sh "$newest" "$SCRATCH" || fail "restore"

# Checks run against the scratch database.
q() { psql -X -tA -d "$SCRATCH" -v ON_ERROR_STOP=1 -c "$1"; }
[ "$(q "SELECT to_regclass('public.pilot_session') IS NOT NULL")" = "t" ] || fail "pilot_session missing"
[ "$(q "SELECT to_regclass('public.poll_log') IS NOT NULL")" = "t" ] || fail "poll_log missing"
[ "$(q "SELECT count(*) FROM schema_migrations")" -gt 0 ] || fail "schema_migrations empty"
polls="$(q "SELECT count(*) FROM poll_log")"
sessions="$(q "SELECT count(*) FROM pilot_session")"

PGDATABASE=postgres psql -X -q -c "DROP DATABASE IF EXISTS \"${SCRATCH}\" WITH (FORCE)"
export PGDATABASE="$LIVE_DB"
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) OK ${newest} poll_log=${polls} pilot_session=${sessions}" | tee "$RESULT"
