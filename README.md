# VATPAC Stats v3

Live pilot and controller statistics for the VATPAC region, built from the public VATSIM data feed. One collector polls the feed every 15 seconds, PostgreSQL 16 stores it, and Grafana shows it. Everything is dockerised and starts with `docker compose up -d`.

## Quick start

Requirements: Docker Desktop (Windows or macOS) or Docker Engine with the Compose plugin (Linux). Nothing else is needed to run the stack.

```
sh ops/gen_env.sh           # writes .env with random passwords (Git Bash or Linux); or: make gen-env
sh ops/check_env.sh         # rejects blank, weak or repeated secrets (make up runs this for you)
docker compose up -d --build
docker compose ps           # wait until db, collector, grafana, backup and watchdog are healthy
```

`.env.example` has blank secrets on purpose: `docker compose` refuses to start until they are set. Without `sh`, copy `.env.example` to `.env` and fill every blank secret yourself (letters and digits only, at least 16 characters, a different value for each, because the passwords are placed inside connection URLs). Never commit `.env`.

Then open Grafana at http://localhost:3300 and log in with `GRAFANA_ADMIN_USER` and `GRAFANA_ADMIN_PASSWORD` from `.env`. Login is required; anonymous access is off. Grafana listens on the loopback address only; set `GRAFANA_BIND` in `.env` to reach it from the LAN (see [runbook: deploy to the VM](docs/RUNBOOKS/deploy-to-vm.md)).

Charts start empty at go-live and fill as data arrives (there is no backfill, ADR-011). Some charts need days or weeks of history before they say anything useful.

On every start the `migrate` container applies any new SQL migrations and re-applies the database roles and grants, then exits; that is expected (`migrate` shows `exited (0)`). On Linux the same commands work unchanged; see [runbook: deploy to the VM](docs/RUNBOOKS/deploy-to-vm.md).

Quick health check:

```
docker compose logs --tail=20 collector
docker compose exec db psql -U vatpac -d vatpac -c "SELECT status, count(*) FROM poll_log WHERE polled_at > now() - interval '5 minutes' GROUP BY 1;"
```

You should see `ok` rows at about one per 15 seconds, plus a few `skipped_unchanged`. (`vatpac` is the default `POSTGRES_USER` and database name.) `make smoke` runs a fuller set of checks. The routine checks are in [runbook: daily and weekly checks](docs/RUNBOOKS/daily-checks.md).

## Where things are

| What | Where |
|---|---|
| Grafana | http://localhost:3300 (port from `GRAFANA_PORT`, interface from `GRAFANA_BIND`, default loopback only) |
| Collector health and metrics | `/health` and `/metrics` on port 8080, inside the Docker network only (not published) |
| Database | Not published. The dev overlay publishes it on `127.0.0.1:55432` only |
| Collector source | `collector/src/vatpac/` |
| Migrations and roles | `db/migrations/`, `db/roles.sql` |
| Dashboards (as code) | `grafana/dashboards/`, `grafana/provisioning/` |
| Reference files | `config/australian_airspace_polygon.json`, `config/controller_callsigns_list.txt` (`config/australian_airspace_sectors.geojson` is kept but not read by v3) |
| Backups, watchdog, ops scripts | `ops/` |
| Documentation | `docs/` (start with [docs/RESUME.md](docs/RESUME.md)) |
| Spec | [SPEC.md](SPEC.md) |

## Commands (Makefile)

On Windows without `make`, run the underlying commands shown.

| Command | What it does |
|---|---|
| `make gen-env` | Write `.env` from `.env.example` with random passwords (refuses to overwrite) |
| `make check-env` | `ops/check_env.sh`: reject blank, weak or repeated secrets in `.env` |
| `make up` | `check-env`, then `docker compose up -d --build` |
| `make dev` | Same as `make up`, with the dev overlay (database on `127.0.0.1:55432`) |
| `make down` | `docker compose down` (volumes are kept) |
| `make logs` | Follow the last 100 log lines of every service |
| `make ps` | Container status |
| `make config` | Validate the compose files with a generated throwaway env (`.env.example` has blank secrets, so it cannot be validated directly) |
| `make lint` | ruff and mypy on the collector, ruff on `ops/` |
| `make drift` | `ops/check_config_drift.py`: collector settings, compose and `.env.example` agree |
| `make test` | Unit tests (no database, no network); the integration tests are skipped without `VATPAC_TEST_DSN` |
| `make smoke` | `ops/smoke.sh` |
| `make size` | Collector size budget check (package under 1400 lines, no module over 300) |
| `make soak` | `ops/soak_report.sh`: soak report from `poll_log`, was a snapshot stored every 15 s for the last `HOURS` (default 24)? Reports PASS only when the whole window is covered with at least 99% of the expected snapshots and a p95 cadence of 17 s or less; shorter runs say INCOMPLETE |
| `make pii` | Fixture PII check |
| `make backup` | Take a backup now |
| `make backup-test` | Restore the newest backup into a scratch database and sanity-check it |
| `make restore FILE=/backups/<dump> [TARGET=vatpac_restore] CONFIRM=<same as TARGET>` | Restore a dump into a staging database, check it, then swap (see [runbook: restore](docs/RUNBOOKS/restore.md)) |

## Running the tests

Unit tests need only Python 3.11 or newer (the container uses 3.12) and the collector requirements:

```
python -m venv collector/.venv
collector/.venv/bin/pip install pytest -r collector/requirements.txt     # Windows: collector\.venv\Scripts\pip
PYTHONPATH=collector/src collector/.venv/bin/python -m pytest -q -m "not integration" tests collector/tests --ignore=tests/.venv
```

`make test` runs the same command using whatever `pytest` is on the path. Static checks that need no database: `python grafana/validate_dashboards.py` (`pip install sqlglot` as well to parse the SQL), `make drift`, `make size`, `make pii`, `make lint`.

Integration tests need a real PostgreSQL 16 with the migrations applied, in a **scratch** database whose name ends in `_test` (the fixtures truncate every data table). Without `VATPAC_TEST_DSN` they are skipped. Never point them at the live database, and never run `db/roles.sql` against the live VM (it resets the live role passwords). The step-by-step method, including the safe throwaway-database method on the VM, is in [runbook: run the tests](docs/RUNBOOKS/run-tests.md); the fixture notes are in [tests/README.md](tests/README.md).

## Current status

A snapshot as of 08-10-2026 taken from [docs/TRACKER.md](docs/TRACKER.md), which is the single source of truth and is updated with each change; read it for the current position. Times are UTC.

Evidenced (see the tracker rows for the evidence):

- The stack builds and runs on the Linux VM and survived a reboot test with all five long-running containers returning unaided (T2).
- CI is green on GitHub, including the integration tests (T3, T10).
- A Telegram test alert was delivered and confirmed (T4).
- Live ingestion matched the live VATSIM feed in 6 of 6 samples at two times of day (T16, T21), and a nightly dump was restored into a scratch database and compared with the live one (T18, [docs/RESTORE_DRILL.md](docs/RESTORE_DRILL.md)).
- 205 unit tests and 166 integration tests exist (371 collected). The integration suite, excluding the two role test files, was last run in full on a throwaway database on 08-10-2026 (117 passed, 1 skipped); CI runs all of them.
- The dashboards hold 8 dashboards with 68 query panels and 9 text panels; `python grafana/validate_dashboards.py` checks their shape and SQL syntax without running anything against PostgreSQL.

Not yet met (the gate for saying the goal is met, in the tracker's checklist):

- The 24-hour soak on the VM (T1, `sh ops/soak_report.sh 24` must print PASS; see [docs/RUNBOOKS/daily-checks.md](docs/RUNBOOKS/daily-checks.md) for how to read it).
- The newest collector fixes, migrations 20261008000030 and 20261008000031 and the rebuilt dashboards are merged but not yet deployed to the VM (T7); the deploy waits for the soak because a redeploy puts a gap in it.
- Recording the evidence in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (T11).
- The 30 to 90 day charts have no history until months after go-live (there is no backfill, ADR-011).

Out of the repository by design (ADR-012): the Docker socket proxy and any Cloudflare tunnel. The watchdog is alert-only, and a container that is running but unhealthy is not restarted automatically until that external piece exists.

## Documentation

Start with [docs/RESUME.md](docs/RESUME.md) if you are picking the project up cold.

| Document | Contents |
|---|---|
| [docs/RESUME.md](docs/RESUME.md) | Entry point for a new operator or session: what must happen next, decisions, how to test |
| [docs/TRACKER.md](docs/TRACKER.md) | The live task list and the goal-met checklist |
| [docs/HANDOVER.md](docs/HANDOVER.md) | Point-in-time hand-over written 07-10-2026 (history; the tracker is current) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, data flow, containers, failure behaviour, scope |
| [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md) | Tables, views, functions, roles |
| [docs/DEFINITIONS.md](docs/DEFINITIONS.md) | Flight, session, online now, airborne minutes, gates, caps, near-miss, and differences from v1 |
| [docs/CONFIG_MAP.md](docs/CONFIG_MAP.md) | Every environment variable, and v1's variables marked kept, changed or dropped |
| [docs/CHART_DEFINITIONS.md](docs/CHART_DEFINITIONS.md) | Every v1 chart and what replaced it |
| [docs/DASHBOARDS_SIMPLIFIED.md](docs/DASHBOARDS_SIMPLIFIED.md) | Dropped charts and why |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Decision log (ADRs) |
| [docs/RESTORE_DRILL.md](docs/RESTORE_DRILL.md) | Result of the restore drill of 08-10-2026 |
| [docs/DOC_AUDIT.md](docs/DOC_AUDIT.md) | Findings of the documentation audit of 08-10-2026 |

Runbooks (`docs/RUNBOOKS/`):

- [Deploy to the VM](docs/RUNBOOKS/deploy-to-vm.md) (first install, updating by git archive, rebuild versus reload, rollback)
- [Daily and weekly checks, and the soak report](docs/RUNBOOKS/daily-checks.md)
- [Alerts: what each Telegram message means](docs/RUNBOOKS/alerts.md)
- [Feed outage](docs/RUNBOOKS/feed-outage.md)
- [Stuck collector](docs/RUNBOOKS/stuck-collector.md)
- [Database full](docs/RUNBOOKS/db-full.md)
- [Restore from backup](docs/RUNBOOKS/restore.md)
- [Update reference files](docs/RUNBOOKS/update-reference-files.md)
- [Add or change a dashboard](docs/RUNBOOKS/dashboards.md)
- [Run the tests](docs/RUNBOOKS/run-tests.md)

## Notes

The repository is private but written to be public-safe: no secrets, no real personal data in fixtures. Names and CIDs are stored and shown, as in v1 (ADR-009); check the VATSIM data-use policy and put the dashboards behind a login before any public exposure.
