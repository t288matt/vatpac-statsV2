# Documentation index

Index of the project documents. Merged work that is not yet deployed is in CHANGELOG.md; documents change in the same branch as the change that makes them wrong (owner decision 22-10-2026). The commit production runs is shown by `bash ops/deploy.sh --status`.

**Status key.** CURRENT: kept up to date, trust it (subject to the notes). HISTORICAL: a dated record of a moment; correct for that date, not maintained, do not edit. OBSOLETE: wrong or superseded; do not use.

**Who.** Owner = Matt. Operator = whoever runs the stack on the VM (Matt or an engineer). Agent = a Claude session or sub-agent doing a task.

Files outside `docs/` that belong with these: [../README.md](../README.md) (what the project is and how to start it), [../SPEC.md](../SPEC.md) (the specification), [../CHANGELOG.md](../CHANGELOG.md) (what changed and when, deployed or not), [../tests/README.md](../tests/README.md) (how the tests are organised), [../.env.example](../.env.example) (every setting, secrets blank).

## 1. Start here

| File | What it is for | Who reads it | Status |
|---|---|---|---|
| [INSTALL.md](INSTALL.md) | How to install a new instance from the source code on a clean Linux machine | Operator, engineer | CURRENT (walked through on a clean Windows PC on 21-10-2026; a fresh Linux machine is still unverified) |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the system fits together, what each container does; section 10 is the goal evidence | Engineers, agents | CURRENT (reviewed 08-10-2026; later changes are in the ADRs and runbooks) |

## 2. Operations (runbooks, in [RUNBOOKS/](RUNBOOKS/))

| File | What it is for | Who reads it | Status |
|---|---|---|---|
| [RUNBOOKS/daily-checks.md](RUNBOOKS/daily-checks.md) | Daily and weekly health checks; the soak report and its verdicts | Operator | CURRENT |
| [RUNBOOKS/alerts.md](RUNBOOKS/alerts.md) | What each Telegram alert means and what to do | Operator, owner | CURRENT (20 invariant checks) |
| [RUNBOOKS/public-access.md](RUNBOOKS/public-access.md) | Public read-only dashboards through a Cloudflare Tunnel and Traefik: the links script, proxy and tunnel set-up, checks, roll back | Owner | CURRENT |
| [RUNBOOKS/deploy-to-vm.md](RUNBOOKS/deploy-to-vm.md) | How to install, deploy, roll back and verify; the special cases and traps (sections 4b to 4f) | Operator | CURRENT; long, accumulates one section per release |
| [RUNBOOKS/restore.md](RUNBOOKS/restore.md) | Backups, restore, the restore drill, a local copy of production for testing, off-box copies | Operator | CURRENT (deployed behaviour) |
| [RUNBOOKS/stuck-collector.md](RUNBOOKS/stuck-collector.md) | Collector up but not polling: diagnose and fix | Operator | CURRENT; plain restart allowed for a hung collector with no code change (owner decision 10-10-2026) |
| [RUNBOOKS/feed-outage.md](RUNBOOKS/feed-outage.md) | VATSIM feed is down or unreachable | Operator | CURRENT |
| [RUNBOOKS/feed-archive.md](RUNBOOKS/feed-archive.md) | The rolling archive of raw feed snapshots | Operator | CURRENT |
| [RUNBOOKS/integrity-checks.md](RUNBOOKS/integrity-checks.md) | The scheduled data-integrity checks in the watchdog | Operator | CURRENT |
| [RUNBOOKS/db-full.md](RUNBOOKS/db-full.md) | Database or disk full | Operator | CURRENT for main (includes the partition-drop change, migration 170, which is on the live database) |
| [RUNBOOKS/outside-heartbeat.md](RUNBOOKS/outside-heartbeat.md) | The check on the owner's PC that sees a dead VM | Owner | CURRENT |
| [RUNBOOKS/update-reference-files.md](RUNBOOKS/update-reference-files.md) | Changing the airspace polygon or the controller whitelist | Operator | CURRENT |
| [RUNBOOKS/dashboards.md](RUNBOOKS/dashboards.md) | Adding or changing a Grafana dashboard (dashboards are code) | Engineer | CURRENT |
| [RUNBOOKS/drop-gate-rehearsal.md](RUNBOOKS/drop-gate-rehearsal.md) | The raw-position drop gate (issue 89 phase 4): what it does and how it was rehearsed; live since 13-10-2026 | Engineer | CURRENT |
| [RUNBOOKS/import-v1-history.md](RUNBOOKS/import-v1-history.md) | One-off load of v1 history (ADR-035; used on 05-11-2026): extract, dry run, apply, verify, roll back, delete the CSV files; the runbook for a repeat or a rollback | Operator, owner | CURRENT |
| [RUNBOOKS/metabase.md](RUNBOOKS/metabase.md) | Metabase beside Grafana (issue 288, not yet deployed): install, first-run set-up, the read-only role, backup, restore, upgrade, roll back | Owner, operator | NEW (06-11-2026; deploy needs the owner's go) |
| [RUNBOOKS/run-tests.md](RUNBOOKS/run-tests.md) | Running the checks and tests safely (throwaway Postgres, caps on the VM) | Engineer, agent | CURRENT |
| [CONFIG_MAP.md](CONFIG_MAP.md) | Every setting (part A) and how v1 settings map to v3 (part B) | Operator, engineer | CURRENT for deployed settings; enforced in part by `ops/check_config_drift.py` |
| [DATA_DICTIONARY.md](DATA_DICTIONARY.md) | Tables, views, functions, roles and grants; how to refresh it | Engineer, analyst | CURRENT to migration 20261013000300; checked against the live database on 05-11-2026 |

## 3. Design and decisions

| File | What it is for | Who reads it | Status |
|---|---|---|---|
| [DECISIONS.md](DECISIONS.md) | The ADR log: every design decision with reason and consequence, ADR-001 to ADR-035 (ADR-035, v1 history import, is ACCEPTED and was loaded on 05-11-2026), plus open decisions | Everyone | CURRENT |
| [DEFINITIONS.md](DEFINITIONS.md) | What a flight, a session, "online now" and so on mean | Owner, analyst | CURRENT (reviewed 08-10-2026; later changes in ADR-023 to ADR-025) |
| [CHART_DEFINITIONS.md](CHART_DEFINITIONS.md) | Every v1 chart and what replaced it, with the rule and the test; why charts were dropped | Owner, analyst | CURRENT |
| [SQL_DEFINITIONS_REFERENCE.sql](SQL_DEFINITIONS_REFERENCE.sql) | Reference text (not applied) of the flight-writer functions and views as they stand after the migrations | Engineer | CURRENT (21-10-2026) |
| [reference/v1_metabase_cards.json](reference/v1_metabase_cards.json) | The exported v1 Metabase card definitions (source data) | Engineer | CURRENT as reference data |
