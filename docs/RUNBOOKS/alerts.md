# Runbook: alerts (what each Telegram message means)

Last reviewed: 09-10-2026 (data_integrity and feed_crosscheck added; plain-language Telegram wording, issue 79), against `ops/watchdog/watchdog.py` and `ops/watchdog/integrity.py`. Times are UTC.

## How alerts work

The `watchdog` container runs a set of checks every `WATCHDOG_INTERVAL_SECONDS` (60). A check changes state only after it has held the new state for `WATCHDOG_CONFIRM_CYCLES` cycles (2, so about two minutes), and a message is sent on each confirmed state change, not on every cycle. If `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in `.env` the message goes to Telegram; every alert is also written to the watchdog log either way (`docker compose logs --tail=50 watchdog`, lines beginning `ALERT`).

What the watchdog logs and writes to the status file is the technical text, unchanged: `WARN <check>: <detail>`, `CRIT <check>: <detail>`, `RECOVERED <check> (was warn|crit): <detail>`, `NEAR-MISS controller callsign(s) not on whitelist: ...`, `RESTARTED <service> (...): <why>`. The Telegram message is the plain-language version of the same alert, described under "Message formats" below. The tables in this runbook still use the technical check names, because that is what the `Technical:` line, the log and the status file show.

The current verdict of every check is in the status file: `docker compose exec -T watchdog cat /var/watchdog/watchdog_status.json`.

The watchdog is **alert-only**: it has no Docker access, so it never restarts anything (ADR-012). Every action below is yours to take.

## Message formats

Every Telegram message is plain ASCII text (no emoji) and has the same shape: `VATPAC Stats: <label> (HH:MM UTC).`, then one or two plain sentences (what happened, whether data is affected, whether you must do anything), then one last line starting `Technical: ` with the check name and the key numbers. The label is `heads up` (WARN), `problem` (CRIT) or `all clear` (RECOVERED). A RECOVERED message says how long the problem lasted ("The problem lasted about 4 minutes.") when the watchdog knows when it began (it keeps the start time in the status file); after an upgrade, or if the start is unknown, the sentence is left out rather than guessed. A WARN that follows a CRIT of the same check starts "This has eased from a problem to a warning." The wording lives in `ops/watchdog/messages.py` (pure functions, tested by `tests/test_humane_messages.py`). If the wording code ever fails, the alert is still sent in the old technical form (`vatpac-stats: WARN ...`) and the error is logged (`could not format the alert`). A check name the wording does not know gets a generic sentence plus the technical line; a test fails if a check in this runbook has no wording of its own, or if an invariant is added to `ops/audit/invariants.sql` without a plain sentence.

Examples:

```
VATPAC Stats: problem (14:05 UTC). No fresh VATSIM data for about 5 minutes. The system keeps retrying; if this lasts, the collector may be stuck (see the stuck-collector guide).
Technical: poll_log_ok_age, newest ok poll is 312s old

VATPAC Stats: all clear (14:09 UTC). Data is flowing again. The problem lasted about 4 minutes.
Technical: poll_log_ok_age recovered (was crit), newest ok poll is 5s old

VATPAC Stats: heads up (14:05 UTC). A routine data check found a problem: no data was recorded for more than 5 minutes at some point in the last 3 days. Nothing to do unless it keeps happening (see the integrity-checks guide).
Technical: data_integrity, 1 of 18 checks failing: snapshot gaps over 5 minutes (last 3 days)

VATPAC Stats: heads up (14:05 UTC). A controller logged on with a callsign that is not on the whitelist, so they are not being recorded: YSSY_X_TWR. It looks like a new position; add it to the whitelist if it is real.
Technical: near-miss YSSY_X_TWR
```

Old wording to new wording (the plain sentence of the new message, without the label and time):

| Old (after `vatpac-stats: `) | New |
|---|---|
| `CRIT poll_log_ok_age: newest ok poll is 312s old` | No fresh VATSIM data for about 5 minutes. The system keeps retrying; if this lasts, the collector may be stuck. |
| `RECOVERED poll_log_ok_age (was crit): ...` | Data is flowing again. The problem lasted about 4 minutes. |
| `WARN network_snapshot_age: newest snapshot is 90s old` | The newest stored picture of the network is about 2 minutes old. Usually a brief hiccup; no action unless it grows. |
| `CRIT collector_health: unreachable (ConnectError)` | The data collector is not answering properly, so new VATSIM data may not be arriving. It keeps trying; check whether VATSIM is down or the collector is stuck. |
| `CRIT grafana_health: ...` | The dashboards (Grafana) are down or unwell. Data collection is not affected. Restart Grafana when convenient. |
| `CRIT database: query/connect failed (...)` | The watchdog cannot read the database, so the checks that need it are blind. Collection may still be running. |
| `WARN db_size: 21.40 GB (limit 20)` | The database has grown to 21.4 GB, more than expected (normally 2 to 3 GB). Nothing is broken yet. |
| `WARN disk: /backups 82% used, / 50% used` | The VM disk is 82% full. Nothing breaks yet, but it needs clearing within days. (CRIT: writes could start to fail soon, clear space today.) |
| `CRIT backup_age: newest backup 27.3 h old` | The newest database backup is 27 hours old; one should be made every night. Take one by hand (make backup) and check the backup log. |
| `CRIT restore_test: ... FAILED ...` | The weekly test that proves a backup can really be restored has failed, so a recent backup may be unusable. Run make backup-test. (WARN forms: overdue, or result unreadable.) |
| `WARN identity_flap: ...` | VATSIM may have changed how it stamps logon times again, so one connection could be stored as two and flight counts inflated. Not urgent today, but an engineer should look. |
| `WARN feed_archive: last write 601s ago ...` | The safety copy of the raw VATSIM data (the archive) has stopped being written. Live collection is not affected. |
| `WARN data_integrity: 1 of 18 invariants report violations: <name> (1). ...` | A routine data check found a problem: (the failing check in plain words). Nothing to do unless it keeps happening. The Technical line reads "1 of 18 checks failing: <name>". |
| `WARN data_integrity: new violations since the last sweep: ...` | The routine data check found a new problem on top of the earlier one: (plain words). |
| `WARN data_integrity: invariant sweep could not run (...)` | The routine data check could not run properly (it retries in an hour). Data collection is not affected. |
| `WARN feed_crosscheck: 1 difference(s) in 24 archived snapshots ...` | The fortnightly check against saved raw VATSIM data found 1 difference: the system may have kept or dropped some pilots or controllers wrongly. Not urgent. |
| `NEAR-MISS controller callsign(s) not on whitelist: X` | A controller logged on with a callsign that is not on the whitelist, so they are not being recorded: X. It looks like a new position; add it to the whitelist if it is real. |
| `container:<svc>`, `docker_proxy`, `restart-cap:<svc>`, `RESTARTED <svc>` (inactive here) | The <svc> container is not running properly; the watchdog cannot reach Docker; the <svc> container has hit the hourly restart limit; the watchdog restarted the <svc> container because it looked stuck. |
| any other check name | A monitoring check (name) has a problem (or a warning). See the alerts guide for what it means. |

## What each check means and what to do

| Check name | Level and trigger | Meaning | What to do |
|---|---|---|---|
| `collector_health` | CRIT when the collector `/health` is unreachable or not HTTP 200 | No good poll in the last 60 s, or the newest snapshot is over 120 s old, or the collector is down | Read `docker compose logs --tail=100 collector`. If the feed is failing (`consecutive_failures` above 0): [feed-outage.md](feed-outage.md), wait. Otherwise [stuck-collector.md](stuck-collector.md). |
| `poll_log_ok_age` | WARN at 60 s, CRIT at 300 s (`WATCHDOG_WARN_SECONDS`, `WATCHDOG_PAGE_SECONDS`) | The newest `ok` row in `poll_log` is this old | Same as `collector_health`. Also fires if the database is slow or unreachable. |
| `network_snapshot_age` | Same thresholds | The newest stored snapshot is this old | Usually fires with `poll_log_ok_age`. If only this one fires, the feed has frozen (polls succeed but the snapshot time does not change): [feed-outage.md](feed-outage.md). |
| `grafana_health` | CRIT when `/api/health` is unreachable or not 200 | Grafana is down or unwell; data collection is unaffected | `docker compose ps grafana`, `docker compose logs --tail=100 grafana`, then `docker compose restart grafana`. |
| `database` | CRIT "query/connect failed" | The watchdog cannot query the database as `watchdog_ro` | `docker compose ps db`, `docker compose logs --tail=100 db`. If you changed `WATCHDOG_DB_PASSWORD` in `.env`, run `docker compose up -d` so `migrate` updates the role. Disk full: [db-full.md](db-full.md). |
| `db_size` | WARN above `WATCHDOG_DB_MAX_GB` (20 GB) | The database is larger than expected (steady state is 2 to 3 GB) | [db-full.md](db-full.md): look for partitions not being dropped. |
| `disk` | WARN at `WATCHDOG_DISK_PCT` (80%), CRIT at 10 points more | A checked filesystem is filling (`WATCHDOG_DISK_PATHS`, default `/backups,/`) | [db-full.md](db-full.md). |
| `backup_age` | CRIT when the newest dump is older than `WATCHDOG_BACKUP_MAX_HOURS` (26 h) | The nightly backup did not run or failed | `docker compose logs --tail=50 backup`; take one now with `make backup` (`docker compose exec backup backup.sh once`); [restore.md](restore.md). Directly after a first install the age counts from the watchdog's first run. |
| `restore_test` | CRIT when the last weekly restore test failed; WARN when the last good one is older than `WATCHDOG_RESTORE_TEST_MAX_DAYS` (9 days), unreadable, or missing after that long | A recent dump may not restore | `make backup-test` and read its output; `docker compose exec backup cat /backups/.last_restore_test`; [restore.md](restore.md). With `BACKUP_RESTORE_TEST_DAY=0` also set `WATCHDOG_RESTORE_TEST_MAX_DAYS=0`. |
| `identity_flap` | WARN when, in the last 24 hours, two sessions of one `cid` and callsign have a `logon_time` within 2 s of each other, or a `logon_time` has a fractional second (or the query fails). RECOVERED when none is left in the window | The feed changed how it writes `logon_time` again (issue 15, ADR-021) or the whole-second rule failed: one connection may be stored as two sessions and flights inflated | Run `docker compose exec -T db sh -c 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < ops/check_identity_flap.sql` (all counts 0 expected); find the pairs, read the collector log, and fetch the payloads around the first-seen time from the archive ([feed-archive.md](feed-archive.md), a `precision` or `gap` anomaly file should exist). One alert per occurrence; it clears by itself after 24 hours without a new pair. |
| `feed_archive` | WARN when the collector reports the archive enabled and nothing was written for twice `ARCHIVE_COMPACT_EVERY_S` (10 minutes by default). Not checked while `collector_health` is bad | The archive volume is full or unwritable, or its thread is stuck. Polling is not affected | [feed-archive.md](feed-archive.md), "If it misbehaves": `df -h /`, the `archive` block of the collector `/health`, `docker compose logs --tail=200 collector` (look for `archive`). |
| `data_integrity` | WARN when the invariant sweep (18 read-only checks in `ops/audit/invariants.sql`, run every 72 hours; repeated every 6 hours while violations stand) finds a violation, or cannot run. RECOVERED when a sweep is clean. One extra message, `WARN data_integrity: new violations since the last sweep: ...`, when a sweep that is already failing finds a new invariant failing | Stored data broke one of the rules it must always obey: duplicates, impossible values, orphans, counts that disagree between tables, a snapshot gap over 5 minutes, an identity flap, missing partitions and so on. Latency is up to 3 days, so a break that is also covered by a live check (collector, snapshot age, identity_flap) has usually been alerted already. A violation that ages out of its 3-day window is not announced; RECOVERED follows at the next 6-hourly recheck. Sample of the technical text in the log: `WARN data_integrity: 1 of 18 invariants report violations: snapshot gaps over 5 minutes (1). Details are in the watchdog log; run ops/audit/invariants.sql (docs/RUNBOOKS/alerts.md)` | [integrity-checks.md](integrity-checks.md): the table of what each invariant means and what to do. Detail: `docker compose logs --since 24h watchdog \| grep "integrity violation"`. `could not run (InsufficientPrivilege)` means the migrate job has not applied the `watchdog_ro` grants: `docker compose up -d --build migrate`. |
| `feed_crosscheck` | WARN when, in the 14-day archive cross-check, the in-region pilot count, the controller count or the set of in-region pilots recomputed from an archived raw-feed payload differs from the stored rows, or when there was nothing to compare, or it could not run. RECOVERED at the next clean run (an hour later if it could not run, otherwise 14 days) | The collector kept or dropped something the independent rules would not (polygon or whitelist regression, changed gate, a feed field that changed meaning), or the archive is empty. Latency up to 14 days. Sample of the technical text in the log: `WARN feed_crosscheck: 1 difference(s) in 24 archived snapshots: 09-10-2026 12:00:03 UTC: pilots_in_region independent 41, stored 40. Details are in the watchdog log (docs/RUNBOOKS/alerts.md)` | [integrity-checks.md](integrity-checks.md) (end of the table) and [feed-archive.md](feed-archive.md) (replay the file through the collector's own parser). |
| `NEAR-MISS ...` | One message per new callsign | A controller logged on with a callsign that is not on the whitelist but looks like one that is (a new position at a known airport) | [update-reference-files.md](update-reference-files.md): add it to `config/controller_callsigns_list.txt`, deploy, reload. Until then that controller is not recorded. |

Checks that only exist when `DOCKER_HOST_URL` is set (it is empty in this deployment, so you will not see them): `container:<service>`, `docker_proxy`, `restart-cap:<service>` and `RESTARTED <service>` messages.

## What does not alert

Know these gaps; check for them by hand:

- **The VM or its network being down.** The watchdog runs on the same VM, so a dead VM sends nothing. An outside heartbeat is an open task (docs/TRACKER.md, T19). `UPTIME_KUMA_PUSH_URL` can feed an external monitor, but nothing is configured by default.
- **A rejected reference-file reload** (`reload_rejected` in the collector `/health`, and an error line in the collector log). See [update-reference-files.md](update-reference-files.md).
- **Telegram itself failing.** A failed send is logged by the watchdog (`telegram send failed`) and the alert is lost. Test delivery after changing the token: stop the collector for a few minutes ([stuck-collector.md](stuck-collector.md), "Deliberately stopping") and confirm a `problem` message then an `all clear` message arrive.
- **Slow decay:** off-box backups missing, data quality, charts that look wrong. That is what [daily-checks.md](daily-checks.md) is for.

## Rules for the messages

Messages are plain text with no emoji (emoji caused UTF-8 errors). Never paste a message that contains a bot token, and never put the token in git, chat or logs.
