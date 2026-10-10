# Runbook: database or disk full

Symptoms: watchdog `WARN/CRIT disk`, `WARN db_size`, Postgres log "No space left on device", collector write errors, Postgres in recovery loops.

## 1. Measure
```sh
df -h /var/lib/docker
docker system df -v | head -40
docker compose exec db psql -U vatpac -d vatpac -c "SELECT pg_size_pretty(pg_database_size('vatpac'))"
docker compose exec db psql -U vatpac -d vatpac -c \
  "SELECT relname, pg_size_pretty(pg_total_relation_size(c.oid)) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p','m') ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 15"
```
Expected steady state: about 2 to 3 GB for 90 days of positions. Much more means partitions are not being dropped.

## 2. Usual culprits, cheapest first
| Culprit | Check | Fix |
|---|---|---|
| Old `pilot_position_*` partitions not dropped | `\dt pilot_position_*` shows days older than `POSITION_RETENTION_DAYS` | Check the collector logs for `maintenance` lines (`docker compose logs collector`). A partition is kept while it holds positions of a session seen within the retention window. Lower `POSITION_RETENTION_DAYS` in `.env` (minimum 2) and `docker compose up -d collector`; maintenance runs at start-up. |
| Docker logs | `du -sh /var/lib/docker/containers/*/*-json.log` | Rotation is 10 MB x 5 per container, so this should be small. |
| Backups | `docker compose exec backup du -sh /backups` | Lower `BACKUP_RETENTION_DAYS`; copy dumps off-box first (`restore.md`). |
| Leftover restore databases and safety dumps | `docker compose exec db psql -U vatpac -d postgres -c "\l"` shows `*_old`; `ls /backups/*.prerestore` | `DROP DATABASE "<name>_old"` once the restore is verified (`restore.md`). Safety dumps rotate with `BACKUP_RETENTION_DAYS`. |
| Unused images and build cache | `docker system df` | `docker image prune -f` and `docker builder prune -f`. Never prune volumes. |
| WAL growth | `docker compose exec db du -sh /var/lib/postgresql/data/pg_wal` | Normally capped near `max_wal_size` (2 GB). |
| Bloat | table size much larger than row count suggests | Plain `VACUUM (VERBOSE) <table>` does not shrink files; schedule a maintenance window for `VACUUM FULL` (locks the table). |

## 3. If Postgres is already down for lack of space
1. Free space first with the safe items above (images, build cache, old dumps), not by deleting anything under the Postgres volume.
2. `docker compose up -d db`, wait for healthy, then `docker compose up -d`.
3. Drop old raw positions only if still short. Prefer the maintenance function, which finalises the flights of the dropped days first so their numbers survive: `docker compose exec db psql -U vatpac -d vatpac -c "SELECT drop_old_pilot_position_partitions(60)"` (drops partitions older than 60 days; use a smaller number to free more). Last resort, oldest first: `DROP TABLE pilot_position_YYYYMMDD;` (raw positions only; sessions are kept, but a hand-made drop skips the finalising step and can change the numbers of recent flights).
4. Grow the disk if the steady state has outgrown it, then raise `WATCHDOG_DISK_PCT` only if the new size warrants it.

## 4. Afterwards
Check `make smoke`, note the data gap (UTC) if the collector could not write, and review retention settings.
