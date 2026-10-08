# Resume note (written 08-10-2026, about 14:00 UTC, because the owner's weekly usage limit resets 10-10-2026 00:00)

Read this first if you are picking the project up cold, then docs/TRACKER.md (the live task list), then docs/HANDOVER.md.
Goal (verbatim): "I want to be able to reproduce these graphs and have them live and available 24/7. I want the system to be dockerised."

## 1. What must happen for the goal to be met (the essential path)
1. **Soak PASS.** On the VM run `ssh vatpac-v3 'cd ~/vatpac-statsV2 && sh ops/soak_report.sh 24'`. It must print PASS (full 24 h, no gap). The clock restarted at the reboot test (07-10-2026 21:48 UTC, one 45 s gap), so the earliest PASS is about **21:50 UTC on 08-10-2026**. At 07:40 UTC it showed 10.01 h, 99.5 % coverage, 1 gap (the reboot), 0 error polls.
2. **Deploy once**, after the soak passes and the review findings are triaged (section 4). Do not redeploy before, it would put a gap in the soak. The VM has no git; deploy with `git archive HEAD | ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'` (keeps the VM's `.env`), then `docker compose up -d --build` and migrations run via the `migrate` service. Pending to ship: collector fixes 6e3d131 and 7f5641a, migrations 20261008000030 and 20261008000031 (view `controller_summaries`), and the rewritten dashboards. After deploy run `sh ops/smoke.sh` and `python3 ops/check_panels.py` on the VM (expect SMOKE PASS and 0 errors).
3. **Record evidence** in docs/ARCHITECTURE.md (soak report output, reboot test, CI green, Telegram alert) and push, then tell the owner the goal is met. Never claim it without running these tools in the current session.

Already done and evidenced (08-10-2026): VM reboot test passed (all five containers returned unaided); CI green on e5816af; Telegram test alert delivered and confirmed by the owner; ingest checked against the live VATSIM feed (6 of 6 samples matched exactly); database integrity queries clean.

## 2. Decisions made this session (binding, owner said them in chat)
- Names and CIDs kept forever, no retention rule (ADR-019). Names and CIDs are not sensitive and need no scrubbing; tokens, passwords, .env and internal IPs stay protected.
- Card 94 and 95 rebuilt to match v1 counting; card 94 uses Monday to Friday (UTC). Cards 135 and 90 keep the aircraft_type lookup categories, not v1's LIKE ladder.
- Retention panels copy v1's rules exactly, including the quirk that "reactivated" needs no earlier session, so every controller counts as reactivated until 60 days of history exist.
- No GitHub CLI or token: tracking is in docs/TRACKER.md. Accepted risk: the Telegram bot token appeared once in a session transcript; not rotated.
- Use agents for parallel card work; owner approved. Data retention rules for all data is LOW priority (T12).

## 3. State of the work in flight (all unmerged and undeployed unless stated)
- **Merged to main, not deployed:** T13 retention dashboard (13 cards), T14 re-check (agents' branches t14-atc, t14-events, t14-pilots merged), cards 94, 95, 90, CI change (no cancel on main).
- **Agents launched 08-10-2026 about 13:20 UTC (results may be waiting):**
  - Builders on unmerged branches in sibling worktrees next to the repo: `t15-a` (`../vatpac-wt-t15a`: cards 180, 181, 182, 175, 183, 155), `t15-b` (`../vatpac-wt-t15b`: 142, 143, 161, 184, 156), `t15-c` (`../vatpac-wt-t15c`: 144, 146, 148, 153, 154, 186). Each writes notes to docs/t15_notes/ on its branch. The agents cannot run integration tests; the coordinator must.
  - Independent read-only reviewer over e90963e to HEAD (T26). Its report decides what must be fixed before deploy.
  - **t15-b finished** (08-10-2026 about 14:15 UTC): commit 2b57b35 on branch t15-b, five cards (142, 143, 161, 184, 156). Before merging: run tests/integration/test_military_events_t15.py on a throwaway database, run its panel SQL against real data, and check that the 156 query over positions (new view `pilot_sample`, migration 20261008000080) prunes partitions and is fast. Owner questions on card 156 are in the tracker. Add rows for these cards to CHART_DEFINITIONS.md and `pilot_sample` to DATA_DICTIONARY.md when merging.
  - **t15-c finished** (08-10-2026 about 14:35 UTC): commit 994bd3b on branch t15-c, cards 144, 146, 153, 154, 186 (148 not built, stays dropped). Before merging: run tests/integration/test_atc_sessions_t15.py on a throwaway database; run the 186 query on real data and check cost (it uses RANGE BETWEEN interval FOLLOWING, untested). Owner questions are in the tracker. Panel 10 of atc-overview.json is superseded by 153/154 and could be removed on approval.
  - If an agent did not finish, inspect its branch with `git -C ../vatpac-wt-t15a log --oneline -3` and `git status -s`; finish or discard its work.
- **To merge a T15 branch:** `git merge t15-a` in the main worktree, then run the checks (section 5), update CHART_DEFINITIONS.md and the tracker, push. Merge only after the owner approves scope.
- Worktrees can be removed when finished: `git worktree remove ../vatpac-wt-t15a` (and the t14 ones).

## 4. Open owner questions (ask one at a time, give a recommendation first)
1. Card 66 hours basis: airborne hours to 1 decimal (current) or v1 connected time in whole hours?
2. Card 131: add a CID table per Monday (v1 had CID lists; repo dropped them)?
3. Card 134: local-share chart window 0700-1200Z (current) or v1's 0800-1200Z?
4. Card 65 (most popular military aircraft): send its current SQL; the panel still uses is_military, v1 may use the RAAFv remarks rule.
5. Cards 41, 43, 98 (ATC interaction): stay dropped (ADR-002); does the owner want a substitute (needs a new ADR)?
6. Card 61: breadth of exclusions (the lookup excludes about 230 more codes than v1 names; E-series).
7. Card 93: rating 11 label ("Administrator" in v1; VATSIM calls 11 Supervisor).
8. Does v1 "total_enroute_time_minutes" mean airborne time or whole connected time (affects cards 93 and 135)?
9. Card 55: show a total column?
10. Backfill v1 history into v3 (T22). Owner actions pending: T8 deploy key, T17 off-box backup (Proxmox Backup Server), T19 outside heartbeat, T24 Metabase host housekeeping (container, 46 MB dump), T25 Docker Desktop repair.

## 5. How to test and check (what actually works here)
- Unit and static: `PYTHONPATH=collector/src python -m pytest -q -m "not integration" tests collector/tests --ignore=tests/.venv` (205 pass); `python grafana/validate_dashboards.py` (pip install sqlglot to parse SQL); `ruff check --config ops/ruff.toml ops`; `python ops/check_config_drift.py`; `python ops/check_size_budget.py` (every module under 300 lines, package under 1400).
- Integration tests need a database whose name ends `_test` and truncate it. The PC has no working Docker or Postgres, so use the VM safely: create a throwaway database `vatpac_test` inside the VM's existing Postgres container (`docker compose exec -T db psql -U $POSTGRES_USER -d postgres -c "CREATE DATABASE vatpac_test"`), apply every migration with `sed '/^-- migrate:down/,$d' file | psql -d vatpac_test -v ON_ERROR_STOP=1`, copy the working tree to a temp folder on the VM, run pytest in a short-lived container from the collector image (`docker run --rm --network vatpac --entrypoint sh -v /tmp/vtest:/work:ro -w /work -e VATPAC_TEST_DSN=... -e PYTHONPATH=/work/collector/src:/tmp/pt vatpac-stats-collector:local`, `pip install --target /tmp/pt pytest pytest-asyncio`), then drop the database and delete the temp folder. Take the DSN password from the db container's environment inside the shell, never print it. **Never run db/roles.sql there**: it would reset the live role passwords. Deselect tests/integration/test_roles.py and test_grafana_ro.py. Last result: 117 passed, 1 skipped.
- Panel queries against real data (read-only): extract `rawSql` from a dashboard JSON, replace `$__timeFrom()` with `(now() - interval '200 days')` and `$__timeTo()` with `(now())` (keep the brackets), wrap in `SELECT count(*) FROM (...) x` and pipe to psql in the db container.
- Do not push in rapid bursts: batch pushes (CI no longer cancels runs on main, but each push still starts a run). The owner reads CI results on GitHub and reports them (private repo, no `gh`).
- Metabase exports live outside git on the owner's PC (`C:\Users\mdora\Claude\metabase_cards.csv`, `metabase_cards_extra.csv`) and were taken from the Metabase application database on the owner's Docker host. Re-export commands are in the tracker history (T23).

## 6. Rules to keep
British English, DD-MM-YYYY, no emoji, state UTC. New numbered migrations only (never edit an applied one). The v1 box is the owner's: read-only queries only, ask first. Cloudflare and the Docker socket proxy are the owner's to add later. Do not expose Grafana publicly. Commit new commits only (never amend).
