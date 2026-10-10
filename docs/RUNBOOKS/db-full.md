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

## 5. The first live partition drop (rehearsal of 10-10-2026, issue #98)

The first raw-position drop on the live system is due about 06-01-2027 (180 days, ADR-026). It was rehearsed on a throwaway Postgres 16.15 (own cluster, 1 GB memory cap, 1 CPU) with 230 daily partitions of about 135,000 positions each (31 million rows, 3.3 GB, 120,000 sessions: the live figure of 09-10-2026 is about 140,000 positions and 400 to 500 sessions a day), a writer that mimicked the collector (update `pilot_session`, insert into `pilot_position`, commit, every 0.2 s, `lock_timeout` 5 s) and a read-only reader. The maintenance session used the `collector` role with `lock_timeout` 5 s and `statement_timeout` 15 min, as `Collector._maintain` does.

| Case | Function | Result |
|---|---|---|
| One partition due (retention 228 days) | previous | dropped 1, 520 sessions finalised (summary rows final 0 to 520), 48 s on a busy 1 CPU box (22 s idle: the finalisation dominates); the writer stalled once for 1.4 s, no error |
| Pinning at the cutoff (retention 180, a session with positions on days -181, -180 and -179) | previous and new | the day-181 partition is kept (pinned by the session), the day 180 is not yet due; the oldest partition left is the pinned one |
| Catch-up of 48 partitions (retention 180) | previous | dropped 48, all finalised (520 to 25,480 final summary rows), 169 s; for about 2 minutes (09:15:08 to 09:17:09) EVERY writer commit failed on its 5 s lock timeout (24 failed polls in a row at one poll per 5 s) and every reader of the views touching `pilot_position` was blocked until its 30 s statement timeout. Cause: the first `DROP` took ACCESS EXCLUSIVE on `pilot_position` and the single transaction held it while it finalised the remaining 47 partitions |
| Same catch-up | new (migration 20261009000170) | dropped 48, identical end state (237 to 189 partitions, same final summary rows, same pinned partition), 0 writer errors; the writer stalled 3 times for under 1 s while the 48 drops ran back to back in about 3 s at the end |

So an ordinary night (one partition) was fine, but a catch-up run, for example after the collector had been stopped for days or after lowering `POSITION_RETENTION_DAYS`, would have stopped polling for minutes. Migration 20261009000170 reorders the function (finalise everything first, drop at the end in one short block, 1 second lock timeout and three attempts per partition, a busy partition is kept and retried the next night instead of failing the whole call); results, signature, pinning and finalise rules are unchanged and are compared with the previous function in `tests/integration/test_partition_drop_locks.py`.

What to expect at the first drop: the maintenance log line `maintenance finished` shows `partitions_dropped` (normally 1). Finalising a day takes about 20 s at idle (longer on a busy box) and holds the flight-summary advisory lock, so the 5 minute summary refresh (20 s statement timeout) can fail once with a logged `flight summary refresh failed`; it recovers on the next cycle. A catch-up of more than about 150 partitions in one call would hold the lock for longer than the 5 s writer timeout (drops take about 60 ms each): lower the number due per call by running `SELECT drop_old_pilot_position_partitions(<larger retention>)` stepwise, for example 250, then 220, then 180.
