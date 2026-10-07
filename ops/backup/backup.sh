#!/bin/bash
# Nightly pg_dump (custom format) with rotation.
#   backup.sh loop   run forever: one backup at BACKUP_TIME_UTC daily (and one at start-up if
#                    the newest backup is older than 24 h); weekly restore test
#   backup.sh once   take one backup now and exit
# Connection comes from the standard PG* environment variables.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
BACKUP_TIME_UTC="${BACKUP_TIME_UTC:-02:00}"
RESTORE_TEST_DAY="${BACKUP_RESTORE_TEST_DAY:-7}"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"; }

take_backup() {
  local stamp file tmp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  file="${BACKUP_DIR}/${PGDATABASE}_${stamp}.dump"
  tmp="${file}.partial"
  log "starting ${file}"
  if pg_dump -Fc --no-password -f "$tmp"; then
    # A dump that pg_restore cannot list is not a backup.
    pg_restore -l "$tmp" > /dev/null
    mv "$tmp" "$file"
    date -u +%Y-%m-%dT%H:%M:%SZ > "${BACKUP_DIR}/.last_success"
    log "ok $(du -h "$file" | cut -f1)"
  else
    rm -f "$tmp"
    log "FAILED pg_dump"
    return 1
  fi
  rotate
}

rotate() {
  # Delete dumps older than RETENTION_DAYS, but never the newest one.
  find "$BACKUP_DIR" -maxdepth 1 -name '*.dump' -mtime +"$RETENTION_DAYS" -print -delete \
    | sed 's/^/backup: rotated out /' || true
  find "$BACKUP_DIR" -maxdepth 1 -name '*.dump.partial' -mmin +120 -delete || true
}

seconds_until_next() {
  local now target
  now="$(date -u +%s)"
  target="$(date -u -d "today ${BACKUP_TIME_UTC}" +%s)"
  if [ "$target" -le "$now" ]; then
    target="$(date -u -d "tomorrow ${BACKUP_TIME_UTC}" +%s)"
  fi
  echo $((target - now))
}

case "${1:-loop}" in
  once)
    take_backup
    ;;
  loop)
    if [ -z "$(find "$BACKUP_DIR" -maxdepth 1 -name '*.dump' -mmin -1440 -print -quit)" ]; then
      log "no backup in the last 24 h; taking one now"
      take_backup || log "start-up backup failed; will retry at ${BACKUP_TIME_UTC} UTC"
    fi
    while true; do
      wait_s="$(seconds_until_next)"
      log "next backup in ${wait_s}s (${BACKUP_TIME_UTC} UTC)"
      sleep "$wait_s"
      take_backup || log "backup failed; will retry tomorrow (watchdog alerts at 26 h)"
      if [ "$RESTORE_TEST_DAY" != "0" ] && [ "$(date -u +%u)" = "$RESTORE_TEST_DAY" ]; then
        restore-test.sh || log "restore test FAILED"
      fi
    done
    ;;
  *)
    echo "usage: backup.sh [loop|once]" >&2
    exit 2
    ;;
esac
