# Documentation index

Written 10-10-2026 and audited the same evening (branch `docs-audit`, against `origin/main` c89e544). The documents describe what is deployed and running (commit 3eb81a3 plus migration 160); merged work that is not yet deployed is in CHANGELOG.md, RESUME.md and [PENDING_DOC_CHANGES.md](PENDING_DOC_CHANGES.md); documents are updated after each deploy is verified live. Reading list: [DOC_AUDIT.md](DOC_AUDIT.md). Nothing was moved, renamed or deleted. This page is a map; it is not a source of facts.

**Status key.** CURRENT: kept up to date, trust it (subject to the notes). HISTORICAL: a dated record of a moment; correct for that date, not maintained, do not edit. OBSOLETE: wrong or superseded; do not use. "Needs update" means the file is current in purpose but has a known stale statement (listed at the end).

**Who.** Owner = Matt. Operator = whoever runs the stack on the VM (Matt, Claude or an engineer). Agent = a Claude session or sub-agent doing a task.

Files outside `docs/` that belong with these: [../README.md](../README.md) (what the project is and how to start it), [../SPEC.md](../SPEC.md) (the specification), [../CHANGELOG.md](../CHANGELOG.md) (what changed and when, deployed or not), [../tests/README.md](../tests/README.md) (how the tests are organised), [../.env.example](../.env.example) (every setting, secrets blank).

## 1. Start here

| File | What it is for | Who reads it | Status |
|---|---|---|---|
| [PENDING_DOC_CHANGES.md](PENDING_DOC_CHANGES.md) | Prepared document changes for merged work that is not deployed yet: one section per deploy, what text to switch in once that deploy is verified live | Writer, manager | CURRENT |
| [IF_CLAUDE_IS_UNAVAILABLE.md](IF_CLAUDE_IS_UNAVAILABLE.md) | One-page card: how to see the system is healthy, what to check, how to restart, back up and restore, and what never to do | Owner | CURRENT (written 10-10-2026) |
| [RESUME.md](RESUME.md) | Cold-start handover: state, deployed versus not, rules, test method, next actions | A new Claude session | CURRENT but long; needs update (see end). A short rewrite is queued (issue #107) |
| [NEXT_SESSION_PROMPT.md](NEXT_SESSION_PROMPT.md) | The text to paste into a new session so it knows what to read and how the owner works | Owner, new session | CURRENT (written 10-10-2026 about 15:00; will age quickly) |
| [TRACKER.md](TRACKER.md) | The live task list (rows T1 onwards) and the map to GitHub issues | Owner, agents | CURRENT but rows are long; shorter rows queued |
| [OWNER_ACTIONS.md](OWNER_ACTIONS.md) | What only the owner can do, step by step (branch protection, PBS restore test, folder rename and so on) | Owner | Needs update: items 1 and 4 are decided or done and still read as open |
| [OWNER_BACKUP_COPY.md](OWNER_BACKUP_COPY.md) | Owner card: one-time Proxmox-side set-up of the off-VM backup copy (ADR-026 item 7) | Owner | CURRENT (written 10-10-2026; not yet run, steps marked UNVERIFIED) |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the system fits together, what each container does; section 10 is the goal evidence | Engineers, agents | CURRENT (reviewed 08-10-2026; later changes are in the ADRs and runbooks) |

## 2. Operations (runbooks, in [RUNBOOKS/](RUNBOOKS/))

| File | What it is for | Who reads it | Status |
|---|---|---|---|
| [RUNBOOKS/daily-checks.md](RUNBOOKS/daily-checks.md) | Daily and weekly health checks; the soak report and its verdicts | Operator | CURRENT |
| [RUNBOOKS/alerts.md](RUNBOOKS/alerts.md) | What each Telegram alert means and what to do | Operator, owner | CURRENT (deployed behaviour; the new wording is in PENDING_DOC_CHANGES.md) |
| [RUNBOOKS/deploy-to-vm.md](RUNBOOKS/deploy-to-vm.md) | How to install, deploy, roll back and verify; the special cases and traps (sections 4b to 4f) | Operator | CURRENT; long, accumulates one section per release |
| [RUNBOOKS/restore.md](RUNBOOKS/restore.md) | Backups, restore, the restore drill, off-box copies | Operator | CURRENT (deployed behaviour; the hardening is in PENDING_DOC_CHANGES.md) |
| [RUNBOOKS/stuck-collector.md](RUNBOOKS/stuck-collector.md) | Collector up but not polling: diagnose and fix | Operator | CURRENT; plain restart allowed for a hung collector with no code change (owner decision 10-10-2026) |
| [RUNBOOKS/feed-outage.md](RUNBOOKS/feed-outage.md) | VATSIM feed is down or unreachable | Operator | CURRENT |
| [RUNBOOKS/feed-archive.md](RUNBOOKS/feed-archive.md) | The rolling archive of raw feed snapshots | Operator | CURRENT |
| [RUNBOOKS/integrity-checks.md](RUNBOOKS/integrity-checks.md) | The scheduled data-integrity checks in the watchdog | Operator | CURRENT |
| [RUNBOOKS/db-full.md](RUNBOOKS/db-full.md) | Database or disk full | Operator | CURRENT for main; a rewrite for the partition-drop change is on an unmerged branch |
| [RUNBOOKS/outside-heartbeat.md](RUNBOOKS/outside-heartbeat.md) | The check on the owner's PC that sees a dead VM | Owner | CURRENT |
| [RUNBOOKS/update-reference-files.md](RUNBOOKS/update-reference-files.md) | Changing the airspace polygon or the controller whitelist | Operator | CURRENT |
| [RUNBOOKS/dashboards.md](RUNBOOKS/dashboards.md) | Adding or changing a Grafana dashboard (dashboards are code) | Engineer | CURRENT |
| [RUNBOOKS/run-tests.md](RUNBOOKS/run-tests.md) | Running the checks and tests safely (throwaway Postgres, caps on the VM) | Engineer, agent | CURRENT |
| [RUNBOOKS/data-dictionary.md](RUNBOOKS/data-dictionary.md) | How to refresh DATA_DICTIONARY.md | Engineer | CURRENT |
| [CONFIG_MAP.md](CONFIG_MAP.md) | Every setting (part A) and how v1 settings map to v3 (part B) | Operator, engineer | CURRENT for deployed settings; enforced in part by `ops/check_config_drift.py` |
| [DATA_DICTIONARY.md](DATA_DICTIONARY.md) | Tables, views, functions, roles and grants | Engineer, analyst | CURRENT (checked 09-10-2026 to migration 160); newer migrations on unmerged branches are not in it yet |
| [DEPLOY_PLAN_REVIEW_FIXES.md](DEPLOY_PLAN_REVIEW_FIXES.md) | The agreed order for deploying the code-review fixes (fix-ops, then fix-collector) | Owner, operator | CURRENT until those deploys are done, then HISTORICAL |

## 3. Design and decisions

| File | What it is for | Who reads it | Status |
|---|---|---|---|
| [DECISIONS.md](DECISIONS.md) | The ADR log: every design decision with reason and consequence, ADR-001 to ADR-027, plus open decisions | Everyone | CURRENT (ADR-028 is on an unmerged branch) |
| [DEFINITIONS.md](DEFINITIONS.md) | What a flight, a session, "online now" and so on mean | Owner, analyst | CURRENT (reviewed 08-10-2026; later changes in ADR-023 to ADR-025) |
| [CHART_DEFINITIONS.md](CHART_DEFINITIONS.md) | Every v1 chart and what replaced it, with the rule and the test | Owner, analyst | CURRENT |
| [DASHBOARDS_SIMPLIFIED.md](DASHBOARDS_SIMPLIFIED.md) | What was dropped or simplified from v1 and why | Owner | CURRENT |
| [V1_COMPARISON.md](V1_COMPARISON.md) | The v1 against v3 comparison, its method, results and the owner's decisions (section 11) | Owner, analyst | HISTORICAL record (complete 09-10-2026; a short re-comparison is planned) |
| [WORKING_AGREEMENT.md](WORKING_AGREEMENT.md) | How the team works: roles, flow, change tiers, approvals, limits | Owner, manager, agents | CURRENT (approved 09-10-2026) |
| [TEAM_BRIEFS.md](TEAM_BRIEFS.md) | How to brief each role | Manager | CURRENT |
| [reference/v1_metabase_cards.json](reference/v1_metabase_cards.json) | The exported v1 Metabase card definitions (source data) | Engineer | CURRENT as reference data |

## 4. History (dated records; do not edit)

| File | What it is | Status |
|---|---|---|
| [HANDOVER.md](HANDOVER.md) | The first handover, 07-10-2026, with a few update notes | HISTORICAL (replaced by RESUME.md) |
| [DOC_AUDIT.md](DOC_AUDIT.md) | The documentation audit of 08-10-2026: findings, what was fixed | HISTORICAL |
| [TEST_AUDIT.md](TEST_AUDIT.md) | The independent test audit (T30), at commit e1cfce3 | HISTORICAL |
| [DATA_INTEGRITY_AUDIT.md](DATA_INTEGRITY_AUDIT.md) | The independent data-integrity audit (T30), 08-10-2026 | HISTORICAL |
| [RELEASE_REVIEW.md](RELEASE_REVIEW.md) | Independent review of the collector release ff89b56, 08-10-2026 | HISTORICAL |
| [RELEASE_REVIEW_FINDINGS.md](RELEASE_REVIEW_FINDINGS.md) | The findings from that review as issue titles | HISTORICAL |
| [RESTORE_DRILL.md](RESTORE_DRILL.md) | The restore drill of 08-10-2026 with row counts | HISTORICAL (still the evidence for the drill; repeat monthly per restore.md) |
| [TRUST_WORK.md](TRUST_WORK.md) | The brief for the "trust in the numbers" session, 09-10-2026 | HISTORICAL |
| [reviews/10-10-2026-fix-ops.md](reviews/10-10-2026-fix-ops.md) | Review record for the fix-ops branch | HISTORICAL record (change merged, not deployed) |
| [reviews/10-10-2026-fix-collector.md](reviews/10-10-2026-fix-collector.md) | Review record for the fix-collector branch | HISTORICAL record (change merged, not deployed) |
| [t14_notes/](t14_notes/) (atc, events, pilots) | Working notes from re-checking v1 cards, 08-10-2026 | HISTORICAL |
| [t15_notes/](t15_notes/) (four notes) | Working notes from building 16 cards, 08-10-2026 | HISTORICAL |
| [OWNER_CHECKLIST.md](OWNER_CHECKLIST.md) | A card-against-panel checklist for comparing Metabase with Grafana | OBSOLETE (bannered 09-10-2026: Metabase is gone) |

## 5. Recommendations for the manager (nothing has been done)

1. **Merge the t14 and t15 notes into CHART_DEFINITIONS.md** (or archive them under a `docs/archive/` folder). Seven small files whose conclusions already live in CHART_DEFINITIONS.md and the tracker.
2. **Create `docs/archive/`** for the HISTORICAL files in section 4 (keeping git history) so the top level shows only live documents. Fix the links in RESUME.md, README.md and DOC_AUDIT.md when moving.
3. **Delete or archive OWNER_CHECKLIST.md** (OBSOLETE; only RESUME.md and the tracker mention it, to say so) and `RELEASE_REVIEW_FINDINGS.md` (the issues exist on GitHub).
4. **Fold TRUST_WORK.md and NEXT_SESSION_PROMPT.md into RESUME.md** once RESUME.md is short; two entry points confuse a cold start. Keep one.
5. **Split deploy-to-vm.md.** The generic procedure (sections 1 to 7 and Updating) stays; the special cases 4b to 4f are one-off release notes that belong in the CHANGELOG or a `deploys/` record once deployed.
6. **Prune OWNER_ACTIONS.md** to the items still open, moving the decided ones to the tracker.
7. **Shorten tracker rows.** Many rows are several paragraphs; the detail belongs in the issue or an ADR (already queued under issue #107).
8. **Reconcile the two collector-restart instructions** (stuck-collector.md versus deploy-to-vm.md 4c).
9. **Add a "last reviewed against commit" line to every current document** (some have a date, some do not) so staleness is visible.

## 6. Statements found to be stale or wrong (found 10-10-2026; fixed on main on 10-10-2026 except the collector restart rule, which waits for an owner decision, issue #110)

| Where | What | Evidence |
|---|---|---|
| RESUME.md line 41 | Says the heartbeat script, runbook and OWNER_ACTIONS.md "arrive on main from the branch owner-actions ... not yet into main". They are on main. | `ls ops/heartbeat docs/OWNER_ACTIONS.md` at f893585 |
| RESUME.md lines 59 and 83 | "origin/main (10d5177 ...)" and "ADR-001 to ADR-025". Main is at f893585 and the log runs to ADR-027. | `git log`, `docs/DECISIONS.md` |
| RESUME.md line 13, deploy-to-vm.md line 3 ("no git" wording) | "no git on the VM". The git program (version 2.43.0) is installed; what is true is that `~/vatpac-statsV2` is not a git checkout (no `.git`). | `ssh vatpac-v3 'git --version; ls -d ~/vatpac-statsV2/.git'` on 10-10-2026 |
| OWNER_ACTIONS.md item 4 | Says the Dependabot bumps are "NOT deployed yet". They were deployed 09-10-2026 13:46 UTC. Item 1 (branch protection) is also decided but still reads as live: it was closed as not possible on the free plan (issue #44, RESUME.md section 1). Items 5 and 7 already say won't do. | RESUME.md section 1; TRACKER T48 |
| daily-checks.md "Integrity checks ... once the watchdog with them is deployed" | The integrity checks are deployed (09-10-2026 19:13 UTC), so the qualifier is stale. | TRACKER T16 |
| deploy-to-vm.md line 69 (section 4e header) | Says "owner's go needed" for the integrity checks, which were deployed 09-10-2026 19:13 UTC. (4f is correct: not yet deployed.) | TRACKER T16 |
| restore.md and alerts.md | Written for code that is merged but not deployed (RESTORE_MAX_POLL_AGE_HOURS, plain-language messages); the VM still has the older scripts. Not wrong for main, but misleading for the VM until deploy 1. | `ssh vatpac-v3 'grep -n "vatpac-stats:" ops/watchdog/watchdog.py'` finds the old prefix |
| stuck-collector.md step 1 | RESOLVED 10-10-2026 (owner): plain restart allowed for a hung collector with no code change; code changes use stop then `up -d --build`. | deploy-to-vm.md 4c, stuck-collector.md |
| TRACKER.md "Last updated: 09-10-2026" (line 12) | The tracker has rows added on 10-10-2026. | git log |
