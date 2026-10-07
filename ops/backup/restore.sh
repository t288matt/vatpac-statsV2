#!/bin/bash
# Restore a pg_dump custom-format file into a target database.
#   restore.sh FILE [TARGET_DB]
# TARGET_DB defaults to "vatpac_restore" (a scratch database; it is dropped and recreated).
# Restoring over the LIVE database requires RESTORE_CONFIRM=yes and should be done with the
# collector stopped (see docs/RUNBOOKS/restore.md).
# Runs inside the backup container:
#   docker compose exec backup restore.sh /backups/<file>.dump [TARGET_DB]
# Connection comes from the standard PG* environment variables.
set -euo pipefail

FILE="${1:-}"
TARGET="${2:-vatpac_restore}"

if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
  echo "usage: restore.sh FILE [TARGET_DB]   (file not found: '${FILE}')" >&2
  exit 2
fi
case "$TARGET" in
  *[!A-Za-z0-9_]*|"") echo "restore: invalid target database name" >&2; exit 2 ;;
esac

pg_restore -l "$FILE" > /dev/null || { echo "restore: not a valid dump" >&2; exit 1; }

if [ "$TARGET" = "$PGDATABASE" ] && [ "${RESTORE_CONFIRM:-}" != "yes" ]; then
  echo "restore: '${TARGET}' is the live database. Re-run with RESTORE_CONFIRM=yes to proceed." >&2
  exit 3
fi

# Connect to the maintenance database to drop/create the target.
export PGDATABASE=postgres
psql -X -q -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"${TARGET}\" WITH (FORCE)"
psql -X -q -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"${TARGET}\""

# Roles are not in the dump; ownership and ACLs are dropped. The migrate job recreates roles
# and grants against the live database on the next `docker compose up`.
pg_restore --no-owner --no-acl --exit-on-error -d "$TARGET" "$FILE"

echo "restore: ${FILE} restored into database '${TARGET}'"
