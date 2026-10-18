# Documentation index

Index of the project documents. Merged work that is not yet deployed is in CHANGELOG.md and [PENDING_DOC_CHANGES.md](PENDING_DOC_CHANGES.md); documents are updated after each deploy is verified live. Issue 89 (phases 1 to 5) is fully live since 13-10-2026 10:05:56 UTC (marker 7a71d30).

**Status key.** CURRENT: kept up to date, trust it (subject to the notes). HISTORICAL: a dated record of a moment; correct for that date, not maintained, do not edit. OBSOLETE: wrong or superseded; do not use.

**Who.** Owner = Matt. Operator = whoever runs the stack on the VM (Matt, Claude or an engineer). Agent = a Claude session or sub-agent doing a task.

Files outside `docs/` that belong with these: [../README.md](../README.md) (what the project is and how to start it), [../SPEC.md](../SPEC.md) (the specification), [../CHANGELOG.md](../CHANGELOG.md) (what changed and when, deployed or not), [../tests/README.md](../tests/README.md) (how the tests are organised), [../.env.example](../.env.example) (every setting, secrets blank).

## 1. Start here

| File | What it is for | Who reads it | Status |
|---|---|---|---|
| [PENDING_DOC_CHANGES.md](PENDING_DOC_CHANGES.md) | Prepared document changes for merged work that is not deployed yet: one section per deploy, what text to switch in once that deploy is verified live | Writer, manager | CURRENT |
| [INSTALL.md](INSTALL.md) | How to install a new instance from the source code on a clean Linux machine | Operator, engineer | CURRENT (written 10-10-2026; UNVERIFIED on a fresh machine) |
| [IF_CLAUDE_IS_UNAVAILABLE.md](IF_CLAUDE_IS_UNAVAILABLE.md) | One-page card: how to see the system is healthy, what to check, how to restart, back up and restore, and what never to do | Owner | CURRENT (written 10-10-2026) |
| [TRACKER.md](TRACKER.md) | The live task list (rows T1 onwards) and the map to GitHub issues | Owner, agents | CURRENT (rows T66 to T71 added and the longest rows shortened 10-10-2026; a few older rows are still long) |
| [OWNER_ACTIONS.md](OWNER_ACTIONS.md) | What only the owner can do, step by step (branch protection, PBS restore test, folder rename and so on) | Owner | Needs update: items 1 and 4 are decided or done and still read as open |
| [OWNER_BACKUP_COPY.md](OWNER_BACKUP_COPY.md) | Owner card: one-time Proxmox-side set-up of the off-VM backup copy (ADR-026 item 7) | Owner | CURRENT (written 10-10-2026; not yet run, steps marked UNVERIFIED) |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the system fits together, what each container does; section 10 is the goal evidence | Engineers, agents | CURRENT (reviewed 08-10-2026; later changes are in the ADRs and runbooks) |

## 2. Operations (runbooks, in [RUNBOOKS/](RUNBOOKS/))

| File | What it is for | Who reads it | Status |
|---|---|---|---|
| [RUNBOOKS/daily-checks.md](RUNBOOKS/daily-checks.md) | Daily and weekly health checks; the soak report and its verdicts | Operator | CURRENT |
| [RUNBOOKS/alerts.md](RUNBOOKS/alerts.md) | What each Telegram alert means and what to do | Operator, owner | CURRENT (main and production agree: 21 invariant checks, marker 7a71d30) |
| [RUNBOOKS/public-access.md](RUNBOOKS/public-access.md) | Public read-only dashboards through a Cloudflare Tunnel and Traefik: the links script, proxy and tunnel set-up, checks, roll back | Owner | CURRENT |
| [RUNBOOKS/deploy-to-vm.md](RUNBOOKS/deploy-to-vm.md) | How to install, deploy, roll back and verify; the special cases and traps (sections 4b to 4f) | Operator | CURRENT; long, accumulates one section per release |
| [RUNBOOKS/restore.md](RUNBOOKS/restore.md) | Backups, restore, the restore drill, off-box copies | Operator | CURRENT (deployed behaviour; the hardening is in PENDING_DOC_CHANGES.md) |
| [RUNBOOKS/stuck-collector.md](RUNBOOKS/stuck-collector.md) | Collector up but not polling: diagnose and fix | Operator | CURRENT; plain restart allowed for a hung collector with no code change (owner decision 10-10-2026) |
| [RUNBOOKS/feed-outage.md](RUNBOOKS/feed-outage.md) | VATSIM feed is down or unreachable | Operator | CURRENT |
| [RUNBOOKS/feed-archive.md](RUNBOOKS/feed-archive.md) | The rolling archive of raw feed snapshots | Operator | CURRENT |
| [RUNBOOKS/integrity-checks.md](RUNBOOKS/integrity-checks.md) | The scheduled data-integrity checks in the watchdog | Operator | CURRENT |
| [RUNBOOKS/db-full.md](RUNBOOKS/db-full.md) | Database or disk full | Operator | CURRENT for main (includes the partition-drop change, migration 170, which is on the live database) |
| [RUNBOOKS/outside-heartbeat.md](RUNBOOKS/outside-heartbeat.md) | The check on the owner's PC that sees a dead VM | Owner | CURRENT |
| [RUNBOOKS/update-reference-files.md](RUNBOOKS/update-reference-files.md) | Changing the airspace polygon or the controller whitelist | Operator | CURRENT |
| [RUNBOOKS/dashboards.md](RUNBOOKS/dashboards.md) | Adding or changing a Grafana dashboard (dashboards are code) | Engineer | CURRENT |
| [RUNBOOKS/run-tests.md](RUNBOOKS/run-tests.md) | Running the checks and tests safely (throwaway Postgres, caps on the VM) | Engineer, agent | CURRENT |
| [RUNBOOKS/data-dictionary.md](RUNBOOKS/data-dictionary.md) | How to refresh DATA_DICTIONARY.md | Engineer | CURRENT |
| [CONFIG_MAP.md](CONFIG_MAP.md) | Every setting (part A) and how v1 settings map to v3 (part B) | Operator, engineer | CURRENT for deployed settings; enforced in part by `ops/check_config_drift.py` |
| [DATA_DICTIONARY.md](DATA_DICTIONARY.md) | Tables, views, functions, roles and grants | Engineer, analyst | CURRENT to migration 160 (checked 09-10-2026); migration 170 (partition drop) is not in it yet |
| [DEPLOY_PLAN_REVIEW_FIXES.md](DEPLOY_PLAN_REVIEW_FIXES.md) | The agreed order for deploying the code-review fixes (fix-ops, then fix-collector) | Owner, operator | CURRENT until those deploys are done, then HISTORICAL. Deploys 1 and 2 ship the exact commit 520dc3e |
| [FLIGHT_VIEW_SCALING.md](FLIGHT_VIEW_SCALING.md) | Measurements of how the flight view slows as history grows, and the proposed fix (issue #89, follow-up #108) | Engineer | CURRENT (written 10-10-2026 with fix-sql) |

## 3. Design and decisions

| File | What it is for | Who reads it | Status |
|---|---|---|---|
| [DECISIONS.md](DECISIONS.md) | The ADR log: every design decision with reason and consequence, ADR-001 to ADR-028, plus open decisions | Everyone | CURRENT |
| [DEFINITIONS.md](DEFINITIONS.md) | What a flight, a session, "online now" and so on mean | Owner, analyst | CURRENT (reviewed 08-10-2026; later changes in ADR-023 to ADR-025) |
| [CHART_DEFINITIONS.md](CHART_DEFINITIONS.md) | Every v1 chart and what replaced it, with the rule and the test | Owner, analyst | CURRENT |
| [DASHBOARDS_SIMPLIFIED.md](DASHBOARDS_SIMPLIFIED.md) | What was dropped or simplified from v1 and why | Owner | CURRENT |
| [V1_COMPARISON.md](V1_COMPARISON.md) | The v1 against v3 comparison, its method, results and the owner's decisions (section 11) | Owner, analyst | HISTORICAL record (complete 09-10-2026; a short re-comparison is planned) |
| [INCIDENTS.md](INCIDENTS.md) | Who acts first in an incident, when to tell the owner, the ten-line blameless write-up | Manager, owner | DRAFT for owner approval (10-10-2026) |
| [reference/v1_metabase_cards.json](reference/v1_metabase_cards.json) | The exported v1 Metabase card definitions (source data) | Engineer | CURRENT as reference data |

## 4. History (dated records; do not edit)

Dated records (old handovers, audits, review records, working notes) are kept in this folder as history and are not updated. Do not edit them.
