# Runbook: load v1 history into v3 (one-off)

**Status (05-11-2026): built and rehearsed on a throwaway database; NOT loaded into production.** The design is ADR-035 in [../DECISIONS.md](../DECISIONS.md), still PROPOSED; issue 279. Nothing here may be run against production without the owner's explicit go. The load is High tier (it writes data tables and is hard to undo), and the go covers one run.

What it does: copies v1's two summary tables into v3, nothing else.

| From (v1 database, read-only) | To (v3) | Rows expected |
|---|---|---|
| `flight_summaries`, completed before 01-04-2026 | `flight_summary`, `session_id` = minus the v1 id | 73,532 (24-08-2025 to 06-03-2026) |
| `controller_summaries`, started before 01-04-2026 | `controller_session`, `id` = minus the v1 id | 11,270 (24-08-2025 to 15-03-2026) |

No raw positions are read. `pilot_session`, `pilot_position` and the two controller event tables get no rows. How each column is filled: [../DATA_DICTIONARY.md](../DATA_DICTIONARY.md) (`flight_summary` and `controller_session`) and [../DEFINITIONS.md](../DEFINITIONS.md) (1b.6, 3 and 5). The files are in `ops/v1_import/` (`extract.sh`, `load.sh`, and the SQL they join: `common.sql`, `stage.sql`, `apply.sql`, `rollback.sql`).

## 0. What the dashboards will show afterwards (tell the owner before the go)

No panel and no dashboard file is changed by this work. After a load:

- **History appears from 24-08-2025.** Panels with a long window or an all-time total now include v1 history. "Data since 07-10-2026" and "since go-live" labels become wrong in wording (the history starts earlier), and the text rows are not edited.
- **A gap with no data from 15-03-2026 to 07-10-2026** (about 206 days). It shows as zero or empty; the owner accepted this (ADR-035).
- **Panels with no time axis count the gap as behaviour:** lapsed 90+ days, reactivated after 60+ days, and retention rate. Controllers who were active in the history and not since will show as lapsed; those who came back after the gap as reactivated.
- **Imported flights have no speed, altitude or SELCAL data** (`max_gs_kt`, `max_alt_ft` NULL, `remarks` NULL, `sample_count` 0). Records and highest-speed or altitude panels leave them out. Their airborne time is v1's figure (capped at time online), not v3's measurement.
- **Counts at the join.** On the overlap window of 09-10-2026 v1 and v3 flight counts agreed to within about 2% (docs/V1_COMPARISON.md section 12), so no step is expected; check the first weeks after loading anyway.
- **Rolling 7 and 30 day windows never reach the history.**
- **End times next to v1 data gaps can be early** (for example 21-12-2025 07:00:58 to 13:20:56). About 1.3% of the flights that could be tested ended just before a window in which v1 stored nothing. The full list of limits is in ADR-035.

## 1. Preconditions

All of these before step 2. Stop if any is missing.

1. The owner's explicit go for this load, in the session.
2. A fresh dump of the target database, taken now (working agreement section 10): `cd ~/vatpac-statsV2 && docker compose exec backup backup.sh once`, then check the new file in `/backups` ([restore.md](restore.md)). The dump is the way back if a check fails after the commit.
3. The exact target. The loader refuses to run unless the name given with `--expect-db` equals `current_database()`. This catches a typo only: it does not tell a restored copy of production from the real thing, and the load runs as the container's superuser. On the VM the stack's database is `vatpac` unless `POSTGRES_DB` in `.env` says otherwise. Never aim it at a restored copy of production that tests will later empty (the integration tests empty every data table: [run-tests.md](run-tests.md); "Make a local copy of production" in [restore.md](restore.md)).
4. The loader connects as the **owner role** (the `collector` role has no right on `flight_summary`). The default connection is `docker compose exec -T db sh -c 'psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1'`, run from `~/vatpac-statsV2`. To use another connection, set `V1_IMPORT_PSQL` to a command (run with `sh -c`) that reads SQL on stdin.
5. The collector is healthy and no deploy or restore is running.

## 2. Extract from v1 (read-only; on the machine that has `ssh vatpac`)

```sh
sh ops/v1_import/extract.sh /path/outside/the/repo/v1_extract
```

- The folder must be **outside the repository**: the CSV files hold pilot and controller names and CIDs. Never commit them.
- It reads only `flight_summaries` and `controller_summaries` on the v1 box (ssh host `$V1_SSH_HOST`, default `vatpac`; container `vatsim_postgres`), `SELECT` only, in sessions set to `default_transaction_read_only = on` with a 30 second statement timeout. It never touches `flights_archive`, `flights`, `transceivers`, `controllers` or `controllers_archive`.
- It writes `v1_flights.csv`, `v1_controllers.csv`, `EXPECTED_COUNTS` (the row counts v1 reported) and `SHA256SUMS`, then prints sizes, counts and checksums, never a row. A folder that already holds `SHA256SUMS` is refused: the files are frozen, use a new folder.
- Compare the printed counts with the table above (73,532 and 11,270 on 05-11-2026). A different number means v1 changed; stop and tell the owner.

## 3. Checksum

`load.sh` verifies the checksums and the row counts itself and refuses a changed, cut-off or mismatched file. To check by hand after copying the folder (the load runs on the VM, so copy it there first, for example `scp -r`):

```sh
cd /path/to/v1_extract && sha256sum -c SHA256SUMS
```

Streaming the files from the PC through ssh into the same command is possible but has not been rehearsed; copy them.

## 4. Dry run (writes nothing)

On the VM, in `~/vatpac-statsV2`:

```sh
sh ops/v1_import/load.sh --expect-db vatpac --data-dir /path/to/v1_extract
```

Everything runs in one transaction that ends in a rollback. It prints, in order: the target (database, role, server version, mode) and the current live and imported counts; the staged counts against `EXPECTED_COUNTS`; the result before writing (rows in file, rejected, already loaded, to insert) with a reason for every rejected row; the timed writes; the checks; and the rows by month. It stops, writing nothing, if the database name differs, a checksum or count differs, or any row is rejected (a rejected row is a row that breaks a rule, for example a callsign not on the whitelist), or a row already loaded differs from the file (a re-run on edited data; the loaded rows are left as they are). `--allow-rejects` loads the rest anyway; use it only if the owner has read the reasons. Exit code 0 is clean, 2 is bad arguments or files, 3 is the database refusing (nothing written).

Read the output with the owner. The numbers must match step 6.

## 5. Apply (one go)

Same command plus `--apply`:

```sh
sh ops/v1_import/load.sh --expect-db vatpac --data-dir /path/to/v1_extract --apply
```

It commits only if every check passes (every loadable row present, live rows unchanged, imported total as expected), then runs `ANALYZE` on both tables. Running it a second time inserts 0 rows and changes nothing. If it stops with an error, nothing was committed.

## 6. Verify

The "AFTER" and "Imported rows by month" blocks of the apply output are the first check. Expected, imported rows by month (flights by `end_time`, controllers by `logon_time`, UTC):

| Month | 08-2025 | 09-2025 | 10-2025 | 11-2025 | 12-2025 | 01-2026 | 02-2026 | 03-2026 | Total |
|---|---|---|---|---|---|---|---|---|---|
| Flights | 2,801 | 10,284 | 11,450 | 10,540 | 12,456 | 13,268 | 10,841 | 1,892 | 73,532 |
| Controllers | 277 | 1,674 | 1,716 | 1,674 | 1,711 | 1,979 | 1,497 | 742 | 11,270 |

Then, read-only, to confirm and to confirm the live rows did not change:

```sh
docker compose exec db psql -U vatpac -d vatpac -c "SELECT (SELECT count(*) FROM flight_summary WHERE session_id < 0) AS flights_imported, (SELECT count(*) FROM flight_summary WHERE session_id >= 0) AS flights_live, (SELECT count(*) FROM controller_session WHERE id < 0) AS controllers_imported, (SELECT count(*) FROM controller_session WHERE id > 0) AS controllers_live"
```

`flights_live` and `controllers_live` must be at least the first block of the dry run (live rows written meanwhile by the collector or writer may raise them; none may be missing). Open two or three dashboards (a retention panel and a pilots total) and check that history starts 24-08-2025 and the gap shows as empty, as section 0 says. Check the daily checks and the watchdog alerts stay quiet ([daily-checks.md](daily-checks.md), [integrity-checks.md](integrity-checks.md)); the integration test covers the invariant sweep and the drop gate on the throwaway database, not the live one.

## 7. Roll back

The whole definition of "imported" is `flight_summary.session_id < 0` and `controller_session.id < 0` (live ids are positive). The same script removes exactly those rows, in one transaction, and checks that none are left and the live counts are unchanged:

```sh
sh ops/v1_import/load.sh --expect-db vatpac --rollback            # dry run
sh ops/v1_import/load.sh --expect-db vatpac --rollback --apply    # remove them
```

It needs no CSV files. If it fails on a foreign key, a row exists in an event table for an imported session: stop and look, do not clear it. If the live data is damaged, restore from the dump of step 1 ([restore.md](restore.md)).

## 8. Delete the CSV files afterwards

The files hold names and CIDs. When the load is verified or abandoned, delete the whole extract folder on **both** machines (the PC and the VM), including `SHA256SUMS` and `EXPECTED_COUNTS`: `rm -r /path/to/v1_extract`. Do not leave a copy in the repository, a backup or a shared folder. Re-extract if a later run is needed.

## Rehearsal (no production)

Use a throwaway Postgres whose database name ends in `_test` ([run-tests.md](run-tests.md)); never a copy of production. Synthetic files, never real rows, come from `tests/tools/v1_import_synth.py`:

```sh
python tests/tools/v1_import_synth.py generate /tmp/v1_synth --full     # the real monthly counts, invented rows
V1_IMPORT_PSQL="psql -X -q -v ON_ERROR_STOP=1 '$VATPAC_TEST_DSN'" sh ops/v1_import/load.sh --expect-db vatpac_test --data-dir /tmp/v1_synth --apply
python tests/tools/v1_import_synth.py compare "$VATPAC_TEST_DSN" /tmp/v1_synth     # row by row against the reference mapping
```

The automated checks are in `tests/integration/test_v1_history_import.py` and `tests/test_v1_import_extract.py` ([../../tests/README.md](../../tests/README.md)).
