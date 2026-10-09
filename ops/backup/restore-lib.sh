#!/bin/bash
# Shared helpers for restore.sh and restore-test.sh (sourced, not executed).

rlog() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) restore: $*"; }

# Restores and the restore test need CREATE/DROP/RENAME DATABASE, which the read-only dump role
# (backup_ro) does not have. They connect as the owner role passed in RESTORE_PGUSER/PASSWORD.
use_owner_role() {
  export PGUSER="${RESTORE_PGUSER:-$PGUSER}"
  export PGPASSWORD="${RESTORE_PGPASSWORD:-$PGPASSWORD}"
}

# sanity_check DB [DUMP_FILE] : the checks that decide whether a restored database is plausible.
# Prints "poll_log=N pilot_session=N" on success; prints the reason on stderr and returns 1 otherwise.
# Beyond "the tables exist" (issue 90, a schema-only dump used to pass) it requires:
#   - pilot_session and poll_log each hold at least one row;
#   - the newest poll_log row is no more than RESTORE_MAX_POLL_AGE_HOURS (default 48; 0 turns this check off) older than the
#     time the dump was taken. That time is read from the file name (<db>_YYYYMMDDTHHMMSSZ.dump, as backup.sh names it) of
#     DUMP_FILE; with no file or no stamp in the name the freshness check is skipped and only the row counts are required.
# RESTORE_SANITY_ALLOW_EMPTY=1 skips the row-count and freshness checks, for restoring a dump of a genuinely new, empty install.
sanity_check() {
  local db="$1" file="${2:-}" out stamp dump_ts max_h="${RESTORE_MAX_POLL_AGE_HOURS:-48}" ps pl fresh
  q() { psql -X -tA -d "$db" -v ON_ERROR_STOP=1 -c "$1"; }
  [ "$(q "SELECT to_regclass('public.pilot_session') IS NOT NULL")" = "t" ] || { echo "pilot_session missing" >&2; return 1; }
  [ "$(q "SELECT to_regclass('public.poll_log') IS NOT NULL")" = "t" ] || { echo "poll_log missing" >&2; return 1; }
  out="$(q "SELECT count(*) FROM schema_migrations")" || { echo "schema_migrations unreadable" >&2; return 1; }
  [ "$out" -gt 0 ] || { echo "schema_migrations empty" >&2; return 1; }
  pl="$(q "SELECT count(*) FROM poll_log")" || { echo "poll_log unreadable" >&2; return 1; }
  ps="$(q "SELECT count(*) FROM pilot_session")" || { echo "pilot_session unreadable" >&2; return 1; }
  if [ "${RESTORE_SANITY_ALLOW_EMPTY:-0}" != "1" ]; then
    [ "$pl" -gt 0 ] || { echo "poll_log is empty (a schema-only or empty dump; set RESTORE_SANITY_ALLOW_EMPTY=1 only for a new install)" >&2; return 1; }
    [ "$ps" -gt 0 ] || { echo "pilot_session is empty (a schema-only or empty dump; set RESTORE_SANITY_ALLOW_EMPTY=1 only for a new install)" >&2; return 1; }
    stamp="$(basename "$file" | sed -n 's/.*_\([0-9]\{8\}\)T\([0-9]\{6\}\)Z.*/\1\2/p')"
    if [ -n "$stamp" ] && [ "$max_h" != "0" ]; then
      dump_ts="$(echo "$stamp" | sed 's/^\(....\)\(..\)\(..\)\(..\)\(..\)\(..\)$/\1-\2-\3 \4:\5:\6+00/')"
      fresh="$(q "SELECT max(polled_at) >= '${dump_ts}'::timestamptz - make_interval(hours => ${max_h}) FROM poll_log")" || { echo "poll_log freshness unreadable" >&2; return 1; }
      [ "$fresh" = "t" ] || { echo "newest poll_log row is more than ${max_h} h older than the dump (${dump_ts}): the dump is stale or the collector had stopped" >&2; return 1; }
    fi
  fi
  echo "poll_log=${pl} pilot_session=${ps}"
}

# drop_db NAME : best-effort drop from the maintenance database.
drop_db() {
  PGDATABASE=postgres psql -X -q -c "DROP DATABASE IF EXISTS \"$1\" WITH (FORCE)" >/dev/null 2>&1 || true
}
