#!/bin/sh
# Issue #89 phase 1: fill flight_record sooner than the 5 minute job would (that job writes 50 pilots per run).
# Usage (the owner runs it on the VM, in ~/vatpac-statsV2), FIRST thing after the phase 1 deploy: sh ops/backfill_flight_record.sh
# Calls SELECT write_finished_flights(500) once per transaction, prints the time and the number of flights written, sleeps 2 s
# (so the 30 second poll keeps its CPU). A batch that writes 0 does NOT mean the work is done (the writer also returns 0 when
# another writer holds the lock, or when a batch held only pilots without flight plans): the loop stops only when no finished
# pilot is left with unprocessed sessions, retries when the lock was busy, and gives up (exit 1) after 5 rounds without progress.
# Safe to stop and re-run: the writer is idempotent and only touches pilots quiet for flight_quiet_hours of stored data.
# Time the FIRST batch by hand at a quiet hour; if it takes more than about 2 s: BATCH=200 sh ops/backfill_flight_record.sh
set -eu
BATCH="${BATCH:-500}"
case "$BATCH" in ''|*[!0-9]*) echo "usage: [BATCH=500] backfill_flight_record.sh  (BATCH is a whole number)" >&2; exit 2 ;; esac
cd "$(dirname "$0")/.."
# q SQL : run one statement as the owner role through stdin (the SQL holds quotes), print the single value
q() { printf '%s\n' "$1" | docker compose exec -T db sh -c 'psql -X -q -t -A -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -f -' | tr -d '[:space:]'; }
REMAINING_SQL="SELECT count(*) FROM (SELECT 1 FROM pilot_session ps WHERE ps.flown_upto IS NULL OR ps.last_seen > ps.flown_upto GROUP BY ps.cid HAVING (SELECT max(observed_at) FROM network_snapshot) - max(ps.last_seen) >= make_interval(secs => (3600 * COALESCE((SELECT value::numeric FROM app_setting WHERE key = 'flight_quiet_hours'), 8))::double precision)) x"
prev=-1
stuck=0
while :; do
  start="$(date +%s)"
  n="$(q "SELECT write_finished_flights($BATCH)")"
  left="$(q "$REMAINING_SQL")"
  case "$n$left" in *[!0-9]*|'') echo "the database call failed (n='$n', left='$left'); stopping" >&2; exit 1 ;; esac
  printf '%s  wrote %s flights in %s s, %s finished pilots still to process\n' "$(date -u '+%d-%m-%Y %H:%M:%S UTC')" "$n" "$(( $(date +%s) - start ))" "$left"
  [ "$left" -eq 0 ] && break
  if [ "$n" -eq 0 ] && [ "$left" -eq "$prev" ]; then stuck=$((stuck + 1)); else stuck=0; fi
  if [ "$stuck" -ge 5 ]; then echo "no progress for 5 rounds with $left finished pilots left (lock busy, or pilots skipped: see the WARNING lines in the database log); stopping" >&2; exit 1; fi
  prev="$left"
  sleep 2
done
echo "done: no finished pilot is left to process (a pilot still within the quiet period is written later by the 5 minute job)"
