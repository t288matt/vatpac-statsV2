# Deploy plan: the code-review fixes (issue #80)

> **Note 14-10-2026 (issue 173).** This is a dated record. The deploy method it describes (`git archive`, `tar -x`, building on the VM) was replaced on 14-10-2026 by a read-only git checkout plus commit-tagged registry images. Current method: [RUNBOOKS/deploy-to-vm.md](RUNBOOKS/deploy-to-vm.md). Do not rewrite the history below.

**SUPERSEDED and DONE (12-10-2026).** This plan was not followed step by step: everything it covers was deployed together on 11-10-2026 (marks at 12:38 and 16:01 UTC, then b3189ad at 20:35 UTC; see CHANGELOG.md). The commit 520dc3e below was not used. Kept as the record of the checks that were planned.

Agreed with the owner 10-10-2026: deploy the fixes one branch at a time, each followed by about 24 hours of watching, so any problem can be traced to a single change. This plan is not a go: each deploy needs the owner's explicit yes (working agreement section 5, until the scripted deploy of issue #104 exists). Merged to main and CI-green: fix-ops (d0ceaaa, review record docs/reviews/10-10-2026-fix-ops.md) and fix-collector (acc55db, docs/reviews/10-10-2026-fix-collector.md). fix-sql is still being built and reviewed; it gets its own deploy later.

## Which commit to ship (added 10-10-2026, when fix-sql was merged)
From the fix-sql merge onwards the tip of main also contains fix-sql (migration 20261009000170, the partition-drop change in maintenance.py, ADR-028 and the dashboard fixes), which belongs to a later deploy. Deploys 1 and 2 therefore ship the EXACT commit 520dc3e (fix-ops, fix-collector, the Telegram wording and docs, without fix-sql), never the tip of main: `git archive 520dc3e`. Check with `git ls-tree -r --name-only 520dc3e db/migrations | grep 20261009000170` that it prints nothing (the migration is absent) before shipping; without `-r` the command lists only the folder name and proves nothing. The fix-sql deploy ships the tip of main after deploys 1 and 2 are clean.

## Deploy 1: fix-ops (HIGH tier) after the weekly restore test
When: Sunday 11-10-2026, after the 02:00 UTC backup and restore test have run and their result is read (`.last_restore_test`, watchdog `restore_test`). The OLD restore check judges that run. Reason: the stricter restore check should start from a known-good result, and the reviewer advised this order.
What changes: the migrate job re-applies roles in one transaction (the collector loses DELETE on nine tables), the watchdog image (outbox, backup_remote and telegram_outbox checks, new wording) and the backup image (validation, timeouts) are rebuilt; `docker-compose.yml` gains the new settings. The collector container is NOT touched (its configuration is unchanged), so polling is not interrupted. Steps and checks: docs/RUNBOOKS/deploy-to-vm.md sections 4e and 4f.
Before: VM clock synced, memory free above 3 GB, fresh dump (`docker compose exec backup backup.sh once`), a rollback copy of the tree (`~/vatpac-statsV2.rollback-<dd-mm>-fixops`), the tree shipped with `git archive` of the exact commit (acc55db or later on main; check what the fix-ops files are at that commit), files deleted in git removed by hand.
Order: `docker compose up -d --build migrate`, then check `exited (0)` and the privileges; then `docker compose up -d --build watchdog backup` (only those).
Checks straight after: `sh ops/smoke.sh` SMOKE PASS; `python3 ops/check_panels.py` 0 errors; restart counts 0 and the collector start time unchanged; watchdog log has no Traceback and no "could not format the alert"; `watchdog_status.json` shows `telegram_outbox`, `backup_remote` (OK, not configured), `restore_test`, `data_integrity`, `feed_crosscheck`; `docker compose exec backup backup.sh once` produces a valid dump with the new timeouts; `make backup-test` passes the stricter restore check on the live backup; the collector still trims `poll_log` (`maintenance finished`, `failed` 0) and writes without "permission denied".
Rollback: restore the previous tree, re-run `docker compose up -d --build migrate watchdog backup` (the old roles.sql restores the DELETE grants).
Watch for 24 hours: the 02:00 UTC backup completes; no new watchdog alerts you did not expect; collector log free of "permission denied"; the first Telegram message in the new wording is read by the owner.

### Extra checks for deploy 1 (added 10-10-2026 from the second review of the privilege and backup changes, docs/reviews/10-10-2026-fix-ops-opus.md on branch review-fix-ops-opus)
1. Privileges: after the migrate job, do not rely on smoke.sh (it tests grafana_ro only). Run a read-only `has_table_privilege` check for the collector, grafana_ro, watchdog_ro and backup_ro on the data tables (the query is in the review record) and confirm the collector has no DELETE on the nine tables and still has DELETE on `poll_log`.
2. The `poll_log` trim cannot be seen straight after the deploy: the next maintenance run is about 15:42 UTC on the day. Read the collector log for `maintenance finished` after that time (failed 0, no "permission denied"), or run the `SET ROLE collector` plus `DELETE ... WHERE false` test inside a rolled-back transaction.
3. Do not deploy within 30 minutes of 15:42 UTC (daily partition creation) or inside 01:50 to 02:30 UTC (backup).
4. Known weakness, not fixed by this deploy (issue #118): backup validation only proves the dump table of contents is readable. After deploy 1, read the first `.last_restore_test` result (the real proof) before trusting the backups.
5. Rollback note: going back to the old roles.sql brings back the brief permission-denied gap on the nine tables.

## Deploy 2: fix-collector (MEDIUM tier) about 24 hours after deploy 1
When: Monday 12-10-2026, after deploy 1's 24-hour watch is clean and the owner says go.
What changes: the collector image only (parsing, error handling, health thresholds, one apply at a time with a deadline). No migration. Polling stops for about 45 seconds.
Before: fresh dump, rollback copy (`~/vatpac-statsV2.rollback-<dd-mm>-collector`), clock check, memory check; `docker compose stop collector`, then `docker compose up -d --build collector` (never `restart`).
Checks straight after: polling resumes within about a minute (`poll_log` ok rows, gap under 90 s); smoke; panels 0 errors; restart counts 0; `/health` 200; identity check 0, 0, 0; the invariant pack run read-only (`ops/audit/invariants.sql`): expect only the known power-cut gap until 12-10-2026 15:42 UTC, then all 18 clean; `network_snapshot.pilots_in_region` equals the stored positions for the newest snapshots.
Rollback: restore the previous tree and rebuild the collector (no migration to undo).
Watch for 24 hours: `poll_log` error rate no higher than before; no "previous snapshot apply is still running" lines; no `bad flight plans` or `feed ... is not a list` errors unless the feed really changed; the next integrity sweep (every 3 days) clean.

## Afterwards
1. The v1 against v3 re-comparison (owner's go needed), DELAYED by the owner on 10-10-2026 until 12 hours after ALL the fixes are deployed (deploys 1 and 2 and the fix-sql deploy), run on the data after the last deploy, as an independent regression check on the numbers. v1 on VM 111 stays up until it is done.
2. fix-sql: review, merge, then its own deploy (dashboard changes load within 30 seconds, and any migration runs through the migrate job).
3. The technical writer batch (issue #107) after all three branches are merged.
4. Delete the rollback copies about a week after each clean deploy.
