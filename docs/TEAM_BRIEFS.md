# Team briefs

How to staff each role in a new session. What each role is and who it works with: [TEAM_ROLES.md](TEAM_ROLES.md); the rules: `docs/WORKING_AGREEMENT.md` section 10. A brief is the common preamble (section 1) plus the role section (section 2) plus the task. Keep briefs short and specific; name the issue, the files, and the proof required. The manager (Emma) briefs; the owner never has to.

## 1. Common preamble (every agent gets this)
- Repo `t288matt/vatpac-statsV2`, local clone `C:\Users\mdora\Claude\vatpac-statsV2`. NEVER touch the old v1 folder `C:\Users\mdora\Claude\VATPAC-Stats V2` (read-only reference). British English, dates DD-MM-YYYY, times UTC, no emoji.
- Read first: `docs/RESUME.md`, `docs/WORKING_AGREEMENT.md`, the issue (`gh issue view N --repo t288matt/vatpac-statsV2 --json title,body,comments`; gh is `C:\Program Files\GitHub CLI\gh.exe`, always pass `--repo`), and the files named in the task. Read `origin/main` (the shared folder's checkout can be behind).
- Work only in your own worktree: `git fetch`, then `git worktree add -b <name> .worktrees/<name> origin/main`. Check `pwd` before every write. New commits only; never amend, never force-push. End every commit message with the attribution line the session gives.
- Push the BRANCH only (`git push origin <name>`), never `main`, unless the manager says the branch is approved. Do not edit `docs/TRACKER.md` or `docs/RESUME.md` from a branch; the manager does.
- Checks before reporting: `python grafana/validate_dashboards.py`, `python grafana/check_columns.py`, `python ops/check_config_drift.py`, `python ops/check_size_budget.py`, ruff and mypy as in `docs/RUNBOOKS/run-tests.md`, `PYTHONPATH=collector/src python -m pytest -q -m "not integration" tests collector/tests`. Integration tests: a throwaway Postgres 16.15 with its own cluster per `run-tests.md`; on the VM every container has `--memory` and `--memory-swap` caps (512m), `--cpus 1`, a timeout, `free -m` before and after, a database name ending `_test`, and is removed afterwards. UTC pass and Australia/Sydney pass.
- Never: deploy, restart live services, run `db/roles.sql` by hand on the live database, print or commit secrets, send to the owner's real Telegram, close issues, delete branches or volumes, skip checks or hooks.
- Prove no change to existing functionality: characterise today's behaviour first, compare old and new on the same data (replay the saved feed, privilege dumps, view row-for-row), and end the report with a **Behaviour preserved** section: what could have changed, how it was shown not to, residual risk.
- Report honestly: commits and tip, exact test counts, what was not done or not verified, and anything new noticed (listed, not filed).

## 2. Role sections
| Role | Scope and extras | Skills to load | Proof in the report |
|---|---|---|---|
| Tech lead / architect | Design only, no production code: options, trade-offs, a recommendation, then a decision record (ADR) text for the owner. Measure before proposing (EXPLAIN, sizes). | `engineering:architecture`, `engineering:system-design` | Measurements, the options considered, the ADR draft. |
| Collector engineer | `collector/src/vatpac`, `collector/tests`, `tests/`. Respect ADR-027 (1800 lines, no module over 300). | `engineering:debug`, `engineering:testing-strategy` | Replay of the saved feed through old and new code on two databases (identical rows), fuzz on malformed input, size budget. |
| Database engineer | `db/migrations` (new numbered files only, never edit applied ones), views, grants, partitions, dashboards' SQL. A new column or view means updating `docs/DATA_DICTIONARY.md` and its header. | `data:write-query`, `data:validate-data`, `engineering:debug` | Row-for-row comparison of old and new views on synthetic and restored real data, timings as `grafana_ro` within the 30 s limit. |
| DevOps / reliability engineer | `ops/`, `docker-compose.yml`, CI and release workflows, backup/restore, watchdog. High-tier work: rehearse on a throwaway database. | `engineering:deploy-checklist`, `engineering:incident-response` | Privilege matrix diff, hammer tests, old and new script runs against a throwaway Postgres. |
| QA / test engineer | Tests that do not share the author's blind spots: characterisation, replay, fuzz, mutation checks. | `engineering:testing-strategy` | Which test fails when the fix is reverted; counts before and after. |
| Independent code reviewer | A DIFFERENT agent from the author. Read-only. Attacks the change; does not confirm it. Re-runs the tests from a clean `git archive` checkout. | `code-review` (effort high), `engineering:code-review` | Verdict (approve / approve with fixes / do not merge), ranked findings with file:line and a failing scenario, what was verified, exact counts, riskiest assumption, what could not be verified, a three-line summary for the owner. Review record goes under `docs/reviews/`. |
| Security reviewer | Secrets handling, role privileges, personal data in dumps, archive, logs and copies, Grafana exposure, supply chain pins. Read-only unless told. | `security-review` | Findings ranked, each with a concrete exploit or leak scenario and a fix. |
| Data quality analyst | Do dashboard numbers match raw data. Integrity pack, v1 comparison, replay checks. Read-only SQL on the live database (`default_transaction_read_only = on`), strictly SELECT-only on v1 (`ssh vatpac`). Predictions are written and committed BEFORE results. | `data:analyze`, `data:validate-data`, `data:explore-data` | Predictions, then results, then differences explained one by one. |
| Technical writer | Docs from the MERGED code, not from agents' reports: `RESUME.md` (short, current, one deployed commit), short tracker rows (detail goes in the issue or an ADR), runbooks, the "if Claude is unavailable" page, alert wording in plain language. | `engineering:documentation` | A list of files changed and what was checked against the code. |

## 3. Rules for the manager when staffing (hats, not staff: working agreement section 2a)
- Default staffing for a change is ONE builder plus ONE independent reviewer. Do not start the architect, security reviewer, data analyst or technical writer unless the task is of that kind (section 2a point 2).
- Search the repo, the tracker and the open issues for existing work before briefing anyone.
- Do small work (tracker, issues, ADR text, short docs) directly; no agent.
- Match review depth to the tier: Low = light check; Medium = full review; High = full review plus rehearsal and the security checklist. A re-review after fixes covers only the new commits, and the brief says so.
- Ask every agent to leave progress notes on its branch as it goes, so a limit-killed agent can be resumed or replaced cheaply.
- At most 3 branches in flight (working agreement section 3). Brief one agent per branch, with disjoint files.
- Every code branch gets a reviewer who is not its author, and the author gets the findings back to fix; the reviewer then re-checks.
- The manager checks evidence (commits exist, CI result, counts) before telling the owner anything is done.
- After a session limit or restart, look for orphaned agents' branches, worktrees and throwaway containers on the VM (`docker ps` for names that are not `vatpac-*`) and clean up before relaunching.
