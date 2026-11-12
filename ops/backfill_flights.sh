#!/bin/sh
# Issue #89 phase 1: fill flight_summary sooner than the 5 minute job would (that job writes 50 pilots per run).
# Usage (the owner runs it on the VM, in ~/vatpac-statsV2), FIRST thing after the phase 1 deploy: sh ops/backfill_flights.sh
# Calls SELECT write_finished_flights(500) once per transaction, prints the time and the number of flights written, sleeps 2 s
# (so the 30 second poll keeps its CPU). A batch that writes 0 does NOT mean the work is done (the writer also returns 0 when
# another writer holds the lock, or when a batch held only pilots without flight plans): the loop stops only when no finished
# pilot is left with unprocessed sessions, retries when the lock was busy, and gives up (exit 1) after 5 rounds without progress.
# Safe to stop and re-run: the writer is idempotent and only touches pilots quiet for flight_quiet_hours of stored data.
# Time the FIRST batch by hand at a quiet hour; if it takes more than about 2 s: BATCH=200 sh ops/backfill_flights.sh
set -eu
BATCH="${BATCH:-500}"
case "$BATCH" in ''|*[!0-9]*) echo "usage: [BATCH=500] backfill_flights.sh  (BATCH is a whole number)" >&2; exit 2 ;; esac
cd "$(dirname "$0")/.."
# q SQL : run one statement as the owner role through stdin (the SQL holds quotes), print the single value
q() { printf '%s\n' "$1" | docker compose exec -T db sh -c 'psql -X -q -t -A -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -f -' | tr -d '[:space:]'; }
# Flights (v1 groups of unprocessed sessions) whose own newest last_seen is flight_quiet_hours behind the newest snapshot: the work the writer still has
# to do. Quiet is per FLIGHT (owner decision 12-10-2026), so a pilot who is flying again in a different flight does not count. The rule and the
# setting are read from the view ops_pending_flight (migration 20261112000010, issues 200 and 238): no copy of the rule and no number here.
REMAINING_SQL="$(cat <<'SQL'
SELECT count(*) FROM ops_pending_flight WHERE is_quiet
SQL
)"
prev=-1
stuck=0
while :; do
  start="$(date +%s)"
  n="$(q "SELECT write_finished_flights($BATCH)")"
  left="$(q "$REMAINING_SQL")"
  case "$n$left" in *[!0-9]*|'') echo "the database call failed (n='$n', left='$left'); stopping" >&2; exit 1 ;; esac
  printf '%s  wrote %s flights in %s s, %s finished flights still to process\n' "$(date -u '+%d-%m-%Y %H:%M:%S UTC')" "$n" "$(( $(date +%s) - start ))" "$left"
  [ "$left" -eq 0 ] && break
  if [ "$n" -eq 0 ] && [ "$left" -eq "$prev" ]; then stuck=$((stuck + 1)); else stuck=0; fi
  if [ "$stuck" -ge 5 ]; then echo "no progress for 5 rounds with $left finished flights left (lock busy, or pilots skipped: see the WARNING lines in the database log); stopping" >&2; exit 1; fi
  prev="$left"
  sleep 2
done
echo "done: no finished flight is left to process (a flight still within the quiet period is written later by the 5 minute job)"
