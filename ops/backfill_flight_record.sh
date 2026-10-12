#!/bin/sh
# Issue #89 phase 1: fill flight_record sooner than the 5 minute job would (500 pilots per 5 minutes).
# Usage (the owner runs it on the VM, in ~/vatpac-statsV2): sh ops/backfill_flight_record.sh
# Calls SELECT write_finished_flights(500) once per transaction, prints the time and the number of flights written, sleeps 2 s
# (so the 30 second poll keeps its CPU) and stops when a batch writes 0. Safe to stop and re-run: the writer is idempotent and
# only touches pilots who have been quiet for flight_quiet_hours of stored data. Time the FIRST batch by hand at a quiet hour;
# if it takes more than about 2 s, run with a smaller batch: BATCH=200 sh ops/backfill_flight_record.sh
set -eu
BATCH="${BATCH:-500}"
case "$BATCH" in ''|*[!0-9]*) echo "usage: [BATCH=500] backfill_flight_record.sh  (BATCH is a whole number)" >&2; exit 2 ;; esac
cd "$(dirname "$0")/.."
while :; do
  start="$(date +%s)"
  n="$(docker compose exec -T db sh -c 'psql -X -q -t -A -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "SELECT write_finished_flights('"$BATCH"')"' | tr -d '[:space:]')"
  printf '%s  wrote %s flights in %s s\n' "$(date -u '+%d-%m-%Y %H:%M:%S UTC')" "$n" "$(( $(date +%s) - start ))"
  [ "$n" -eq 0 ] && break
  sleep 2
done
echo "done: nothing left to write (a pilot still within the quiet period is written later by the 5 minute job)"
