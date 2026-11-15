# Runbook: ATC stats loader

The loader is the container `vatsim-atc-stats`. It keeps every ATC session on the VATSIM network (all callsigns, the whole network) in the table `atc_stats_session`, for the number of years set by `STATS_RETENTION_YEARS`. Table columns: [DATA_DICTIONARY.md](../DATA_DICTIONARY.md), section 2. Settings and their defaults: [CONFIG_MAP.md](../CONFIG_MAP.md), part A1. Alert wording: [alerts.md](alerts.md), check `stats_loader`. All times are UTC.

## 1. What the loader does

Once a day, at `STATS_RUN_AT` (06:00 UTC by default), it asks VATSIM's public history API for the ATC sessions that started on each UTC day it has not stored yet, one day at a time, and writes each day to `atc_stats_session` in one transaction. A session that VATSIM has closed is stored with its end time and its counts; a session that is still open is stored without an end and is updated by a later run. At the end of a run with no failed day it deletes sessions older than the retention period, and it records every run in `atc_stats_run_log`.

## 2. See what it is doing

The newest runs, with the query run from the VM in `~/vatpac-statsV2`:

```sh
docker compose exec db sh -c 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT id, started_at, finished_at, status, days_loaded, requests, last_day_at, error FROM atc_stats_run_log ORDER BY id DESC LIMIT 5;"'
```

`days_loaded` counts the days written in the run, `requests` the requests it made, and `last_day_at` the time it last wrote a day. The `failed_days` column lists the dates that could not be written.

| Status | Meaning | Alert |
|---|---|---|
| `running` | A run is in progress. A run that has written a day in the last 36 hours counts as working. | None while the run is working |
| `success` | Every day was written or confirmed empty, and retention ran. | None |
| `failed` | The run could not write one or more days, or the API stopped it. `error` says why. Retention does not run in a failed run. | WARN |
| `stopped` | The container received its stop signal (a restart or a deploy). | None |
| `interrupted` | The row was still `running` when the loader started again, so the earlier run crashed or was killed. | WARN |

**A day that cannot be read.** A day that keeps failing its checks, even as one-hour windows, is not written. The run skips it and carries on with the next day, and the run ends `failed` with that date in `failed_days` and the reason in `error`. Three failed days in a row stop the run at once. Retention does not run after a failed run. The WARN stays until a later run finishes successfully, which happens when the day reads cleanly or falls outside the retention period. There is no list for skipping a known-bad date. The maintainer checks the run's row in the run log, reads the log lines for that date, and checks whether VATSIM's history for that date has changed.

The container's health (`docker compose ps vatsim-atc-stats`) is `healthy` when a run succeeded in the last 36 hours or a run has written a day in that time.

The log shows one line for each day written (`written:`) and one for each day that could not be written (`not written:`, with the reason): `docker compose logs --tail=100 vatsim-atc-stats`.

## 3. The first run

Every run is the same kind of run. A run loads the recent days first: the last three days on an empty database, and afterwards the days from two days before the newest stored date (`STATS_OVERLAP_DAYS`) up to yesterday. Then it loads each older date that has no sessions stored, newest first, back to the start of the retention period. A date with no sessions is confirmed on all 24 hours and recorded as empty, so it is not asked for again. When a run finds an empty date older than every stored date, it stops searching, because that is the start of VATSIM's history.

The first run therefore has the most dates to load. At the default rate of one request every 5 seconds, it is estimated at about 29 hours for 3 years of history and about 8 days for 15 years. Progress shows in `days_loaded`, in `last_day_at` moving forward, and in the log lines. If a run is still going at the next `STATS_RUN_AT`, that time is skipped: the following run starts at the first `STATS_RUN_AT` after the run ends.

## 4. Alerts and what to do

The watchdog reads the run log every minute. The exact wording is in [alerts.md](alerts.md).

- **WARN: the newest finished run is `failed` or `interrupted`.** Read the run's `error` and the log lines for its days. A failed run leaves its missing days missing, so the next run tries them again; nothing is lost. If `error` says HTTP 429 or server errors, wait: VATSIM is limiting or failing the requests. If it names a date, read the log line for that date. An `interrupted` run means the container was killed or crashed; `docker compose ps` shows whether it restarted itself.
- **CRIT: no successful run and no run writing days for 36 hours.** Check `docker compose ps vatsim-atc-stats` and `docker compose logs --tail=100 vatsim-atc-stats`. If the container is not running, start it (section 5). If it runs but writes nothing, read the log for the reason.
- **No alert for `stopped`.** A clean stop, such as a deploy or a restart, is not a problem.
- **A new install** has an empty run log. The check stays OK for 36 hours after the watchdog's first start.

## 5. Stop, start, restart and run again

- Stop: `docker compose stop vatsim-atc-stats`. The run ends `stopped`. Each day is written in one transaction, so a stop loses at most the day in progress, and the next run loads it.
- Start: `docker compose start vatsim-atc-stats`. Restart: `docker compose restart vatsim-atc-stats`. Each start marks any row still `running` as `interrupted`.
- Run again without waiting: there is no separate command. After a start, the loader waits 60 seconds before its first request, and it starts a run then, unless a run has already finished successfully on the same UTC date. If one has, the next run is at the next `STATS_RUN_AT`.
- Change the daily time: set `STATS_RUN_AT=HH:MM` (UTC) in `.env`, then `docker compose up -d vatsim-atc-stats`.
- Before a restore of the database, stop the loader: `docker compose stop vatsim-atc-stats`. `ops/backup/restore.sh` refuses to run while any session is connected to the database, and the loader holds one. Start it again after the restore ([restore.md](restore.md)).

## 6. Retention: how long sessions are kept

The number of years is `STATS_RETENTION_YEARS` in `docker-compose.yml`, under `vatsim-atc-stats`. It is a plain whole number of at least 1 and is not read from `.env`. Change it as a policy value, through a pull request and a deploy ([CONFIG_MAP.md](../CONFIG_MAP.md), "Where each setting lives").

- **Lowering it** deletes the older sessions at the end of the next run with no failed day. The cut-off is the start of a whole UTC date, and the delete runs in batches of 10,000 rows. There is no confirmation step.
- **Raising it** makes the next run load the extra years. Going from 3 to 15 years adds about 6 days of loading at the default rate (an estimate).

## 7. Rate, timeouts and errors

- The loader makes one request at a time, and never sooner than 5 seconds after the previous one, whatever the response. `STATS_MIN_INTERVAL_SECONDS` can make the gap longer; the loader never goes below 5 seconds, so a smaller value is raised to 5.
- A request with no answer within 30 seconds, or HTTP 504, means the window is too long for that age. The day is loaded as 24 one-hour windows instead. This is not a failure, but if one of those windows also times out, the day is not written.
- HTTP 429 (VATSIM is limiting requests) stops the run at once. It ends `failed` with `HTTP 429 from the API`, and the next scheduled run carries on.
- Three server errors (HTTP 5xx or no connection) in a row stop the run and end it `failed`.
- A day that cannot be read cleanly is not written. A day whose session count changes while it is read is loaded again in one-hour windows. If a day still fails, it is not written, the run goes on with the next day, and the run ends `failed`. Three such days in a row stop the run.
- HTTP responses other than 429 and 5xx (for example 400) fail that day in the same way.

## 8. The database password and the first deploy

The migrate job creates the role `stats_loader` from `STATS_LOADER_DB_PASSWORD` in `.env`, and the loader connects with it. The role can select, insert and update `atc_stats_session` and `atc_stats_run_log`, can delete from `atc_stats_session` (the retention rule), and has no other rights. Its sessions are in UTC.

The line must be in `.env` before any Compose command runs on the machine: without it, Compose refuses to start and names the missing variable. A new install gets the value from `ops/gen_env.sh`. On the production VM the owner adds the line before the deploy, as part of the owner's go; the value is never printed or pasted. The steps are in [deploy-to-vm.md](deploy-to-vm.md), section 2f.

## 9. Backups and privacy

Each session row holds the controller's VATSIM account number (`cid`), the callsign, the start and end times and counts of what the controller did. The rows are kept for `STATS_RETENTION_YEARS`. A deleted row stays in database dumps and in VM backups until those expire; the dump retention is `BACKUP_RETENTION_DAYS` ([CONFIG_MAP.md](../CONFIG_MAP.md)).
