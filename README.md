# VATPAC Stats v3

[![CI](https://github.com/t288matt/vatpac-statsV2/actions/workflows/ci.yml/badge.svg)](https://github.com/t288matt/vatpac-statsV2/actions/workflows/ci.yml)

Live pilot and controller statistics for the VATPAC region, built from VATSIM's public live data and running around the clock.

## About

VATPAC Stats shows live and historical statistics for pilots and air traffic controllers in the VATPAC region on VATSIM. The numbers are updated every 30 seconds and shown as fourteen dashboards. Anyone can view them on the internet without logging in, and nobody can change them (see "Public read-only access" below).

This version (v3) replaces the original system (v1).

- A flight is counted once it has finished, about 8 hours after the pilot's last position. If a pilot disconnects and reconnects on the same flight within two hours, it counts as one flight (details in [docs/DEFINITIONS.md](docs/DEFINITIONS.md)).
- The charts started empty on 07-10-2026 and fill as data arrives. Nothing earlier was loaded, so the 30 to 90 day charts need that much history before they say anything useful.
- Pilot and controller names and VATSIM IDs are shown, as in the original system.

## Getting started

A complete step-by-step install for a new instance on a clean Linux machine is in [docs/INSTALL.md](docs/INSTALL.md). The short version:

**Requirements:** Docker Desktop (Windows or macOS), or Docker Engine with the Compose plugin (Linux). Nothing else is needed to run the stack. If it runs in a virtual machine, set the CPU type to `host` or `x86-64-v2-AES` (the collector uses numpy 2, which needs it).

```
sh ops/gen_env.sh            # writes .env with random passwords (Git Bash or Linux); or: make gen-env
sh ops/check_env.sh          # rejects blank, weak or repeated secrets (make up runs this for you)
docker compose up -d --build   # PC and first install: builds the images locally (on the production VM use pull and --no-build, below)
docker compose ps            # wait until db, collector, grafana, backup and watchdog are healthy
```

`.env.example` has blank secrets on purpose: the stack will not start until they are set. Without `sh`, copy `.env.example` to `.env` and fill every blank secret yourself (letters and digits only, at least 16 characters, a different value for each, because the passwords sit inside connection URLs). Never commit `.env`.

On every start the `migrate` container applies new SQL migrations and re-applies the database roles, then exits. `migrate` showing `exited (0)` is expected. The migrations are inside the `migrate` image: `make up` rebuilds it, but a plain `docker compose up -d` does not, so after editing a migration run `docker compose build migrate` first.

**How production is deployed.** The commands above build the images on the machine (local-build mode, the default for a PC, CI and a first install). The production VM is different: it is a read-only git checkout, GitHub Actions publishes four images (collector, watchdog, backup, migrate) to a private registry (GHCR) on request, tagged by commit (`sha-<7>`) and `latest`, and the VM runs `make deploy` (`git pull --ff-only`, `docker compose pull`, a check that the images match the checkout, then `docker compose up -d --no-build`). One `.env` line, `VATPAC_IMAGE_PREFIX`, chooses this mode ([docs/CONFIG_MAP.md](docs/CONFIG_MAP.md)); no `.env` edit is needed per deploy. On the VM never use `up -d --build` or `make up`: they rebuild locally and replace the pulled image. To go back to an earlier build, pin its `sha-<7>` tag. The procedure, the rollback and the old method kept as a fallback are in [docs/RUNBOOKS/deploy-to-vm.md](docs/RUNBOOKS/deploy-to-vm.md).

**Check that it works:**

```
docker compose logs --tail=20 collector
make smoke
```

You should see an `ok` poll about every 30 seconds in `poll_log`, plus a few `skipped_unchanged`. Routine checks are in [docs/RUNBOOKS/daily-checks.md](docs/RUNBOOKS/daily-checks.md).

## Usage

Open Grafana at http://localhost:3300 and log in with `GRAFANA_ADMIN_USER` and `GRAFANA_ADMIN_PASSWORD` from `.env`. Anonymous access is off. Grafana listens on the loopback address only; set `GRAFANA_BIND` in `.env` to reach it from the LAN (see [docs/RUNBOOKS/deploy-to-vm.md](docs/RUNBOOKS/deploy-to-vm.md)). To publish the dashboards read-only, see [docs/RUNBOOKS/public-access.md](docs/RUNBOOKS/public-access.md).

Dashboards (as code in `grafana/dashboards/`): Pilots: traffic and aircraft, Pilots: records and totals, Routes and airports, Aircraft and airlines, Pilots: onboarding and lapsed pilots, ATC: controllers, ATC: position hours, ATC: positions and controllers, ATC: hours by rank, ATC: retention and reactivation, Demographics: pilot origins and cohorts, Military: aircraft and routes, Events: Monday event, and Monitoring: collector and data freshness. Every chart is explained in [docs/CHART_DEFINITIONS.md](docs/CHART_DEFINITIONS.md).

## Public read-only access

All fourteen dashboards can be shown to anyone on the internet, read-only, without a login. Each dashboard has its own public link, with a fixed time range, and a short address such as `https://vpac.mpengor.com/traffic` that redirects to it; visitors cannot send their own queries, change the time range or edit anything. Anonymous access in Grafana stays off. The links are created by `ops/grafana_public.py`, and traffic reaches Grafana through Cloudflare, a Cloudflare Tunnel and a small forwarder on the owner's Traefik machine. Setup, checks and how to switch it off: [docs/RUNBOOKS/public-access.md](docs/RUNBOOKS/public-access.md); what runs on the Traefik machine (<TRAEFIK_HOST_IP>, outside this repository) is in its section "The Traefik machine". The public address is `https://vpac.mpengor.com` (DNS and Cloudflare are outside this repository).

The collector serves `/health` on port 8080 inside the Docker network only (not published). The database is not published either; the dev overlay (`make dev`) publishes it on `127.0.0.1:55432` only.

## For dashboard users

Fourteen dashboards, all in Grafana (folder: dashboard). Times are UTC. What each one shows is in Grafana itself (dashboard and panel descriptions); the exact rule behind every chart is in [docs/CHART_DEFINITIONS.md](docs/CHART_DEFINITIONS.md), and the meaning of "flight", "session", "online now" and "airborne minutes" is in [docs/DEFINITIONS.md](docs/DEFINITIONS.md).

Charts start empty at go-live (07-10-2026) and fill as data arrives (v1 history was loaded on 05-11-2026 under ADR-035, so history starts on 24-08-2025, with a gap from 15-03-2026 to 07-10-2026: [docs/RUNBOOKS/import-v1-history.md](docs/RUNBOOKS/import-v1-history.md).); a flight appears in the flight totals about 8 hours after its last position.

## Configuration

Secrets and per-machine values are set in `.env` (names in `.env.example`). Policy values (retention periods and alert thresholds) are fixed in `docker-compose.yml` and changed by pull request (ADR-034). Every variable, and which file holds it, is listed in [docs/CONFIG_MAP.md](docs/CONFIG_MAP.md) ("Where each setting lives"). The ones you are most likely to touch:

| Variable | Default | Meaning |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | 30 | How often the collector reads the feed. The airborne gap cap, the archive gap and the "online now" windows are all derived from it |
| `POSITION_RETENTION_DAYS` | 180, fixed in `docker-compose.yml` | How long raw positions are kept (sessions and flights are kept forever). The first possible drop is about 05-04-2027 (ADR-026, ADR-030) |
| `HOME_ICAO_PREFIXES` | `Y` | Which airports count as the home region |
| `GRAFANA_BIND`, `GRAFANA_PORT` | loopback, 3300 | Where Grafana listens |
| `BACKUP_RETENTION_DAYS` | 14, fixed in `docker-compose.yml` | How many days of backup dumps are kept |
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
| `docs/` | Documentation (index: [docs/README.md](docs/README.md)) |
| `SPEC.md` | The specification |

## Testing and development

`make` is optional. Every check is defined once in `ops/run_checks.sh`, which CI and `make` both call; on Windows without `make`, run it from Git Bash, for example `sh ops/run_checks.sh all` (or one group: `lint`, `tests`, `repo-checks`, `config`). The runbook [docs/RUNBOOKS/run-tests.md](docs/RUNBOOKS/run-tests.md) lists the checks.

| Command | What it does |
|---|---|
| `make test` | Unit tests and the skip count (no database, no network). Integration tests are skipped without `VATPAC_TEST_DSN`. Slow groups carry the markers `slow_fuzz` and `slow_shell`; skip them locally with `-m "not slow_fuzz and not slow_shell"` (CI runs them) |
| `make lint` | ruff and mypy on the collector, ruff and mypy on `ops/`, shell syntax |
| `make checks` | Every check CI runs that needs no secret or service (`sh ops/run_checks.sh all`) |
| `make drift` | Collector settings, compose and `.env.example` agree |
| `make size` | Collector size budget (package under 1800 lines, no module over 300) |
| `make smoke` | Health checks on a running stack |
| `make config` | Validate the compose files with a throwaway env |
| `make soak` | Soak report from `poll_log`: was a snapshot stored every poll interval over the last `HOURS` (default 24) |
| `make up` / `make dev` / `make down` | Start (optionally with the dev overlay) or stop the stack; volumes are kept. `make up` builds the images locally; it is not the registry deploy, and it refuses to run in registry mode (added by pull request 180; see the deploy runbook) |
| `make deploy` | Registry mode only: `git pull --ff-only`, `docker compose pull`, `ops/check_pulled_images.sh` (stops if the images were not built from the checkout), then `docker compose up -d --no-build`. It does not run `--preflight`, so run that first |
| `make logs` / `make ps` | Follow logs; container status |
| `make backup` / `make backup-test` | Take a backup now; restore the newest into a scratch database and check it |
| `make restore FILE=... CONFIRM=...` | Restore a dump ([docs/RUNBOOKS/restore.md](docs/RUNBOOKS/restore.md)) |

Unit tests need Python 3.11 or newer and the collector requirements:

```
python -m venv collector/.venv
collector/.venv/bin/pip install pytest -r collector/requirements.txt     # Windows: collector\.venv\Scripts\pip
PYTHONPATH=collector/src collector/.venv/bin/python -m pytest -q -m "not integration and not slow_fuzz and not slow_shell" tests collector/tests
```

Integration tests need a real PostgreSQL 16 with the migrations applied, in a **scratch** database whose name ends in `_test` (the tests truncate every data table). Never point them at the live database, and never run `db/roles.sql` against the live VM (it resets the live role passwords). The safe step-by-step method is in [docs/RUNBOOKS/run-tests.md](docs/RUNBOOKS/run-tests.md). GitHub Actions runs everything on every push to `main` and on pull requests; read the result there.

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
| [docs/INSTALL.md](docs/INSTALL.md) | Install a new instance from the source code |
| [docs/README.md](docs/README.md) | Index of every document, with its status |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Decision log (ADRs) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, data flow, containers, failure behaviour |
| [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md) | Tables, views, functions, roles |
| [docs/DEFINITIONS.md](docs/DEFINITIONS.md) | Flight, session, online now, airborne minutes and the other definitions |
| [docs/CHART_DEFINITIONS.md](docs/CHART_DEFINITIONS.md) | Every v1 chart and what replaced it |
