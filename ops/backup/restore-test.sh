#!/bin/bash
# Prove the newest backup restores: restore into a scratch database, sanity-check, drop it.
# Exit non-zero on any failure; the scratch database is dropped on every exit path.
# Result (first token is always a UTC timestamp) is written to $BACKUP_DIR/.last_restore_test.
set -euo pipefail
# shellcheck source=restore-lib.sh
. "${RESTORE_LIB:-/usr/local/bin/restore-lib.sh}"

BACKUP_DIR="${BACKUP_DIR:-/backups}"
SCRATCH="vatpac_restore_test"
RESULT="${BACKUP_DIR}/.last_restore_test"
LIVE_DB="$PGDATABASE"
stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

newest="$(find "$BACKUP_DIR" -maxdepth 1 -name '*.dump' -printf '%T@ %p\n' | sort -nr | head -n1 | cut -d' ' -f2-)"
if [ -z "$newest" ]; then
  echo "$(stamp) FAIL no backup found in $BACKUP_DIR" | tee "$RESULT"
  exit 1
fi

use_owner_role
export PGDATABASE=postgres
trap 'drop_db "$SCRATCH"' EXIT

fail() { echo "$(stamp) FAIL $1 ($newest)" | tee "$RESULT"; exit 1; }

drop_db "$SCRATCH"
psql -X -q -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"${SCRATCH}\"" || fail "create scratch database"
pg_restore --no-owner --no-acl --exit-on-error -d "$SCRATCH" "$newest" || fail "restore"
counts="$(sanity_check "$SCRATCH" "$newest" 2>&1)" || fail "sanity: $counts"

export PGDATABASE="$LIVE_DB"
echo "$(stamp) OK ${newest} ${counts}" | tee "$RESULT"
