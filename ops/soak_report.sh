#!/bin/sh
# Soak report: did the collector store a snapshot every 30 s for the last N hours (default 24)?
# Usage: sh ops/soak_report.sh [hours]
# PASS needs the full window covered, at least 99% of the expected snapshots, and p95 cadence <= 32 s.
# A shorter run is reported as INCOMPLETE, never as PASS. Reads poll_log only (read-only).
set -eu
HOURS="${1:-24}"
case "$HOURS" in
  ''|*[!0-9]*) echo "usage: soak_report.sh [hours]  (whole number)" >&2; exit 2 ;;
esac
cd "$(dirname "$0")/.."
docker compose exec -T db sh -c \
  'psql -X -P pager=off -x -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -v hours='"$HOURS"' -f -' \
  < ops/soak_report.sql
echo "--- container restarts (a restart count above 0 is not a failure by itself; check the gaps above)"
for svc in db collector grafana backup watchdog; do
  id="$(docker compose ps -q "$svc" 2>/dev/null || true)"
  [ -n "$id" ] || continue
  printf '%s: restarts=%s started=%s\n' "$svc" \
    "$(docker inspect -f '{{.RestartCount}}' "$id")" "$(docker inspect -f '{{.State.StartedAt}}' "$id")"
done
