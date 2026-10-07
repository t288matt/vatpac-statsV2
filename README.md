# VATPAC Stats v3

Live pilot and controller statistics for the VATPAC region, built from the public VATSIM data feed. One collector polls the feed every 15 seconds, PostgreSQL 16 stores it, and Grafana shows it. Everything is dockerised and starts with `docker compose up -d`.

## Quick start

Requirements: Docker Desktop (Windows or macOS) or Docker Engine with the Compose plugin (Linux). Nothing else is needed to run the stack.

```
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
# edit .env: replace every "change-me" value (URL-safe passwords: letters and digits)
docker compose up -d --build
docker compose ps           # wait until db, collector, grafana, backup and watchdog are healthy
```

Then open Grafana at http://localhost:3300 and log in with `GRAFANA_ADMIN_USER` and `GRAFANA_ADMIN_PASSWORD` from `.env`. Login is required; anonymous access is off.

Charts start empty at go-live and fill as data arrives (there is no backfill, ADR-011). Some charts need days or weeks of history before they say anything useful.

On the first start the `migrate` container applies the SQL migrations and creates the read-only roles, then exits; that is expected. On Linux the same commands work unchanged; see [runbook: deploy to the VM](docs/RUNBOOKS/deploy-to-vm.md).

Quick health check:

```
docker compose logs --tail=20 collector
docker compose exec db psql -U vatpac -d vatpac -c "SELECT status, count(*) FROM poll_log WHERE polled_at > now() - interval '5 minutes' GROUP BY 1;"
```

You should see `ok` rows at about one per 15 seconds, plus a few `skipped_unchanged`.

## Where things are

| What | Where |
|---|---|
| Grafana | http://localhost:3300 (port from `GRAFANA_PORT`) |
| Collector health and metrics | `/health` and `/metrics` on port 8080, inside the Docker network only (not published) |
| Database | Not published. The dev overlay publishes it on `127.0.0.1:55432` only |
| Collector source | `collector/src/vatpac/` |
| Migrations and roles | `db/migrations/`, `db/roles.sql` |
| Dashboards (as code) | `grafana/dashboards/`, `grafana/provisioning/` |
| Reference files | `config/australian_airspace_polygon.json`, `config/controller_callsigns_list.txt` |
| Backups, watchdog, ops scripts | `ops/` |
| Spec | [SPEC.md](SPEC.md) |

## Commands (Makefile)

On Windows without `make`, run the underlying commands shown.

| Command | What it does |
|---|---|
| `make up` | `docker compose up -d --build` |
| `make dev` | Same, with the dev overlay (database on `127.0.0.1:55432`) |
| `make down` | `docker compose down` (volumes are kept) |
| `make logs` | Follow the last 100 log lines of every service |
| `make ps` | Container status |
| `make config` | `docker compose config -q` (validate the compose file) |
| `make lint` | ruff and mypy on the collector |
| `make test` | Unit tests (no database, no network) |
| `make smoke` | `ops/smoke.sh` |
| `make size` | Collector size budget check (1400 lines, no module over 300) |
| `make soak` | Soak report from `poll_log`: was a snapshot stored every 15 s for the last `HOURS` (default 24)? Reports PASS only for a full, gap-free window; shorter runs say INCOMPLETE |
| `make pii` | Fixture PII check |
| `make backup` | Take a backup now |
| `make backup-test` | Restore the newest backup into a scratch database and sanity-check it |
| `make restore FILE=/backups/<dump> [TARGET=vatpac_restore]` | Restore a dump |

## Running the tests

Unit tests need only Python 3.12 and the collector requirements:

```
python -m venv collector/.venv
collector/.venv/bin/pip install pytest -r collector/requirements.txt     # Windows: collector\.venv\Scripts\pip
make test
```

Integration tests need a real PostgreSQL 16 with the migrations applied, in a **scratch** database whose name ends in `_test` (the fixtures truncate every data table). One way:

1. `make dev` (publishes the database on `127.0.0.1:55432`).
2. Create and migrate a scratch database, for example create `vatpac_test` and apply `db/migrations/*.sql` to it with dbmate.
3. Run:

```
VATPAC_TEST_DSN="postgresql://vatpac:<password>@127.0.0.1:55432/vatpac_test" \
  collector/.venv/bin/python -m pytest tests/integration -m integration -q
```

On Windows PowerShell set `$env:VATPAC_TEST_DSN` instead. Without `VATPAC_TEST_DSN` the integration tests are skipped. The detailed notes, including the anonymised fixture, are in [tests/README.md](tests/README.md) (parts of that file predate the first integration run and still say the tests are unexecuted).

## Current verification status

Verified on 07-10-2026 (Docker Desktop on Windows):

- The stack (db, migrate, collector, grafana, backup, watchdog) builds and runs; all six migrations apply on PostgreSQL 16.4.
- Live VATSIM ingestion runs at one snapshot every 15 s, about 200 ms per poll.
- The filters were cross-checked against an independent count of the live feed: 15 pilots inside the polygon, 14 with a flight plan. No whitelisted controllers were online at the time, so the controller side was not exercised live.
- 288 tests pass (194 unit and 94 integration against a real PostgreSQL), 1 skipped; ruff and mypy are clean.
- All 61 Grafana panel queries run through Grafana's API with no errors: 45 return rows, 16 are empty because the data was only minutes old.
- Backup and restore test passes.
- A crash (out-of-memory kill) is restarted by Docker. Note `docker kill` counts as a manual stop for Docker and is not restarted.
- A feed outage leaves the collector up, records error rows and opens the circuit breaker.
- The watchdog alerted and recovered correctly. Telegram is not configured, so alerts were logged only.

**Not yet verified:**

- A 24-hour soak (SPEC acceptance 2 and 7).
- Running on the Linux VM.
- Telegram alert delivery.
- Long-window charts (weeks of history).
- The retention charts (active, lapsed, retained and reactivated controllers): UNVERIFIED pending the owner's Metabase export.

Out of the repository by design (ADR-012): the Docker socket proxy and any Cloudflare tunnel. The watchdog is alert-only by default, and a container that is running but unhealthy is not restarted automatically until that external piece exists.

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, data flow, containers, failure behaviour, scope |
| [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md) | Tables, views, functions, roles |
| [docs/DEFINITIONS.md](docs/DEFINITIONS.md) | Flight, session, online now, airborne minutes, gates, caps, near-miss, and differences from v1 |
| [docs/CONFIG_MAP.md](docs/CONFIG_MAP.md) | Every environment variable, and v1's variables marked kept, changed or dropped |
| [docs/CHART_DEFINITIONS.md](docs/CHART_DEFINITIONS.md) | Every v1 chart and what replaced it |
| [docs/DASHBOARDS_SIMPLIFIED.md](docs/DASHBOARDS_SIMPLIFIED.md) | Dropped charts and why |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Decision log (ADRs) |

Runbooks:

- [Feed outage](docs/RUNBOOKS/feed-outage.md)
- [Database full](docs/RUNBOOKS/db-full.md)
- [Restore from backup](docs/RUNBOOKS/restore.md)
- [Stuck collector](docs/RUNBOOKS/stuck-collector.md)
- [Update reference files](docs/RUNBOOKS/update-reference-files.md)
- [Deploy to the VM](docs/RUNBOOKS/deploy-to-vm.md)

## Notes

The repository is private but written to be public-safe: no secrets, no real personal data in fixtures. Names and CIDs are stored and shown, as in v1 (ADR-009); check the VATSIM data-use policy and put the dashboards behind a login before any public exposure.
