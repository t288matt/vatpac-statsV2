#!/bin/bash
# Restore a pg_dump custom-format file into a target database, safely.
#   RESTORE_CONFIRM=<target name> restore.sh FILE [TARGET_DB]
# TARGET_DB defaults to "vatpac_restore". Steps:
#   1. refuse system databases (postgres, template0, template1) and unsafe names
#   2. require RESTORE_CONFIRM to equal the target database name exactly
#   3. refuse if anything else is connected to the target (stop collector/grafana/watchdog first)
#   4. if the target exists and has tables, take a safety dump of it first
#      ($BACKUP_DIR/<target>_prerestore_<stamp>.prerestore; not counted as a nightly backup)
#   5. restore into <target>_restoring and run the same sanity checks as restore-test.sh
#   6. swap: rename <target> to <target>_old, then <target>_restoring to <target>
# The previous database stays as <target>_old until the operator drops it. Roles are not in the dump:
# re-run the migrate job afterwards to re-apply grants (docs/RUNBOOKS/restore.md).
# Runs inside the backup container:
#   docker compose exec -e RESTORE_CONFIRM=vatpac backup restore.sh /backups/<file>.dump vatpac
set -euo pipefail
# shellcheck source=restore-lib.sh
. "${RESTORE_LIB:-/usr/local/bin/restore-lib.sh}"

FILE="${1:-}"
TARGET="${2:-vatpac_restore}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"

if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
  echo "usage: RESTORE_CONFIRM=<target> restore.sh FILE [TARGET_DB]   (file not found: '${FILE}')" >&2
  exit 2
fi
case "$TARGET" in
  *[!A-Za-z0-9_]*|"") echo "restore: invalid target database name (letters, digits, underscore only)" >&2; exit 2 ;;
esac
if [ "${#TARGET}" -gt 48 ]; then echo "restore: target name too long (max 48; suffixes are added)" >&2; exit 2; fi
case "$TARGET" in
  postgres|template0|template1) echo "restore: refusing system database '${TARGET}'" >&2; exit 2 ;;
  *_restoring|*_old) echo "restore: '${TARGET}' looks like a working name this script creates; pick another" >&2; exit 2 ;;
esac
if [ "${RESTORE_CONFIRM:-}" != "$TARGET" ]; then
  echo "restore: set RESTORE_CONFIRM=${TARGET} (the exact target database name) to proceed." >&2
  exit 3
fi

pg_restore -l "$FILE" > /dev/null || { echo "restore: not a valid dump" >&2; exit 1; }

use_owner_role
export PGDATABASE=postgres
Q() { psql -X -tA -v ON_ERROR_STOP=1 -c "$1"; }

STAGING="${TARGET}_restoring"
OLD="${TARGET}_old"
exists() { [ "$(Q "SELECT count(*) FROM pg_database WHERE datname = '$1'")" = "1" ]; }

cleanup_staging() { drop_db "$STAGING"; }
trap cleanup_staging EXIT

if exists "$OLD"; then
  echo "restore: '${OLD}' already exists from a previous restore. Check it, then drop it: DROP DATABASE \"${OLD}\";" >&2
  exit 4
fi

target_exists=0
if exists "$TARGET"; then
  target_exists=1
  others="$(Q "SELECT count(*) FROM pg_stat_activity WHERE datname = '${TARGET}' AND pid <> pg_backend_pid()")"
  if [ "$others" != "0" ]; then
    echo "restore: ${others} other session(s) are connected to '${TARGET}'. Stop collector, grafana and watchdog first." >&2
    exit 4
  fi
  tables="$(psql -X -tA -d "$TARGET" -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r','p')")"
  if [ "$tables" -gt 0 ]; then
    safety="${BACKUP_DIR}/${TARGET}_prerestore_$(date -u +%Y%m%dT%H%M%SZ).prerestore"
    rlog "safety dump of '${TARGET}' to ${safety}"
    if ! pg_dump -Fc --no-password -d "$TARGET" -f "${safety}.partial"; then
      rm -f "${safety}.partial"
      echo "restore: safety dump failed; aborting before any change" >&2
      exit 1
    fi
    pg_restore -l "${safety}.partial" > /dev/null
    mv "${safety}.partial" "$safety"
  fi
fi

rlog "restoring ${FILE} into '${STAGING}'"
drop_db "$STAGING"
psql -X -q -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"${STAGING}\""
# Roles are not in the dump; ownership and ACLs are dropped (the migrate job re-applies grants).
pg_restore --no-owner --no-acl --exit-on-error -d "$STAGING" "$FILE"

rc=0
counts="$(sanity_check "$STAGING" "$FILE")" || rc=$?
if [ "$rc" -eq 2 ]; then
  echo "restore: ${counts}" >&2
  echo "restore: this dump is complete but its newest poll data is old. If that is expected (a known outage), accept it by running again with RESTORE_MAX_POLL_AGE_HOURS=0 (no age check) or a larger number of hours. '${TARGET}' was not touched" >&2
  exit 1
elif [ "$rc" -ne 0 ]; then
  echo "restore: sanity checks failed on the restored copy; '${TARGET}' was not touched" >&2
  exit 1
fi
rlog "sanity checks passed (${counts})"

if [ "$target_exists" = "1" ]; then
  others="$(Q "SELECT count(*) FROM pg_stat_activity WHERE datname = '${TARGET}' AND pid <> pg_backend_pid()")"
  if [ "$others" != "0" ]; then
    echo "restore: a session connected to '${TARGET}' during the restore; not swapping. '${STAGING}' is dropped." >&2
    exit 4
  fi
  Q "ALTER DATABASE \"${TARGET}\" RENAME TO \"${OLD}\"" > /dev/null
  if ! Q "ALTER DATABASE \"${STAGING}\" RENAME TO \"${TARGET}\"" > /dev/null; then
    Q "ALTER DATABASE \"${OLD}\" RENAME TO \"${TARGET}\"" > /dev/null || true
    echo "restore: swap failed; the original database was put back" >&2
    exit 1
  fi
  trap - EXIT
  rlog "'${TARGET}' replaced. The previous database is kept as '${OLD}'; once satisfied: DROP DATABASE \"${OLD}\";"
else
  Q "ALTER DATABASE \"${STAGING}\" RENAME TO \"${TARGET}\"" > /dev/null
  trap - EXIT
  rlog "'${TARGET}' created from ${FILE}"
fi
echo "restore: next, re-apply roles/grants: docker compose up migrate  (see docs/RUNBOOKS/restore.md)"
