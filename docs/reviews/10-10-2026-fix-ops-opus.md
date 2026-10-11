# Second review record: fix-ops for deploy 1 (exact commit 520dc3e)

Reviewed 10-10-2026 (about 21:00 UTC) by a second, independent reviewer (Opus), read-only, after the Sonnet review in
`docs/reviews/10-10-2026-fix-ops.md`. Scope narrowed by the owner during the review: area 1 (privileges) and area 2
(backup and restore). **Area 3 (alerting: Telegram outbox, backup_remote and telegram_outbox checks, wording) was NOT
reviewed by this reviewer.**

Method: the deployed tree on the VM (`~/vatpac-statsV2`) was copied read-only and compared with `git archive 520dc3e`;
live grants, function ownership and backup files were read with SELECT-only queries and `ls`. No container was started,
nothing was changed on the VM.

## Verdict: GO with two extra checks in the deploy steps (no code change needed for tomorrow)

## What deploy 1 actually changes (deployed tree versus 520dc3e)
- `db/roles.sql`, `ops/migrate/migrate.sh`, `ops/backup/{backup,restore,restore-test,restore-lib}.sh`, `ops/watchdog/*`,
  `docker-compose.yml` (backup and watchdog settings only), `ops/smoke.sh`, audit helpers; collector source also differs
  (fix-collector) but is not rebuilt in deploy 1.
- **No new migration**: `db/migrations` is identical on the VM and at 520dc3e (last is 20261009000160). The rebuilt migrate
  job only re-runs roles.
- `collector` is not a dependency of `migrate`, `backup` or `watchdog` in compose, so `up -d --build migrate` and
  `up -d --build watchdog backup` cannot rebuild or recreate the collector by accident (it would have shipped fix-collector early).

## Area 1: privileges

Live state (PostgreSQL 16.15) before the deploy: the collector has SELECT, INSERT, UPDATE, DELETE on the ten tables;
after 520dc3e it keeps DELETE on `poll_log` only (nine tables lose it, matching the plan). No triggers, no rules, no row
security in the database, so no hidden DELETE runs as the collector.

Every statement the RUNNING collector issues was walked (the deployed, old code, which keeps running until deploy 2,
and the 520dc3e code; both are the same in this respect):
| Statement | Needs | After deploy 1 |
|---|---|---|
| upserts in store.py (INSERT ... ON CONFLICT DO UPDATE ... RETURNING), app_setting sync | SELECT, INSERT, UPDATE | kept |
| `DELETE FROM poll_log ...` (maintenance.py:75 trim_poll_log) | DELETE on poll_log | kept |
| `ensure_pilot_position_partitions`, `drop_old_pilot_position_partitions`, `refresh_flight_summary`, `refresh_materialized_views` | EXECUTE; SECURITY DEFINER, owner `vatpac` (superuser) | kept; live `prosecdef = t`, EXECUTE granted |
| `refresh_flight_summary_sessions` (SECURITY INVOKER) | only called from inside the definer functions, so runs as `vatpac` | not called by the collector directly |
| CheckViolation path (main.py) | calls ensure_pilot_position_partitions | kept |

No DELETE, TRUNCATE, DROP or DDL is issued by the collector on any of the nine tables. Conclusion: nothing the collector
does today breaks.

New daily partitions: created by the definer function, owned by `vatpac` (live: 24 partitions, all owned by vatpac), with
no grants of their own. That is correct and unchanged: the collector and watchdog_ro reach them through the parent
(PostgreSQL checks the parent's privileges), grafana_ro reads only views, backup_ro has `pg_read_all_data`. No default
privileges are needed.

Partial failure: roles.sql now runs `BEGIN ... COMMIT` under `ON_ERROR_STOP`; on any error psql exits and the open
transaction is rolled back, so the grants stay exactly as before and migrate exits non-zero (the plan's `exited (0)` check
catches it). Catalog changes are invisible to other sessions until COMMIT, and GRANT/REVOKE take no table lock, so a
collector mid-write and grafana_ro see no gap. The watchdog_ro block is a separate, second transaction; a failure there
leaves roles.sql committed and watchdog_ro unchanged: also safe.

Findings (all low):
- P1 (low) Rollback reintroduces the original issue-84 gap. The old roles.sql runs REVOKE then GRANT as separate
  autocommit statements, so during a rollback the collector (and grafana_ro) can see "permission denied" for a fraction of
  a second; a poll could fail once. Scenario: rollback at a busy moment, one poll lost, collector retries. Acceptable;
  a narrower privilege-only rollback is `GRANT DELETE ON app_setting, controller_frequency_event, controller_rating_event,
  controller_session, controller_unlisted_sighting, network_snapshot, pilot_position, pilot_session, reference_data_change
  TO collector;` (one statement, no gap).
- P2 (low) Concurrent catalog update. The one transaction updates the pg_class row of every table and partition until
  COMMIT. If the collector's partition creation (daily at about 15:42 UTC, or the CheckViolation path) or a concurrent
  GRANT touches the same rows, one side can fail with "tuple concurrently updated". Scenario: deploy at 15:42 UTC; migrate
  exits 1 (rolled back, harmless, re-run) or the collector's maintenance task fails and retries 60 s later. Avoid deploying
  near 15:42 UTC.

## Area 2: backup and restore

Live state: eight dumps from 09-10 and 10-10-2026 (2.8 to 5.4 MB), 162 GB free. **No `.last_restore_test` exists yet:
the weekly restore test has never run.** 11-10-2026 is a Sunday (`BACKUP_RESTORE_TEST_DAY=7`), so the first ever restore
test runs at 02:00 UTC with the OLD scripts, as the plan intends; the new restore-test first runs on its own on
18-10-2026 02:00 UTC (and once by hand via `make backup-test` after the deploy; `make` exists on the VM).

Verified safe:
- A failed or timed-out pg_dump, or a dump that `pg_restore -l` cannot list, removes only its own `.partial`, returns
  before rename, marker, off-box copy and rotation: older backups are untouched (backup.sh:31-49).
- Rotation runs only after a dump has been validated and moved into place, so it can never leave zero dumps; disk full
  makes pg_dump fail first. `.partial` clean-up (older than 120 min) cannot hit a running dump (30 min dump plus 5 min
  validation limits).
- Overlap of `backup.sh once` with the loop: distinct file names per second, no shared temp file. Deploy runs well after
  02:00, so no overlap in practice.
- restore.sh: confirm name, system-database refusal, connected-session check before and just before the swap, safety dump
  before any change, restore into `_restoring`, put-back if the second rename fails. Target never touched on a sanity
  failure (including the new STALE result, which is a refusal for restore.sh).
- restore-test: STALE gives `WARN` (exit 0) and the new watchdog reads WARN; FAIL stays CRIT. A tiny but real dump passes;
  a schema-only or empty dump now FAILs (issue 90). An old dump is judged against its own file-name stamp, so a good old
  dump is not a false FAIL; the separate backup-age check covers "no new dump".

Findings:
- B1 (medium, pre-existing, not a blocker) "Valid" means only that the table of contents is readable. A dump of the wrong
  or an empty database (for example `PGDATABASE` changed in `.env`) passes validation, is kept as good, and after 14 days
  rotation deletes every real dump. The weekly restore test is the only detector (FAIL "poll_log is empty"), at most 7
  days later, so there is a 7-day margin. Suggest later: run the same row-count sanity (poll_log > 0) on the live database
  at dump time, or keep a floor of N newest dumps regardless of age.
- B2 (low) In `once` mode `set -e` is active inside take_backup: a failing `du`, or a failed write of
  `.last_remote_success` after a successful hook (backup.sh:55, 64), exits non-zero before rotation although the dump is
  good. Scenario: deploy step "fresh dump" reports failure with a good dump in place; operator confusion only.
- B3 (low) restore.sh:76: if `pg_restore -l` of the safety dump fails, the script exits with no message and leaves a
  `.prerestore.partial` (cleaned after 2 h). Target untouched. The manual safety pg_dump has no timeout.
- B4 (low) restore-test sanity checks only poll_log, pilot_session and schema_migrations; empty pilot_position or
  controller tables in a dump would still pass. pg_dump with `--exit-on-error` restores make this unlikely.

## Gaps in the deploy plan (docs/DEPLOY_PLAN_REVIEW_FIXES.md, deploy 1)
1. "check `exited (0)` and the privileges" names no query, and `ops/smoke.sh` tests grafana_ro only. Add, right after
   migrate (read-only, as the owner role in the db container):
   `SELECT t, p, has_table_privilege('collector', t, p) FROM unnest(ARRAY['poll_log','pilot_session','app_setting']) t,
   unnest(ARRAY['INSERT','UPDATE','DELETE']) p;` Expected: all `t` except DELETE on pilot_session and app_setting (`f`).
2. "the collector still trims poll_log (maintenance finished, failed 0)" cannot be seen "straight after": the collector is
   not restarted and its daily maintenance next runs at about 15:42 UTC on 11-10-2026. Either check that log line after
   15:43 UTC, or prove the privilege now, without changing data: `docker compose exec -T db psql -U "$POSTGRES_USER" -d
   "$POSTGRES_DB" -c "SET ROLE collector" -c "BEGIN" -c "DELETE FROM poll_log WHERE false" -c "ROLLBACK"` (must succeed).
3. `up -d --build watchdog backup` without `--no-deps` makes Compose re-run the completed migrate job; harmless (idempotent,
   transactional) but expect a second migrate run in the log. Runbook 4f uses `--no-deps`; either is fine.
4. Do not deploy near 15:42 UTC (P2).

## Not verified
- Area 3 alerting (not reviewed by this reviewer, owner's instruction).
- Nothing was executed against the database except SELECTs; the one-transaction behaviour and lock claims are from the
  PostgreSQL semantics and the author's race test, not re-run here.
- The new backup image's `timeout` binary was not checked inside the built image (Debian base, coreutils expected).
