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
3. Drop old raw positions only if still short. Prefer the maintenance function, which finalises the flights of the dropped days first so their numbers survive: `docker compose exec db psql -U vatpac -d vatpac -c "SELECT drop_old_pilot_position_partitions(60)"` (drops up to 10 partitions older than 60 days per call: repeat it until it returns 0; use a smaller number to free more). Last resort, oldest first: `DROP TABLE pilot_position_YYYYMMDD;` (raw positions only; sessions are kept, but a hand-made drop skips the finalising step and can change the numbers of recent flights).
4. Grow the disk if the steady state has outgrown it, then raise `WATCHDOG_DISK_PCT` only if the new size warrants it.

## 4. Afterwards
Check `make smoke`, note the data gap (UTC) if the collector could not write, and review retention settings.

## 5. The first live partition drop (rehearsal of 10-10-2026, issue #98)

The first raw-position drop on the live system is due about 06-01-2027 (180 days, ADR-026). It was rehearsed on a throwaway Postgres 16.15 (own cluster, 1 GB memory cap, 1 CPU) with 230 daily partitions of about 135,000 positions each (31 million rows, 3.3 GB, 120,000 sessions: the live figure of 09-10-2026 is about 140,000 positions and 400 to 500 sessions a day), a writer that mimicked the collector (update `pilot_session`, insert into `pilot_position`, commit, every 0.2 s, `lock_timeout` 5 s) and a reader. The maintenance session used the `collector` role with `lock_timeout` 5 s and `statement_timeout` 15 min, as `Collector._maintain` does.

**Findings with the original function (migration 20261007000021)**

| Case | Result |
|---|---|
| One partition due (retention 228 days) | dropped 1, 520 sessions finalised, 48 s on a busy 1 CPU box (22 s on an idle repeat); the writer stalled once for 1.4 s, no error |
| Pinning at the cutoff (retention 180, a session with positions on days -181, -180 and -179) | the day-181 partition is kept (pinned), the day 180 is not yet due |
| Catch-up of 48 partitions (retention 180) | dropped 48, all finalised (520 to 25,480 final summary rows), 169 s; for about 2 minutes EVERY writer commit failed on its 5 s lock timeout (24 consecutive failures) and every reader of the views touching `pilot_position` was blocked until its 30 s statement timeout. Cause: a `DROP` of a partition takes ACCESS EXCLUSIVE on the parent (before it waits for the partition), and the single transaction held it while it finalised the remaining 47 partitions |

**The fix (migration 20261009000170, plus a loop in `maintenance.drop_old_partitions`).** The function now (1) finalises at most 10 partitions per call, (2) re-checks each candidate with no lock held (no session seen again inside the retention window, every session still has a final summary, no position newer than the final summary of its session), (3) drops them in one short block: per partition `LOCK ... NOWAIT` first, so a busy partition (a reader on it) is detected without touching the parent and nobody queues behind the drop, then a cheap re-check, then `DROP TABLE IF EXISTS` with a 1 second lock timeout (it only waits for in-flight writer transactions). Until the first drop has succeeded a busy partition is retried 3 times, half a second apart (no parent lock is held then, so sleeping stalls nobody); after the first drop the parent is locked until the call ends, so there is no sleeping or waiting at all and a busy partition is skipped at once. A skipped partition is kept with a NOTICE and dropped by the next run; the function no longer raises on it, so the caller's commit keeps the finalised summaries. The collector calls the function in a loop until it drops nothing, so a catch-up is a series of short transactions and the parent lock ends with each call (about 60 ms per drop, so at most about 1 second per call). Pinning, the finalise rule, the signature and the return value are unchanged; `tests/integration/test_partition_drop_locks.py` compares the new function with the previous one on the same data (retention 90 and 180).

**Before and after, busy partitions** (`tests/scale/measure_partition_drop.py`: four partitions due, the oldest k with a reader holding a lock on them; a writer inserting every 50 ms with a 5 s lock timeout; the "before" function is the first version of this migration, which retried inside the locked transaction):

| Case | Before: dropped, call time, writer waits over 0.5 s, worst wait | After: dropped, call time, writer waits over 0.5 s, worst wait |
|---|---|---|
| 0 busy | 4, 0.2 s, 0, 0.01 s | 4, 0.3 s, 0, 0.01 s |
| 1 busy | 3, 4.2 s, 3, 0.96 s | 3, 2.4 s, 0, 0.01 s |
| 2 busy | 2, 8.2 s, 6, 0.99 s | 2, 4.4 s, 0, 0.01 s |
| 3 busy | 1, 12.2 s, 9, 1.00 s | 1, 6.4 s, 0, 0.03 s |
| 48 due, one reader holding the 20th partition for 40 s | 47, 7.7 s, 1, 4.00 s | 47 (in 5 calls), 10.3 s, 0, 0.10 s |

No writer error in any case (the stalls stay under the 5 s writer timeout, which is what the two busy, long-running reader cases in the rehearsal exceeded with bigger data and slower finalisation). The after column never waits more than 0.1 s.

**What to expect at the first drop.** The maintenance log line `maintenance finished` shows `partitions_dropped` (normally 1). Finalising a partition took 22 to 48 s in the single-partition runs (520 sessions, 1 CPU box, a cold cache straight after loading 3.3 GB into a 1 GB container) but about 3.5 s each on average in the 48-partition catch-up; the difference is not explained (the likeliest cause is the cold cache), so plan for up to about 50 s per partition on the live box. A call finalises at most 10 partitions, so one call is at most about 8 minutes, under the collector's 15 minute `statement_timeout`; if a call ever did exceed it, it would roll back including its finalisation, so step the retention down in stages (for example 250, then 220, then 180) instead of one big jump. The flight-summary advisory lock is held until the end of the whole call (up to 10 partitions, so up to about 8 minutes), not only while one partition is being finalised, so the 5 minute summary refresh (20 s statement timeout) can fail on every cycle for that whole time with a logged `flight summary refresh failed` and recovers on the next cycle. Running the function by hand drops at most 10 partitions per call: repeat the `SELECT` until it returns 0.
