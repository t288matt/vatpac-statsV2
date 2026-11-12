#!/bin/bash
# Nightly pg_dump (custom format) with rotation.
#   backup.sh loop   run forever: one backup at BACKUP_TIME_UTC daily (and one at start-up if
#                    the newest backup is older than 24 h); weekly restore test
#   backup.sh once   take one backup now and exit
# Connection comes from the standard PG* environment variables (the read-only role backup_ro).
# BACKUP_REMOTE (optional): a shell command run after each successful backup with BACKUP_FILE set to
# the new dump (off-box copy, e.g. rclone/rsync). Its failure is logged, never fatal.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
BACKUP_TIME_UTC="${BACKUP_TIME_UTC:-02:00}"
RESTORE_TEST_DAY="${BACKUP_RESTORE_TEST_DAY:-7}"
BACKUP_REMOTE="${BACKUP_REMOTE:-}"
# A hung pg_dump or pg_restore must not block the loop for ever: seconds allowed for each (a timeout is a failed backup).
DUMP_TIMEOUT_S="${BACKUP_DUMP_TIMEOUT_S:-1800}"
VALIDATE_TIMEOUT_S="${BACKUP_VALIDATE_TIMEOUT_S:-300}"
# A dump must hold real data to be kept (issue 118): at least MIN_SIZE_PERCENT of the newest previous good dump (a database does
# not halve overnight), or MIN_FIRST_BYTES when there is no previous dump; and data entries for the key tables. Rotation never
# leaves fewer than MIN_KEEP good dumps.
MIN_SIZE_PERCENT="${BACKUP_MIN_SIZE_PERCENT:-50}"
MIN_FIRST_BYTES="${BACKUP_MIN_FIRST_BYTES:-1048576}"
MIN_KEEP="${BACKUP_MIN_KEEP:-3}"
KEY_TABLES="poll_log pilot_session network_snapshot app_setting pilot_position"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"; }

take_backup() {
  # Every step is checked explicitly: take_backup is called as `take_backup || ...` in the loop, and bash ignores
  # `set -e` inside a function called that way, so a failing step would otherwise be silently carried past (issue 81).
  local stamp file tmp toc
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  file="${BACKUP_DIR}/${PGDATABASE}_${stamp}.dump"
  tmp="${file}.partial"
  log "starting ${file}"
  local rc=0
  timeout -k 30 "$DUMP_TIMEOUT_S" pg_dump -Fc --no-password -f "$tmp" || rc=$?
  if [ "$rc" -ne 0 ]; then
    rm -f "$tmp"
    if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then log "FAILED pg_dump (timed out after ${DUMP_TIMEOUT_S}s)"; else log "FAILED pg_dump"; fi
    return 1
  fi
  # A dump that pg_restore cannot list is not a backup: no rename, no success marker, no off-box copy, no rotation.
  rc=0
  # The listing is kept (issue 217) and handed to check_content: the same file, not touched in between, so one pg_restore -l serves both.
  toc="$(timeout -k 30 "$VALIDATE_TIMEOUT_S" pg_restore -l "$tmp")" || rc=$?
  if [ "$rc" -ne 0 ]; then
    rm -f "$tmp"
    log "FAILED validation (pg_restore -l could not read the dump, rc=${rc}); nothing kept, older backups untouched"
    return 1
  fi
  # Readable is not enough: an empty or wrong database also lists fine. Rejected dumps are moved aside, never deleted.
  local why
  if ! why="$(check_content "$tmp" "$toc")"; then
    log "FAILED content check: ${why}; dump moved aside to ${file}.rejected, older backups untouched, nothing rotated"
    mv "$tmp" "${file}.rejected" || rm -f "$tmp"
    return 1
  fi
  if ! mv "$tmp" "$file"; then
    rm -f "$tmp"
    log "FAILED to move the finished dump into place"
    return 1
  fi
  # The dump itself is good and already in place. If the marker cannot be written (the marker is only informational; the
  # watchdog reads the dump files), warn and carry on, so the off-box copy and rotation still happen. Success is not claimed.
  if ! date -u +%Y-%m-%dT%H:%M:%SZ > "${BACKUP_DIR}/.last_success"; then
    log "WARNING: could not write .last_success; the backup is kept and the remaining steps still run"
  fi
  log "ok $(du -h "$file" | cut -f1)"
  run_remote "$file"
  rotate
  return 0
}

# check_content DUMP TOC : TOC is the output of pg_restore -l on DUMP (already taken by take_backup). Prints the reason and returns 1 when the dump is too small or lacks data for a key table.
# The previous good dump is the newest *.dump; rejected dumps are named *.rejected and so never count.
check_content() {
  local dump="$1" toc="$2" size prev prev_size min t
  size="$(stat -c %s "$dump")"
  prev="$(ls -1t "$BACKUP_DIR"/*.dump 2>/dev/null | head -n 1 || true)"
  if [ -n "$prev" ]; then
    prev_size="$(stat -c %s "$prev")"
    min=$((prev_size * MIN_SIZE_PERCENT / 100))
    [ "$size" -ge "$min" ] || { echo "dump is ${size} bytes, under ${MIN_SIZE_PERCENT}% of the previous good dump ($(basename "$prev"), ${prev_size} bytes)"; return 1; }
  else
    [ "$size" -ge "$MIN_FIRST_BYTES" ] || { echo "first dump is ${size} bytes, under the minimum ${MIN_FIRST_BYTES}"; return 1; }
  fi
  for t in $KEY_TABLES; do
    # TOC data lines look like "123; 0 16400 TABLE DATA public poll_log owner"; a partitioned table has <name>_<yyyymmdd> entries.
    # A here-string, not a pipe: grep -q exits at the first match and an echo into it would get SIGPIPE (status 141 under pipefail)
    # once the list outgrows the 64 KB pipe buffer, which would reject a good dump.
    grep -Eq "TABLE DATA public ${t}(_[0-9]{8})? " <<<"$toc" || { echo "no table data for ${t} in the dump"; return 1; }
  done
}

run_remote() {
  [ -n "$BACKUP_REMOTE" ] || return 0
  log "running BACKUP_REMOTE hook"
  if BACKUP_FILE="$1" timeout 3600 sh -c "$BACKUP_REMOTE"; then
    date -u +%Y-%m-%dT%H:%M:%SZ > "${BACKUP_DIR}/.last_remote_success"
    log "BACKUP_REMOTE ok"
  else
    log "BACKUP_REMOTE FAILED (the local backup is intact)"
  fi
}

rotate() {
  # Delete dumps (and restore safety dumps) older than RETENTION_DAYS, oldest first, but never so many that fewer than MIN_KEEP
  # good dumps remain (only *.dump counts; *.rejected files are not good dumps and are not counted).
  local total f
  total="$(find "$BACKUP_DIR" -maxdepth 1 -name '*.dump' | wc -l)"
  while read -r f; do
    [ -n "$f" ] || continue
    if [ $((total - 1)) -lt "$MIN_KEEP" ]; then
      log "rotation kept ${f} (deleting it would leave fewer than ${MIN_KEEP} good dumps)"
      continue
    fi
    rm -f "$f" && total=$((total - 1)) && log "rotated out ${f}"
  done < <(find "$BACKUP_DIR" -maxdepth 1 -name '*.dump' -mtime +"$RETENTION_DAYS" -printf '%T@ %p
' | sort -n | cut -d' ' -f2-)
  find "$BACKUP_DIR" -maxdepth 1 \( -name '*.prerestore' -o -name '*.rejected' \) -mtime +"$RETENTION_DAYS" -print -delete     | sed 's/^/backup: rotated out /' || true
  find "$BACKUP_DIR" -maxdepth 1 \( -name '*.dump.partial' -o -name '*.prerestore.partial' \) -mmin +120 -delete || true
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
    touch "${BACKUP_DIR}/.loop_started"   # the container healthcheck tolerates the first 24 h
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
