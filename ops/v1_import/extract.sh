#!/bin/sh
# Issue 279 / ADR-035 (proposed): read-only extract of the two v1 summary tables into frozen CSV files with a checksum.
#   sh ops/v1_import/extract.sh OUTPUT_DIR        (OUTPUT_DIR outside the repository: the files hold names and CIDs; never commit them)
# Reads ONLY flight_summaries (completion_time < 2026-04-01) and controller_summaries (session_start_time < 2026-04-01) on the v1 box
# (ssh host $V1_SSH_HOST, default "vatpac"; container vatsim_postgres), SELECT only, in sessions set to default_transaction_read_only=on
# with a 30 s statement timeout. It never queries flights_archive, flights, transceivers, controllers or controllers_archive.
# Writes v1_flights.csv, v1_controllers.csv, EXPECTED_COUNTS (the row counts v1 reports, which load.sh checks) and SHA256SUMS.
# Refuses to overwrite an existing extract: the files are frozen. Prints sizes, counts and checksums, never a row.
set -eu
out="${1:-}"
[ -n "$out" ] || { echo "usage: extract.sh OUTPUT_DIR" >&2; exit 2; }
host="${V1_SSH_HOST:-vatpac}"
mkdir -p "$out"
[ ! -e "$out/SHA256SUMS" ] || { echo "extract.sh: $out/SHA256SUMS exists; the extract is frozen. Use a new folder." >&2; exit 2; }
cutoff="2026-04-01 00:00:00+00"
# v1 SQL: stdin is the SQL text, stdout is CSV. -q hides command tags; the three SETs make the session read-only, bounded and UTC.
v1() {
  { printf '%s\n' "SET default_transaction_read_only = on; SET statement_timeout = '30s'; SET TimeZone = 'UTC';"; cat; } |
    ssh -o ConnectTimeout=20 "$host" "docker exec -i vatsim_postgres sh -c 'psql -X -q -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -v ON_ERROR_STOP=1 --csv -f -'"
}
rm -f "$out"/*.part
printf '%s\n' "SELECT count(*) FROM flight_summaries WHERE completion_time < '$cutoff'" | v1 > "$out/n_flights.part"
printf '%s\n' "SELECT count(*) FROM controller_summaries WHERE session_start_time < '$cutoff'" | v1 > "$out/n_controllers.part"
cat > "$out/q_flights.part" <<EOF
SELECT id, cid, callsign, departure, arrival, logon_time, name, server, flight_rules, route, deptime, aircraft_type, aircraft_faa,
       planned_altitude, pilot_rating, military_rating, time_online_minutes, total_enroute_time_minutes, completion_time
FROM flight_summaries WHERE completion_time < '$cutoff' ORDER BY id
EOF
cat > "$out/q_controllers.part" <<EOF
SELECT id, cid, callsign, name, session_start_time, session_end_time, rating, facility, server, frequencies_used
FROM controller_summaries WHERE session_start_time < '$cutoff' ORDER BY id
EOF
v1 < "$out/q_flights.part" > "$out/v1_flights.csv.part"
v1 < "$out/q_controllers.part" > "$out/v1_controllers.csv.part"
nf="$(tail -n 1 "$out/n_flights.part")"; nc="$(tail -n 1 "$out/n_controllers.part")"
case "$nf$nc" in ''|*[!0-9]*) echo "extract.sh: v1 did not return row counts" >&2; exit 1 ;; esac
mv "$out/v1_flights.csv.part" "$out/v1_flights.csv"
mv "$out/v1_controllers.csv.part" "$out/v1_controllers.csv"
printf 'flights=%s\ncontrollers=%s\n' "$nf" "$nc" > "$out/EXPECTED_COUNTS"
rm -f "$out"/*.part
(cd "$out" && sha256sum v1_flights.csv v1_controllers.csv EXPECTED_COUNTS > SHA256SUMS)
echo "extract.sh: flights=$nf controllers=$nc"
(cd "$out" && wc -c v1_flights.csv v1_controllers.csv && cat SHA256SUMS)
