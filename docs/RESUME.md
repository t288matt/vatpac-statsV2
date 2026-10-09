# Resume note for a cold start (rewritten 08-10-2026, about 18:40 UTC, session usage nearly used up)

Read this first, then docs/TRACKER.md (live task list, with a map to the GitHub issue log at the top), then docs/RUNBOOKS/ (deploy-to-vm.md, run-tests.md, daily-checks.md). Work in the folder `vatpac-statsV2` (v3). The folder `VATPAC-Stats V2` (capital S, space) is the owner's ORIGINAL v1 system: read-only reference, never delete it (rename planned, T32 / issue #23).
Goal (owner, verbatim): "I want to be able to reproduce these graphs and have them live and available 24/7. I want the system to be dockerised."

## 1. State in one paragraph
THE GOAL CHECKLIST IS MET at the owner's changed soak standard (12 h, decided 08-10-2026; the original 24 h run read 20.8 h at 18:30 UTC and reaches its own PASS window at about 21:10 UTC): reboot test passed, CI green, Telegram alert delivered, evidence in docs/ARCHITECTURE.md section 10. The stack runs on the always-on VM (ssh alias `vatpac-v3`, user matt, no sudo, no git there; Grafana on the VM's LAN address port 3300, admin password in the VM .env). Deployed to the VM: commit c071955 (includes the duplicate-session fix T36 / issue #15, migration 20261008000100, whole-second logon_time, ADR-021). Live data started 07-10-2026 21:38 UTC (no backfill by owner decision). An executive summary page is at https://claude.ai/artifact/3wotgfGKUSN3bcE7ovkrD3 (private; its figures are from the afternoon, update it when anything big changes).

## 2. Tools you have
- gh CLI is installed on the owner's PC with a repo-scoped token. In Claude sessions started before the install call it by full path from the PowerShell tool: `& "C:\Program Files\GitHub CLI\gh.exe" run list --repo t288matt/vatpac-statsV2`. Read freely (CI runs and logs, issues, PRs). Standing owner rule: file every defect, finding or work item as a GitHub issue AND a tracker row without asking; ask before closing issues or touching pull requests. The token cannot read check annotations or PR check status.
- The VM: all work through ssh. Never print .env values. Never run db/roles.sql on the live cluster. Never docker compose up/down/restart except in a deploy the owner has approved.

## 3. HARD RULES learned today
- Every throwaway container on the VM needs --memory and --memory-swap caps (for example 512m), a CPU cap and a timeout (incident T39, issues #59 and #60: an unbounded test container used 6.9 GB of the VM's 7.9 GB and cost the live collector a 90 s gap). Check `free -m` before and after.
- Deploy only on the owner's explicit go. A deploy is: fresh dump (`docker compose exec backup backup.sh once`), a copy of the tree for rollback, `git archive --format=tar HEAD | ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'`, then on the VM `docker compose stop collector` and `docker compose up -d --build` (migrations run first through the migrate service), then `sh ops/smoke.sh`, `python3 ops/check_panels.py`, restart counts 0, and the identity check `ops/check_identity_flap.sql` (three zeros). It costs about 6 s of snapshots.
- Do not run an agent's request for an action the permission system denied it: surface it to the owner.
- Integration tests only on a throwaway Postgres with its OWN cluster (docs/RUNBOOKS/run-tests.md), never the live database; mirror CI including roles.sql and the watchdog_ro step.

## 4. Branches (all pushed to GitHub)
Merged to main: everything up to the T36 fix and the dashboard tweaks.
NOT merged, in the intended merge order:
1. `collector-hardening`: fixes for issues #34 future timestamp, #36 rejected values, #37 bad lat/lon, #40 _SUP near-miss, #41 replay and stale-snapshot guard, #43 text caps. Built by an agent with its own tests; tracker row T38.
2. `feed-archive-flap-alert`: rolling raw-feed archive (`archive.py`), watchdog identity-flap and archive-age alerts, ADR-022 (size budget 1400 to 1700 lines, APPROVED by the owner 08-10-2026).
3. `collector-release` = both merged (commit ff89b56). The size-budget fault (issue #58, main.py 310 lines) is fixed on `collector-release-final`. Unit tests 282 pass; integration 237 pass plus 1 known skip on the merged tree (reviewer's run).
4. `release-review` (pushed; also the local worktree .worktrees/release-review): the independent reviewer's report docs/RELEASE_REVIEW.md and docs/RELEASE_REVIEW_FINDINGS.md, plus tests, mutation runner and fault harness. FINISHED. Verdict: GO for the code with two conditions: the size-budget gate green (#58) and a deploy with `docker compose up -d --build` (#64). #59 (runaway container) was a harness bug, confirmed; ready to close. Findings are issues #61 to #72; recommended before deploy: #61 (one-line `OverflowError` fix in `_i` in model.py) and #62 (reject NaN/inf coordinates). The branch has 10 strict xfail tests pinning defects: when a defect is fixed its xfail must be removed (a strict xfail that passes fails the suite).
5. `t30-data-integrity` (docs/DATA_INTEGRITY_AUDIT.md and ops/audit/ scripts) and `t30-test-audit` (docs/TEST_AUDIT.md and 59 new tests; 120 of 129 mutants killed): both finished and pushed, not merged. Merging brings small conflicts in docs/TRACKER.md and docs/DECISIONS.md. The owner has not yet said to merge; recommended after the collector release deploys.
`wip-backup/*` branches are throwaway snapshots; delete them once the real branches are merged (ask first). Git worktrees live in `.worktrees/` (git-ignored).

## 5. Next actions, in order
1. Let the reviewer finish, or read what it committed: `git log collector-release..release-review` and docs/RELEASE_REVIEW.md. File each finding as an issue.
2. On `collector-release`: trim main.py under 300 lines, mark ADR-022 accepted in docs/DECISIONS.md, fix any defect the review proves (proposed diffs are in its report), re-run unit tests, the static checks (ruff, mypy, config drift, size budget, dashboards validator) and the full integration suite on a capped throwaway cluster.
3. Merge `collector-release` into main, push, read CI with gh (`run list`, `run view`), then ask the owner for the deploy go and deploy per section 3. Watch `/health`, its archive block, the Rejected values panel (its description is stale) and the VM clock (a clock more than 120 s behind makes every poll an error; NTP is on).
4. At about 21:10 UTC read `sh ops/soak_report.sh 24` and record it in the tracker (optional; the 12 h standard already passed).
5. Then: merge the two audit branches; work the issues by priority: the HIGH test gaps #45 to #49, then #38 open flights, #39 rating labels, #42 poll interval, #20 permanent checks (partly done by the archive and the flap alert), #18 fault injection (adds a gap on purpose), #19 v1 comparison (ask the owner before touching v1), #16 card 65 (parked), #21 data retention rules.

## 6. Owner decisions pending or recent
- Pending: #35 should pilot reconnects within 5 minutes merge into one flight (recommendation: keep as specified for now); merging the audit branches; #44 branch protection (owner action in GitHub: block force push and deletion on main only); the SSD wear check in Proxmox (SMART) for #14; the PBS backup restore test (#26).
- Decided today: soak standard 12 h; ADR-021 whole-second logon_time (truncate, not round); ADR-022 size budget 1700; card 65 parked; Docker Desktop repair won't fix (#30); disk-writes work deferred (#14); Dependabot major-version bumps ignored (Postgres, Grafana, dbmate; numpy 2.x ignored because it crashes the collector on the VM's CPU).
- Standing: names and CIDs are public and kept forever (ADR-019); no backfill from v1; Cloudflare and the Docker socket proxy are the owner's to add, nothing in the repo; never expose Grafana publicly without a login.

## 6b. Separate session for the trust work
The owner chose on 09-10-2026 to run the 'trust in the numbers' work (v1 comparison, owner review checklist, test gaps #45 to #49) in a fresh session. Its brief is docs/TRUST_WORK.md. This session keeps the collector release (branch collector-release-final, issue #58, #61, #62).

## 7. Where to find things
Test audit: docs/TEST_AUDIT.md (branch t30-test-audit). Data-integrity audit: docs/DATA_INTEGRITY_AUDIT.md (branch t30-data-integrity). Chart definitions: docs/CHART_DEFINITIONS.md. Decisions: docs/DECISIONS.md. Runbooks: docs/RUNBOOKS/. Evidence: docs/ARCHITECTURE.md section 10. v1 card SQL: docs/reference/v1_metabase_cards.json. Style: British English, DD-MM-YYYY, no emoji, state UTC, one decision at a time with a recommendation first, plan before multi-step work, evidence over assertion, new commits only.
