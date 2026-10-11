# Independent review: deploy-checks (tip 91718b4, based on f893585), 10-10-2026

Reviewer: independent agent (not the author). MEDIUM tier. Verdict: **approve with fixes; do not merge as it stands.** Finding 1 breaks CI and must be fixed first; findings 2 to 4 should be fixed or consciously accepted.

## Findings, ranked
1. BLOCKER. `ops/deploy.sh:44` (also `${BASH_REMATCH[n]}`, `[[ =~ ]]`, `local`): the CI step "shell syntax (ops scripts)" runs `sh -n` on `ops/*.sh` (.github/workflows/ci.yml:50). On ubuntu `sh` is dash. Proven in Ubuntu (WSL) and with Git Bash dash: `sh -n ops/deploy.sh` gives `Syntax error: "(" unexpected (expecting "}")`, rc 2. Merging turns CI red on main, and `--preflight` itself requires CI green. Fix: check deploy.sh with `bash -n` (as ops/backup/*.sh already are) and exclude it from the `sh -n` loop. No CI run exists for the branch yet, so the author could not have seen it.
2. HIGH. `ops/deploy.sh:268`: `--verify` runs whatever `ops/smoke.sh` is on the VM. The deployed one (read-only look; mtime 09-10 17:49) is the PRE-#86 version: `DELETE FROM poll_log` and `INSERT ... VALUES ('ok')` as grafana_ro with `default_transaction_read_only = off`. Harmless only while grafana_ro lacks the privileges. I checked first (`has_table_privilege` DELETE and INSERT on poll_log, CREATE on public, superuser: all false), then ran `--verify`. If deploy 1's role re-apply ever grants too much, `--verify` wipes poll_log, which breaks the "reads only" claim. Fix: refuse unless the VM's smoke.sh contains the `ro-check-begin` marker, or stream the local copy (`ssh vm 'cd dir && sh -s' < ops/smoke.sh`).
3. HIGH (false pass). `ops/deploy.sh:314-329` invariants: rows whose count is not numeric are dropped by `grep -E '\|[0-9]+\|'`, and the row count is not pinned. Proven in the fake harness: one row of output gives `PASS invariants 1 invariants checked`; a row `name||` (NULL count) is silently dropped. The real pack has about 18. Fix: require the expected row count, or fail on any unparsable line.
4. MEDIUM (hidden fault). Expected-warning entries ignore the count: `snapshot gaps ... = 500` shows WARN while the entry is valid (`classify_invariant`, lines 63-75). Add a maximum count per entry. Also `health` (line 299) passes with `last_write_age_seconds` 999999 (stale archive), and fails if `errors` ever becomes the last key (`"errors": 0}` has no comma).
5. MEDIUM (false fail that will bite). `polling_gap` (line 286) looks at the newest 20 `poll_log` rows (10 minutes at the 30 s cadence) with a 90 s limit. After deploy 2 (collector stopped about 45 s), a legitimate restart gap of 100 s or more makes `--verify` FAIL for about 10 minutes (harness: gap 100 gives FAIL). Live today it already FAILs on a real 125 s gap (24 h histogram: one gap of 90-119 s, one of 120-129 s), so it catches something real, but after a deploy it needs a "wait 10 minutes" hint or a window starting at the collector StartedAt.
6. MEDIUM. Hangs: ssh has `ConnectTimeout` only (lines 99-100). A VM that connects but stalls (the memory-starved case) hangs `docker compose exec` for ever. Add `-o ServerAliveInterval=10 -o ServerAliveCountMax=3` and `timeout` on remote docker calls.
7. MEDIUM. `--mark` (lines 363-375): accepts any commit that exists locally (harness with ancestor check failing still rc 0), so an unmerged branch can be recorded as live; it never checks the VM tree matches the commit, so the marker is a claim, not a fact. If the append to `release.log` fails after `mv`, it reports FAIL though `.deploy/release` was already replaced (harness: log path made a directory). Injection is NOT possible: the argument only reaches `git rev-parse`, and only the 40-hex result is sent. Hostile arguments (`abc1234; touch /tmp/x`, `$(id)`, backticks, spaces, leading space) were refused; no file appeared. Note the regex alone is weak: bash `[[ =~ $ ]]` accepted `abc12345` followed by a newline and text, so git is the real barrier.
8. LOW-MEDIUM. Coverage gaps against docs/DEPLOY_PLAN_REVIEW_FIXES.md: no "collector start time unchanged" comparison (printed only, no baseline); no rollback-copy check (`~/vatpac-statsV2.rollback-*`); dump allowed up to 26 h old while the plan says a fresh `backup.sh once` dump; restore-test result (`.last_restore_test`) not read though deploy 1 depends on it; the 01:50-02:30 window is hard-coded while `BACKUP_TIME_UTC` is configurable; collector log not checked for "permission denied" or "maintenance finished"; `watchdog_status.json` keys not checked. Covered well: clock, memory (MemAvailable), disk, health, restarts, identity flap, invariant pack, watchdog log, polling gap, panels, smoke. `.deploy/lock` is never created by anything (harmless).
9. LOW. Lines 201 and 270 use `${out:+}${out:-fallback}`, which prints the whole output (full smoke output, full timedatectl) after the FAIL text instead of only the fallback. A dump dated in the future passes (`-27 h old`). `--status` exits 0 when the VM is unreachable. `gh run list --limit 50` loses the sha after about 25 merges (use `--commit <sha>`). Git commands use the current directory (run elsewhere gives a false FAIL; use `cd "$HERE/.."`). `foreign_containers` FAILs on other agents' test containers (live now: `d109-pg`): by design, but frequent; consider WARN for stopped ones.
10. Expected-warnings file: exact-name match works; a typo gives FAIL (good); a CRLF file gives FAIL "malformed" (safe); expiry is exact to the minute (tested on both sides). Real risk is finding 4 only. CI check: in-progress, failed, cancelled, skipped or missing runs all FAIL; empty gh output FAILs; good.

## Read-only claim
Read every line. `--preflight`, `--verify`, `--status` have no redirection to VM files, no temp files, no `docker run/stop/restart/build`, no `compose up`. SQL runs as the superuser inside the db container with `SET default_transaction_read_only = on`; both SQL files hold only SELECTs (invariants.sql is one READ ONLY transaction). `pg_restore -l` only reads. The only write path is `--mark`: `release.new` then `mv` (atomic for the marker), then an append to the log. Caveat: finding 2.

## Verified (clean `git archive` of origin/deploy-checks, Git Bash)
- `python grafana/validate_dashboards.py`: OK (0 errors, 4 warnings). `python grafana/check_columns.py`: bad 0. `python ops/check_config_drift.py`: OK. `python ops/check_size_budget.py`: 1742 of 1800. ruff 0.8.4 with the CI commands (`cd collector && ruff check .`, `ruff check --config ops/ruff.toml ops`) pass; the new test file also passes ruff.
- `PYTHONPATH=collector/src python -m pytest -q -m "not integration" tests collector/tests`: **877 passed, 527 deselected, 288 s**.
- Live, read-only: `--status` 2.2 s OK. `--preflight f893585`: 11 PASS, 1 FAIL (`foreign_containers: d109-pg`, another agent's container). `--verify`: 8 PASS, 1 WARN, 2 FAIL (polling_gap 125 s, finding 5; and invariant "snapshot rows without an ok poll_log row = 2", not in the expected file; owner should look, I did not investigate). Real ssh/docker quoting worked for every check, which the fake harness cannot prove.

## Mutation results (scratch copies; tests only)
| Mutation | Result |
|---|---|
| `mem_ok` always succeeds | 7 tests fail |
| CI check: drop the "no run found" branch | 2 tests fail |
| Backup window inverted | 10 tests fail |
| `--status` touches a file on the VM | `test_read_only_modes_write_nothing_on_the_vm[status]` fails |
| `--preflight` runs `docker compose restart` | `test_no_forbidden_command_reaches_the_vm[preflight]` fails |

All five caught. Not covered by any test: findings 3, 4, stale archive, mark of a non-main commit, the `sh -n` CI step.

## Cost
New test file alone: **99 passed in 274.6 s** (about 2.8 s per test, many bash processes per run). It is about 275 s of the 285 s total; the other 778 tests take about 10 to 13 s, so the file makes the Windows run roughly 22 times slower. On ubuntu-latest process start is much cheaper; I could not measure it (no pytest in WSL); my estimate is 30 to 60 s. Proportionate fix: keep one failing case per check (drop duplicates such as three ci_green and five recent_dump variants), batch pure-logic cases into one bash process, add a marker (for example `slow_shell`) so local quick runs can skip it with `-m "not integration and not slow_shell"` while CI runs it. Target under 60 s on Windows.

## Portability
Needs bash; runbook correctly says `bash ops/deploy.sh`. Tests use `shutil.which("bash")` and skip if absent, same as tests/test_backup_script.py; Git Bash locally and ubuntu in CI both fine for the tests. The CI problem is the `sh -n` step (finding 1).

## Proportionality
872 added lines for a 22-step manual procedure with about ten manual deploys and no failures: over-built for a first slice. Keep `--preflight`, `--status`, `--mark` and a lean `--verify`. Cut or defer: the expiry machinery (one entry; printing known WARNs would do), `.deploy/lock`, and roughly half the test cases. It did catch two real things live today (a stray container, a polling gap), so trim rather than drop.

## Could not verify
CI on ubuntu (no run for the branch; only the dash syntax check done); Linux test timing; behaviour during a real deploy; whether the 2 invariant violations are real.

## Cleanup
I created no containers, networks or volumes. Removed /tmp scratch files and the WSL copy. Other scratch is in the session scratchpad only.

## Summary for the owner
1. Do not merge yet: CI's `sh -n` check will fail on this bash-only script (small CI fix), and `--verify` runs the old VM smoke script that still has the unguarded DELETE (safe today, grants checked).
2. Two checks can wrongly pass (partial invariant output; a known-warning entry hides a growing count) and the polling-gap check will wrongly fail for 10 minutes after deploy 2.
3. Tests do fail when guards are broken (5 of 5 mutations caught) but cost 4.5 minutes on this PC; trim them, and trim the feature to preflight, status, mark and a leaner verify.

---
# Re-check of ae870f4 (on 91718b4), 10-10-2026

Verdict: **approve (merge-ready)**, with two small notes below. Read only `git diff 91718b4..ae870f4`.

Fixes verified as real:
1. CI: `.github/workflows/ci.yml` changed in that one step only (2 lines out, 2 in): `sh -n` for everything except `ops/deploy.sh`, `bash -n` for `ops/backup/*.sh` and `ops/deploy.sh`. Ran both loops as CI does (dash for sh): no failures.
2. Smoke: `--verify` now runs `grep -c ro-check-begin ops/smoke.sh` on the VM and only runs smoke if it is 1. Live today: FAIL "SKIPPED ... old unguarded version", and the old script was NOT run. (Consequence: `--verify` cannot go fully green until deploy 1 ships the guarded smoke.sh; that is the intended order.)
3. Invariants: expected rows are counted from the local invariants.sql (18; matches 17 UNION ALL + 1); a different row count, or a NULL or text count, is FAIL. Live: "18 invariants checked".
4. Expected warnings carry a maximum count; above it is FAIL. Health is parsed as JSON in the container with a 600 s last-write limit (live: PASS, 250 s).
5. Polling: newest row at or over 90 s old, or a gap over 300 s, FAIL; 90 to 300 s WARN with its end time. Live PASS.
6. ssh keepalive options and `timeout` (resolves to /usr/bin/timeout in Git Bash, not the Windows one). `--mark` now needs the commit on origin/main and writes the log before replacing the marker. Load, backup-time-from-.env, dump-age WARN over 2 h, lock removed, `--status` exits 1 when the VM is down, git runs via `git -C`.
7. No VM write path added: new remote commands are `grep -c`, `awk` on /proc/loadavg, and `sed -n` of the single BACKUP_TIME_UTC line from .env (printed value forced to HH:MM, otherwise 02:00). `--mark` is still the only write.

Measured: `tests/test_deploy_script.py` 8 passed in 70.8 s (was 274.6 s); whole non-integration suite 786 passed, 527 deselected, 83.6 s (was 877 in 288 s). ruff clean. Live: `--status` OK, `--preflight f893585` OK with one WARN (dump 18 h old), `--verify` 9 PASS, 2 WARN (both expected-count entries), 1 FAIL (the intended smoke skip). Mutation re-check (mem check always passes, plus row-count pin removed): 2 behaviour tests fail.

Remaining, low:
- `gh run list --commit <sha>` is not limited to main: if a PR run ever shares the merge sha (rebase or fast-forward merge) a cancelled PR run would make `ci_green` FAIL (safe side). Acceptable; note in the runbook.
- The second expected-warnings entry (poll_log timeouts, issue 113) expires 13-10-2026 16:00 UTC; remember to delete both lines when they expire.
