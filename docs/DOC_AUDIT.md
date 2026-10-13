# Documentation audit (08-10-2026)

> **Note, issue 89 phase 5 (LIVE since 13-10-2026, marker 7a71d30 at 10:05:56 UTC).** This is a dated record. It uses the names as they were when it was written: `flight_summary` meant the old pieces table, `flight_record` the table of finished flights. Migration 20261013000300 (live) dropped the pieces table, `flight_part`, `flight_legacy`, `refresh_flight_summary` and `refresh_flight_summary_sessions`, and renamed `flight_record` to `flight_summary`. Current description: [DATA_DICTIONARY.md](DATA_DICTIONARY.md) and ADR-030.

Independent audit of the project documentation by a reviewer who did not write it. Scope: `README.md`, `SPEC.md`, `docs/*.md`, `docs/RUNBOOKS/*`, `docs/reference/` (read only), `tests/README.md`, the comments at the top of `ops/*.sh` and `.env.example`; the `docs/t14_notes/` working notes were checked only for contradictions with current code. Baseline: commit `8eb4e6f` (branch `main`); work is on branch `docs-audit`. Every statement of fact was checked against the repository (compose file, migrations, collector source, dashboards JSON, `ops/`, tests, CI workflow, `.env.example`), not against other documents. No code, SQL, migration, dashboard, test or workflow was changed. Times are UTC. Line numbers in the table refer to the baseline commit.

Totals: 37 findings (10 high, 17 medium, 10 low).

## 1. Findings

| Id | File and line | Severity | What was wrong | What was done | Verified how |
|---|---|---|---|---|---|
| F01 | README.md:10 | high | Quick start said to copy .env.example and replace "change-me" values, but the file has blank secrets on purpose and compose refuses to start until all six are set (and check_env rejects weak values). | Quick start now uses `sh ops/gen_env.sh` and `sh ops/check_env.sh`, says why, and explains the manual route. | Read .env.example, ops/gen_env.sh, ops/check_env.sh, the `:?` guards in docker-compose.yml, Makefile `up`/`check-env`. |
| F02 | docs/RUNBOOKS/stuck-collector.md:5,26 | high | Said the watchdog restarts the collector after 3 bad minutes, and that a hand stop makes the watchdog restart it. The deployed watchdog is alert-only (`DOCKER_HOST_URL` empty, ADR-012) and restarts nothing. | Rewritten: alerts you will see, restart is manual, how to stop deliberately without noise. | Read ops/watchdog/watchdog.py `cycle()`/`try_restart()` (only runs when `cfg.docker` is set) and the watchdog block of docker-compose.yml. |
| F03 | docs/RUNBOOKS/update-reference-files.md:3,9 | high | Step 4 told the operator to `git pull` on the VM, which has no git. The intro said an alert is raised on a rejected reload; none is sent (log line and `/health reload_rejected` only). The 70% whitelist-shrink guard and the CRLF/SHA caveat were missing. | Step 4 uses the git-archive method; alert wording corrected; 70% guard and SHA note added. | Read collector main.py `reload_reference()`, health.py, watchdog.py (no reload check), docs/HANDOVER.md section 7.2. |
| F04 | docs/DATA_DICTIONARY.md:3,156-163 | high | Said six migrations; roles table said the owner role is used by the collector and backup and listed no `collector` or `backup_ro` role; `flight_summary`, `controller_summaries`, `refresh_flight_summary`, `refresh_materialized_views` were not documented, and `flight` was described as computed live. The dashboards read 16 relations, only some documented. | Added flight_summary, controller_summaries (columns), the functions, all roles and grants, `flight` as a thin view, and the list of the 16 relations the dashboards read. 13 migrations. | Read all 13 migrations, db/roles.sql, ops/migrate/migrate.sh; relation list from `python grafana/validate_dashboards.py` output; `python grafana/check_columns.py` derives controller_summaries with 7 columns. |
| F05 | docs/DATA_DICTIONARY.md:135, docs/DEFINITIONS.md:60 | high | Said `online_now` still returns rows if the collector has stopped. Migration 24 added a 5-minute wall-clock guard. | Both corrected; the two online-now forms are now described as nearly equal. | Read db/migrations/20261007000024_online_now_guard_and_tuning.sql. |
| F06 | docs/CONFIG_MAP.md:5,14-17 | high | Told the reader to replace `change-me` values; omitted the required secrets COLLECTOR_DB_PASSWORD and BACKUP_DB_PASSWORD (compose fails without them); said the collector uses POSTGRES_USER/PASSWORD (it uses role `collector`). | Corrected; both secrets added; DATABASE_URL and backup connection rows corrected. | Read docker-compose.yml collector and backup environment; script compared every `${VAR}` in compose with .env.example and with this file. |
| F07 | docs/CONFIG_MAP.md (A1/A3) | medium | Eight .env.example variables absent (BACKUP_DB_PASSWORD, COLLECTOR_DB_PASSWORD, BACKUP_REMOTE, BACKUP_EXTRA_APT_PACKAGES, GRAFANA_BIND, GRAFANA_COOKIE_SECURE, WATCHDOG_DISK_PATHS, WATCHDOG_RESTORE_TEST_MAX_DAYS); CIRCUIT_* listed as "not wired through compose" but compose passes them; Telegram "not verified". | All added or moved; Telegram status updated; LOG_LEVEL note (watchdog does not receive it). | Script: example keys minus CONFIG_MAP keys, before and after; `python ops/check_config_drift.py` still OK. |
| F08 | docs/HANDOVER.md:163 | high | Contains the VM's internal LAN IP address in section 7.4. | Replaced with "LAN address omitted here". The value remains in git history before this branch (not rewritten). | grep for IPv4 patterns across docs, README, SPEC, ops comments and .env.example: no other internal address. |
| F09 | docs/HANDOVER.md:1-5, 40-72, 244-251 | high | Written before many changes: said CI never ran, reboot test, Telegram, retention charts, soak not done; next-actions list done; actions unpinned. | Added a point-in-time banner pointing at RESUME and TRACKER and short "Update 08-10-2026" notes on each statement that would mislead; history otherwise unchanged. | Compared with docs/TRACKER.md T2, T3, T4, T10, T13 and docs/RESUME.md. |
| F10 | README.md:94-116 | high | Status section dated 07-10-2026: six migrations, 288 tests, 61 panel queries, Telegram not configured, retention UNVERIFIED, "not yet verified: running on the VM". | Replaced by a dated snapshot that defers to docs/TRACKER.md and states what is and is not met. | Counts from `pytest --collect-only` (205 unit, 166 integration) and the validator (68 query panels, 9 text, 8 dashboards); evidence items quoted from TRACKER rows, not re-run. |
| F11 | docs/RUNBOOKS/deploy-to-vm.md:79-84 | high | Update and rollback described only as `git checkout <tag>`; the production method (git archive over ssh), what needs a rebuild versus a reload, the soak-gap warning, and how to verify were missing. | Added the git-archive section, a rebuild/restart/reload table, a rollback procedure (including the stale-migration trap) and verification steps. | Checked against docker-compose.yml mounts, the Dockerfiles (what is baked into images), dashboards.yml (30 s rescan), HANDOVER 7.2. |
| F12 | README.md:61, SPEC acceptance, HANDOVER:120, ARCHITECTURE soak row | medium | `make soak` and others described PASS as "gap-free". The script passes with at least 99% of snapshots and p95 cadence 17 s or less (about 14 minutes of gaps in 24 h). | Wording matches the script; daily-checks.md explains it; owner question 1. | Read ops/soak_report.sql verdict CASE. |
| F13 | docs/ARCHITECTURE.md:76, SPEC.md:82 | medium | `/health` described as "last good poll under 60 s"; code also requires the newest snapshot under 120 s and ignores unchanged polls. | Both corrected. | Read collector health.py. |
| F14 | docs/ARCHITECTURE.md:54 | medium | migrate row listed only `grafana_ro`; it also creates `collector`, `backup_ro` (roles.sql) and `watchdog_ro`. | Corrected; note that ./db is mounted so new migrations need no rebuild. | Read migrate.sh, roles.sql. |
| F15 | docs/ARCHITECTURE.md:75 | medium | Maintenance row omitted the flight_summary refresh (also a separate 5-minute loop) and the finalise-before-drop rule. | Corrected; flight_summary added to the database paragraph. | Read maintenance.py, main.py, migration 21. |
| F16 | docs/ARCHITECTURE.md:106 | medium | Telegram delivery "not yet verified"; no reboot row; no place to record goal evidence (RESUME and tracker T11 say to record it in ARCHITECTURE.md). | Updated per TRACKER T2/T4; added section 10 "Goal evidence" with the soak row left to fill. | TRACKER rows T2, T3, T4, T10, T11. |
| F17 | SPEC.md:17,23,56,82,86,89 | medium | Watchdog "may restart"; lookups "seeded from the feed"; heavy views "may be materialised" (flight_summary table exists); migrate creates only grafana_ro; retention charts pending; CI list incomplete; folder names capitalised; dev port described as a profile. | Each corrected (see the SPEC diff); SPEC otherwise unchanged. | Read model.py/store.py (no lookup seeding), migrations 3/11, docker-compose.dev.yml, dashboards.yml, ci.yml. |
| F18 | docs/DASHBOARDS_SIMPLIFIED.md:5, docs/HANDOVER.md:205 | medium | "17 kept, 19 changed"; CHART_DEFINITIONS (and TRACKER T27) say 18 kept, 18 changed. | Both set to 18/18. | Script counted the 36 rebuilt rows in CHART_DEFINITIONS by status; 21 dropped rows; total 57. |
| F19 | docs/CHART_DEFINITIONS.md:9,21,75 | medium | Verification paragraph said 61 queries (now 68); "11 screenshot-only panels, all UNVERIFIED" (now 5 plus 13 retention panels built from v1 SQL); view sources omitted migrations 30 and 31; section heading wrong. | Rewritten; heading renamed. | Validator output; script confirmed all 68 cited panel titles exist and all cited test functions exist. |
| F20 | docs/DASHBOARDS_SIMPLIFIED.md:61,68 | medium | Said military cards 65, 66 and 78 use the is_military lookup (66 and 78 now use the RAAFv remarks rule); said the retention dashboard is built from screenshots and UNVERIFIED. | Corrected. | Read military.json SQL; CHART_DEFINITIONS rows 66, 78; tracker T13. |
| F21 | docs/CONFIG_MAP.md:122-126 | medium | FLIGHT_SUMMARY_ENABLED and the poll intervals marked DROPPED with "no summary tables"; ADR-016 added flight_summary refreshed every 5 minutes. | Marked CHANGED with the real mechanism. | Read ADR-016, maintenance.py. |
| F22 | tests/README.md:3-8,24-36 | medium | 94 passed; applying roles.sql without the collector and backup password variables (it raises an error); "written from SPEC"; assumptions section described tests as unrun; no pointer to the safe VM method. | Rewritten around current counts (205 + 166), the three roles.sql variables, the sed step, and run-tests.md. | Counted tests with pytest; read roles.sql, tests/integration/conftest.py, .github/workflows/ci.yml. |
| F23 | README.md:77-92 | medium | Integration tests setup said to apply migrations "with dbmate" (not installed locally) and had no warning about roles.sql or the live VM. | Replaced by a short pointer and the warnings; full method in docs/RUNBOOKS/run-tests.md. | Compared with RESUME section 5 and CI. |
| F24 | docs/DECISIONS.md:222 | medium | SPEC says decisions belong in this log, but the owner's decisions of 08-10-2026 (cards 94/95, lookup categories for 90/135, retention quirk, accepted token risk) were only in RESUME; open list omitted backfill (T22) and retention (T12). | Added ADR-020 recording them with the source, and extended the open list. No decision was invented. | Text taken from docs/RESUME.md section 2 and TRACKER T12, T22. |
| F25 | docs/TRACKER.md:16,24 | medium | T5 "IN PROGRESS, rebuild pending" while T13 (the rebuild) is DONE; T13 said the new integration test had not been seen in CI while T3 says the e5816af run was green including it. | T5 closed as "rebuilt in T13"; T13 status aligned with T3. T28 row added. | Compared rows T3, T5, T13; commits e90963e, 7adb5f9 precede e5816af. |
| F26 | docs/RUNBOOKS/db-full.md:18,30 | medium | Suggested dropping partitions with `DROP TABLE`, which skips the finalising of flights; "open session" wording inaccurate. | Prefer `drop_old_pilot_position_partitions(<days>)`; hand drop kept as last resort with the caveat; wording fixed. | Read migration 21 function body and maintenance.py. |
| F27 | docs/RESTORE_DRILL.md:36 | low | Said `$POSTGRES_USER` is expanded inside the container, but the commands as written use double quotes (the host shell would expand it). Recommendation 6 (add drill commands to restore.md) was open. | Note corrected; drill commands added to restore.md with correct quoting; recommendation marked done. | Read the command block; quoting reasoning only, not run (no Docker). |
| F28 | docs/CHART_DEFINITIONS.md:87, docs/DASHBOARDS_SIMPLIFIED.md:14 | low | Card 133 title still said 0800-1200z; v1 now says 0700-1200z (t14 notes). | Title notes the change; card stays dropped. | docs/t14_notes/events.md. |
| F29 | docs/HANDOVER.md:155,178,191,274-287 | low | Latest migration, module line count, view list, ADR range, Makefile targets out of date. | Update notes added in place. | Counted: 13 migrations, 1335 lines, `ops/check_size_budget.py`. |
| F30 | docs/HANDOVER.md decision (10) | low | "Store every sample, compressed" contradicts SPEC and ADR-003 (no compression). | Annotated as superseded. | SPEC section 2, ADR-003. |
| F31 | docs/DATA_DICTIONARY.md:87,156 | low | `pilots_kept` "equals pilots_inside" (not when a malformed record is skipped); `controller_activity_summary` described as behind the retention panels (no dashboard reads it now). | Corrected; marked unused and unverified. | Read store.py, validator relation list. |
| F32 | docs/RUNBOOKS (missing) | medium | No runbook for daily/weekly checks and the soak report, what each Telegram alert means, adding or changing a dashboard, running the tests safely; restore drill commands not in restore.md. | Added daily-checks.md, alerts.md, dashboards.md, run-tests.md; extended restore.md and deploy-to-vm.md. | Every command checked against Makefile, ops/, compose and CI; the Docker/VM commands could not be executed here (see "not verified"). |
| F33 | docs/t14_notes/*.md | low | Working notes say tests "not run" and list questions since answered or tracked; no factual contradiction with current code found (panel ids and titles match the dashboards). | One-line banner pointing at TRACKER and CHART_DEFINITIONS. | Panel ids/titles compared with the dashboard JSON. |
| F34 | docs/RESUME.md:7 | low | Soak PASS described as "no gap" without noting the script's threshold. | Clarifying sentence and pointers to the runbooks added. | ops/soak_report.sql. |
| F35 | db/migrations/20261007000024...sql (COMMENT ON VIEW movements) | low | SQL comment says "SPEC decision pending on excluding them", but ADR-018 decided to keep them. Code, outside the documentation scope. | NOT changed. Would need a new numbered migration (comment only). | Read the migration and ADR-018. |
| F36 | .github/dependabot.yml:1-2 | low | Header refers to TODO comments in the workflows that no longer exist (already tracker T27). | NOT changed (configuration, out of scope). | Read the file and ci.yml. |
| F37 | docs/CHART_DEFINITIONS.md:85-86 | low | Two non-ASCII arrows inside quoted v1 card titles (125, 126). | Left verbatim (not emoji; titles are source data). | Character scan of all in-scope files. |

## 2. OPEN questions for the owner

These are disagreements or gaps that the code cannot settle. The documents were left as they are; none of them was decided by the audit.

1. **What counts as a passed soak?** `ops/soak_report.sh` prints `PASS` for a full window with at least 99% of the expected snapshots and a p95 cadence of 17 s or less, which tolerates about 14 minutes of gaps in 24 hours (a 45-second restart still passes). `docs/TRACKER.md` T1 calls it a "gap-free soak" and `docs/RESUME.md` section 1 plans to wait for a window with no gap at all (the clock was restarted after the reboot test for that reason). Recommendation: accept the script's `PASS` as the gate and require in addition that no container restarted during the window (the report already prints restart counts); say which you want before the goal is declared met (T1, T11).
2. **Backfill of v1 history.** SPEC sections 2 and 13 and ADR-011 say there is no backfill and charts start empty; `docs/TRACKER.md` T22 and `docs/RESUME.md` section 4 item 10 keep the question open (30 to 90 day charts are meaningless for months without it). The documents agree with each other that no backfill is done today; only you can change that, and it needs a new ADR.
3. **Card 134 window (and other chart questions).** SPEC section 10 and card 131 use the Monday event window 0700-1200Z; v1 still uses 0800-1200Z for card 134 while the repo uses 0700 for both (`docs/CHART_DEFINITIONS.md`, `docs/RESUME.md` section 4 item 3). The rest of the chart questions in RESUME section 4 (cards 66, 131, 65, 41/43/98, 61, 93, the meaning of v1 enroute time, card 55 total column) are the same kind: definitions only you can settle. They are tracked in RESUME and the tracker, and ADR-020 points to them.
4. **Backup container holds the owner password** (ADR-017, already listed as open): a restore-only role would be tighter.
5. **Data retention for all data sets** other than names and CIDs (T12, low priority) and the **off-box backup copy** (T17), **outside heartbeat** (T19): no documented decision yet. The alerts runbook lists the heartbeat gap under "What does not alert".
6. **`controller_activity_summary` view.** It is unused by any dashboard and was built from screenshots (UNVERIFIED). Keep or remove by a new migration? (Low.) **Resolved 08-10-2026 (owner): removed** by migration 20261008000090 (the down section recreates it).
7. **Closing T5.** The audit changed T5 to "DONE (rebuilt in T13)" because its own text says the rebuild was pending on T13 and T13 is done. Confirm that the owner agrees T5 is closed.

## 3. Documents judged fine (after the edits above)

- `.env.example` (comments match `docker-compose.yml`, `ops/check_env.sh` and `ops/gen_env.sh`; `ops/check_config_drift.py` passes).
- The header comments of `ops/smoke.sh`, `ops/soak_report.sh`, `ops/check_env.sh`, `ops/gen_env.sh` and the comments at the top of `docker-compose.yml` and `Makefile`: accurate, unchanged.
- `docs/RUNBOOKS/feed-outage.md` and `docs/RUNBOOKS/restore.md` (apart from the additions above): commands and behaviour match `ops/backup/*.sh` and the Makefile targets.
- `docs/RESTORE_DRILL.md`: a dated record of what was done; only the quoting note was corrected. Its numbers were not re-measured.
- `docs/DEFINITIONS.md`, `docs/DECISIONS.md` (ADR-001 to ADR-019), `docs/CHART_DEFINITIONS.md` rows and `docs/DASHBOARDS_SIMPLIFIED.md`: consistent with the SQL after the corrections; a script confirmed that all 68 cited panel titles exist in the dashboards and that every cited test function exists; thresholds in the rows for cards 61, 63, 66, 78, 93, 94, 95, 108 and 137 were compared with the panel SQL by hand.
- `docs/t14_notes/*`: working notes, no contradiction with current code (panel ids and titles match).
- `docs/reference/v1_metabase_cards.json`: exported source data; scanned for IP addresses, passwords and tokens: none found.

## 4. What could not be verified (needs the VM, Docker or the owner)

- Nothing was executed against Docker, PostgreSQL, the VM or the network (the PC has no working Docker). Statements about runtime behaviour rest on reading the code: that `docker compose up -d` starts the exited `migrate` job again (the runbooks tell the operator to confirm with `docker compose ps -a migrate` and offer `docker compose up migrate`), that Grafana names folders after the directory names, and the exact output of `ops/smoke.sh`, `ops/check_panels.py` and `ops/soak_report.sh` on the VM.
- The commands in `docs/RUNBOOKS/run-tests.md` section 2 (throwaway database inside the VM's Postgres) were assembled from `docs/RESUME.md` section 5, the CI job and the code; their syntax was reviewed but they were not run. The 117 passed, 1 skipped result is the one recorded on 08-10-2026, not re-measured.
- Whether the 68 dashboard query panels run without error against the VM database (only the static validator, `python grafana/check_columns.py` and the synthetic-data tests cover them; `ops/check_panels.py` must be run on the VM after the next deploy). Whether HEAD is green in CI (the last confirmed commit is e5816af, plus the pinned-actions run).
- The evidence claims taken from the tracker (reboot test, Telegram delivery, cross-checks against the live feed, the restore drill numbers) were quoted, not reproduced. Part B of `docs/CONFIG_MAP.md`, the "difference from v1" paragraphs in `docs/DEFINITIONS.md` and section 11 of `docs/HANDOVER.md` describe v1, whose files are not in this repository; they were not re-checked.
- `.claude/agents/*.md` (role definitions) and the GitHub workflows other than their job lists were outside the scope.
- The internal IP address removed from `docs/HANDOVER.md` is still in the git history of earlier commits; rewriting history was out of scope and the owner should decide whether that matters (it is a private LAN address).
- The paths flagged by the link check below that do not exist in this repository are deliberate: `config/init.sql`, `app/` and `data_service.py` are v1 files; `FUNCTIONALITY_INVENTORY.md` and `docs/metabase_charts/` are untracked items in the owner's v1 working copy; `get-docker.sh` and `cmoplpete-cleanup.sh` are files left in the VM's home folder; `ops/check_data_integrity.py` and `ops/check_feed_crosscheck.py` are planned (T16), not yet written.

## 5. Checks run on the final tree

Raw output of the checks, run from the repository root on the final tree. Nothing under `collector/`, `db/`, `grafana/`, `ops/`, `tests/` (except `tests/README.md`) or `.github/` changed, so the code checks confirm that no behaviour changed: 205 unit tests pass exactly as before the audit. The link check found no broken markdown link and no missing repo path except the nine intentional references to files that are not in this repository (explained in section 4); `docs/DOC_AUDIT.md` repeats them, hence the duplicates.

```
### 1. Link and path check (script: every relative markdown link, every backticked repo-looking path, in all in-scope documents)
MISSING PATH docs\CONFIG_MAP.md config/init.sql
MISSING PATH docs\DECISIONS.md app/
MISSING PATH docs\DOC_AUDIT.md config/init.sql
MISSING PATH docs\DOC_AUDIT.md app/
MISSING FILE docs\DOC_AUDIT.md data_service.py
MISSING FILE docs\DOC_AUDIT.md FUNCTIONALITY_INVENTORY.md
MISSING PATH docs\DOC_AUDIT.md docs/metabase_charts/
MISSING FILE docs\DOC_AUDIT.md get-docker.sh
MISSING FILE docs\DOC_AUDIT.md cmoplpete-cleanup.sh
MISSING PATH docs\DOC_AUDIT.md ops/check_data_integrity.py
MISSING PATH docs\DOC_AUDIT.md ops/check_feed_crosscheck.py
MISSING FILE docs\HANDOVER.md FUNCTIONALITY_INVENTORY.md
MISSING PATH docs\HANDOVER.md docs/metabase_charts/
MISSING FILE docs\HANDOVER.md get-docker.sh
MISSING FILE docs\HANDOVER.md cmoplpete-cleanup.sh
MISSING PATH docs\HANDOVER.md app/
MISSING FILE docs\HANDOVER.md data_service.py
MISSING PATH docs\TRACKER.md ops/check_data_integrity.py
MISSING PATH docs\TRACKER.md ops/check_feed_crosscheck.py
problems 19

### 2. Style scans over README.md SPEC.md tests/README.md docs/*.md docs/RUNBOOKS/*.md docs/t14_notes/*.md .env.example ops/*.sh ops/*/*.sh
files scanned 38 | emoji lines 0 | American-spelling hits 0 | non-loopback IPv4 hits 0 | trailing-whitespace lines 0 | non-ASCII lines [('docs\\CHART_DEFINITIONS.md', 84), ('docs\\CHART_DEFINITIONS.md', 85)]

### 3. python grafana/validate_dashboards.py
dashboards: 8  (folders: atc=2, demographics=1, events=1, military=1, monitoring=1, pilots=2)
panels with queries: 68   text panels: 9   queries checked: 76
sql statements parsed with sqlglot (postgres): 169
relations read by panels: cohorts, controller_online_now, controller_session_ranked, controller_summaries, data_freshness, data_since, flight, flight_ext, monday_attendance, monitor_freshness, movements, near_miss_controllers, network_history, pilot_latest, pilot_online_now, poll_history
WARNING: dashboards/atc/atc-retention.json: panel 15 'Controller activity trends': no time range, now() or live view
RESULT: OK (0 errors, 1 warnings)
Note: nothing was executed against PostgreSQL.

### 4. python grafana/check_columns.py (tail)
bad 0

### 5. PYTHONPATH=collector/src python -m pytest -q -m "not integration" tests collector/tests --ignore=tests/.venv
.............................................................            [100%]
205 passed, 166 deselected in 1.51s

### 6. python ops/check_config_drift.py; python ops/check_size_budget.py (tail); git diff --stat against the baseline for code paths
config drift check OK (19 settings, 12 collector env keys, 42 .env.example keys)
 1335  TOTAL (limit 1400)
files changed outside README.md, SPEC.md, docs/, tests/README.md since 8eb4e6f: 0
```

