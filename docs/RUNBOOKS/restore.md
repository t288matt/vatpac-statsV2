# Runbook: backups and restore

Backups: the `backup` service writes `vatpac_<UTC stamp>.dump` (pg_dump custom format) to the `backups` named volume at `BACKUP_TIME_UTC` (default 02:00) and keeps `BACKUP_RETENTION_DAYS` (default 14) days. The watchdog alerts if the newest dump is older than 26 h. The dump holds data and schema, not roles (the migrate job recreates roles).

## See what exists
```sh
docker compose exec backup ls -lh /backups
docker compose exec backup cat /backups/.last_success /backups/.last_restore_test
```

## Take one now
```sh
make backup        # docker compose exec backup backup.sh once
```

## Prove a backup restores (do this routinely; it runs weekly automatically)
```sh
make backup-test   # restores the newest dump into scratch DB vatpac_restore_test, checks tables, drops it
```

## Restore into a scratch database (safe, to inspect data)
```sh
make restore FILE=/backups/vatpac_20261007T020000Z.dump TARGET=vatpac_restore
docker compose exec db psql -U vatpac -d vatpac_restore -c "SELECT count(*) FROM poll_log"
```

## Disaster recovery: replace the live database
Use when the live database is lost or corrupt.
1. Stop writers: `docker compose stop collector grafana watchdog` (the watchdog would otherwise restart things).
2. Restore over the live DB (destructive; needs explicit confirmation):
   ```sh
   docker compose exec -e RESTORE_CONFIRM=yes backup restore.sh /backups/<file>.dump vatpac
   ```
   (replace `vatpac` with `POSTGRES_DB`).
3. Re-apply roles and grants: `docker compose up -d migrate` (idempotent).
4. Start everything: `docker compose up -d`, then `make smoke`.
5. Positions between the backup and the failure are lost (no backfill); note the gap.

## Restore into a brand-new volume (fresh VM or after `docker compose down -v`)
1. `docker compose up -d db` then `docker compose up migrate` (creates empty schema and roles).
2. Copy the dump onto the new host, then into the backup container:
   `docker compose cp ./vatpac_<stamp>.dump backup:/backups/` (start the `backup` service first).
3. Follow "replace the live database" from step 2.

## Off-box copies
The `backups` volume lives on the same disk as the database. Copy dumps off the VM regularly:
```sh
docker run --rm -v vatpac_backups:/b -v "$PWD":/out alpine sh -c 'cp /b/*.dump /out/'
```
then rsync/rclone them elsewhere. Dumps contain real names and CIDs: treat them as personal data.
