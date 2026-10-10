# VATPAC Stats v3

[![CI](https://github.com/t288matt/vatpac-statsV2/actions/workflows/ci.yml/badge.svg)](https://github.com/t288matt/vatpac-statsV2/actions/workflows/ci.yml)

Live pilot and controller statistics for the VATPAC region, built from the public VATSIM data feed, running 24/7 in Docker.

## About

One collector polls the VATSIM feed every 30 seconds and keeps the pilots inside the Australian airspace polygon and the controllers on a callsign whitelist. PostgreSQL 16 stores the data and Grafana shows it as dashboards. Backups, a watchdog that sends Telegram alerts, and a rolling archive of raw feed snapshots keep it running unattended.

v3 replaces the original v1 system (a FastAPI service with Metabase). The two were compared on overlapping hours; the results and the differences are in [docs/V1_COMPARISON.md](docs/V1_COMPARISON.md).

- Flights are built from sessions: reconnects with the same callsign and airports within 120 minutes are one flight, and flights still connected are left out of the flight counts (see [docs/DEFINITIONS.md](docs/DEFINITIONS.md)).
- Charts start empty at go-live (07-10-2026, 21:38 UTC) and fill as data arrives. There is no backfill. The 30 to 90 day charts need that much history before they say anything useful.
- Names and CIDs are stored and shown, as in v1.

## Getting started

**Requirements:** Docker Desktop (Windows or macOS), or Docker Engine with the Compose plugin (Linux). Nothing else is needed to run the stack.

```
sh ops/gen_env.sh            # writes .env with random passwords (Git Bash or Linux); or: make gen-env
sh ops/check_env.sh          # rejects blank, weak or repeated secrets (make up runs this for you)
docker compose up -d --build
docker compose ps            # wait until db, collector, grafana, backup and watchdog are healthy
```

`.env.example` has blank secrets on purpose: the stack will not start until they are set. Without `sh`, copy `.env.example` to `.env` and fill every blank secret yourself (letters and digits only, at least 16 characters, a different value for each, because the passwords sit inside connection URLs). Never commit `.env`.

On every start the `migrate` container applies new SQL migrations and re-applies the database roles, then exits. `migrate` showing `exited (0)` is expected.

**Check that it works:**

```
docker compose logs --tail=20 collector
make smoke
```

You should see an `ok` poll about every 30 seconds in `poll_log`, plus a few `skipped_unchanged`. Routine checks are in [docs/RUNBOOKS/daily-checks.md](docs/RUNBOOKS/daily-checks.md).

## Usage

Open Grafana at http://localhost:3300 and log in with `GRAFANA_ADMIN_USER` and `GRAFANA_ADMIN_PASSWORD` from `.env`. Anonymous access is off. Grafana listens on the loopback address only; set `GRAFANA_BIND` in `.env` to reach it from the LAN (see [docs/RUNBOOKS/deploy-to-vm.md](docs/RUNBOOKS/deploy-to-vm.md)). Never expose it to the internet without a login in front.

Dashboards (as code in `grafana/dashboards/`): Pilots now, Pilots traffic, Pilot onboarding, ATC overview, ATC retention, Pilot origins, Military, Monday events, Management, and Collector health. Every chart is explained in [docs/CHART_DEFINITIONS.md](docs/CHART_DEFINITIONS.md).

The collector serves `/health` and `/metrics` on port 8080 inside the Docker network only (not published). The database is not published either; the dev overlay (`make dev`) publishes it on `127.0.0.1:55432` only.

## Configuration

Everything is set through `.env` (names and defaults in `.env.example`; every variable is listed in [docs/CONFIG_MAP.md](docs/CONFIG_MAP.md)). The ones you are most likely to touch:

| Variable | Default | Meaning |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | 30 | How often the collector reads the feed. The airborne gap cap, the archive gap and the "online now" windows are all derived from it |
| `POSITION_RETENTION_DAYS` | 90 | How long raw positions are kept (sessions and flights are kept forever) |
| `HOME_ICAO_PREFIXES` | `Y` | Which airports count as the home region |
| `GRAFANA_BIND`, `GRAFANA_PORT` | loopback, 3300 | Where Grafana listens |
| `BACKUP_RETENTION_DAYS` | 14 | How many backup dumps are kept |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | blank | Where the watchdog sends alerts |

### Configuration files and why they are needed

The collector is configured from three places. Each exists for a reason.

**1. `.env` (settings and secrets).** One file holds the passwords for the database roles, Grafana's admin login, the Telegram token and every tunable setting. It is not in git, and `.env.example` shows the names with blank secrets so the stack refuses to start until you fill them. It exists so the same code runs on a laptop and on the VM without editing any file in the repository, and so secrets never reach git.

**2. Two reference files in `config/` (what to keep from the feed).** The VATSIM feed covers the whole world, about 1,500 pilots and 130 controllers at any moment. v3 only wants the Australian region, so two files tell the collector what to keep. Both are mounted read-only into the collector.

| File | What it is | Why it is required |
|---|---|---|
| `config/australian_airspace_polygon.json` | A GeoJSON polygon (longitude, latitude order) around the VATPAC airspace | A pilot is stored from the first poll in which they are inside it, and not stored outside. Without it the database would fill with every pilot in the world and every "pilots online" number would be wrong. A point exactly on the edge counts as outside |
| `config/controller_callsigns_list.txt` | The controller callsigns that are real positions, one per line as `CALLSIGN, FREQ` (254 today). The match is exact and case-sensitive | A controller is stored only if their callsign is listed. This keeps observers (`_OBS`), ATIS and radio-silent entries out of the ATC numbers. A callsign that looks like a listed one (same prefix, not on the list) is not stored, but it is logged and raises one Telegram alert, so a new position that opens at a known airport is noticed |

Both files are loaded when the collector starts and reloaded without a restart when you send it a reload signal (`docker compose kill -s HUP collector`). A broken new file, or a whitelist that shrinks to under 70% of the old one, is rejected and the old one stays in use. A missing or empty whitelist stops the collector from starting: it fails closed rather than storing every controller. A polygon change affects new snapshots only; stored history is not re-filtered. The steps are in [docs/RUNBOOKS/update-reference-files.md](docs/RUNBOOKS/update-reference-files.md). `config/australian_airspace_sectors.geojson` is kept in the folder but is not read by v3, because sector matching was dropped.

**3. The `app_setting` table (settings the database views need).** The collector publishes a few of its settings (the poll interval, the airborne gap cap and the home airport prefixes) into this table when it starts, so the SQL views and dashboards use exactly the same values as the collector and no number is written in two places.

## Project structure

| Path | Contents |
|---|---|
| `collector/src/vatpac/` | The collector (Python) |
| `db/migrations/`, `db/roles.sql` | SQL migrations and database roles |
| `grafana/dashboards/`, `grafana/provisioning/` | Dashboards and data sources as code |
| `config/` | Polygon and controller whitelist |
| `ops/` | Backups, watchdog, smoke test, soak report, audit and heartbeat scripts |
| `tests/`, `collector/tests/` | Unit and integration tests |
| `docs/` | Documentation (start with [docs/RESUME.md](docs/RESUME.md)) |
| `SPEC.md` | The specification |

## Testing and development

On Windows without `make`, run the underlying commands.

| Command | What it does |
|---|---|
| `make test` | Unit tests (no database, no network). Integration tests are skipped without `VATPAC_TEST_DSN` |
| `make lint` | ruff and mypy on the collector, ruff on `ops/` |
| `make drift` | Collector settings, compose and `.env.example` agree |
| (team and way of working) | see `docs/WORKING_AGREEMENT.md` and `docs/TEAM_BRIEFS.md` |
| `make size` | Collector size budget (package under 1800 lines, no module over 300) |
| `make smoke` | Health checks on a running stack |
| `make config` | Validate the compose files with a throwaway env |
| `make soak` | Soak report from `poll_log`: was a snapshot stored every poll interval over the last `HOURS` (default 24) |
| `make up` / `make dev` / `make down` | Start (optionally with the dev overlay) or stop the stack; volumes are kept |
| `make logs` / `make ps` | Follow logs; container status |
| `make backup` / `make backup-test` | Take a backup now; restore the newest into a scratch database and check it |
| `make restore FILE=... CONFIRM=...` | Restore a dump ([docs/RUNBOOKS/restore.md](docs/RUNBOOKS/restore.md)) |

Unit tests need Python 3.11 or newer and the collector requirements:

```
python -m venv collector/.venv
collector/.venv/bin/pip install pytest -r collector/requirements.txt     # Windows: collector\.venv\Scripts\pip
PYTHONPATH=collector/src collector/.venv/bin/python -m pytest -q -m "not integration" tests collector/tests
```

Integration tests need a real PostgreSQL 16 with the migrations applied, in a **scratch** database whose name ends in `_test` (the tests truncate every data table). Never point them at the live database, and never run `db/roles.sql` against the live VM (it resets the live role passwords). The safe step-by-step method is in [docs/RUNBOOKS/run-tests.md](docs/RUNBOOKS/run-tests.md). GitHub Actions runs everything on every push to `main`; read the result there.

## Operations

| Runbook | When |
|---|---|
| [Deploy to the VM](docs/RUNBOOKS/deploy-to-vm.md) | First install, updating, rollback |
| [Daily and weekly checks](docs/RUNBOOKS/daily-checks.md) | Routine checks and the soak report |
| [Alerts](docs/RUNBOOKS/alerts.md) | What each Telegram message means |
| [Outside heartbeat](docs/RUNBOOKS/outside-heartbeat.md) | The check that runs outside the VM |
| [Feed outage](docs/RUNBOOKS/feed-outage.md), [Stuck collector](docs/RUNBOOKS/stuck-collector.md), [Database full](docs/RUNBOOKS/db-full.md) | Something is wrong |
| [Restore from backup](docs/RUNBOOKS/restore.md) | Data loss |
| [Update reference files](docs/RUNBOOKS/update-reference-files.md), [Add or change a dashboard](docs/RUNBOOKS/dashboards.md) | Routine changes |

## Documentation

| Document | Contents |
|---|---|
| [docs/RESUME.md](docs/RESUME.md) | Entry point for a new operator or session: current state, rules, next actions |
| [docs/TRACKER.md](docs/TRACKER.md) | The live task list; the single source of truth for status |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Decision log (ADRs) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, data flow, containers, failure behaviour |
| [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md) | Tables, views, functions, roles |
| [docs/DEFINITIONS.md](docs/DEFINITIONS.md) | Flight, session, online now, airborne minutes and the other definitions |
| [docs/CHART_DEFINITIONS.md](docs/CHART_DEFINITIONS.md) | Every v1 chart and what replaced it |
| [docs/V1_COMPARISON.md](docs/V1_COMPARISON.md) | v3 against v1 on overlapping hours |
| [docs/HANDOVER.md](docs/HANDOVER.md) | Point-in-time hand-over of 07-10-2026 (history) |
| [docs/OWNER_ACTIONS.md](docs/OWNER_ACTIONS.md) | Steps only the owner can do |

## Contributing

The repository is private and run by one owner with Claude. Pushes go to `main` and GitHub Actions reads the result afterwards; the tracker ([docs/TRACKER.md](docs/TRACKER.md)) records every change. Work in a git worktree under `.worktrees/` so other sessions do not change your branch. Add a new numbered SQL migration instead of editing an applied one, keep the collector within the size budget (`make size`), and add a test with every fix. Deploy only on the owner's say-so.

## Licence

MIT, see [LICENSE](LICENSE). The repository is written to be public-safe (no secrets, no real personal data in fixtures). Before any public exposure, check the VATSIM data-use policy and put the dashboards behind a login, because names and CIDs are shown.

## Credits

Built by the owner with Claude. Data from the public VATSIM data feed.
