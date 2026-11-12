# Archived: tests for the one-off v1 history import

These tests cover `ops/v1_import/` (`extract.sh`, `load.sh` and the SQL they join, including the rollback). CI and `pytest tests` do not run them; run them on demand as shown below.

| File | What it covers |
|---|---|
| `test_v1_import_extract.py` | `extract.sh` with a fake `ssh`: only SELECTs on the two summary tables, read-only sessions, frozen files, checksum. Needs bash. |
| `test_v1_import_default_command.py` | `load.sh` without `V1_IMPORT_PSQL`: the default `docker compose exec` command, with a fake `docker`. Needs bash. |
| `test_v1_history_import.py` | `load.sh` for real on a throwaway `_test` database with synthetic files: every loaded column, month counts, a second run inserts nothing, dry run, rollback, refusals, rejects. Needs Postgres, `psql` on the path and bash. |
| `v1_import_synth.py` | The synthetic-file generator (`generate OUT_DIR [--full]`, `compare DSN OUT_DIR`). |
| `conftest.py` | Loads the fixtures and helper modules from `tests/integration`. |

## Run them on demand

From the repository root. Name the folder: `pytest tests` and the CI commands skip it.

```sh
# the two shell tests need no database
PYTHONPATH=collector/src python -m pytest -q tests/archive/v1_import/test_v1_import_extract.py tests/archive/v1_import/test_v1_import_default_command.py

# the whole folder, with the import test: needs a migrated throwaway Postgres whose database name ends in _test
# (docs/RUNBOOKS/run-tests.md, section 3; never production or a copy of it, the tests empty every data table)
export VATPAC_TEST_DSN=postgresql://postgres:x@127.0.0.1:55436/vatpac_test
PYTHONPATH=collector/src python -m pytest -q tests/archive/v1_import
```

Without `VATPAC_TEST_DSN` the import test is skipped, not failed.
