# Handover: scheduled integrity checks (issue #20, tracker T16 / T53), branch `integrity-checks`

Written 09-10-2026 (UTC) when the session limit hit. NOTHING IS DEPLOYED and NOTHING IS ON MAIN. Branch only. Do not close issue #20. Issue #77 (T54, invariant pack defects) is filed and open.

## Decisions (owner, 09-10-2026)
- Slim scope only. Invariant sweep every 3 DAYS (`WATCHDOG_INTEGRITY_INTERVAL_HOURS=72`), archive cross-check every 14 DAYS (`WATCHDOG_CROSSCHECK_INTERVAL_DAYS=14`), `WATCHDOG_CROSSCHECK_SAMPLES=24`; 0 disables either interval. The last-run time is persisted in the watchdog status file (`state.integrity.<name>.last_run`), so restarts neither re-run at once nor skip a due run. A failed run is retried after 1 h; a sweep with violations is rechecked after 6 h. One alert level (WARN). Heavy audit tools stay manual.
- Runs in the watchdog (existing role, alert path, de-dup, status file). The cross-check uses the collector's raw-feed archive (compact files: no network, no new secrets) rather than the live feed; trade-offs are in docs/RUNBOOKS/integrity-checks.md.

## Done
- `ops/watchdog/integrity.py` (new): sweep (runs `ops/audit/invariants.sql` as one read-only repeatable-read transaction; a missing invariant row is a failure), archive cross-check (uses `ops/audit/independent.py`), persisted schedule, follow-up notice for new violations. Public functions `sweep(plan)` and `crosscheck(plan, now)`.
- `ops/watchdog/watchdog.py`: Config builds `integrity.Plan`; `cycle` calls `integrity.run_due` and sends notices.
- Packaging: `ops/watchdog/Dockerfile` builds with context `./ops` (copies integrity.py, independent.py, invariants.sql); `docker-compose.yml` (build context, 3 settings, `archive:/archive:ro`, `./config:/config:ro`), `.github/workflows/release.yml` (context ops), `ci.yml` (mypy list), `.env.example`.
- Grants: `ops/migrate/migrate.sh` adds SELECT on pilot_position, flight_summary, reference_data_change for watchdog_ro (applied by the migrate job; restarts nothing).
- `ops/audit/invariants.sql` fixed (issue #77): gap threshold 45 s to 5 minutes; the first snapshot of a database is excluded from the mass-logon check; the 22 s correlated query rewritten (whole pack 0.7 s); the "newest snapshot" check always returns a row. It has 18 invariants (docs said 17).
- Tests: `tests/test_integrity_schedule.py` (unit), `tests/integration/test_integrity_checks.py` (clean world, one broken database per invariant with exact knock-on sets, guard test, cross-check cases, role test), `tests/conftest.py` (sys.path), cycle tests in `tests/test_watchdog_decisions.py` (World stubs `integrity.run_due`), `tests/test_watchdog_checks.py` and `tests/integration/test_watchdog_flap.py` updated for the new grants.
- Docs: `docs/RUNBOOKS/integrity-checks.md` (new, complete).

## Half done (docs not yet updated)
`docs/RUNBOOKS/alerts.md` (add `data_integrity` and `feed_crosscheck` rows, sample messages, 3-day latency note, review date), `daily-checks.md`, `deploy-to-vm.md` (new section 4e from "Deploy steps" below; the "what a change needs" table: ops/audit/independent.py and invariants.sql now need a watchdog rebuild), `run-tests.md` (new files; the VM variant skips the watchdog_ro test), `docs/CONFIG_MAP.md` (3 settings, `ARCHIVE_DIR` and `REFERENCE_CONFIG_DIR` set by compose, 2 new mounts), `ops/audit/README.md` (18 invariants, used by the watchdog), optional ADR-026, tracker rows T53 and T54 and the issue map (#77 open, 13 open issues; only the T16 row is updated so far).
Not run since the last edit: `ops/check_config_drift.py`, `check_size_budget.py`, the two `grafana/*.py` checks, ruff and mypy after renaming `_sweep` and `_crosscheck` to `sweep` and `crosscheck` (they passed before), the whole integration suite, the Sydney pass, CI.

## Not started
Push to main and CI, the comment on issue #20 with the evidence, removal of the worktree.

## Last known test results
- Unit, on the PC (Python 3.11): 501 passed, 437 deselected, before the final rename and the 24-sample default; the integrity unit file alone then: 36 passed.
- Integration, `tests/integration/test_integrity_checks.py` only, UTC, strict mode, throwaway Postgres 16.15 with its own cluster on the VM: 42 passed (commit "wip: invariant thresholds, first-snapshot exclusion"; before the rename and the 24-sample default). Mutation checks: with the gap invariant neutered its test failed; with an invariant deleted the suite failed at collection. NOT YET RUN: the whole integration suite, the Australia/Sydney pass.

## Live read-only invariant results (VM, 09-10-2026 16:31 UTC, fixed pack)
17 of 18 at 0. "snapshot gaps over 5 minutes" = 1 (the 6 minute power cut, 15:35:57 to 15:41:57, T51; it leaves the 3 day window at 12-10-2026 15:42 UTC). The first, unfixed pack run showed 6 gaps and 1 mass-logon (the go-live snapshot): bad checks, see #77. Live cross-check dry run (collector role, read-only, capped container): 48 samples over 1 day and over 14 days, 0 mismatches; about 0.9 s CPU per sample, so 24 samples is about 22 s per run.
Consequence: the first sweep after deploy raises WARN data_integrity for the power-cut gap and RECOVERED after 12-10-2026 15:42 (up to 6 h later). Ask Matt whether to deploy after that time or accept it as a live proof of the alert.

## VM clean-up (done)
Removed container `vp-ic-pg` (throwaway Postgres, 512m caps), container `vp-ic-wd`, image `vp-ic-wd:test`, /tmp/ic and helper scripts. Nothing of mine is left. Not mine and left alone: containers `nice_driscoll` and `vp65-db` (seen at 16:18 UTC).

## Open questions for Matt
1. Deploy timing against the first-run WARN (above).
2. The cross-check blocks the watchdog cycle for about 22 s every 14 days (could move to a thread; not done).
3. Samples are evenly spread, not weighted to the evening peak; raise `WATCHDOG_CROSSCHECK_SAMPLES` if wanted.

## Resume steps
1. `cd C:\Users\mdora\Claude\vatpac-statsV2\.worktrees\integrity-checks` (branch `integrity-checks`); `git fetch`, merge origin/main as a new commit if needed.
2. Finish the docs under "Half done"; tracker T53 and T54 rows; issue map.
3. Static checks from the worktree root: `python grafana/validate_dashboards.py`, `python grafana/check_columns.py`, `python ops/check_config_drift.py`, `python ops/check_size_budget.py`, `ruff check --config ops/ruff.toml ops`, `mypy --strict --ignore-missing-imports ops/watchdog/watchdog.py ops/watchdog/integrity.py ops/check_config_drift.py ops/check_size_budget.py ops/check_panels.py`, and the unit tests.
4. Integration on the VM, capped (run-tests.md section 3 variant): `git archive HEAD | ssh vatpac-v3 'mkdir -p /tmp/ic && cd /tmp/ic && tar -x'`; `docker run -d --name vp-ic-pg --memory 512m --memory-swap 512m --cpus 1 -e POSTGRES_PASSWORD=x postgres:16.15-bookworm`; create `vatpac_test`, apply the migrations, `db/roles.sql` with throwaway passwords and the watchdog_ro block of migrate.sh; run pytest inside `vatpac-stats-collector:local` with `--network container:vp-ic-pg --memory 512m --memory-swap 512m --cpus 1`, `-e VATPAC_TEST_STRICT=1`, installing only `pytest pytest-asyncio httpx pyyaml` into a target dir (NOT shapely: it breaks numpy). Run once in UTC and once with `?options=-c%20TimeZone%3DAustralia/Sydney` on the DSN and `VATPAC_TEST_EXPECT_TZ=Australia/Sydney`. Check `free -m` before and after; remove the container and /tmp/ic.
5. Then `git fetch`, make sure origin/main is an ancestor, `git push origin HEAD:main`, read CI with `gh run list --repo t288matt/vatpac-statsV2 --branch main`, comment on #20.

## Deploy steps (later, only on Matt's go; do NOT do now)
Fresh dump first (`docker compose exec backup backup.sh once`); copy the tree (`git archive`). (a) `docker compose up -d --build migrate` (or `docker compose up migrate`): applies the three new watchdog_ro grants; restarts nothing. (b) `docker compose up -d --build watchdog`: recreates only the watchdog (new build context, mounts, settings); collector polling is not interrupted. Verify: `docker compose ps`; watchdog healthy; `docker compose logs --tail=50 watchdog | grep -i integrity` shows the "invariant sweep" and "archive cross-check" lines on the first cycle; `docker compose exec -T watchdog cat /var/watchdog/watchdog_status.json` has `data_integrity` and `feed_crosscheck`; `sh ops/smoke.sh`; `python3 ops/check_panels.py`. `could not run (InsufficientPrivilege)` means step (a) did not run. Never run db/roles.sql by hand.
