# Runbook: run the tests and checks

Last reviewed: 08-10-2026. There are 252 unit tests (`collector/tests`, `tests` outside `tests/integration`) and 231 integration tests (`tests/integration`), 483 collected in total (counted on the archive branch, 08-10-2026; the integration tests include `test_watchdog_flap.py`, whose role tests need `watchdog_ro`: CI creates it from the migrate script). GitHub Actions runs all of them on every push to `main` and `dev` and on pull requests (`.github/workflows/ci.yml`); the owner reads the result on GitHub (the repository is private and `gh` is not installed). The sections below are for running them yourself.

> **Never run `db/roles.sql` by hand against the live database or the VM's Postgres container.** Roles are cluster-wide: running it with throwaway passwords resets the live `grafana_ro`, `collector` and `backup_ro` passwords and the stack stops working until `docker compose up -d` re-applies the real ones. It is safe only in a throwaway container (section 3).
>
> **Integration tests `TRUNCATE` every data table.** They refuse to run unless the database name ends in `_test`. Never point `VATPAC_TEST_DSN` at the live database.

## 1. Checks that need no database

From the repository root. These are the same commands CI runs.

```sh
# unit tests (pytest, shapely and the collector requirements installed; Python 3.11 or newer)
PYTHONPATH=collector/src python -m pytest -q -m "not integration" tests collector/tests --ignore=tests/.venv    # expect 252 passed
python grafana/validate_dashboards.py          # dashboard shape and SQL syntax; pip install sqlglot to parse SQL
python grafana/check_columns.py                # every dashboard column exists in a view; expect "bad 0"
python ops/check_config_drift.py               # collector settings, compose and .env.example agree (needs PyYAML)
python ops/check_size_budget.py                # collector package under 1700 lines, no module over 300
python ops/check_fixture_pii.py tests/fixtures # the committed fixture holds no real names or CIDs
ruff check --config ops/ruff.toml ops          # lint of the ops scripts
cd collector && ruff check . && mypy src       # lint and strict typing of the collector
```

`make test`, `make drift`, `make size`, `make pii` and `make lint` wrap the same commands. On Windows without `make`, run the commands above. Set up the Python environment once: `python -m venv collector/.venv`, then `collector/.venv/bin/pip install pytest -r collector/requirements.txt` (Windows: `collector\.venv\Scripts\pip`).

## 2. Integration tests: the safe method on the VM

Use this when there is no working Docker on the PC (Docker Desktop on the owner's PC is broken, docs/TRACKER.md T25). It uses a throwaway database inside the VM's running Postgres container, a copy of the committed code in `/tmp`, and a short-lived container built from the collector image. The live database is not touched. This method gave 117 passed, 1 skipped on 08-10-2026; it excludes the two role test files (`test_roles.py`, `test_grafana_ro.py`, 48 tests), which need roles that must not be created on the live cluster. CI runs those.

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
  -e VATPAC_TEST_DSN -e PYTHONPATH=/work/collector/src:/tmp/pt vatpac-stats-collector:local \
  -c 'pip install -q --target /tmp/pt pytest pytest-asyncio && python -m pytest -q -p no:cacheprovider -m integration tests/integration --ignore=tests/integration/test_roles.py --ignore=tests/integration/test_grafana_ro.py'

# 5. clean up: drop the database and delete the copy
unset VATPAC_TEST_DSN PGP
docker compose exec -T db sh -c 'psql -X -q -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE vatpac_test WITH (FORCE)"'
rm -rf /tmp/vtest
```

Notes:

- Do not run this during a soak if the VM is short of CPU or memory: the tests share the host with the live stack for a minute or two. They do not touch live data.
- Step 4 needs the image `vatpac-stats-collector:local`, which exists after any `docker compose up -d --build` on the VM. The container installs pytest into `/tmp/pt` at run time, so it needs outbound network access to PyPI.
- A database named `vatpac_test` may already exist from earlier work (the restore drill found one). Drop it first (`DROP DATABASE vatpac_test WITH (FORCE)`) if step 1 says it exists, or the tests may fail on leftover data.

## 3. Integration tests: a throwaway Postgres container on a machine with working Docker

This also runs the role tests, because the throwaway container has its own cluster.

```sh
docker run -d --name vp-test -p 127.0.0.1:55436:5432 -e POSTGRES_PASSWORD=x postgres:16.4-bookworm
docker exec vp-test psql -U postgres -c "CREATE DATABASE vatpac_test"
for f in db/migrations/*.sql; do sed '/^-- migrate:down/,$d' "$f" | docker exec -i vp-test psql -U postgres -d vatpac_test -v ON_ERROR_STOP=1 -q; done
docker exec -i vp-test psql -U postgres -d vatpac_test -v ON_ERROR_STOP=1 -v grafana_ro_password=testpw -v collector_password=cpw -v backup_password=bpw -q < db/roles.sql
# watchdog_ro is created by ops/migrate/migrate.sh, not roles.sql: run its SQL block too (the watchdog role tests skip without it)
sed -n "/<<'SQL'/,/^SQL\$/p" ops/migrate/migrate.sh | sed '1d;\$d' | docker exec -i vp-test psql -U postgres -d vatpac_test -X -q -v ON_ERROR_STOP=1 -v wd_pw=wpw
export VATPAC_TEST_DSN=postgresql://postgres:x@127.0.0.1:55436/vatpac_test
export VATPAC_TEST_GRAFANA_RO_DSN=postgresql://grafana_ro:testpw@127.0.0.1:55436/vatpac_test
PYTHONPATH=collector/src python -m pytest tests collector/tests -q          # all 483
docker rm -f vp-test
```

`db/roles.sql` needs all three password variables (`grafana_ro_password`, `collector_password`, `backup_password`) or it raises an error. The passwords above belong to a throwaway container that is deleted at the end; they are not real credentials. Without `VATPAC_TEST_DSN` the integration tests are skipped, not failed. The fixture and the test layout are described in [../../tests/README.md](../../tests/README.md).

## 4. After a run

Report the exact counts you saw (for example `117 passed, 1 skipped`), the commit tested, and which files were excluded. Do not call a run green from memory: the numbers in other documents are dated and may be out of date. If a test fails, read the failure first; two of the early failures were bugs in the tests themselves, not in the product (docs/TRACKER.md, T14).
