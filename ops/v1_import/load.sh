#!/bin/sh
# Issue 279 / ADR-035 (proposed): load v1 history (flight_summaries, controller_summaries) into flight_summary and controller_session
# from the frozen CSVs that ops/v1_import/extract.sh wrote. One psql session, ONE transaction. DRY RUN is the default: it does every
# step, prints the counts and rejects, then rolls back. Only --apply commits. Re-runnable: a second --apply inserts 0 rows.
#
#   sh ops/v1_import/load.sh --expect-db NAME --data-dir DIR              dry run (writes nothing)
#   sh ops/v1_import/load.sh --expect-db NAME --data-dir DIR --apply      load
#   sh ops/v1_import/load.sh --expect-db NAME --rollback [--apply]        remove the imported rows (session_id < 0, id < 0)
#   --allow-rejects   load the loadable rows even though some rows were rejected or already-loaded rows differ from the file (default: either stops the run)
#   --whitelist FILE  controller callsign list (default config/controller_callsigns_list.txt)
#
# --expect-db is required, but it is ONLY A TYPO GUARD: it compares the name you typed with current_database() and stops on a
# mismatch. It does NOT tell a live database from a restored copy of production with the same name, and the load runs as whatever
# role V1_IMPORT_PSQL connects as (on the VM the container superuser; no narrower role is used, because creating one is a roles.sql
# change). The only inserts are negative ids; the only deletes (--rollback) are negative ids. Check the target by eye.
# The connection comes from V1_IMPORT_PSQL, a command (run with sh -c) that reads SQL on stdin and connects as the OWNER role (the
# collector role has no right on flight_summary). Default, on the VM in ~/vatpac-statsV2 (the owner runs it; copy the data directory there first):
#   docker compose exec -T db sh -c 'psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1'
# The CSV files hold names and CIDs: if they are copied to the VM, delete them after the load. (Streaming them from the PC through
# ssh into the same docker command is possible, but that form has not been rehearsed.)
# Take a fresh dump first. Steps, checks and rollback: docs/RUNBOOKS/import-v1-history.md. Never run it against a restored copy of
# production that tests will empty.
# Exit codes: 0 done (or dry run clean); 2 bad arguments, missing or changed files; 3 the database refused (wrong database, rejected
# rows, rows already loaded that differ from the file, a failed check): in that case nothing was written, because the single
# transaction is rolled back.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
die() { echo "load.sh: $*" >&2; exit 2; }
expect_db=""; data_dir=""; apply=false; rollback=false; allow=false
whitelist="$repo/config/controller_callsigns_list.txt"
range_end="2026-04-01 00:00:00+00"  # flights: completion_time < this; controllers: session_start_time < this (ADR-035)
while [ $# -gt 0 ]; do
  case "$1" in
    --expect-db) [ $# -ge 2 ] || die "--expect-db needs a name"; expect_db="$2"; shift 2 ;;
    --data-dir) [ $# -ge 2 ] || die "--data-dir needs a folder"; data_dir="$2"; shift 2 ;;
    --whitelist) [ $# -ge 2 ] || die "--whitelist needs a file"; whitelist="$2"; shift 2 ;;
    --apply) apply=true; shift ;;
    --rollback) rollback=true; shift ;;
    --allow-rejects) allow=true; shift ;;
    -h|--help) sed -n '2,/^set -eu/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument $1 (see the header of this file)" ;;
  esac
done
case "$expect_db" in ''|*[!A-Za-z0-9_]*) die "--expect-db NAME is required (letters, digits and _ only)" ;; esac
psql_cmd="${V1_IMPORT_PSQL:-cd \"$repo\" && docker compose exec -T db sh -c 'psql -X -q -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -v ON_ERROR_STOP=1'}"

n_flights=0; n_controllers=0
if [ "$rollback" = false ]; then
  [ -n "$data_dir" ] && [ -d "$data_dir" ] || die "--data-dir must be the folder extract.sh wrote"
  [ -f "$whitelist" ] || die "whitelist file $whitelist not found"
  for f in v1_flights.csv v1_controllers.csv EXPECTED_COUNTS SHA256SUMS; do [ -f "$data_dir/$f" ] || die "$data_dir/$f is missing"; done
  (cd "$data_dir" && sha256sum -c SHA256SUMS >/dev/null) || die "checksum check failed: the frozen files changed since extract.sh wrote them"
  n_flights="$(sed -n 's/^flights=\([0-9][0-9]*\)$/\1/p' "$data_dir/EXPECTED_COUNTS")"
  n_controllers="$(sed -n 's/^controllers=\([0-9][0-9]*\)$/\1/p' "$data_dir/EXPECTED_COUNTS")"
  [ -n "$n_flights" ] && [ -n "$n_controllers" ] || die "EXPECTED_COUNTS is malformed"
  # a line holding only \. would end the inline COPY data early
  if grep -qx '\\\.' "$data_dir/v1_flights.csv" "$data_dir/v1_controllers.csv" "$whitelist"; then die "a data file has a line that is only \\. (not loadable inline)"; fi
fi

echo "load.sh: target database $expect_db; mode: $( [ "$rollback" = true ] && echo rollback || echo load ), $( [ "$apply" = true ] && echo APPLY || echo 'dry run' )" >&2
{
  printf '%s\n' '\set ON_ERROR_STOP on' "\\set expect_db '$expect_db'" "\\set is_apply $apply" "\\set allow_rejects $allow" \
    "\\set range_end '$range_end'" "\\set expect_flights $n_flights" "\\set expect_controllers $n_controllers"
  cat "$here/common.sql"
  if [ "$rollback" = true ]; then
    cat "$here/rollback.sql"
  else
    cat "$here/stage.sql"
    printf '%s\n' 'COPY v1_whitelist_raw (line) FROM STDIN (FORMAT text);'
    cat "$whitelist"
    printf '%s\n' '\.' 'COPY v1_flight FROM STDIN (FORMAT csv, HEADER match);'
    cat "$data_dir/v1_flights.csv"
    printf '%s\n' '\.' 'COPY v1_controller FROM STDIN (FORMAT csv, HEADER match);'
    cat "$data_dir/v1_controllers.csv"
    printf '%s\n' '\.'
    cat "$here/apply.sql"
  fi
} | sh -c "$psql_cmd"
