# Runbook: make a local copy of production (owner only)

Last reviewed: 21-10-2026, the day it was first done. Use it to try a change, rehearse a migration or check a query against real data and real volumes on the owner's PC. It never changes production: the dump is read-only and the restore happens only on the local stack.

**The copy holds personal data** (real cids and names). Keep it on the owner's PC, do not share it, and delete it when you are finished (step 6). The dump is streamed straight into a Docker volume, so no file with real data lands on the PC's disk outside Docker.

**Never run the integration tests against this copy.** They `TRUNCATE` every data table and would empty it. Use a separate throwaway database for tests ([run-tests.md](run-tests.md) section 1b).

You need a local stack installed as in [../INSTALL.md](../INSTALL.md) (local-build mode, healthy), Docker Desktop running, and the ssh alias `vatpac-v3`. On Windows run everything in Git Bash and note `MSYS_NO_PATHCONV=1` below: without it Git Bash rewrites the `/backups/...` path.

## 1. Take a fresh dump from production, into the local backup container
Read-only (`pg_dump` as the `backup_ro` role); nothing is written on the VM. A dump of about 20 MB takes seconds.
```sh
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ssh vatpac-v3 'cd ~/vatpac-statsV2 && docker compose exec -T backup timeout 180 pg_dump -Fc --no-password' \
  | docker compose exec -T backup sh -c "cat > /backups/prod_copy_${STAMP}.dump"
MSYS_NO_PATHCONV=1 docker compose exec -T backup pg_restore -l /backups/prod_copy_${STAMP}.dump | head -3
```
Check: `pg_restore -l` prints an archive header (`Archive created at ...`, `dbname: vatpac`). An empty or missing listing means the stream failed: do not restore.

## 2. Stop what writes to the local database
```sh
docker compose stop collector grafana watchdog
```
`restore.sh` refuses to run while anything else is connected to the target.

## 3. Restore with the project's own script
```sh
MSYS_NO_PATHCONV=1 docker compose exec -T -e RESTORE_CONFIRM=vatpac backup restore.sh /backups/prod_copy_<stamp>.dump vatpac
```
It takes a safety dump of the current local database, restores into `vatpac_restoring`, runs the same sanity checks as the weekly restore test, and only then swaps by renaming: the previous local database stays as `vatpac_old`. Nothing is overwritten until you drop it ([restore.md](restore.md) "How `restore.sh` protects you"), and it will not run again while a `vatpac_old` exists: drop it first (step 6). On 21-10-2026 this took a few seconds.

## 4. Re-apply the roles and start again
Roles and their passwords are not in a dump, so run the migrate job once more (it re-applies `db/roles.sql` and the watchdog grants, and finds no new migration), then start everything:
```sh
docker compose up migrate
docker compose up -d
```

## 5. Check the copy
```sh
sh ops/smoke.sh                       # expect SMOKE PASS
python3 ops/check_panels.py           # expect 0 errors; with real data nearly every panel has rows
{ echo "SET default_transaction_read_only = on;"; cat ops/audit/invariants.sql; } | docker compose exec -T db psql -X -U vatpac -d vatpac -At -F'|'
```
The invariant pack prints one row per invariant (name, violations, detail) between `BEGIN` and `COMMIT`; every violation count should be 0, as on production. To compare row counts, run the same `SELECT count(*)` list on the copy and on production (`ssh vatpac-v3 'cd ~/vatpac-statsV2 && docker compose exec -T db psql -X -U vatpac -d vatpac -At'`, read-only). The tables that only grow differ by a few rows, because production keeps polling and so does the local collector; the lookup and event tables match exactly. The structure, the three flight-writer functions and the 29 migrations match production exactly ([../INSTALL.md](../INSTALL.md) section 5b).

## 6. Clean up
Drop the old local database once you are satisfied, and delete the dump and the safety dump from the backups volume (they hold real data too):
```sh
docker compose exec -T db psql -X -U vatpac -d postgres -c 'DROP DATABASE "vatpac_old"'
MSYS_NO_PATHCONV=1 docker compose exec -T backup sh -c 'rm -f /backups/prod_copy_<stamp>.dump /backups/vatpac_prerestore_*.prerestore'
```
The restored database itself stays on the PC until you remove the volume (`docker compose down -v` deletes all of the stack's volumes; see INSTALL.md section 4).

## What to expect
A fresh restore of 21-10-2026 (a 21.7 MB dump) passed `restore.sh`'s own sanity checks, `ops/smoke.sh`, the panel check (0 errors) and all of the integrity invariants. The local collector keeps polling the public feed after the restore, so the copy is not frozen at the dump time.
