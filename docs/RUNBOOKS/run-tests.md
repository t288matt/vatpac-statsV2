# Runbook: run the tests and checks

Last reviewed: 21-10-2026 (section 1b); the rest 10-10-2026. The unit tests are `collector/tests` and `tests` outside `tests/integration`; the integration tests are `tests/integration` (counts are deliberately not quoted here, because they go stale; section 4 gives the command that prints the current ones, and a report quotes what the run printed; the integration tests include `test_watchdog_flap.py`, whose role tests need `watchdog_ro`: CI creates it from the migrate script). GitHub Actions runs all of them on every push to `main` and `dev` and, for a pull request, according to what the pull request changes (section 1c; `.github/workflows/ci.yml`); the owner reads the result on GitHub. The integration job runs the whole suite twice on the same database: once as it is, once with the test connection set to the `Australia/Sydney` time zone (section 5), and both passes run in strict mode (section 5). The sections below are for running them yourself.

> **Never run `db/roles.sql` by hand against the live database or the VM's Postgres container.** Roles are cluster-wide: running it with throwaway passwords resets the live `grafana_ro`, `collector` and `backup_ro` passwords and the stack stops working until `docker compose up -d` re-applies the real ones. It is safe only in a throwaway container (section 3).
>
> **Integration tests `TRUNCATE` every data table.** They refuse to run unless the database name ends in `_test`. Never point `VATPAC_TEST_DSN` at the live database.

## 1. Checks that need no database

From the repository root. These are the same commands CI runs.

```sh
# unit tests (pytest, pyyaml, shapely and the collector requirements installed; Python 3.11 or newer)
PYTHONPATH=collector/src python -m pytest -q -m "not integration" tests collector/tests --ignore=tests/.venv    # expect all passed; add -m "not integration and not slow_fuzz" for the quick local run (section 6); the strict xfails (known defects pinned by tests) are integration tests and show as xfailed there
python grafana/validate_dashboards.py          # dashboard shape and SQL syntax; pip install sqlglot to parse SQL
python ops/check_config_drift.py               # collector settings, compose and .env.example agree (needs PyYAML)
python ops/check_size_budget.py                # collector package under 1700 lines, no module over 300
ruff check --config ops/ruff.toml ops          # lint of the ops scripts
# the ops tooling tests run with the unit tests above: tests/test_deploy_script.py (deploy.sh, including the stale-checkout refusal), tests/test_env_readers.py (the shell and Python .env readers must agree) and tests/test_grafana_public.py (the shared Grafana client)
cd collector && ruff check . && mypy src       # lint and strict typing of the collector
# strict typing of the watchdog and ops scripts (the exact list is in .github/workflows/ci.yml; the watchdog needs the two audit files on its path)
mypy --strict --ignore-missing-imports ops/watchdog/watchdog.py ops/watchdog/integrity.py ops/watchdog/messages.py ops/check_config_drift.py ops/check_size_budget.py ops/check_panels.py ops/grafana_public.py ops/grafana_api.py ops/env_file.py ops/ci_scope.py
```

`make test`, `make drift`, `make size` and `make lint` wrap the same commands. On Windows without `make`, run the commands above. Set up the Python environment once: `python -m venv collector/.venv`, then `collector/.venv/bin/pip install pytest -r collector/requirements.txt` (Windows: `collector\.venv\Scripts\pip`).

## 1c. What CI runs for which pull request (issue 290, 06-11-2026)

A pull request runs a `scope` job first. It lists the changed paths (`git diff -z --name-status -M`, renames and deletes included) and `ops/ci_scope.py` classifies each one; the most expensive class wins. A push to `main` or `dev` is always the full suite.

| Changed paths | Scope | What runs |
|---|---|---|
| Only `grafana/dashboards/<group>/*.json`, `docs/CHART_DEFINITIONS.md`, and the six dashboard-only tests (`test_dashboard_descriptions.py`, `test_dashboard_nav.py`, `test_chart_definitions_tests.py`, `test_phase3_switch.py`, `test_dashboards_look.py`, `test_dashboards_sql_unchanged.py`) | `dashboards` | `scope`, `secret-scan`, `config-drift`, `dashboards` (validator and nav check), `size-budget`, `unit-tests` on the dashboard test files, `integration-tests` on the dashboard SQL tests (strict, both time zone passes) |
| Only root `*.md` and `docs/**` other than the full-suite docs below | `docs` | `scope` and `secret-scan` |
| `docs/RUNBOOKS/**`, `docs/reference/**`, `docs/DATA_DICTIONARY.md`, `docs/DEFINITIONS.md`, `docs/CONFIG_MAP.md`, root `SPEC.md` (tests read them) | `full` | everything |
| `collector/`, `db/`, `ops/` (including `ops/ci_scope.py`), `.github/`, `docker-compose*.yml`, `Makefile`, any other test file, `grafana/validate_dashboards.py`, `grafana/look_rules.py`, `grafana/provisioning/`, anything not named above, any mix with these, an empty or unreadable change list | `full` | everything, as before |

The scope job classifies with the base branch's copy of `ops/ci_scope.py` and forces `full` itself when `.github/` or the script is in the change, so a pull request cannot loosen its own filter. `ci-result` is the one check to require: it always runs, and it fails unless every job the scope needs succeeded (a job may be skipped only when the scope does not need it). The lists of dashboard tests are in `ops/ci_scope.py`; `tests/test_ci_scope.py` fails when a test that reads the dashboards is missing from them. Nothing is deselected or skipped inside a scope. To see the answer for your own branch: `git diff -z --name-status -M origin/main | python ops/ci_scope.py`. To run the dashboards-scope tests yourself: `python -m pytest -q -m "not integration" $(python ops/ci_scope.py tests unit)`, and, with the section 1b database, `python -m pytest $(python ops/ci_scope.py tests integration) -m integration -q`.

## 1b. Integration tests on the PC with Docker Desktop (works since 13-10-2026; the same database as CI)

Docker Desktop on the owner's PC works again (13-10-2026; the older note in section 2 about T25 no longer applies when `docker version` shows a server). This builds the SAME database CI builds: the PostgreSQL 16 image plus the production migrate image (dbmate, then `db/roles.sql` and `watchdog_ro`), in a throwaway container on the PC. It never touches the VM or the live database. Run in Git Bash from the repository root (or a worktree). Use one database per person or agent: the tests `TRUNCATE` every data table, and the name must end in `_test`.

```sh
# once: a network and a database container (the alias `db` is what migrate.sh connects to)
docker network create vatpac-pgtest
docker run -d --name vatpac-pgtest-db --network vatpac-pgtest --network-alias db -p 54329:5432   -e POSTGRES_PASSWORD=ci-superuser-pw -e POSTGRES_DB=vatpac_test postgres:16
# every time migrations change (and the first time): apply them with the production migrate image
docker build -q -t vatpac-migrate-ci -f ops/migrate/Dockerfile .
MSYS_NO_PATHCONV=1 docker run --rm --network vatpac-pgtest   -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=ci-superuser-pw -e POSTGRES_DB=vatpac_test   -e GRAFANA_RO_PASSWORD=ci-grafana-ro-pw -e COLLECTOR_DB_PASSWORD=ci-collector-pw -e BACKUP_DB_PASSWORD=ci-backup-pw   -e WATCHDOG_DB_PASSWORD=ci-watchdog-pw -v "$(pwd -W)/db:/db:ro" vatpac-migrate-ci
# run the tests (libpq must be findable by Python: the PostgreSQL 17 client that is installed on the PC has it)
export PATH="$PATH:/c/Program Files/PostgreSQL/17/bin" PYTHONPATH=collector/src
export VATPAC_TEST_DSN='postgresql://postgres:ci-superuser-pw@localhost:54329/vatpac_test'
export VATPAC_TEST_GRAFANA_RO_DSN='postgresql://grafana_ro:ci-grafana-ro-pw@localhost:54329/vatpac_test'
VATPAC_TEST_STRICT=1 python -m pytest tests/integration -m integration -q
```

To test a branch with a NEW migration against a clean database: `docker rm -f vatpac-pgtest-db`, then run the three steps again (about 30 seconds). Two people or agents at the same time: add a second database to the same container (`docker exec vatpac-pgtest-db psql -U postgres -c "CREATE DATABASE vatpac_other_test"`), run the migrate step with `POSTGRES_DB=vatpac_other_test`, and point `VATPAC_TEST_DSN` at it. The passwords are test-only values (the same ones CI uses for its throwaway container). On 13-10-2026 `tests/integration/test_flight_writer.py`, `test_flight_quiet_per_flight.py` and `test_flight_views_switch.py` together ran green in about half a minute on this route. `VATPAC_TEST_STRICT=1` is what CI sets: a missing database or any skipped test turns the run red, so a green result cannot hide tests that did not run. **Windows:** the two environment-only failures this section used to list (the restore-sanity tests picked the WSL `bash` launcher, which cannot read `C:\` paths; the unreachable-database test raced a 2 second connect timeout) are fixed in the tests themselves (13-10-2026), so on this route nearly every test should pass. Walk-through 21-10-2026 (Docker Desktop on Windows 11): with the PostgreSQL client on the `PATH` as above the whole suite runs; without it 7 tests skip (the drop-gate, role-hardening and smoke-check tests need a real `psql`, and strict mode would turn that red). Two timing tests in `tests/integration/test_panel_rules_gaps.py` (`test_snapshots_missed_compares_stored_snapshots_with_the_poll_interval` and `test_data_freshness_table_counts_the_last_hour`) failed on and off on this route and passed on CI's Linux runner; they compare the PC's clock with the container's to within a fraction of a second (cause not confirmed), so re-run them on their own before treating a failure as real. Three traps: (1) never run two test runs against the same database: each empties the tables, and the other run then fails with errors that are not real; (2) put `MSYS_NO_PATHCONV=1` on the one `docker run` line only and never `export` it for the session, because with it set the restore-sanity tests fail with `can't open file ...\bin\psql`; (3) never point `VATPAC_TEST_DSN` at a restored copy of production ([restore.md](restore.md), "Make a local copy of production"): the tests would empty it. The Australia/Sydney pass of CI is not reproduced here; set the test connection's time zone with the `PGOPTIONS` environment variable to try it. Remove the container when finished: `docker rm -f vatpac-pgtest-db && docker network rm vatpac-pgtest`.

## 2. Integration tests: the safe method on the VM

Use this when there is no working Docker on the PC (Docker Desktop on the owner's PC was broken; it works since 13-10-2026, see section 1b). It uses a throwaway database inside the VM's running Postgres container, a copy of the committed code in `/tmp`, and a short-lived container built from the collector image. The live database is not touched. It excludes the two role test files (`test_roles.py`, `test_grafana_ro.py`), which need roles that must not be created on the live cluster. CI runs those.

From the PC, in the repository root (so the VM gets exactly what is committed, without `.env`):

```sh
git archive --format=tar HEAD | ssh vatpac-v3 'rm -rf /tmp/vtest && mkdir -p /tmp/vtest && cd /tmp/vtest && tar -x'
```

On the VM, in `~/vatpac-statsV2`:

```sh
# 1. a throwaway database whose name ends in _test
docker compose exec -T db sh -c 'psql -X -q -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE vatpac_test"'

# 2. apply every migration (the up half only, as dbmate does)
for f in db/migrations/*.sql; do
  sed '/^-- migrate:down/,$d' "$f" | docker compose exec -T db sh -c 'psql -X -q -U "$POSTGRES_USER" -d vatpac_test -v ON_ERROR_STOP=1'
done

# 3. a DSN built in the shell and never printed
PGU="$(docker compose exec -T db printenv POSTGRES_USER | tr -d '\r')"
PGP="$(docker compose exec -T db printenv POSTGRES_PASSWORD | tr -d '\r')"
export VATPAC_TEST_DSN="postgresql://${PGU}:${PGP}@db:5432/vatpac_test"

# 4. run the tests in a short-lived container on the stack's network
docker run --rm --network vatpac --entrypoint sh -v /tmp/vtest:/work:ro -w /work \
  -e VATPAC_TEST_DSN -e PYTHONPATH=/work/collector/src:/tmp/pt vatpac-stats-collector:latest \
  -c 'pip install -q --target /tmp/pt pytest pytest-asyncio && python -m pytest -q -p no:cacheprovider -m integration tests/integration --ignore=tests/integration/test_roles.py --ignore=tests/integration/test_grafana_ro.py'

# 5. clean up: drop the database and delete the copy
unset VATPAC_TEST_DSN PGP
docker compose exec -T db sh -c 'psql -X -q -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE vatpac_test WITH (FORCE)"'
rm -rf /tmp/vtest
```

Notes:

- Do not run this during a soak if the VM is short of CPU or memory: the tests share the host with the live stack for a minute or two. They do not touch live data.
- Step 4 needs the collector image. In local-build mode that is `vatpac-stats-collector:latest`, which exists after any `docker compose up -d --build`. On the production VM (registry mode) it is the pulled image: use the name `docker compose config --images` prints for the collector (`ghcr.io/t288matt/vatpac-stats-collector:latest`) in place of `vatpac-stats-collector:latest` in that command. The `git archive ... | tar -x` in step 1 writes a scratch copy under `/tmp/vtest`, not the live folder, so it stays valid. The container installs pytest into `/tmp/pt` at run time, so it needs outbound network access to PyPI.
- A database named `vatpac_test` may already exist from earlier work (the restore drill found one). Drop it first (`DROP DATABASE vatpac_test WITH (FORCE)`) if step 1 says it exists, or the tests may fail on leftover data.

## 3. Integration tests: a throwaway Postgres container on a machine with working Docker

CI builds its database with the production `ops/migrate` image (dbmate, then `db/roles.sql`, then the `watchdog_ro` block), not with the `sed | psql` loop below; the manual steps below are the shortcut for a machine without that image. To mirror CI exactly, run the image against your throwaway container (see the `integration-tests` job in `.github/workflows/ci.yml`).

This also runs the role tests, because the throwaway container has its own cluster.

```sh
docker run -d --name vp-test -p 127.0.0.1:55436:5432 -e POSTGRES_PASSWORD=x postgres:16-bookworm
docker exec vp-test psql -U postgres -c "CREATE DATABASE vatpac_test"
for f in db/migrations/*.sql; do sed '/^-- migrate:down/,$d' "$f" | docker exec -i vp-test psql -U postgres -d vatpac_test -v ON_ERROR_STOP=1 -q; done
docker exec -i vp-test psql -U postgres -d vatpac_test -v ON_ERROR_STOP=1 -v grafana_ro_password=testpw -v collector_password=cpw -v backup_password=bpw -q < db/roles.sql
# watchdog_ro is created by ops/migrate/migrate.sh, not roles.sql: run its SQL block too (the watchdog role tests skip without it)
sed -n "/<<'SQL'/,/^SQL$/p" ops/migrate/migrate.sh | sed '1d;$d' | docker exec -i vp-test psql -U postgres -d vatpac_test -X -q -v ON_ERROR_STOP=1 -v wd_pw=wpw
export VATPAC_TEST_DSN=postgresql://postgres:x@127.0.0.1:55436/vatpac_test
export VATPAC_TEST_GRAFANA_RO_DSN=postgresql://grafana_ro:testpw@127.0.0.1:55436/vatpac_test
PYTHONPATH=collector/src python -m pytest tests collector/tests -q          # everything
docker rm -f vp-test
```

`db/roles.sql` needs all three password variables (`grafana_ro_password`, `collector_password`, `backup_password`) or it raises an error. The passwords above belong to a throwaway container that is deleted at the end; they are not real credentials. Without `VATPAC_TEST_DSN` the integration tests are skipped, not failed (unless `VATPAC_TEST_STRICT=1`, section 5). The fixture and the test layout are described in [../../tests/README.md](../../tests/README.md).

**Integrity checks (issue #20).** `tests/test_integrity_schedule.py` (unit: judging of rows, persisted schedule, alert text, de-duplication, settings) and `tests/integration/test_integrity_checks.py` (a clean database reports 0 for every one of the 20 invariants, one deliberately broken database per invariant, a guard that fails if the invariant list and the cases differ, the archive cross-check against damaged rows and files, and a `watchdog_ro` grant test) run with the rest. The sweep is run as `watchdog_ro` when that role exists, so on the VM variant (section 2) the role test is excluded or skipped: use section 3 (a throwaway cluster of your own, on the VM if the PC has no Docker, with the container capped: `--memory 512m --memory-swap 512m --cpus 1`, and `free -m` before and after) for the full run, which also needs the `watchdog_ro` block of `ops/migrate/migrate.sh`. After changing an invariant, prove the guard: make one invariant always return 0 (or delete it) in `ops/audit/invariants.sql` and the file must fail; restore it afterwards.

## 4. After a run

Report the exact counts you saw (the last line pytest printed, for example `N passed in S s`, with any `skipped` or `failed` figures in full), the commit tested, and which files were excluded. Do not call a run green from memory, and do not copy a count from another document: counts change with every test added. To see the current number of tests without running them: `PYTHONPATH=collector/src python -m pytest --collect-only -q tests collector/tests | tail -1` (all) or add `-m "not integration"` (unit only). If a test fails, read the failure first; two of the early failures were bugs in the tests themselves, not in the product.

## 5. Strict mode and the time-zone pass

- **Strict mode** (`VATPAC_TEST_STRICT=1`, set by CI): a missing `VATPAC_TEST_DSN` aborts the integration run (exit 4) instead of skipping every test, and any skipped integration test (a missing role, a missing module) makes the run fail with the list of skipped tests. Use it only when the roles and `watchdog_ro` exist (a full run). Without it a skip is allowed and shows as `skipped` in the last line: read that line.
- **Time-zone pass**: production runs in UTC, so a SQL expression that forgot `AT TIME ZONE 'UTC'` is invisible there. CI therefore repeats the integration tests with the session time zone `Australia/Sydney`: add `?options=-c%20TimeZone%3DAustralia/Sydney` to `VATPAC_TEST_DSN` (leave `VATPAC_TEST_GRAFANA_RO_DSN` alone: that role is pinned to UTC) and set `VATPAC_TEST_EXPECT_TZ=Australia/Sydney`, which makes `test_the_session_time_zone_is_what_the_run_asked_for` fail if the option was ignored. `tests/integration/test_time_zones.py` also moves its own session to Sydney and Los Angeles, so those tests fail in a plain UTC run too.
- **Test housekeeping**: the `conn` fixture empties the data tables and puts `app_setting` back to what the migrations seeded, so no test leaves a setting for the next one. `poll_interval_seconds` is not seeded (the collector publishes it), so a test that needs it calls `helpers.set_poll_interval`.

## 6. Replay and fuzz tests (issue 103)

Real feed data and malformed variants of it are the witness that does not share the authors' blind spots (issue 80 found a defect, issue 82, in code with hundreds of green tests).

| File | What it asserts | Needs |
|---|---|---|
| `tests/integration/test_replay_real_feed.py` | The committed replay set (`tests/fixtures/replay/`: one full and one compact real raw-feed snapshot of 10-10-2026, cut down; plus the anonymised sample) goes through `parse_snapshot`, `apply_snapshot` and `write_poll_log` without raising; every snapshot is stored; `network_snapshot`, `poll_log` and the stored positions equal a count taken straight from the raw payload (not from `model.py`); replaying again changes nothing; the checker itself fails when a stored row is damaged; the command line runs twice to identical dumps, and refuses a database not ending in `_test` | Postgres |
| `tests/integration/test_fuzz_ingest.py` | Seeded malformed variants of the real payload (one bad entry, or several) go through parser, database and `poll_log`: every other entry is stored, the counts agree, a second apply is a no-op. Named cases: a cid outside bigint is bisected out and counted, a fractional logon time, duplicate sessions, odd or oversized callsigns. | Postgres |
| `tests/test_fuzz_parser.py` | The same property on `parse_snapshot` alone over a grid of single-entry faults (null, wrong types, missing keys, huge, negative, non-finite numbers, odd Unicode, non-object `flight_plan`), plus changed feed shapes, truncated JSON at many cut points, a duplicated key, and the `1e999` / `NaN` literals, through `FeedClient` | none |
| `tests/test_replay_tool.py` | The committed replay set is small, real and overlapping; the independent count agrees with the parser; the dump diff | none |

The cases are generated by `tests/tools/fuzz_payloads.py` from fixed lists and a seeded `random.Random`, so a failure names its case (for example `pilots[43] cid=inf`) and repeats on every machine. `hypothesis` is not a dependency and was not added.

**The replay script.** `tests/tools/replay.py` replays any files or folders of raw payloads (`.json` or `.json.gz`, the feed archive layout works as it is) into a throwaway database, checks them, and can dump the stored rows:

```sh
python tests/tools/replay.py run tests/fixtures/replay --dsn "$VATPAC_TEST_DSN" --dump new.json    # this checkout
python tests/tools/replay.py run tests/fixtures/replay --dsn "$OTHER_TEST_DSN" --src /path/to/old/collector/src --dump old.json
python tests/tools/replay.py diff old.json new.json                                                   # exit 1 and the differing rows
```

To compare old code with new code, extract the old commit (`mkdir -p /tmp/old && git archive <commit> | tar -x -C /tmp/old`), run it with `--src /tmp/old/collector/src` against a SECOND migrated database whose name ends in `_test`, and diff the two dumps. `run` empties the data tables of the database it is given. The dump holds natural keys only (no surrogate ids, no clock values, timestamps in UTC), so identical code gives identical dumps. Replaying from the live archive: copy a handful of files off the VM read-only (feed-archive.md, "Read a file"), keep them outside the repository, and pass their folder.

**New replay fixtures.** `tests/tools/make_replay_fixtures.py --full <full file> --compact <compact file>` rebuilds `tests/fixtures/replay/` from two archive files (deterministic selection, every in-region pilot and listed controller kept).

**Proof the tests have teeth.** After changing the parser or `store.py`, re-run the proof: copy the checkout to a scratch folder (never edit the real source), apply one fault, and the named tests must fail. Faults used when these tests were written: the model before the issue 82 fix (`git show 48dde6e^:collector/src/vatpac/model.py`) fails `test_every_single_bad_entry_keeps_the_rest_of_the_snapshot` and the non-object entry and flight-plan cases; removing `OverflowError` from the `parse_all` except clause fails the grid test at `cid=inf`; narrowing the `except` in `store._write` to `IntegrityError` fails the bigint cid cases; skipping the polygon test in `apply_snapshot` fails the replay count checks.

**Quick and full runs.** The full fuzz grids carry the marker `slow_fuzz` (about 20 s of the parser grid plus the whole-grid ingest test). CI runs them with everything else; `-m "not integration and not slow_fuzz"` skips them for a quick local run.

**Fixed defects (issues 111 and 114).** An absurd multi-byte callsign, departure or arrival (indexed text) is now cut to 500 UTF-8 bytes at parse time (`model.cap_text`, counted as one rejected value), and `store._write` also bisects out a `ProgramLimitExceeded`, so no index overflow can lose a snapshot. A listed controller with frequency `inf`, `Infinity` or `-Infinity` no longer raises `OverflowError` from `parse_frequency_hz`. Their former pins are ordinary passing tests.
