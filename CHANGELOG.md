# Changelog

What changed in VATPAC Stats v3, newest first, in plain words. Dates are DD-MM-YYYY and times are UTC. Written 10-10-2026 from the git history, docs/TRACKER.md, [docs/DECISIONS.md](docs/DECISIONS.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) section 10; updated in the evening of 10-10-2026 (docs batch 2) for the state of `origin/main` at commit 27cb266 (CI green at 27cb266; main then moved to c89e544).

How to read it. "Deployed" means running on the production VM, with the date and time and commit taken from the deploy records in the tracker and the runbooks. Where a record gives only part of that, the entry says "not recorded". The code history is much finer than this page; the full list is `git log`. Decision numbers (ADR-nnn) are in docs/DECISIONS.md and issue numbers (#nn) are on GitHub.

Updated 22-10-2026. The commit production runs is shown by `bash ops/deploy.sh --status` (release marker); this page does not name it. The marker history of 11 to 13-10-2026 (b3189ad, c70e72f for phase 1, 3d4b5b6 for phase 1b, 38e3958 for phase 3, 7a71d30 for phases 4 and 5 at 10:05:56 UTC on 13-10-2026) is kept in the entries below. The entries for phases 1, 1b and 3 are dated records and use the names of their time: `flight_record` is today's `flight_summary`, and `flight_legacy`, `flight_part` and the old pieces table (then called `flight_summary`) existed until phase 5.

## ATC hours staffed by position type (04-11-2026, issue 267 group G; pull request pending, not yet merged or deployed)

- **What is new.** Two panels on the "ATC: controllers" dashboard answer "how many hours were CTR, TWR, APP, DEP, GND, DEL, FSS and FMP positions staffed?": a stacked weekly chart per position type, and a table of the eight types for the last 7 days, last 30 days, last full month and since go-live. Dashboard and tests only: no migration and no image release.
- **The rule.** Position-hours: merged controller sessions, clipped to the period, summed per type, so two CTR sectors online for 3 hours count as 6. The type is the callsign suffix, so APP and DEP are separate and CTR excludes FSS.
- **What does not change.** The existing "CTR hours staffed per week" panel is untouched; its CTR figure can differ slightly because it credits a whole session to the week it started and uses the feed's facility code. FMP may often show 0 (not yet checked how it appears in the feed).

## Images are published automatically after green CI on main (04-11-2026, issue 268; pull request pending, not yet merged or deployed)

- **What changes.** The `release` workflow now also starts by itself when the `ci` workflow finishes successfully for a push to main, so the owner no longer has to click "Run workflow" before `make deploy`. It publishes the commit only if it is still main's tip; a run for an older commit stops with a notice and publishes nothing, so `latest` never moves backwards when two CI runs finish out of order.
- **What does not change.** Nothing deploys by itself: `make deploy` on the VM is still run by hand, and it still refuses to start if the pulled images do not match the checkout. The manual run still works for a rollback or a docs-only tip. No service code changes.

## Poll duration no longer climbs to 30 seconds and resets (27-10-2026, issue 261; pull request 262, not yet merged or deployed)

- **The cause.** `poll_history.duration_ms` rose from about 300 ms to about 30 s over 29 to 45 hours, then dropped back. Polls start on a fixed 30 s grid, and the 30 s floor between requests (`MIN_REQUEST_GAP_S`) is also 30 s. The floor stamped the moment it woke up from its wait, so every wake that was a few milliseconds late made the next poll wait a few milliseconds longer, until the wait reached the full 30 s and the schedule re-synchronised. The wait was counted in `duration_ms`; nothing was slow on the network or in the database.
- **The fix.** The floor still stamps the real send time, so no two requests are ever closer than 30 s. The poll loop now schedules the next poll one interval after the last real send (if that is later than its own tick), so a poll starts only when the floor has already elapsed and the wait is normally 0. After a stall, a 429 or an unchanged feed it recovers in one poll. The cadence is now about 30.003 s instead of a drift-free grid.
- **Logging.** A poll over 2 s logs a `slow poll` warning with `duration_ms`, `wait_ms` (the floor wait of the last attempt; retry back-off is not counted) and `request_ms` (the GET itself). The first poll after every restart still waits up to 30 s, because the start-up feed discovery counts as a request; that warning is expected.

## Deploy tooling made stricter and shared (22-10-2026, issue 257; merged, not yet deployed)

- **`ops/deploy.sh --verify` fails from a stale checkout.** It counts the integrity checks and runs the flap check from the copies of `ops/audit/*.sql` in the checkout it is run from; on 22-10-2026 a stale checkout counted 21 invariants while production runs 20. Now `--verify` first compares the local checkout with the VM checkout. If they differ (or either cannot be read) it prints a "STALE CHECKOUT" banner, fails a `checkout_current` line and stops before the checks that read local files. `--mark <commit>` refuses unless the local `HEAD` is exactly that commit, and `--preflight` and `--status` print `local checkout is at <10 characters>`. `--verify` and `--mark` also fail when there are uncommitted or untracked files under `ops/`, since those are what they run. Run them from a clean checkout of the VM's commit, for example a fresh worktree at that commit (not `origin/main`, which may have moved on since the release).
- **One reader of the release marker and of `gh` (issue #215).** `ops/release_common.sh` is shared by `ops/deploy.sh` and `ops/stale_issues.sh`: a marker commit of 7 to 40 characters is read the same way in both, and `GH_BIN`, then `DEPLOY_GH`, names the GitHub CLI.
- **One way of reading `.env` (issue #212).** `ops/env_key.sh` (shell scripts, and the remote reads of `deploy.sh`) and `ops/env_file.py` (Python tools) follow the same rules: the assignment starts in column 0 after an optional `export`, the last one wins, carriage returns and surrounding whitespace are dropped, one matching pair of quotes is dropped. `ops/check_env.sh` now accepts quoted or exported secrets (quotes used to count as characters and failed it); `BACKUP_TIME_UTC` and `GRAFANA_RO_PASSWORD` now take the last assignment in `.env` (before: the first) and honour quotes and a leading `export`, as compose reads the file.
- **One wording of the sha-pin warning (issue #213)** in `ops/image_mode.sh`, `ops/check_env.sh` and `deploy.sh --preflight`; the preflight adds which pin the VM's `.env` has.
- **`deploy.sh` reads the VM `HEAD` once per run (issue #214)**, and `registry_mode` lost a tag argument it ignored.
- **One Grafana client (issue #210).** `ops/grafana_api.py` serves `ops/check_panels.py` and `ops/grafana_public.py`: the admin login goes in clear text only to an address of this machine and over https otherwise, redirects are never followed, and `check_panels.py` prints the message of a non-JSON error reply. Typed with `mypy --strict` in CI.
- No change to the running stack or its images.

## Retention list panels show the highest rating (22-10-2026, issue 236; merged, not yet deployed)

- The two retention list panels, "Who might need help reactivating?" and "Who reactivated after being away for more than 60 days?", now take the rank group (S1, S2, C1 and so on) from the controller's highest rating. Before, a controller promoted from S2 to C1 showed S2; now C1. Labels and blanks are unchanged. Two integration tests pin it.

## Clean-ups and tidier checks, deployed as 2dc8a92 (marked 22-10-2026 16:56 UTC) and 450a74c (marked 22-10-2026 19:02 UTC)

- **Identity-flap check in one file (issue #201, pull request 250; deployed as 2dc8a92).** The watchdog (last 24 hours) and `ops/deploy.sh --verify` (whole history) now run the same file, `ops/audit/identity_flap.sql`; the separate copy `ops/check_identity_flap.sql` is gone, and the 24 hour query uses the `last_seen` index again.
- **Documents (2dc8a92 and 450a74c).** Process, review and history documents are no longer in the repository; the repository keeps only what an end user needs to install and run the product. The unused one-off audit scripts under `ops/audit/` were removed (issue #229); three files remain.
- **The collector needs `DATABASE_URL` (issue #228; 450a74c).** The unused `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER` and `POSTGRES_PASSWORD` fallback settings are gone from the collector, so a missing `DATABASE_URL` stops it at start-up instead of silently building a connection from other variables. Docker Compose already sets `DATABASE_URL`, so nothing changes on the VM; the database container's own `POSTGRES_*` variables are unchanged.
- **One marker reader in the watchdog (issue #216; 450a74c).** The backup-remote check and the restore-test check read their marker files with the same function and parse the time stamp the same way. The alerts are unchanged.
- **One invariant fewer (issue #243; 450a74c).** The data-integrity sweep drops the check "positions outside plausibility caps", so it has 20 checks instead of 21 (`ops/deploy.sh --verify` shows 20). The three CHECK constraints on `pilot_position` (altitude, groundspeed, heading), the collector's clamp and `ops/audit/independent.py` are unchanged; the consequence is that nobody is alerted if those constraints are dropped or loosened.
- **Dependency (pull request 197).** pydantic-settings 2.15.0 to 2.16.0 in the collector requirements.
- **Deploy notes and a test exemption (pull request 248, issues #234 and #242).** Stale deploy notes were corrected and the live-definition comparison was recorded; a stale `near_miss` exemption was removed.
- Also in 450a74c: dead helper functions removed from the collector, and stale comments and test addresses cleaned up (issue #251).

## Policy values moved into docker-compose.yml (19-10-2026, issue 21; deployed as c072491, marked 21-10-2026 14:50 UTC)

- The retention periods and the watchdog thresholds are now fixed values in `docker-compose.yml` instead of settings in `.env`, so what the repository says is what runs (ADR-034). The code default for raw positions is 180 days (it was 90) and the watchdog database warning is 5 GB (it was 20), as decided in ADR-026.
- The production VM already ran 180 days; its `.env` still has the old lines, which are now ignored. After the deploy, delete every moved key from the VM's `.env` (11 are there today: `POSITION_RETENTION_DAYS`, `BACKUP_RETENTION_DAYS` and nine `WATCHDOG_*` keys; `sh ops/check_env.sh .env` lists them). Their values equal the new literals, except `WATCHDOG_DB_MAX_GB`, which goes from 20 to 5. The database warning drops from 20 GB to 5 GB when the new image is deployed; the database is about 160 MB.
- `ops/check_config_drift.py` now fails when a policy value is read from `.env`, appears in `.env.example`, or differs between compose and the code.
- A table of where every setting lives is in `docs/CONFIG_MAP.md`.

## Deploy by git checkout and registry images: LIVE 14-10-2026 (issue 173)

- Merged to main: a manual release workflow that publishes four commit-tagged images (collector, watchdog, backup, migrate) to a private registry (PR 177), and optional `VATPAC_IMAGE_PREFIX` and `VATPAC_IMAGE_TAG` settings that make `docker-compose.yml` use them (PR 178). With both unset the stack builds locally as before. Run by the owner on 14-10-2026 and verified: the VM folder is now a read-only git checkout (15 stale files removed, saved in a tar), the release workflow published commit 67c1ca0 (tag `sha-67c1ca0`), the registry login and the two `.env` lines are in place, and `docker compose pull` plus `up -d --no-build` recreated migrate, collector, backup and watchdog (db and grafana untouched); `deploy.sh --verify` 10 PASS; release marker 67c1ca0 at 22:29:46 UTC. Not yet run: a second registry deploy, a rollback to a previous tag, a token renewal. The owner confirmed on the GitHub Packages page that the packages are private.
- Documents: the deploy runbook now has an overview, the registry procedure (sections 2e and 2f), the rollback, and the old `git archive` method kept as a fallback with its no-delete warning; new owner cards 15 to 18; ADR-031; related documents updated. Steps not yet run are marked so. The first rollback can only use the local `:local` images (no earlier registry tag exists).

## Post-deploy records of phases 4 and 5 (13-10-2026)

- Deploy: preflight 12 checks PASS; pre-deploy dump restore-tested OK; the build applied both migrations and recreated the backup, watchdog and collector containers, all healthy after 48 seconds; the polling gap was 55 seconds (the collector restart). Verify: smoke 10 of 10, 73 panels with 11 empty and 0 errors, restart counts all 0, health 200, identity flap 0/0/0. Source: comments on #89, #140 and #141.
- Restore test after the deploy: a fresh dump (taken 10:07 UTC) restored and passed the restore test including the renamed table (`flight_summary=1797`). The 02:00 UTC nightly backup on the new schema was not yet read when this was written.
- Panel timings, read-only on the live VM on 13-10-2026 (`PANEL_TIMINGS_13-10-2026.md`, kept privately by the owner): 36 flight panels, median of 3 EXPLAIN ANALYZE runs, old definition (`flight_legacy`) 1320 ms in total, new definition (thin `flight` view over the finished-flights table) 411 ms in total, 3.2 times faster, every one of the 36 panels faster. Caveat: this was measured while phase 3 was live and before the phase 4 and 5 deploy, so the "old" definition is the one phase 5 then removed; only about 6 days of data existed, so the growth with history is not yet measurable. It was not re-run after phase 5.
- Comparison A (old pieces against finished flights, run just before the deploy): all differing rows were classified (63 and 53 differing rows, as recorded by the manager); the per-row classification is not reproduced here.
- 24 hour watch: runs from 13-10-2026 10:05 UTC; the plan (collector health, polling gaps, the 5 minute writer job, `DROP REFUSED`, `flight_summary` growth, the invariant sweep, panels, the 02:00 UTC backup, the restore test, Telegram, the known feed-freeze invariants) is section 5 of `DEPLOY_PLAN_PHASE_4_5.md` kept privately by the owner.

## Issue 89 phase 5: the old flight machinery is removed and the table takes its final name (deployed 13-10-2026, marked commit 7a71d30 at 10:05:56 UTC)

Migration 20261013000300 (refs #89 and #141; design docs/DESIGN_89_FINAL.md sections 4, 7.2 and 7.4 to 7.6; requirement-to-test table docs/CONFORMANCE_89_PHASE5.md). Forward-only and destructive: the way back is the pre-deploy dump (taken at 10:00 UTC on 13-10-2026 and restore-tested before the deploy; a rollback copy of the tree was kept). Applied on the VM in 67 ms after phase 4's migration (33 ms), by the owner in one deploy with phase 4. Read-only checks of the live database after it (13-10-2026): `flight_summary` holds the finished flights (1797 rows at the first check, 1796 in `flight_record` just before), and `flight_record`, `flight_part`, `flight_legacy` and the old pieces table no longer exist.
- Dropped: the old pieces table (with its `final` flag and foreign key), the views `flight_part` and `flight_legacy`, the functions `refresh_flight_summary` and `refresh_flight_summary_sessions`. No CASCADE.
- Renamed: the finished-flights table `flight_record` becomes `flight_summary`, with its primary key, CHECK constraint and four indexes; the writer, the reconciliation helper and the drop function are re-created with only the table name changed. Every chart view follows automatically and returns the same rows (`tests/integration/test_phase5_remove_old.py`).
- The collector's 5-minute job now calls `write_finished_flights(50)` directly (it used to run only at the end of the removed refresh function); the collector role is granted EXECUTE on it in the migration and in `db/roles.sql` in the same deploy. A statement timeout is logged and the next run continues.
- Watchdog: the two checks about the old pieces are removed (the pack shrank from 23 to 21 then, and to 20 on 22-10-2026). The restore check also reads dumps made before the deploy, where the finished flights are still `flight_record`.
- Removed tools of the old model: `ops/compare_flights_89.sql`, `ops/compare_views_89_phase3.sql`, `ops/audit/panel_recompute.py`, `logon_precision_impact.py` and the two throwaway SQL drills. `ops/backfill_flight_record.sh` is `ops/backfill_flights.sh`; `ops/audit/recompute_flights.py` now checks the v1 rule.
- Tested against a throwaway PostgreSQL before the deploy (see the conformance document) and rehearsed on a restore of a live dump: the views were identical by row count and md5 before and after, and 13 object checks were true.

## Exit test of phase 1 (#138): comparison B against v1, recorded 13-10-2026

Read-only exports from v3 and v1, matched in Python (`compare_raw.py`; method in `docs/qa_89/README.md` section 2 on branch p1-qa, comments on GitHub issue #138). Window 08-10-2026 08:00 to 11-10-2026 03:00 UTC (both systems collecting; v1 was off 11-10 03:00 to 12-10 about 15:30); the power cut of 09-10 15:30 to 15:50 and 30 minutes at each edge excluded; every flight ended at least 30 hours before the run. v3 had phase 1b live (since 13-10-2026 08:01 UTC) when this was measured.
- Against v1's raw positions, rebuilt with v1's own rule: v1 998 flights, v3 977. **974 of 998 match one to one, 0 split by v3.** v1-only 24 (23 amended flight plans and 1 flight that began before the window); v3-only 3 (1 amendment mirror and 2 flights of 4 and 1 positions that v1 never recorded).
- Against v1's summary table (less complete): v1 1008, v3 996; 966 exact plus 18 with a logon a few minutes apart; 0 split, 0 merged; v1-only 24; v3-only 12. The 12 v3-only are flights whose raw positions v1 holds but whose summary v1 never wrote.
- Timing on the 974: v1's first row is never earlier than v3's start (median 1.1 minutes later). Of 127 flights whose start differs by more than 5 minutes, 120 are explained (90 positions before the plan was filed, 30 plan changes inside one connection) and 7 are not individually traced. 12 flights where v1's last row is later than v3's end are plan amendments (the first comment's "up to 3.9 minutes" was corrected to 5 to 130 minutes).
- The owner agreed in chat on 13-10-2026 that comparison B passes and that the leftovers are explainable (recorded on #138; the issue stays open until the owner closes it). Comparison A was rerun right after the pre-deploy dump on 13-10-2026 at about 10:02 UTC, before phase 5: sums equal, all 63 old-only and 53 new-only rows classified (25 airborne cap, 18 frozen plan time, 10 eight-hour cuts), none unclassified.

## Issue 89 phase 4: the raw-position drop gate checks the written flights (written 12-10-2026; deployed 13-10-2026, marked commit 7a71d30 at 10:05:56 UTC)

Changes when old daily partitions of `pilot_position` may be deleted (migration 20261013000210). Refs #89 and #140; design in docs/DESIGN_89_FINAL.md section 3 (branch opus-89), requirement-to-test table in docs/CONFORMANCE_89_PHASE4.md.
- A day is dropped only when every session with positions in it has been processed by the flight writer, every position of a session with a flight plan lies inside a written flight of that session, and every flight wholly inside the day has the right sample count. Any doubt keeps the day and logs `DROP REFUSED`. The old finalisation of the pieces table is no longer a condition.
- New internal function `unreconciled_partition_sessions()` (read-only; used by the drop function and the rehearsal), new integrity checks `processed flight-plan sessions in no written flight (ended 2 to 30 days ago)` and `raw-position days kept by the deletion check for more than 3 days` (23 checks when phase 4 was written; phase 5 removed the two checks of the old pieces table, so the live pack has 21), rehearsal script `ops/rehearse_drop_gate.sql` and runbook [docs/RUNBOOKS/drop-gate-rehearsal.md](docs/RUNBOOKS/drop-gate-rehearsal.md).
- One partition is dropped per call, and the collector loops over the days that are due with a 0.5 second pause. The rehearsal (runs 1 and 2, `docs/RUNBOOKS/drop-gate-rehearsal.md`) was done on a restore of a live dump before the deploy. Migration 20261013000210 applied in 33 ms; `drop_gate_refusal` has 0 rows and the first collector maintenance run after the deploy dropped 0 partitions with 0 failures.
- First possible raw-position drop: about 05-04-2027, at 180 days retention (read on the VM; nothing is due before then, so the deploy deleted nothing). The owner accepted on #140 (13-10-2026) that the lock-hold time of one drop at live volume (about 78,000 positions a day) is measured before that first real drop, not before the deploy; it has not been measured.

## Issue 89 phase 3: the charts read the written flights (deployed 13-10-2026, marked commit 38e3958 at 08:14:12 UTC)

Migration 20261013000200 switches the flight readers to `flight_record` (refs #89 and #139; design in docs/DESIGN_89_FINAL.md). It needed phase 1 deployed and the backfill finished first; the exit-test evidence is in the entry "Exit test of phase 1 (#138)" below. Live since 13-10-2026 08:14:12 UTC; the live database has 24 views including `flight_legacy`, and the `flight` view definition reads `flight_record` (read-only checks on 13-10-2026).
- `flight` becomes a thin view over `flight_record`; `flight_ext`, `movements`, `cohorts` and `monday_attendance` follow it. The old merging view is kept as `flight_legacy` for comparison only (no panel may read it). The charts now show a flight only when it is finished: expect the newest flights to appear about 8 hours after the pilot disconnects.
- Nothing is shown in progress: a finished flight appears in the charts about 8 hours after its pilot's last connection ends. The `NOT is_in_progress` filter is removed from the 11 panels that carried it and from `flight_ext`; `flight` loses `gap_segment`, `span_part` and `is_in_progress`.
- Monday attendance follows the standard rule (distinct pilots of finished flights overlapping 07:00 to 12:00Z); a Monday is complete about 8 hours after the last pilot in the window disconnects.
- A connection longer than 8 hours, or with a long silence, is one flight (v1's rule; about 0.5% fewer flights).
- Old against new on the same data: `tests/integration/test_flight_views_switch.py` (each affected view) and `ops/compare_views_89_phase3.sql` (for the owner, read only). Requirement-to-test table: docs/CONFORMANCE_89_PHASE3.md.

## Issue 89 phase 1b: quiet per flight, not per pilot (deployed 13-10-2026, marked commit 3d4b5b6 at 08:01:09 UTC)

Owner decision 12-10-2026. Migration 20261012000200 replaces `write_finished_flights()`: a flight is written once the flight itself has had no position for 8 hours of stored data, even when the same pilot is flying again in a later flight. The first live run showed 141 pilots, online again within 8 hours, holding back all their older finished flights under the per-pilot rule. Same v1 join rule, same airborne rule, same locking. The backfill script's remaining-work query and the two watchdog checks about unprocessed flights now work per flight (the "pilots quiet..." check is renamed "flights quiet..."). Explained in [docs/DEFINITIONS.md](docs/DEFINITIONS.md) section 1b with a worked example; decision recorded as ADR-030 in [docs/DECISIONS.md](docs/DECISIONS.md); conformance in docs/CONFORMANCE_89_PHASE1.md. Live result recorded on issue #138 (13-10-2026): migration 20261012000200 applied, the backfill wrote 9 flights with none left, and `flight_record` held 1776 flights with 226 sessions of 149 pilots in flights still active (correct: a flight is written only once it is quiet for 8 hours). The false alarm of 257 sessions seen after phase 1 was gone. Refs #89 #137.

## Issue 89 phase 1: flights written once (deployed 12-10-2026, marked commit c70e72f at 17:58:21 UTC; its per-pilot quiet rule was superseded by phase 1b above, live since 13-10-2026)

Adds the table `flight_record` and the writer `write_finished_flights()` (migration 20261012000190), run side by side with today's pieces table. Nothing reads the new table yet, so no chart changes. Refs #89 and #137; design in docs/DESIGN_89_FINAL.md (branch opus-89), requirement-to-test table in docs/CONFORMANCE_89_PHASE1.md.
- In the first version a pilot's flights were written, once, 8 hours of stored data after the pilot was last seen (phase 1b, live since 13-10-2026, changed this to 8 hours after the flight's own last position); the rule for joining reconnects is v1's (same pilot, callsign, departure and arrival; joined when the new connection logged on within 120 minutes of the last point).
- `pilot_session.flown_upto`, the setting `flight_quiet_hours`, `ops/backfill_flight_record.sh`, `ops/compare_flights_89.sql` (comparison A) and `ops/compare_flights_v1_89.sql` (comparison B, for the owner to run), three new watchdog checks, and `flight_record` in the restore check and the `watchdog_ro` grant.

No deploy happened on 10-10-2026. The VM stayed on commit 3eb81a3 (deployed 09-10-2026) all that day; the work merged on 10-10-2026 went live in the deploys of 11-10-2026.

## Merged 09-10 to 10-10-2026 (deployed 11-10-2026)

The items below were written while they were still undeployed. They are now live: they shipped in the deploy marked at 12:38:39 UTC on 11-10-2026 (marked commit 34e5194), which contains the branch tips named below. The original plan to ship them in separate steps (deploys 1 and 2 at commit 520dc3e, then fix-sql) was replaced by that single deploy; the repository does not record the owner's reason or the step-by-step checks of that deploy (listed under "Things this page could not confirm").

### fix-ops (merged about 08:42 UTC on 10-10-2026, branch tip d0ceaaa; independently reviewed in two rounds)
Safer backups, restores, roles and alerts. Fixes issues #81, #83, #84, #86, #90, #91, #95, #96, #97, #101 and #102.
- A backup that fails its own check is no longer kept as good, and `pg_dump` and its check have time limits.
- The restore test is stricter: it refuses a dump that is empty or stale, not only one that is missing tables.
- The database roles are applied in one go, and the collector keeps the right to delete rows from one table only (`poll_log`) instead of nine.
- Telegram messages that cannot be sent are kept and retried in order instead of being lost; a new check warns when the copy of the backup to a second place has not worked recently, and when the Telegram queue is stuck.
- The smoke test can no longer change data. The test database in CI is built the same way as production. The release workflow only moves the `latest` tag for the highest version.
- Dates in integrity alerts read DD-MM-YYYY.
- Review record: docs/reviews/10-10-2026-fix-ops.md.

### fix-collector (merged about 08:51 UTC on 10-10-2026, branch tip acc55db; independently reviewed in two rounds)
A sturdier collector. Fixes issues #82, #85, #87, #88, #92, #99 and #100.
- Odd or changed shapes in the VATSIM feed are counted as errors instead of stopping a poll.
- Health thresholds follow the polling interval instead of fixed numbers.
- Only one snapshot is written at a time, with a time limit, so a slow write cannot pile up behind the next poll.
- The pilots-in-region count leaves out pilots whose record was rejected.
- Expect a polling gap of about 45 seconds when it is deployed.
- Review record: docs/reviews/10-10-2026-fix-collector.md.

### Plain-language Telegram messages (issue #79; commits 3c8df0e, aacd655, bed4dfa, cdfdc1b; on main 09-10-2026 evening)
Alerts become a short sentence in everyday words with a last line starting `Technical:` for the detail, labelled "heads up", "problem" or "all clear". When an alert clears, the message says how long it lasted. Independently reviewed. Details in [docs/RUNBOOKS/alerts.md](docs/RUNBOOKS/alerts.md).

### fix-sql (on main by the evening of 10-10-2026; branch tip when reviewed b9f0170; the merge time is not recorded)
Three database and dashboard fixes and one measurement. Fixes issues #93, #94 and #98; #89 is measured and documented only. Independently reviewed in two rounds (round-2 record is on branch `review-fix-sql-r2`; its low follow-ups are issue #112).
- Controllers who log back in no longer count as "reactivated" unless they have an earlier session (ADR-028, confirmed by the owner; the live count goes from 53 to 0 until 90 days of history exist) (issue #93).
- The Monday event panel shows this Monday, not last Monday, before 07:00 UTC (issue #94).
- Dropping old position partitions is rewritten as migration 20261009000170: it takes locks without waiting, re-checks each partition before dropping and drops at most 10 per call; `maintenance.py` repeats the call. The first real drop is not due until about 06-01-2027 (issue #98).
- The flight view's growth with history is measured (docs/FLIGHT_VIEW_SCALING.md); the fix is future work (issue #108).
- The first real partition drop is still not due until about 06-01-2027 (that was the date under the 90 day retention; at 180 days since 13-10-2026 it is about 05-04-2027).

### Replay and fuzz tests (issue #103; on main 10-10-2026, last commit 27cb266 at 20:39 UTC)
Tests only; nothing to deploy. Two real raw-feed snapshots are kept as fixtures, with a replay tool, seeded fuzz generators, parser fuzz tests and ingest tests against Postgres. They found two real defects, now issues #111 (oversized multi-byte callsign) and #114 (infinite controller frequency), pinned as expected failures until fixed. The full fuzz grids run in CI.

### Documentation (10-10-2026)
- Batch 1: `docs/IF_CLAUDE_IS_UNAVAILABLE.md` (since removed), the documentation index [docs/README.md](docs/README.md), this changelog, and fixes to ten stale statements (issue #110).
- Owner card for the off-VM backup copy on the Proxmox host (`docs/OWNER_BACKUP_COPY.md`, since removed, 10-10-2026 16:45 UTC; ADR-026 item 7; not yet run).
- Deploy plan pins the commit to ship (520dc3e) and the owner decision that a hung collector with no code change may be restarted plainly (issue #110).
- Batch 2 (RESUME.md rewritten short, tracker rows T66 to T71, working agreement additions, this section): branch `docs-batch-2`.

### Also on main, no effect on the running system
- Collector size budget raised from 1700 to 1800 lines (ADR-027, 10-10-2026).
- Retention rules decided (ADR-026, 09-10-2026): raw positions kept 180 days, sessions, flights and controller data kept forever, and more. Decided but not built yet (issue #21).
- Working agreement and team briefs (since 18-10-2026 these team files live outside the repository, in the owner's Claude working folder), the deploy plan and the cold-start prompt.
- CI and release workflow fixes (a line-length error had kept CI red for four commits on 10-10-2026).

### Collector fixes for oversized and odd feed values (10-10-2026, c89e544, PR 117; issues #111 and #114)
A callsign, departure or arrival is capped in bytes and an index-size error is bisected out like a bad row; an infinite or huge controller frequency is treated as malformed. Before, either could lose the whole snapshot on every poll. Found by the replay and fuzz tests; the expected-failure pins are removed. Deployed in the 11-10-2026 12:38 deploy.

### Read-only deploy checks (10-10-2026, 991ddc0, PR 116; issue #104 trimmed)
`ops/deploy.sh` with `--preflight`, `--verify`, `--status` and `--mark` (a release marker on the VM). Reading checks only; every deploy still needs the owner's go. The runner and automatic rollback are parked.

### Second review of deploy 1 (10-10-2026)
An Opus review of privileges and backup and restore says go with two extra checks, now in docs/DEPLOY_PLAN_REVIEW_FIXES.md (record on branch `review-fix-ops-opus`). It also filed issue #118 (backup validation accepts a dump of an empty or wrong database).

### Not on main
- Incident 10-10-2026 (issue #113): two snapshots were stored late (apply timeouts at 15:47 and 15:52 UTC) because several test containers ran on the live VM at once. No data was lost; new rule in the working agreement.

## Deployed

### 11-10-2026 16:01 (marked): faster "online now" and a stricter backup check (commit aeaf72f; issues #109 and #118)
- `online_now`, `pilot_latest` and `pilot_online_now` look up the newest position of each session with a `LATERAL ... LIMIT 1` lookup instead of probing every position partition, and the read-only Grafana role has JIT off (migration 20261011000180, commit 60d848c; issue #109). Results are meant to be identical; the speed-up matters once ADR-026 retention creates about 190 partitions.
- A backup is only kept if it holds real data: at least 50 percent of the size of the newest good dump (or at least 1 MiB when there is none), data entries for the key tables, and rotation never leaves fewer than 3 good dumps (settings `BACKUP_MIN_SIZE_PERCENT`, `BACKUP_MIN_FIRST_BYTES`, `BACKUP_MIN_KEEP`; issue #118, commits 9c3d866 and 3caf4bd). The backup container was recreated at 15:56 UTC (container start time).
- Rollback copy on the VM: `~/vatpac-statsV2.rollback-11-10-grafana` (15:55).

### 11-10-2026 12:38 (marked) and 12:47 (marked, documents only): the combined deploy of everything merged on 09-10 and 10-10-2026 (commits 34e5194, then b2a4cd6)
- Contains fix-ops, fix-collector, the plain-language Telegram wording, fix-sql (migration 20261009000170), fix-111-114 and the read-only deploy checks, all described in the section above, plus the collector changes of the morning of 11-10-2026:
- **A hard floor of 30 seconds between any two requests to VATSIM**, retries and discovery included (`MIN_REQUEST_GAP_S` in `feed.py`, commit 4d5b16e). Owner rule: never overpoll the feed. Once the feed has stalled for a full interval the collector polls again at the normal interval instead of faster (commit 0c720de), and waits for the feed end at once on shutdown (commit f780b20).
- The collector and watchdog containers were recreated about 12:35 UTC (container start times, restart counts 0). The 12:47 mark covers a documents-only update (reviews kept in docs/reviews).
- Rollback copies on the VM: `~/vatpac-statsV2.rollback-11-10-all2` (dated 11-10 12:46, fits this deploy). `-all` is dated 09-10 19:13, so its contents were not verified and it may be an older copy.
- Context, not caused by the deploy: the Proxmox host restarted or crashed at about 08:49 UTC on 11-10-2026 (database container start time, verified), and the owner reported the VATSIM feed frozen from about 03:08 to about 12:00 UTC (not verified here); both left a data gap. The invariant checks that count snapshot gaps are expected to clear about 14-10-2026.

### 11-10-2026 20:35: stable public links and short addresses (commit b3189ad)
The ten public dashboard links now have fixed tokens (the first 32 hex characters of sha256 of `vatpac-public:` plus the dashboard uid), so they no longer change when recreated, and ten short addresses (`/live`, `/traffic`, `/new-pilots`, `/origins`, `/atc`, `/atc-retention`, `/monday`, `/military`, `/monthly`, `/health`) redirect to them. The fixed `ops/grafana_public.py` (finds Grafana through `GRAFANA_BIND` and `GRAFANA_PORT`; `--traefik-labels --host` prints the Traefik labels) is on the VM tree; the 10 links were replaced once by the stable ones. The owner pasted the label block into `~/vatpac-proxy/docker-compose.yml` on the Traefik machine and recreated the container. Checked from outside through Cloudflare: each short address answers 302, the dashboard loads (200), anonymous API calls answer 401. The hostname `vpac.mpengor.com` is confirmed. See [docs/RUNBOOKS/public-access.md](docs/RUNBOOKS/public-access.md).

### 11-10-2026 about 19:40: Grafana 12.4.12 and public read-only dashboards (commit 848526c)
Grafana was upgraded from 11.6.5 to 12.4.12 (image `grafana/grafana`, the OSS edition, because the 11.6 line ended with 11.6.15 on 03-06-2026 and `grafana-oss` stops at 11.6.5); it was tested first on a copy of the live volume. All ten dashboards are now published read-only on the internet through Grafana public dashboards (one link each, fixed time range, anonymous access off; owner decision, ADR-029) via Cloudflare, a Cloudflare Tunnel, Traefik and a small helper container on the owner's other machine. The Departure board is fixed to YSSY. Links are managed by `ops/grafana_public.py`. Rollback copies are on the VM (`~/grafana_data_pre_12.tgz`, `~/vatpac-statsV2.rollback-11-10-grafana`). Setup: [docs/RUNBOOKS/public-access.md](docs/RUNBOOKS/public-access.md). 

### 09-10-2026 19:13: scheduled data-integrity checks (commit 3eb81a3)
The watchdog now runs a set of 18 data checks every 3 days and compares the live data with the saved raw feed every 14 days, and sends a Telegram message if either finds something (issue #20, closed). Only the watchdog was rebuilt; the collector was not restarted. The first run found one thing, the 6-minute gap from the power cut below. It clears from the 3-day window on 12-10-2026 at about 15:42. The owner confirmed the Telegram message arrived.

### 09-10-2026 about 16:30: military aircraft card 65 rebuilt from v1's list (migration 20261009000160)
The "Most popular military aircraft" panel now uses the same list of aircraft types as v1 (issue #16, closed). Database change and dashboard file only; no container restarted. Deployed commit not recorded in the tracker (built in e208565 and the dashboard changes around it).

### 09-10-2026 about 15:57: movements comment (migration 20261009000150)
A comment-only database change that states the decided behaviour of airport movements (ADR-018) (issue #32, closed). No container restarted. Deployed commit not recorded in the tracker (built in 0e98b26 and 0d2c24e).

### 09-10-2026 15:36 to 15:42: unplanned power cut (not a release)
The Proxmox host lost power; the owner restarted the VM at about 15:41. Everything came back unaided, the database recovered cleanly and there was one snapshot gap of about 6 minutes (15:36 to 15:42). This counts as a second reboot test.

### 09-10-2026 13:46 to 13:47: image updates (commit 8905d85, carrying aecec21)
Postgres 16.4 to 16.15 (database and backup images), Grafana 11.2.2 to 11.6.5 and dbmate 2.21.0 to 2.36.0 (issue #31, closed). Postgres 17 and 18, Grafana 13 and numpy 2.x were deliberately skipped. The database and Grafana containers were recreated.

### 09-10-2026 12:24: fixes bundle (commit a600905)
- Flights that are still connected are left out of the flight counts, as they cannot yet be judged complete (ADR-025, issue #38).
- The pilot-origins panel names rating 11 "Supervisor" and 12 "Administrator", as in the feed (issue #39); the collector-health panel describes rejected values correctly (issue #71).
- Raw-feed archive fixes: the size cap removes routine files before anomaly files, a failed write cleans up after itself and backs off, snapshots rejected for being from the future are kept as anomalies, and the archive stops cleanly on shutdown (issues #65 to #69).

### 09-10-2026 11:58: collector release, flight merging and 30-second polling (commit da864f9)
- **Polling every 30 seconds instead of 15** (ADR-024, issue #42). Everything that depends on the interval (the gap limit for airborne time, the "online now" windows, the soak report, the missed-snapshots panel) is derived from one setting, `POLL_INTERVAL_SECONDS`. For 24 hours after this the 24-hour coverage figures read wrong.
- **Reconnects of one flight are merged into one flight**, as v1 did (ADR-023, issue #35): the same pilot, callsign and airports within 120 minutes. Flights on the dashboards went from 234 to 177 in a simulation on 08-10-2026 data (v1: 173).
- **Collector hardening and the rolling raw-feed archive**: a compact, hourly and anomaly archive of the raw feed so that odd events can be replayed later, plus watchdog alerts for an identity flap and a stalled archive, and several defence fixes (infinite or not-a-number coordinates, snapshots dated in the future, free-text length caps) (issues #34, #36, #37, #40, #41, #43, #61 to #64).
- Deployed after an independent release review (GO with two conditions: the size-budget check green and a stop-then-build deploy).
- The v1 against v3 comparison was completed the same day: no v3 defect found (docs/V1_COMPARISON.md).

### 08-10-2026 17:10: duplicate-session fix (commit c071955)
At about 04:08 on 08-10-2026 the VATSIM feed changed the precision of logon times and 31 pilot and 3 controller sessions were stored twice. Session identity now uses whole-second logon times (ADR-021, issue #15, closed); a migration merged the twins (31 pilot and 3 controller pairs, no positions lost) and a check was added (`ops/check_identity_flap.sql`).

### 08-10-2026 12:37: dashboards rebuilt from v1 (commit fec82a4)
- The controller retention dashboard rebuilt from v1's real SQL (13 cards) and 16 more cards from v1 added (the list is in docs/CHART_DEFINITIONS.md), cards 94 and 95 matched to v1's counting, card 90 aligned with v1.
- New database views for these, the unused `controller_activity_summary` view removed, and the Grafana read-only role pinned to UTC.
- Pre-deploy dump taken; only the collector was recreated; checks passed (84 panel queries, 0 errors).
- Goal checks recorded the same day: a 12-hour soak PASS at the owner's standard (08-10-2026), and a full 24-hour soak PASS on 09-10-2026 at 07:23.

### 07-10-2026: first live deployment
The first working stack (collector, Postgres, Grafana, backup, watchdog) was built and deployed to the VM on 07-10-2026, and live data started at 21:38 UTC (no history was copied from v1: ADR-011). The same evening the reboot test passed at 21:48 and a Telegram test alert reached the owner. The exact commit and time of this first deploy are not recorded in the repository. v1 stays running on its own VM as a read-only reference.

## Earlier work, same day as the first deployment (07-10-2026, before the first live data)
Specification and scaffold (commits e5616a5 and 450b0fb, about 16:10 UTC), the full first build of collector, schema, dashboards, compose stack, ops scripts and tests (d6d4398, about 19:32 UTC), an architecture and docs pass (5aff4f3), and a round of review fixes: flights as an incrementally maintained table, least-privilege database roles, collector hardening, the soak report (1587ba9, 7c361d4). Decisions ADR-001 to ADR-019 date from this day.

## Things this page could not confirm
- The checks run at each of the four deploys marked on 11-10-2026 (smoke, panel check, restart counts, watch period): the VM's release record holds only the commit and time, and no deploy report for 11-10-2026 is in the repository. Which of the changes in the 12:38 deploy were applied at that mark, and which at later ones, is inferred from the branch contents of the marked commits and the VM's file and container times.
- Whether the plan of separate deploys 1 and 2 was dropped by an owner decision (not recorded in the repository).
- The result of the 12-10-2026 pending checks (collector maintenance at about 12:35, the 02:00 backup under the new content check, first message in the new wording): not read for this page.
- A Grafana 12.x first-start note: it downloads default plugins from grafana.com (reported in the session handover of 11-10-2026, not checked here); the owner has not yet decided whether to switch that off.
- The commit and time of the first deploy on 07-10-2026 (see above).
- The deployed commits of the two small database-only changes on 09-10-2026 (about 15:57 and about 16:30); the tracker gives only the times.
- Whether any deploy happened between 07-10-2026 and 08-10-2026 12:37 other than the first: the tracker and ARCHITECTURE.md record none.
- Merge times are git commit times converted to UTC (git records them in British Summer Time, one hour ahead of UTC); deploy times come from the tracker rows and were not re-read from the VM, except that the VM's running containers (database, collector, grafana and backup up for about 24 hours, watchdog for about 20 hours at 15:24 on 10-10-2026) are consistent with the deploys above.
