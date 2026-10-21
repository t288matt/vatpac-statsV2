# Runbook: backups and restore

Backups: the `backup` service writes `vatpac_<UTC stamp>.dump` (pg_dump custom format) to the `backups` named volume at `BACKUP_TIME_UTC` (default 02:00) and keeps `BACKUP_RETENTION_DAYS` (default 14) days. The dump runs as the read-only role `backup_ro` (member of `pg_read_all_data`; password `BACKUP_DB_PASSWORD`). The dump holds data and schema, not roles (the migrate job recreates roles).

Dump acceptance: a finished dump is kept as good only if `pg_restore -l` can read it, it is at least `BACKUP_MIN_SIZE_PERCENT` (50) of the newest previous dump (the first-ever dump: at least `BACKUP_MIN_FIRST_BYTES`, 1 MiB), and its table of contents holds data entries for `poll_log`, `pilot_session`, `network_snapshot`, `app_setting` and `pilot_position` (the parent or its partitions). Otherwise the backup run fails with a `FAILED content check` log line, the file is moved aside as `<name>.dump.rejected` (kept for `BACKUP_RETENTION_DAYS`, never counted as a backup, never restored automatically) and nothing is rotated; with no new `.dump` the watchdog `backup_age` alert follows at 26 h. Rotation also never deletes below `BACKUP_MIN_KEEP` (3) good dumps, so a run of failed backups cannot empty the volume. A database that really shrank by half (for example after a retention drop of old partitions) is rejected every night until the owner takes one dump with the check lowered: `docker compose exec -e BACKUP_MIN_SIZE_PERCENT=0 backup backup.sh once`. The 02:00 run still uses the container's setting.

**Not in the backup: Grafana's own data.** The `grafana_data` volume (the public dashboard links and the admin password) is NOT in `ops/backup`, which dumps the database only. To rebuild it after losing the volume: `docker compose up -d grafana`, wait about a minute, then `python3 ops/grafana_public.py --public-url https://vpac.mpengor.com` (it re-creates the 10 links). The tokens are stable (the first 32 hex characters of sha256 of `vatpac-public:` followed by the dashboard uid), so the public addresses come back unchanged. The admin password then comes from `GRAFANA_ADMIN_PASSWORD` in `.env` (first start of an empty volume). Dashboards and data sources come from git, so nothing else is lost.

Monitoring:
- The `backup` container has a healthcheck: healthy while a dump younger than about 25 h exists, or the backup loop started less than 24 h ago.
- The watchdog alerts if the newest dump is older than `WATCHDOG_BACKUP_MAX_HOURS` (26 h), if the last weekly restore test is `FAIL` (critical), and if the last successful restore test is older than `WATCHDOG_RESTORE_TEST_MAX_DAYS` (9 days, warning).

Roles: dumping needs only `backup_ro`. Restores and the restore test must `CREATE`, `DROP` and `RENAME` databases, which `backup_ro` cannot do, so `restore.sh` and `restore-test.sh` connect as the owner role through `RESTORE_PGUSER` / `RESTORE_PGPASSWORD` (set in the backup service from `POSTGRES_USER` / `POSTGRES_PASSWORD`). The nightly dump never uses the owner role.

## See what exists
```sh
docker compose exec backup ls -lh /backups
docker compose exec backup cat /backups/.last_success /backups/.last_restore_test
```
`.last_restore_test` is one line: `<UTC timestamp> OK|FAIL <detail>`. On success `restore-test.sh` prints `OK /backups/<dump> poll_log=<n> pilot_session=<n> flight_summary=<n>` (the owner's run of 13-10-2026 10:07 UTC); `flight_summary` is the table of finished flights (the check also reads dumps made before the phase 5 deploy of 13-10-2026, where that table had another name, so the figure may be absent or the dump older). A dump taken at 10:07 UTC on 13-10-2026, after the phase 4 and 5 deploy, restored and passed with `flight_summary=1797`.

## Take one now
```sh
make backup        # docker compose exec backup backup.sh once
```

## Prove a backup restores (do this routinely; it runs weekly automatically)
```sh
make backup-test   # restores the newest dump into scratch DB vatpac_restore_test, checks tables, drops it
```
The scratch database is dropped on every exit path, including failure.

## Check a dump by hand without touching the live database (the restore drill)
`make backup-test` only checks that `pilot_session`, `poll_log` and `schema_migrations` exist and are readable. For a fuller check, restore the newest dump into a scratch database inside the running Postgres container and compare it with the live one. The first such drill, 08-10-2026, passed. Repeat monthly, and after any migration that changes tables or partitioning. Run from the stack folder (`~/vatpac-statsV2` on the VM); no passwords are typed, the `sh -c` arguments are in single quotes so the container's own `$POSTGRES_USER` is used:
```sh
docker compose exec -T backup ls -la /backups                      # pick the newest .dump
docker compose exec -T backup cat /backups/<file>.dump | docker compose exec -T db sh -c 'cat > /tmp/restore_drill.dump'
docker compose exec -T db sh -c 'psql -X -q -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE vatpac_restore_test"'
docker compose exec -T db sh -c 'pg_restore --no-owner --no-acl --exit-on-error -U "$POSTGRES_USER" -d vatpac_restore_test /tmp/restore_drill.dump'
# compare: run the same read-only SELECTs (row counts, newest timestamps) against -d vatpac and -d vatpac_restore_test
docker compose exec -T db sh -c 'rm -f /tmp/restore_drill.dump; psql -X -q -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE vatpac_restore_test WITH (FORCE)"'
```
These are the options `restore-test.sh` and `restore.sh` use. Never restore into the live database name this way; use `make restore` (above), which takes a safety dump first.

## How `restore.sh` protects you
`RESTORE_CONFIRM=<target> restore.sh FILE [TARGET]` (default target `vatpac_restore`):
1. Refuses the system databases `postgres`, `template0`, `template1`, and names that are not letters, digits and underscores.
2. Requires `RESTORE_CONFIRM` to equal the target database name exactly (not `yes`).
3. Refuses if any other session is connected to the target (`pg_stat_activity`). Stop the writers first.
4. If the target exists and has tables, takes a safety dump first: `/backups/<target>_prerestore_<stamp>.prerestore`. It is not counted as a nightly backup and rotates with `BACKUP_RETENTION_DAYS`.
5. Restores into `<target>_restoring` and runs the same sanity checks as the restore test (`pilot_session`, `poll_log`, non-empty `schema_migrations`). If they fail, the live database is untouched and `<target>_restoring` is dropped.
6. Swaps with two `ALTER DATABASE ... RENAME`: `<target>` becomes `<target>_old`, then `<target>_restoring` becomes `<target>`. It will not run again while `<target>_old` exists: check it, then drop it.

## Restore into a scratch database (safe, to inspect data)
```sh
make restore FILE=/backups/vatpac_20261007T020000Z.dump TARGET=vatpac_restore CONFIRM=vatpac_restore
docker compose exec db psql -U vatpac -d vatpac_restore -c "SELECT count(*) FROM poll_log"
```
Roles are not in the dump and ownership is dropped, so the copy is owned by the owner role. That is fine for inspection.

To make a full local copy of PRODUCTION for testing (a fresh dump streamed from the VM, then this same script): [local-prod-copy.md](local-prod-copy.md).

## Disaster recovery: replace the live database
Use when the live database is lost or corrupt. Replace `vatpac` with `POSTGRES_DB` if you changed it.
1. Stop everything that connects: `docker compose stop collector grafana watchdog`. (The restore refuses if any other session remains.)
2. Restore over the live name:
   ```sh
   make restore FILE=/backups/<file>.dump TARGET=vatpac CONFIRM=vatpac
   ```
   The old database is kept as `vatpac_old`, and a safety dump of it is in `/backups/*.prerestore`.
3. Re-apply roles and grants to the new database: `docker compose up migrate` (idempotent; the dump carries no roles or ACLs).
4. Start everything: `docker compose up -d`, then `make smoke`.
5. When satisfied, drop the old copy: `docker compose exec db psql -U vatpac -d postgres -c 'DROP DATABASE "vatpac_old"'`. It occupies disk until you do.
6. Positions between the backup and the failure are lost (no backfill); note the gap.

## Restore into a brand-new volume (fresh VM or after `docker compose down -v`)
**Rebuilding a whole VM (issue 173; the method is the one used to convert the production VM on 14-10-2026, but a whole-VM rebuild itself has not been run).** Install Docker (deploy-to-vm.md section 1), then get the code as a read-only git checkout of the commit that was live (section 2d, steps 2 to 5 for the deploy key; a fresh folder needs only the clone and `git checkout --detach <commit>`), write a new `.env` with `sh ops/gen_env.sh` (the old `.env` is not in git or in the backup: keep your own copy in your password manager, or the role passwords change; `POSTGRES_PASSWORD` and `GRAFANA_ADMIN_PASSWORD` apply only at first start), and choose the mode. Local-build mode: leave the image variables unset; the images are built on the machine. Registry mode: log in (section 2e: a new classic `read:packages` token, `docker login ghcr.io`), add the two `VATPAC_IMAGE_*` lines for the live commit, and use `docker compose pull` instead of a build. The commit must have been published, otherwise use local-build mode. Then continue with the steps below. Dumps are not in git: bring the newest dump from the off-box copy.
1. `docker compose up -d db` then `docker compose up migrate` (creates empty schema and roles). In registry mode run `docker compose pull` first and add `--no-build` to the `up` commands.
2. Start the `backup` service, then copy the dump in: `docker compose cp ./vatpac_<stamp>.dump backup:/backups/`.
3. Follow "replace the live database" from step 1 (the empty live database is not backed up first because it has tables from migrate; the safety dump is just tiny).

## Off-box copies
The `backups` volume lives on the same disk as the database, so a dead disk loses both. Set `BACKUP_REMOTE` in `.env` to run a command after every successful nightly dump. The new file is in `$BACKUP_FILE`. Default empty = off. A failure is logged (`BACKUP_REMOTE FAILED`) but never fails the backup; success writes `/backups/.last_remote_success`.

The backup image has no copy tools. Add them at build time with `BACKUP_EXTRA_APT_PACKAGES` (space separated Debian packages) and rebuild (`docker compose up -d --build backup`). This works only in local-build mode. The production VM is in registry mode since 14-10-2026: the published backup image is pulled, `BACKUP_EXTRA_APT_PACKAGES` is ignored, and `--build` must not be used there, so the tools would be missing. Adding them to the published image or choosing another copy method is an open owner decision (OWNER_BACKUP_COPY.md):
```sh
# rclone to a remote you configured (mount the config via a compose override, or use env-based RCLONE_CONFIG_* variables)
BACKUP_EXTRA_APT_PACKAGES=rclone
BACKUP_REMOTE=rclone copyto "$BACKUP_FILE" remote:vatpac-backups/

# rsync over ssh (mount a key and known_hosts via a compose override; do not bake keys into the image)
BACKUP_EXTRA_APT_PACKAGES=rsync openssh-client
BACKUP_REMOTE=rsync -e "ssh -i /run/keys/id -o StrictHostKeyChecking=yes" "$BACKUP_FILE" backup@host:/srv/vatpac/
```
Note the command runs through `sh -c`, so keep `$BACKUP_FILE` quoted. Credentials never go in the repo or in `.env.example`. Manual copy, without the hook:
```sh
docker run --rm -v vatpac_backups:/b -v "$PWD":/out alpine sh -c 'cp /b/*.dump /out/'
```
then rsync/rclone them elsewhere. Dumps contain real names and CIDs: treat them as personal data and encrypt them at the destination.
