# Resume note for a cold start (rewritten 10-10-2026, issue #107 batch 2)

Read this first. A new session pastes [NEXT_SESSION_PROMPT.md](NEXT_SESSION_PROMPT.md). Then read [WORKING_AGREEMENT.md](WORKING_AGREEMENT.md) (team, flow, tiers, limits), [TEAM_BRIEFS.md](TEAM_BRIEFS.md) (how to brief each role), [DEPLOY_PLAN_REVIEW_FIXES.md](DEPLOY_PLAN_REVIEW_FIXES.md) and the live task list [TRACKER.md](TRACKER.md). Dates DD-MM-YYYY, times UTC, British English, no emoji. Facts below are as of 10-10-2026 about 21:00 UTC, taken from `origin/main` (c89e544), git history, GitHub and a read-only look at the VM.

Goal (owner, verbatim): "I want to be able to reproduce these graphs and have them live and available 24/7. I want the system to be dockerised."

Work in the folder `vatpac-statsV2` (v3), and inside it only in a git worktree under `.worktrees/`. The folder `VATPAC-Stats V2` (capital S, space) is the owner's ORIGINAL v1 system: read-only reference, never edit or delete it. Check `pwd` before every write.

## 1. State in one paragraph
The goal is met and the system is in routine operation (checklist at the end of TRACKER.md; evidence in ARCHITECTURE.md section 10). Postgres, collector, Grafana, watchdog and backup (plus the one-shot migrate job) run on the always-on VM: ssh alias `vatpac-v3`, user matt, no sudo. Grafana is on the VM's LAN address, port 3300, admin password in the VM's `.env` (never print `.env` values). Live data started 07-10-2026 21:38 UTC, no backfill from v1. The old v1 system is still UP on Proxmox VM 111 (ssh alias `vatpac`, containers vatsim_app and vatsim_postgres): read-only reference, SELECT-only rules in V1_COMPARISON.md section 1. Main holds a lot of reviewed work that is NOT deployed; the next job is to deploy it in the agreed order, each step on the owner's explicit go.

## 2. What is on the VM, and what is only on main
**Deployed (the ONE commit on the VM): 3eb81a3**, shipped 09-10-2026 19:13 (scheduled integrity checks). The VM tree `~/vatpac-statsV2` is not a git checkout (git is installed; no GitHub credentials on the VM). Checked 10-10-2026: its newest migration is 20261009000160 and the main containers had been up about 29 hours. Deploy records: TRACKER (T7, T16, T41 to T53), ARCHITECTURE section 10, [../CHANGELOG.md](../CHANGELOG.md).

**On main, CI green, NOT deployed** (each item is in the CHANGELOG with its date):
- `fix-ops` (d0ceaaa): backup validation and timeouts, roles applied in one transaction, Telegram outbox with retry, new watchdog checks, stricter restore check, CI and release fixes. Review: reviews/10-10-2026-fix-ops.md.
- `fix-collector` (acc55db): robust feed parsing, health thresholds from the poll interval, one apply at a time with a deadline. Review: reviews/10-10-2026-fix-collector.md.
- Plain-language Telegram wording (#79).
- `fix-sql`: migration 20261009000170 (lock-friendly partition drop, looped by `maintenance.py`), ADR-028 (reactivated controllers need an earlier session; the live count goes from 53 to 0 until 90 days of history exist), Monday event panel fix (#94), flight-view measurements (FLIGHT_VIEW_SCALING.md). Its round-2 review record is on branch `review-fix-sql-r2`, not on main; the low follow-ups are issue #112.
- Replay and fuzz tests (#103): tests only, no deploy effect.
- `fix-111-114` (c89e544, PR 117): #111 byte cap on callsign, departure and arrival plus a ProgramLimitExceeded bisect; #114 infinite or huge controller frequency no longer loses the snapshot. Medium tier; deploys after fix-sql.
- `deploy-checks` (991ddc0, PR 116): `ops/deploy.sh` with read-only `--preflight`, `--verify`, `--status`, `--mark` (RUNBOOKS/deploy-to-vm.md, Scripted checks). The runner, rollback and Low-without-go policy stay parked on branch `design-deploy`.
- Docs batch 1, the stale-statement fixes (#110), the backup copy card (OWNER_BACKUP_COPY.md), the deploy plan.

**WHICH COMMIT TO SHIP.** Deploys 1 and 2 ship the exact commit **520dc3e** (fix-ops, fix-collector, wording; migration 170 absent), never the tip of main. Check: `git ls-tree -r --name-only 520dc3e db/migrations | grep 20261009000170` must print nothing. The fix-sql deploy ships the tip of main afterwards. Detail: DEPLOY_PLAN_REVIEW_FIXES.md.

**Not on main yet:** `design-109` (architect, #109), `design-deploy` (parked full design for #104, not to be built). Docs batch 2 is branch `docs-batch-2`.

## 3. The owner's standing rules
1. **Every deploy needs his explicit go on the day**, whatever the tier. Never work round a denied action; report it.
2. **Models:** Opus 5.5 only for complex database and code changes, never alerting or docs; Sonnet for the rest.
2b. **Main must stay green.** No push to main until the change's database tests have passed; use a pull request so CI runs on the branch first (owner OK 10-10-2026). Read CI after EVERY push to main, docs-only included (CI was red unnoticed for four commits on 10-10-2026).
3. **Independent review** of every change by a different agent than the author; evidence (commits, CI, counts), not summaries. Tiers: WORKING_AGREEMENT section 5.
4. **Token economy** (10-10-2026): short replies, short briefs, reports under 300 words, few agents, cheaper models for mechanical work. Hats not staff (WORKING_AGREEMENT 2a).
5. **No new test infrastructure.** A separate test VM, staging copy, golden dataset, weekly replay and a restore-test extension were all DROPPED on 10-10-2026. Do not re-propose them.
6. One decision at a time, recommendation first, plain language; plan before multi-step work; file every defect as a GitHub issue and a tracker row without asking; ask before closing issues. Never expose Grafana publicly.
7. At most 3 code branches in flight; docs-only branches do not count (owner 10-10-2026).

## 4. Next actions, in order
1. **Sunday 11-10-2026 after 02:00:** read the restore test result (`.last_restore_test`, watchdog `restore_test`) and the 02:00 backup, then ask for the go for **deploy 1** (fix-ops plus wording, high tier, exact commit 520dc3e). A second (Opus) review of deploy 1 says go with two extra checks, now in DEPLOY_PLAN_REVIEW_FIXES.md (record on branch `review-fix-ops-opus`). `ops/deploy.sh --preflight` and `--verify` are read-only checks that run from the PC clone of main (they are not in commit 520dc3e and not on the VM); use them from there.
2. **Monday 12-10-2026:** **deploy 2** (fix-collector, medium tier, polling gap about 45 s) after deploy 1's 24 h watch is clean and he says go.
3. Then the **fix-sql deploy** (high tier: a function that drops tables) from the tip of main. #109 must be fixed before retention goes to 180 days.
4. **v1 re-comparison**: DELAYED by the owner until 12 hours after ALL fixes are deployed. Read-only, predictions committed first, his go on the day. Then a final dump of v1 and stop v1 (his go).
5. **Retention build** (#21, ADR-026): POSITION_RETENTION_DAYS=180 before the first drop about 06-01-2027, WATCHDOG_DB_MAX_GB=5, network_snapshot 5-year trim, BACKUP_REMOTE copy (the owner does the Proxmox side: OWNER_BACKUP_COPY.md), clean-up of about 70 leaked test volumes from an approved list.
6. **Dated:** #27 on 14-10-2026 and 06-11-2026; #22 after 19-10-2026; #25 on 06-11-2026; delete the VM rollback copies about 16-10-2026 (`~/vatpac-statsV2.rollback-09-10-bumps`, `-mig150`, `-card65`, `-integrity`, and volume `vatpac_grafana_data_pre_bumps`; list names first); #18 needs a time from the owner. The data_integrity warning from incident #113 clears on 13-10-2026 about 15:52.
7. Writer duty: after each deploy is verified live, switch in the prepared changes (docs/PENDING_DOC_CHANGES.md, on branch `docs-audit`) and update RESUME, TRACKER, CHANGELOG and runbooks within one working day (WORKING_AGREEMENT section 2b). Documents describe the deployed state only.

## 5. Open issues (41 open on 10-10-2026 night; `gh issue list --repo t288matt/vatpac-statsV2 --label P1`)
Labels: P1 deploy path, fixed on main, needs a verified deploy; P2 next build, in order; P3 soon, small; P4 dated, owner-only, later or low value.
- **P1** (stay open until deployed and verified; the owner closes): #79, #80 and findings #81 to #102 (fix-ops: #81 #83 #84 #86 #90 #91 #95 #96 #97 #101 #102; fix-collector: #82 #85 #87 #88 #92 #99 #100; fix-sql: #89 #93 #94 #98), and #114 (fixed on main, not deployed; also P2).
- **P2:** #21 retention, #109 partition probing, #112 fix-sql follow-ups, #114, #118 (backup validation accepts a dump of an empty or wrong database). Closed 10-10-2026: #103, #110, #113 (with the owner) and #111 (callsign byte cap, fixed on main, not deployed).
- **P3:** #18, #104 (trimmed, checks merged), #107 (this writer work).
- **P4:** #14, #22, #23, #25, #26, #27, #76, #105, #108.
- **Owner only:** #26 Proxmox restore test of VM 100 (never boot a restored copy on the LAN); #23 folder rename; heartbeat task check; the Proxmox side of the backup copy; SSD wear check (#14); VM memory (8 GiB, live stack about 0.65 GB; reduce only after tests are off the VM).

## 6. Tools and test method
- **gh**: `"C:\Program Files\GitHub CLI\gh.exe"` (Git Bash: `"/c/Program Files/GitHub CLI/gh.exe"`), always `--repo t288matt/vatpac-statsV2`. Read freely. `gh issue view N` may print nothing: use `--json title,body,comments`. The token is repo-scoped, cannot merge pull requests, read check annotations or administer.
- **Shell**: use Git Bash. `bash` on PATH in PowerShell is WSL and breaks the script tests.
- **Unit and static checks** from the repository root: `python grafana/validate_dashboards.py`, `python grafana/check_columns.py`, `python ops/check_config_drift.py`, `python ops/check_size_budget.py`, then `PYTHONPATH=collector/src python -m pytest -q -m "not integration and not slow_fuzz" tests collector/tests`; also `ruff` and `mypy` (RUNBOOKS/run-tests.md section 1). Strict xfails show as xfailed.
- **Integration tests** need a throwaway Postgres 16.15 with its OWN cluster and a database name ending `_test`; recreate the method from run-tests.md section 3 (CI does the same through the production migrate image, UTC pass and Australia/Sydney pass). The PC has no working Docker (#30 won't fix), so they run capped on the VM or in CI. The fuzz grids marked `slow_fuzz` run in CI.
- **On the live VM**: ONE test container at a time, load average below 2.0, `--memory` and `--memory-swap` caps, CPU cap, timeout, `free -m` before and after, removed afterwards (incidents 08-10-2026 T39 and 10-10-2026 #113). Check `docker ps` for names not `vatpac-*`. NEVER run `db/roles.sql` on the live cluster. Never `docker compose restart` for a code change.
- **Deploy procedure and traps**: RUNBOOKS/deploy-to-vm.md. In short: fresh dump, rollback copy, clock check, `git archive` of the exact commit (tar never deletes: remove deleted files by hand; `.env` is not in git), stop collector then `up -d --build`, then `sh ops/smoke.sh`, `python3 ops/check_panels.py` (0 errors), restart counts, `/health` archive block, `ops/check_identity_flap.sql`.
- **Outside heartbeat**: Windows task `VatpacV3Heartbeat` on the owner's PC (runbook outside-heartbeat.md); pause it before a planned VM restart.
- **Local `main` in the shared folder is far behind origin.** Always `git fetch`, base work on `origin/main`, use `git worktree add -b <name> .worktrees/<name> origin/main`. After a session limit, look for orphaned worktrees, branches and non-vatpac containers first.

## 7. Behaviour you must know (all deployed)
- Polling every 30 s, held in ONE variable `POLL_INTERVAL_SECONDS` (ADR-024); never hard-code 15 or 30.
- Flights merge reconnects within 120 minutes (ADR-023); in-progress flights are excluded from flight counts (ADR-025); session identity uses whole-second `logon_time` (ADR-021); parked sessions still count as flights (ADR-018).
- Names and CIDs are public data kept forever (ADR-019); tokens and passwords stay secret. The Telegram bot token was exposed once in a transcript and not rotated (accepted risk, T4).
- The v1 comparison is DONE (V1_COMPARISON.md): no v3 defect; owner decisions in its section 11. Other decisions: DECISIONS.md (ADR-001 to ADR-028).

## 8. Scheduled and pending set-up
- Daily digest: task `vatpac-daily-digest` emails mdorahy@gmail.com at 12:00 UTC. The cron is LOCAL time, `0 13 * * *` while London is on BST: change to `0 12 * * *` on 25-10-2026 and back to `0 13 * * *` on 28-03-2027. It runs only while the Claude desktop app is open.
- Moving the Claude project directory to the v3 folder: owner said NOT YET (after the deploys). Memory lives under `C--Users-mdora-Claude-VATPAC-Stats-V2`; plan: copy it to `C:\Users\mdora\.claude\projects\C--Users-mdora-Claude-vatpac-statsV2\memory\`, copy the 5 allow rules from the v1 folder's `.claude\settings.local.json`, re-sync just before the switch. Folder rename (#23) is optional and later and would change the memory path again.

## 9. Where to find things
Map of every document: [README.md](README.md). Owner card for when Claude is unavailable: IF_CLAUDE_IS_UNAVAILABLE.md. Settings: CONFIG_MAP.md. Tables and views: DATA_DICTIONARY.md. Definitions: DEFINITIONS.md, CHART_DEFINITIONS.md. Owner-only steps: OWNER_ACTIONS.md. Runbooks: RUNBOOKS/. Dated records, do not edit: TEST_AUDIT, DATA_INTEGRITY_AUDIT, RELEASE_REVIEW, RESTORE_DRILL, TRUST_WORK, HANDOVER (history), OWNER_CHECKLIST (obsolete).
