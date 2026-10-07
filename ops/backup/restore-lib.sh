#!/bin/bash
# Shared helpers for restore.sh and restore-test.sh (sourced, not executed).

rlog() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) restore: $*"; }

# Restores and the restore test need CREATE/DROP/RENAME DATABASE, which the read-only dump role
# (backup_ro) does not have. They connect as the owner role passed in RESTORE_PGUSER/PASSWORD.
use_owner_role() {
  export PGUSER="${RESTORE_PGUSER:-$PGUSER}"
  export PGPASSWORD="${RESTORE_PGPASSWORD:-$PGPASSWORD}"
}

# sanity_check DB : the checks that decide whether a restored database is plausible.
# Prints "poll_log=N pilot_session=N" on success; prints the reason on stderr and returns 1 otherwise.
sanity_check() {
  local db="$1" out
  q() { psql -X -tA -d "$db" -v ON_ERROR_STOP=1 -c "$1"; }
  [ "$(q "SELECT to_regclass('public.pilot_session') IS NOT NULL")" = "t" ] || { echo "pilot_session missing" >&2; return 1; }
  [ "$(q "SELECT to_regclass('public.poll_log') IS NOT NULL")" = "t" ] || { echo "poll_log missing" >&2; return 1; }
  out="$(q "SELECT count(*) FROM schema_migrations")" || { echo "schema_migrations unreadable" >&2; return 1; }
  [ "$out" -gt 0 ] || { echo "schema_migrations empty" >&2; return 1; }
  echo "poll_log=$(q "SELECT count(*) FROM poll_log") pilot_session=$(q "SELECT count(*) FROM pilot_session")"
}

# drop_db NAME : best-effort drop from the maintenance database.
drop_db() {
  PGDATABASE=postgres psql -X -q -c "DROP DATABASE IF EXISTS \"$1\" WITH (FORCE)" >/dev/null 2>&1 || true
}
