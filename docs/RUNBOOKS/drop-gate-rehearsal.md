# Runbook: the raw-position drop gate (issue 89 phase 4) and its rehearsal

Written 12-10-2026 (branch `p4-drop-gate`, issues #89 and #140). **Status: NOT YET DEPLOYED.** Nothing in this file has been run on the VM or the live database. The live function is still the one from migration `20261009000170` (it finalises the pieces table before a drop). Everything below describes migration `20261013000210` and the script `ops/rehearse_drop_gate.sql`, which exist only on the branch.

> **After phase 5.** This version names the finished-flights table `flight_summary`. Phase 5 (migration 20261013000300, NOT YET DEPLOYED) renames the finished-flights table `flight_summary` to `flight_summary` and removes the old pieces table; in production the finished flights are still in `flight_summary` until it is deployed. To rehearse on a restore of a pre-phase-5 dump, read `flight_summary` wherever this runbook says `flight_summary` (and use the pre-phase-5 copy of `ops/rehearse_drop_gate.sql`).

## 1. What the gate does

The daily maintenance asks `drop_old_pilot_position_partitions(retention_days)` to delete old daily partitions of `pilot_position`. Raw positions are irreplaceable, so the function keeps a day unless it can show that the flights made from it are safely written. A day is dropped only when all of this holds:

1. It is due (the day ended on or before today minus the retention) and no session seen inside the retention window has positions in it (the pinning rule, unchanged).
2. **Processed:** every session with positions in the day has `flown_upto` at or after its last position in that day.
3. **Covered:** every position of a session that has a flight plan lies inside the `start_time` to `end_time` of a `flight_summary` row that lists the session in `session_ids`. A session without a plan makes no flight and needs only rule 2.
4. **Counted:** every `flight_summary` row that lies wholly inside the day has `sample_count` equal to the positions stored for it.

Anything else keeps the day. Nothing is deleted on a doubt. The old finalisation of the pieces table (the `final` flag of the old pieces table) is no longer a precondition and is never run by this function.

The function writes one notice per refused day, which the collector logs as a warning:

```
DROP REFUSED: partition pilot_position_20270406 kept: 3 session(s) or flight(s) not reconciled with the written flights (positions not inside a written flight); first ids {123,124,125}
```

A day kept for more than twice the retention also gets `partition ... is kept beyond twice the retention: ...`.

A call drops **at most one day**. After a DROP the parent table `pilot_position` stays locked until the call ends, and the last check before each drop reads the whole day; doing that for several days under one lock could stall the collector's inserts past their 5 second timeout. The collector therefore calls the function again after a 0.5 second pause (up to 600 times per run), so a backlog drains one day per call. In the rehearsal, part B therefore makes `expected_to_drop + 1` calls and `calls` equals `partitions_dropped + 1`.

## 2. How you hear about a refusal

- The collector log: `docker compose logs --since 24h collector | grep "DROP REFUSED"`. The line is written on every daily maintenance run (and on every call of the catch-up loop) while the day stays refused.
- The watchdog: the integrity sweep (every 72 hours, repeated every 6 hours while it fails) has a check, `processed flight-plan sessions in no written flight (ended 2 to 30 days ago)`. It sees the session-side cause of a refusal (a session marked processed that no written flight lists) long before a day is due. The older check `sessions ended more than 2 days ago with unprocessed positions` sees the other cause (the writer has stopped). So the alert comes from the data, not from the maintenance run, and can be up to 3 days late.
- Not alerted: a flight whose `sample_count` is wrong more than 3 days after it was written (the existing check looks only at flights written in the last 3 days), and a position inside the time range of a flight but outside the range of the flight's own session list. These show only as `DROP REFUSED` in the log.

Nothing is lost when a day is refused: the positions stay and the next run tries again. The cost is disk (about 5 MB a day) and, if the cause is never fixed, a partition that stays for ever. The fallback in `docs/DESIGN_89_FINAL.md` section 6 is to raise `POSITION_RETENTION_DAYS` to 365.

## 3. Before the first real drop (checklist)

With `POSITION_RETENTION_DAYS=180` (ADR-026) the first real drop is about 06-04-2027. Before that date, in this order:

1. Phase 1 is live, the backfill has finished and the integrity sweep is clean for the two writer checks.
2. The rehearsal in section 4 has been run on a restore of a recent dump and both runs ended `REHEARSAL RESULT: OK`.
3. **Phase 3 is live (it is, since 13-10-2026).** The old pieces table and its refresh functions are removed by phase 5, so nothing can delete the pieces of a session whose positions are gone and no ops script can run a full refresh.
4. A manual backup is taken just before the first drop (`make backup`).
5. After the first drop, check `partitions_dropped` in the "maintenance finished" log line and grep the log for `DROP REFUSED`.

## 4. The rehearsal on a restored dump (owner runs it; about 30 minutes)

The aim: prove, on a copy of the real data, that the gate drops exactly the days it should and nothing else, before it ever runs on the live database. It never touches the live database: it works in a scratch database inside the running Postgres container (the same method as the restore drill in `restore.md`, no new container). The script refuses to run unless the database name ends in `_test`.

**Limits (see `docs/RUNBOOKS/run-tests.md` and the VM load rule):** one scratch database at a time, run when the load is low (`uptime` shows a 1-minute load below 2.0 before you start), and do not run it during a backup. Free disk must be at least twice the size of the dump (`df -h`).

All commands run from `~/vatpac-statsV2` on the VM. No password is typed and no `.env` value is printed: the `sh -c` arguments are in single quotes, so the container's own `$POSTGRES_USER` is used.

### 4.1 Fetch the migration and the script without changing the checked-out code

```sh
git fetch origin
git show origin/p4-drop-gate:db/migrations/20261013000210_drop_gate_reconciles_flights.sql | sed '/^-- migrate:down/,$d' > /tmp/gate_up.sql
git show origin/p4-drop-gate:ops/rehearse_drop_gate.sql > /tmp/rehearse_drop_gate.sql
wc -l /tmp/gate_up.sql /tmp/rehearse_drop_gate.sql        # about 250 and 90 lines
```

### 4.2 Restore the newest dump into the scratch database

```sh
docker compose exec -T backup ls -la /backups                      # pick the newest .dump, call it <file>
docker compose exec -T backup cat /backups/<file>.dump | docker compose exec -T db sh -c 'cat > /tmp/gate_drill.dump'
docker compose exec -T db sh -c 'psql -X -q -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE vatpac_gate_test"'
docker compose exec -T db sh -c 'pg_restore --no-owner --no-acl --exit-on-error -U "$POSTGRES_USER" -d vatpac_gate_test /tmp/gate_drill.dump'
```

Check it is a usable copy of a database that already has phase 1:

```sh
docker compose exec -T db sh -c 'psql -X -q -t -A -U "$POSTGRES_USER" -d vatpac_gate_test -c "SELECT to_regclass('"'"'flight_summary'"'"') IS NOT NULL AS has_flight_summary, (SELECT count(*) FROM flight_summary) AS flights, (SELECT count(*) FROM pilot_session) AS sessions, (SELECT count(*) FROM pg_inherits WHERE inhparent = '"'"'pilot_position'"'"'::regclass) AS partitions"'
```

Expected: `t|<flights>|<sessions>|<partitions>` with flights greater than 0. If `has_flight_summary` is `f`, the finished-flights table is not there (phase 1 not live, or a pre-phase-5 dump): stop, the gate cannot be rehearsed yet.

### 4.3 Install the new gate in the copy only

```sh
docker compose exec -T db sh -c 'psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d vatpac_gate_test -f -' < /tmp/gate_up.sql
```

No output means it worked. This changes the scratch database only.

Then let the collector's role call the function, as it does in production (the restore used `--no-acl`, so the grant is missing in the copy; the role itself already exists in the cluster):

```sh
docker compose exec -T db sh -c 'psql -X -q -U "$POSTGRES_USER" -d vatpac_gate_test -c "GRANT EXECUTE ON FUNCTION drop_old_pilot_position_partitions(int, date) TO collector"'
```

### 4.3b Let the writer catch up in the copy

The dump holds the live moment, when the newest pilots had not yet been quiet for 8 hours, so their flights are not written yet and the gate would rightly refuse the days they touch. In production the 5 minute job writes them later. Reproduce that in the copy by moving the writer's clock one day on and running the writer until it writes nothing:

```sh
docker compose exec -T db sh -c 'psql -X -q -U "$POSTGRES_USER" -d vatpac_gate_test -c "INSERT INTO network_snapshot (observed_at, pilots_in_region, controllers_in_region, pilots_global, controllers_global) SELECT max(observed_at) + interval '"'"'1 day'"'"', 0, 0, 0, 0 FROM network_snapshot"'
docker compose exec -T db sh -c 'psql -X -q -t -A -U "$POSTGRES_USER" -d vatpac_gate_test -c "SELECT write_finished_flights(500)"'     # repeat until it prints 0
```

After this, any day the gate refuses in run 1 is a finding, not a timing effect. (To tell the two apart if you skip this step: a refused day whose example session has `last_seen` within about 8 hours of the dump's newest `network_snapshot` is a timing effect; any other refused day is a finding.)

### 4.4 Run 1: the untouched copy

Choose the test date. The live database holds only a few days, so the real retention would make nothing due. The rehearsal uses a tiny retention and a date a few days after the newest data: take the date of the dump (`<d>`, as `YYYY-MM-DD`) plus 2 days, and a retention of 2.

```sh
docker compose exec -T db sh -c 'psql -X -v ON_ERROR_STOP=1 -v retention=2 -v today=<YYYY-MM-DD> -v as_role=collector -U "$POSTGRES_USER" -d vatpac_gate_test -f -' < /tmp/rehearse_drop_gate.sql
```

Read the output top to bottom:

- Part A prints one summary row (`partitions`, `due`, `pinned`, `refused_by_gate`, `expected_to_drop`, `positions_expected_gone`) and then, if any, the days that are due but that the gate will keep, with the reason and an example session.
- Part B prints the `DROP REFUSED` and `pinned beyond twice the retention` notices as they happen and then `calls`, `partitions_dropped`.
- Part C prints six numbers that must all be 0 and the last line. The sixth, `gate_too_lax`, comes from an independent per-position count in part A (written differently from the gate: no aggregate, no range join); it is not 0 if the gate would have called a day reconciled while that count finds a position that is unprocessed or in no written flight of its session.

**Success:** the last line is `REHEARSAL RESULT: OK`, and `partitions_dropped` equals `expected_to_drop`, and `expected_to_drop` is greater than 0. If `expected_to_drop` is 0 the run proves nothing: repeat from 4.2 with a later date. **Failure:** `REHEARSAL RESULT: FAIL`, or `refused_by_gate` is greater than 0 on a copy where every pilot has long since left. That is a finding: stop, keep the output, do not deploy, report it on issue #140.

A day refused on a real copy is not automatically a bug. List what stopped it:

```sh
docker compose exec -T db sh -c 'psql -X -U "$POSTGRES_USER" -d vatpac_gate_test -c "SELECT * FROM unreconciled_partition_sessions('"'"'<day> 00:00+00'"'"', '"'"'<day+1> 00:00+00'"'"')"'
```

and compare the session ids with `pilot_session` (is `flown_upto` empty or before `last_seen`?) and `flight_summary` (`session_ids`).

### 4.5 Run 2: prove it refuses

Throw the scratch database away and restore again (4.2 and 4.3: `DROP DATABASE vatpac_gate_test WITH (FORCE)` first). Then break exactly one flight in the copy: the command below deletes the single oldest `flight_summary` row (one flight, identified by `session_id` and `start_time`), whose positions lie in the oldest due day:

```sh
docker compose exec -T db sh -c 'psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d vatpac_gate_test -c "DELETE FROM flight_summary WHERE (session_id, start_time) = (SELECT session_id, start_time FROM flight_summary ORDER BY start_time LIMIT 1)"'
```

Run the script exactly as in 4.4. **Success:** `REHEARSAL RESULT: OK` (the prediction and the outcome agree), the first day appears in the "due but the gate should keep" list with reason `positions not inside a written flight`, the log has a `DROP REFUSED: partition pilot_position_<day> kept` line naming that session, and that partition still exists afterwards (`\dt pilot_position_*` in the copy, or `partitions` in the summary is larger than `partitions_dropped` by at least one).

### 4.6 Clean up and report

```sh
docker compose exec -T db sh -c 'rm -f /tmp/gate_drill.dump; psql -X -q -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS vatpac_gate_test WITH (FORCE)"'
rm -f /tmp/gate_up.sql /tmp/rehearse_drop_gate.sql
```

Paste the three summary blocks (part A summary, `partitions_dropped`, the part C numbers and last line) of each run as a comment on issue #140. The gate may be deployed only when both runs were as described in 4.4 and 4.5.

## 5. When a partition is refused

1. Read the line: it names the partition, the number of sessions or flights and the reason, and up to five session ids.
2. Reasons:
   - `positions not yet processed by the flight writer`: the writer has not run, has stopped, or the pilot is not yet quiet. Check `SELECT write_finished_flights(500)` as the owner role (it writes: do it deliberately) and the collector log for `write_finished_flights failed`. See `integrity-checks.md`.
   - `positions not inside a written flight`: the session is marked processed but no flight of that session covers a position. Causes: a writer fault, a manual change to `flight_summary`, or a plan filed only after the first part of the session was processed (that part made no flight). Do not edit `flight_summary` by hand. Raise it on issue #89 / #140 with the session ids.
   - `flight sample_count differs from its stored positions`: a position was added to, or removed from, a day that a flight lies in, after the flight was written. Raise it on issue #89 / #140.
   - `session row missing`: a position without a session row. Should be impossible (foreign key). Raise it.
3. To see the full list for a day: `SELECT * FROM unreconciled_partition_sessions('<day> 00:00+00', '<day+1> 00:00+00')` as the owner role.
4. A day that stays refused costs only disk. If many days are refused and cannot be cleared quickly, raise `POSITION_RETENTION_DAYS` to 365 (design section 6; the lever and its steps are there).

The table `drop_gate_refusal` (one row per refused day, with `first_refused`) feeds the integrity check `raw-position days kept by the deletion check for more than 3 days`, which works at any age of the day. Look at it with `SELECT * FROM drop_gate_refusal`.

## 6. What was and was not tested (12-10-2026)

Tested on a throwaway local PostgreSQL 16.2 with synthetic dates (no waiting for a real date): every rule above, the lock behaviour of migration 20261009000170, the rehearsal script (its refusal on a database not ending `_test`, its prediction against the real outcome), the new integrity check and the roles. Requirement-to-test table: `docs/CONFORMANCE_89_PHASE4.md`.

Not tested: a real restored live dump (this runbook is the procedure for it); run times at live volume (about 90,000 positions a day at 5 years, 575,000 rows in the whole `flight_summary`); behaviour on PostgreSQL 16.15 (CI and the VM) as opposed to 16.2; the deploy.
