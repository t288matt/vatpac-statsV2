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
| Old `pilot_position_*` partitions not dropped | `\dt pilot_position_*` shows days older than `POSITION_RETENTION_DAYS` | Check collector maintenance logs. A partition is kept while it holds an open session. Lower `POSITION_RETENTION_DAYS` in `.env` and `docker compose up -d collector`. |
| Docker logs | `du -sh /var/lib/docker/containers/*/*-json.log` | Rotation is 10 MB x 5 per container, so this should be small. |
| Backups | `docker compose exec backup du -sh /backups` | Lower `BACKUP_RETENTION_DAYS`; copy dumps off-box first (`restore.md`). |
| Unused images and build cache | `docker system df` | `docker image prune -f` and `docker builder prune -f`. Never prune volumes. |
| WAL growth | `docker compose exec db du -sh /var/lib/postgresql/data/pg_wal` | Normally capped near `max_wal_size` (2 GB). |
| Bloat | table size much larger than row count suggests | Plain `VACUUM (VERBOSE) <table>` does not shrink files; schedule a maintenance window for `VACUUM FULL` (locks the table). |

## 3. If Postgres is already down for lack of space
1. Free space first with the safe items above (images, build cache, old dumps), not by deleting anything under the Postgres volume.
2. `docker compose up -d db`, wait for healthy, then `docker compose up -d`.
3. Drop the oldest daily partitions by hand only if still short, oldest first:
   `DROP TABLE pilot_position_YYYYMMDD;` (raw positions only; sessions are kept).
4. Grow the disk if the steady state has outgrown it, then raise `WATCHDOG_DISK_PCT` only if the new size warrants it.

## 4. Afterwards
Check `make smoke`, note the data gap if the collector could not write, and review retention settings.
