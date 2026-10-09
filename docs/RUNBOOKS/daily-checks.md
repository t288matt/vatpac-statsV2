# Runbook: daily and weekly checks, and the soak report

Last reviewed: 08-10-2026. All times are UTC. Run the commands from the stack folder: `~/vatpac-statsV2` on the production VM (reach it with `ssh vatpac-v3`). Read the clock on the VM with `date -u`; do not estimate times.

The watchdog already alerts by Telegram on the worst failures ([alerts.md](alerts.md)). These checks catch what it does not, and prove the system is healthy rather than merely quiet.

## Daily (about two minutes)

1. **Containers.**
   ```sh
   docker compose ps
   ```
   Expect `db`, `collector`, `grafana`, `backup` and `watchdog` all `healthy`, and `migrate` as `exited (0)`. Anything restarting, unhealthy or missing: see [stuck-collector.md](stuck-collector.md).
2. **Smoke test.**
   ```sh
   sh ops/smoke.sh
   ```
   Expect `PASS` for every check and `SMOKE PASS` at the end (it checks container health, the collector `/health`, a recent `ok` poll, Grafana's own health, and that the Grafana role cannot write). It waits up to four minutes for containers to settle, so it is also safe straight after a deploy.
3. **Freshness.**
   ```sh
   docker compose exec -T db sh -c 'psql -X -P pager=off -x -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT * FROM data_freshness"'
   ```
   Expect `snapshot_age_seconds` under about 30, `last_ok_age_seconds` under about 30, `ok_last_hour` close to 240 (one per 15 seconds), and `error_last_hour` at or near 0. `skipped_last_hour` counts polls where the feed had not changed and is normal. (The same view feeds the Grafana dashboard "Monitoring: collector and data freshness".)
4. **Watchdog verdict.**
   ```sh
   docker compose exec -T watchdog cat /var/watchdog/watchdog_status.json
   ```
   Expect `"overall": "ok"` and an `updated` time within the last couple of minutes. Any `warn` or `crit` check is named in the file; [alerts.md](alerts.md) says what each means.

## Weekly (about ten minutes)

1. **Backups.**
   ```sh
   docker compose exec backup ls -lh /backups
   docker compose exec backup cat /backups/.last_success /backups/.last_restore_test
   ```
   Expect a dump from the last night (taken at `BACKUP_TIME_UTC`, 02:00 by default), up to `BACKUP_RETENTION_DAYS` (14) dumps, and a `.last_restore_test` line ending `OK` that is under 9 days old (the automated restore test runs after the Sunday backup, so that file does not exist until the first Sunday after a fresh install). If there is no off-box copy yet, say so: dumps on the same disk are lost with the VM (see [restore.md](restore.md), "Off-box copies").
2. **Disk and database size.**
   ```sh
   df -h /var/lib/docker
   docker compose exec db psql -U vatpac -d vatpac -c "SELECT pg_size_pretty(pg_database_size('vatpac'))"
   ```
   Steady state is about 2 to 3 GB for 90 days of positions. Trend upwards means [db-full.md](db-full.md).
3. **Partitions are rolling.** `docker compose exec db psql -U vatpac -d vatpac -c '\dt pilot_position_*'` should show days from today minus the retention up to a week ahead.
4. **Near-miss callsigns.** `docker compose exec db psql -U vatpac -d vatpac -c "SELECT * FROM near_miss_controllers"`. Any row is a controller position missing from the whitelist: [update-reference-files.md](update-reference-files.md).
5. **Dashboards still answer.** `python3 ops/check_panels.py` (on the VM, from the stack folder). Expect `0 errors`; empty panels are fine while data is young.
6. **Open items.** Skim [../TRACKER.md](../TRACKER.md) for anything waiting on a date, and the GitHub Dependabot pull requests (tracker T9).

(`vatpac` in the commands above is the default value of `POSTGRES_USER` and `POSTGRES_DB`; use the values from `.env` if you changed them.)

## The soak report

The soak report answers the acceptance question "has the collector stored a snapshot about every 15 seconds for 24 hours?" It reads `poll_log` only and changes nothing.

```sh
sh ops/soak_report.sh 24          # or: make soak HOURS=24; the argument is a whole number of hours
```

The output is one record followed by one line per container with its restart count and start time.

| Field | Meaning |
|---|---|
| `first_snapshot`, `last_snapshot` | The first and last snapshot stored inside the window (UTC). |
| `hours_covered`, `hours_requested` | Span between them, and the window you asked for. |
| `snapshots_stored`, `snapshots_expected` | Distinct snapshots stored, and one per 15 seconds over the span. |
| `coverage_pct` | Stored divided by expected. |
| `gaps_over_limit`, `longest_gap` | Count of gaps over 2.5 poll intervals between consecutive snapshots, and the longest gap. |
| `p95_cadence_s` | 95th percentile of the gap between consecutive snapshots (normally 15). |
| `error_polls`, `skipped_polls` | Polls that failed, and polls where the feed had not changed (normal). |
| `verdict` | See below. |

Verdicts:

- `PASS`: the window is covered (at least 98% of the hours requested), at least 99% of the expected snapshots are stored, and the p95 cadence is 17 seconds or less.
- `INCOMPLETE: only N h covered of M h requested`: not enough history yet. This is never a pass. Wait, then run it again.
- `FAIL: coverage below 99%` or `FAIL: p95 cadence above the poll interval + 2 s`: too many snapshots missing or the cadence is slow. Look at `gaps_over_limit` and `longest_gap`, then at what happened at that time (a deploy, a restart, a feed outage: `docker compose logs --since <time> collector`).
- `FAIL: no snapshots in the window`: nothing was stored; the collector is down or the database is empty.

Notes for reading it:

- The 99% threshold tolerates about 14 minutes of missing snapshots in 24 hours. A single 45-second gap (a collector restart) still passes. The project's own plan, however, restarted the soak clock after any gap so that the final run is clean ([../RESUME.md](../RESUME.md), section 1); decide with the owner which standard applies before announcing the goal met (see [../DOC_AUDIT.md](../DOC_AUDIT.md), open question 1).
- Restarts and rebuilds inside the window create gaps. Do not deploy during a soak unless you must ([deploy-to-vm.md](deploy-to-vm.md)); say so if you do.
- A restart count above 0 on a container is not a failure by itself; correlate it with the gaps.
- When the verdict is `PASS` for a full 24 hours, record the whole output in [../ARCHITECTURE.md](../ARCHITECTURE.md), section 10, together with the reboot test, CI and Telegram evidence, and tell the owner. Never record a result that was not produced in that session.
