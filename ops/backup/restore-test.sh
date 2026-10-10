#!/bin/bash
# Prove the newest backup restores: restore into a scratch database, sanity-check, drop it.
# Exit non-zero on any failure; the scratch database is dropped on every exit path.
# Result (first token is always a UTC timestamp, the second OK, FAIL or WARN = restored fine but the data in the dump is old)
# is written to $BACKUP_DIR/.last_restore_test.
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
rc=0
counts="$(sanity_check "$SCRATCH" "$newest" 2>&1)" || rc=$?
if [ "$rc" -eq 2 ]; then
  # The dump restored and holds data, but its newest poll row is old (a long outage or a quiet period before the dump).
  # That is worth a warning, not a failed restore test: the first tokens are "<UTC timestamp> WARN".
  export PGDATABASE="$LIVE_DB"
  echo "$(stamp) WARN ${newest} ${counts}" | tee "$RESULT"
  exit 0
elif [ "$rc" -ne 0 ]; then
  fail "sanity: $counts"
fi

export PGDATABASE="$LIVE_DB"
echo "$(stamp) OK ${newest} ${counts}" | tee "$RESULT"
