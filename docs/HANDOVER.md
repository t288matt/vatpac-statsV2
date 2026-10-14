# HANDOVER: VATPAC Stats v3 (point-in-time, 07-10-2026)

> **Note, issue 89 phase 5 (LIVE since 13-10-2026, marker 7a71d30 at 10:05:56 UTC).** This is a dated record. It uses the names as they were when it was written: `flight_summary` meant the old pieces table, `flight_record` the table of finished flights. Migration 20261013000300 (live) dropped the pieces table, `flight_part`, `flight_legacy`, `refresh_flight_summary` and `refresh_flight_summary_sessions`, and renamed `flight_record` to `flight_summary`. Current description: [DATA_DICTIONARY.md](DATA_DICTIONARY.md) and ADR-030.

> **Point-in-time document.** Written 07-10-2026 at the end of the first build session and kept as history; it is not kept up to date. For the current state read [RESUME.md](RESUME.md) (the entry point for a cold start) and [TRACKER.md](TRACKER.md) (the live task list). Statements below that would now mislead carry an "Update 08-10-2026" note; everything else is as written on 07-10-2026.

Written 07-10-2026 at the end of the first build session, for an agent with NO memory of that session.
Style rules from the owner (Matt): British English, dates DD-MM-YYYY, no emoji, warm but direct, thorough explanations, say what you actually think, never claim something is done unless you ran the check in this session.

Contents
1. The goal and the success test
2. State at hand-over (what is running where)
3. Immediate next actions (start here)
4. Outstanding work, blocked items and open decisions
5. Access, environments and secrets
6. How to verify everything (commands)
7. How to deploy and operate
8. What v3 is (architecture, data model, behaviour)
9. Decisions (the owner's 17 plus ADRs)
10. Charts: what is kept, changed, dropped, unverified
11. What v1 is and what we learned from it
12. Evidence log (numbers measured this session)
13. Bugs found by running things, and review findings with status
14. Known limitations and risks
15. Gotchas and tooling quirks (they cost hours)
16. Repo map and where other material lives
17. Working with agents (how this session was run)
18. Owner profile and rules
19. Things you must NOT do

---

## 1. The goal and the success test
The owner's words, set as a goal (a stop hook keeps firing because it is not yet met):
> "I want to be able to reproduce these graphs and have them live and available 24/7. I want the system to be dockerised."

"These graphs" = the owner's Metabase charts of VATSIM pilots and controllers in the VATPAC region (Australia and the Pacific). v3 is a clean rebuild: Postgres 16 + a small Python collector + Grafana provisioned from files in git, all in Docker Compose.

**Success is only true when ALL of these are demonstrated, with evidence from this repo's tools:**
1. Reproducible from git: a clean clone plus a generated `.env` plus `docker compose up -d --build` brings everything up healthy. (Demonstrated on this PC and on the VM.)
2. Dashboards reproduce the in-scope charts, provisioned from files. (61 panel queries, 0 errors, demonstrated; retention charts UNVERIFIED.) *Update 08-10-2026: the retention charts were rebuilt from the owner's Metabase SQL export (T13); there are now 68 query panels.*
3. Live: a snapshot is stored every 15 s. (Demonstrated.) *Update 09-10-2026: the interval is now 30 s, held only in `POLL_INTERVAL_SECONDS` (ADR-024).*
4. 24/7: runs on an ALWAYS-ON host (the VM, not the owner's PC), `sh ops/soak_report.sh 24` prints `verdict | PASS` for a full 24 h, a reboot test passes, and alerts reach the owner. **Not yet demonstrated.** *Update 08-10-2026: the reboot test passed (07-10-2026 21:48 UTC) and a Telegram test alert was delivered; the 24 h soak is still outstanding (see TRACKER T1).*
5. CI green on GitHub. **Never run.** *Update 08-10-2026: CI is green (TRACKER T3, T10).*

Do not tell the owner the goal is met before 4 and 5 are done. The owner has been told repeatedly, accurately, that it is not met.

## 2. State at hand-over (07-10-2026, about 21:45 UTC)

| Item | State |
|---|---|
| Git repo | `t288matt/vatpac-statsV2` (PRIVATE). Branch `main`, last pushed commit `d5de461` ("Pin numpy 1.26.4 ..."). Local clone `C:\Users\mdora\Claude\vatpac-statsV2` (clean tree at that commit plus this document). SSH remote `git@github.com:t288matt/vatpac-statsV2.git`; the owner's PC has a working SSH key and a git identity |
| Local stack (owner's Windows PC, Docker Desktop) | **STOPPED on 07-10-2026 at the owner's request** (`docker compose stop`, containers kept, data kept in the volumes `vatpac_pgdata`, `vatpac_grafana_data`, `vatpac_backups`, `vatpac_watchdog_status`). It had run from 19:32 to about 21:50 UTC with 2.16 h covered. Start it again for development with `docker compose up -d` in the local clone; its `.env` is local and git-ignored. Only the VM runs 24/7 |
| The VM (the real 24/7 target) | Proxmox VM "100", a clone of the owner's v1 box, wiped of v1 on 07-10-2026 (section 7.4). SSH alias on the owner's PC: `ssh vatpac-v3` (user `matt`, key `~/.ssh/vatpac_ubuntu`, in the `docker` group, **`sudo` needs a password you do not have**). Ubuntu 24.04, 4 vCPU, 7.8 GB RAM, about 197 GB disk, Docker 29.6.1, Compose 5.3.1. v3 deployed to `~/vatpac-statsV2` from commit `d5de461`, own `.env` generated on the VM, Grafana bound to the VM's LAN address on port 3300. At 21:38 UTC all 5 long-running containers healthy, first ok poll recorded (21 pilots in region, 0 controllers). **The 24 h soak clock on the VM started at about 21:38 UTC on 07-10-2026.** Nothing else about the VM has been verified yet: run section 6 |
| Tests | 326 passed, 1 skipped (a role-dependent skip in `tests/integration/test_ingest.py`); 205 unit tests re-run green after the numpy pin; ruff and mypy --strict clean; `ops/check_size_budget.py` 1325 of 1400 lines |
| Dashboards | 8 dashboards, 61 panel queries through Grafana's API: 0 errors (47 returned rows on the rebuilt local stack, the rest empty because data is young) |
| v1 (the old system) | Owner's, on the original VM. NOT TOUCHED. Read-only reference. Its Postgres is reachable on the LAN and the owner allowed read-only queries only |
| Not done | VM soak, VM reboot test, GitHub CI result, Telegram delivery, Metabase export, off-box backup copy, items in section 4. *Update 08-10-2026: the reboot test, CI, Telegram delivery and the Metabase export are done; the soak and the off-box copy are still open.* |

## 3. Immediate next actions (start here, in this order)
*Update 08-10-2026: this list is superseded. Items 1, 2, 3, 5 and 6 are done; the current next actions are in [RESUME.md](RESUME.md) section 1 and [TRACKER.md](TRACKER.md).*

1. `ssh vatpac-v3 'cd ~/vatpac-statsV2 && docker compose ps'`: expect backup, collector, db, grafana, watchdog all `(healthy)` and migrate `exited (0)`. The collector crash-looped once on the VM (CPU lacked x86-64-v2, section 13) and was fixed in `d5de461`; confirm it stayed up (`RestartCount` in `docker inspect`).
2. `ssh vatpac-v3 'cd ~/vatpac-statsV2 && sh ops/smoke.sh'` expecting `SMOKE PASS`; then `python3 ops/check_panels.py` expecting `0 errors`.
3. Confirm Grafana is reachable from the owner's PC at `http://<VM LAN IP>:3300` (the IP is in `~/.ssh/config` under `Host vatpac-v3`). Login: user `admin`, password = `GRAFANA_ADMIN_PASSWORD` in the VM's `.env` (read it on the VM with `grep GRAFANA_ADMIN_PASSWORD ~/vatpac-statsV2/.env`; never paste secrets into chat or files).
4. Record the soak start time (poll_log's first ok row), then after 24 h run `sh ops/soak_report.sh 24`. Record the verdict and numbers in `docs/ARCHITECTURE.md` (verified table) and commit.
5. Ask the owner for: (a) what the GitHub Actions tab shows; (b) Telegram bot token and chat id to put in the VM `.env`; (c) the Metabase export for the retention charts; (d) decisions in section 4.3; (e) permission/ability for a reboot test (needs `sudo`, which only the owner has).
6. Fix CI failures as they are reported (section 4.1).

## 4. Outstanding work, blocked items and open decisions

### 4.1 Required for the goal
| # | Item | Blocked on | Notes |
|---|---|---|---|
| 1 | 24 h soak on the VM | time | Use `sh ops/soak_report.sh 24`. PASS needs: full window covered (98% of requested), at least 99% of expected snapshots, p95 cadence at most 17 s. A short run says INCOMPLETE, never PASS. Restarts/rebuilds during the window create gaps: do not redeploy during the soak unless necessary (a redeploy costs about 45 s of snapshots) |
| 2 | VM reboot test | owner (`sudo reboot`) | *Update 08-10-2026: DONE 07-10-2026 21:48 UTC (TRACKER T2).* After reboot everything must return by itself (`restart: unless-stopped`); then re-check smoke |
| 3 | GitHub Actions CI | owner reports result | *Update 08-10-2026: DONE, green (TRACKER T3, T10).* Never run when written. Jobs: lint, unit-tests, integration-tests (Postgres service), config-drift, dashboards, compose-config, size-budget, fixture-pii (removed 08-10-2026, TRACKER T29), secret-scan (gitleaks), image-build. Likely first-run problems: gitleaks needs a licence for orgs (personal repos fine), the integration job applies migrations with plain psql (the `-- migrate:down` half stripped), actions pinned by tag |
| 4 | Telegram alerts | owner: bot token + chat id into the VM `.env` | *Update 08-10-2026: DONE, test alert delivered (TRACKER T4).* Currently the watchdog only LOGS alerts. After adding, test by stopping the collector for a few minutes and confirm a message arrives and a RECOVERED follows. Remember the owner's rule: plain text, no emoji (emoji caused UTF-8 errors) |
| 5 | Retention charts | owner: Metabase export | *Update 08-10-2026: DONE, rebuilt from the v1 SQL (TRACKER T13); the screenshot-based guesses below are superseded.* Active controllers (178 in screenshot), Lapsed 30 days (28), Controller Retention Rate gauge (84%), Reactivated in last 30 days after 60+ days lapsed (74), "Who reactivated after being away >60 days?", "Who might need help reactivating?". Layout and columns are known from screenshots; exact definitions are not (178/(178+28) is 86.4%, not 84%). Port them exactly into a migration (views) and `grafana/dashboards/atc/atc-retention.json`, update `docs/CHART_DEFINITIONS.md`, then remove the UNVERIFIED marks. Note: the 60-day lapse can't be observed until 60 days after go-live (no backfill) |

### 4.2 Hardening still open (from the code review, section 13)
- Watchdog does not yet alert on `/health.reload_rejected > 0` (the collector exposes `reload_rejected` and `last_reload_error`; the watchdog check was not written).
- Off-box backups: dumps live on the same disk as the database. `BACKUP_REMOTE` hook exists (a shell command run after each dump, with `BACKUP_FILE` set; needs `BACKUP_EXTRA_APT_PACKAGES` e.g. `rclone` and a rebuild) but nothing is configured. A VM or disk loss destroys data and backups together.
- Third-party GitHub Actions are pinned by tag with `# TODO pin to SHA`; Docker images are pinned by exact tag, not digest. *Update 08-10-2026: the actions are now pinned to commit SHAs (TRACKER T10); images are still pinned by tag.*
- Grafana container does not use `read_only: true` (not validated).
- The backup container holds the DB owner password (needed by restore scripts). A dedicated `CREATEDB` restore role would be tighter (ADR-017).
- `poll_log` gets a row for every poll attempt including skipped ones: check the real daily row count after a few days (retention is 400 days).
- The `flight` view's 8 h span split is an approximation (fixed buckets), documented in the view.
- The two "online now" definitions differ slightly (core view: within 5 min of now and 2 x the poll interval of the newest snapshot, 60 s at 30 s polling, ADR-024; dashboard views: last two snapshots and a 5-minute wall-clock guard).
- A deploy key for the VM so it can `git pull` (today code is pushed with `git archive | ssh`). Planned 14-10-2026 in issue 173 (draft runbook section 2d).

### 4.3 Open owner decisions
1. ~~Retention or anonymisation of names and CIDs~~ DECIDED 07-10-2026: keep forever, no rule (ADR-019).
2. Whether to add a dedicated restore-only DB role (ADR-017).
3. (Decided) Movements stay the same as v1 (ADR-018): every flight-gate session counts, including parked connections (12% of v1's home-region departures had zero airborne time).

### 4.4 Owner-managed, deliberately NOT in this repo
- Cloudflare tunnel / any public exposure. Names are visible on dashboards, so put a login (for example Cloudflare Access) in front, and the owner must check VATSIM's data-use policy first (ADR-009).
- Docker socket proxy / anything that lets the watchdog restart unhealthy containers (ADR-012). Today: Docker restarts crashed containers; a container that is running but unhealthy is NOT restarted. The watchdog (alert-only) runs without Docker access; if `DOCKER_HOST_URL` is set it would check containers and restart (capped 3 per hour) but that endpoint does not exist.
- v1 on the original VM.

## 5. Access, environments and secrets

### 5.1 Machines
- **Owner's PC**: Windows 11, Git Bash is the shell used here (path style `/c/Users/mdora/...`), Python 3.11 at system level, Docker Desktop 28.1.1 (engine works; WSL2 reported "not supported" at one point but the containerised engine ran). `make` is NOT installed (run the commands the Makefile wraps). `gh` CLI is NOT installed. *Update 08-10-2026: Docker Desktop's engine now returns HTTP 500, so local integration tests cannot run; use the VM method in RESUME.md section 5 (TRACKER T25).* Working directories: v3 `C:\Users\mdora\Claude\vatpac-statsV2`; v1 working copy `C:\Users\mdora\Claude\VATPAC-Stats V2` (this is where the Claude session's cwd was; contains untracked `FUNCTIONALITY_INVENTORY.md` and `docs/metabase_charts/`).
- **The VM**: see section 2. SSH: `ssh vatpac-v3` (alias added to the owner's `~/.ssh/config` this session). Its SSH host key is IDENTICAL to the original v1 box's (it is a clone): the owner's `known_hosts` had stale entries for that IP from an earlier machine, which were replaced after verifying by fingerprint that the key matched the original box.
- **v1 box**: original VM (alias `vatpac` in `~/.ssh/config`). Do not modify. Its Postgres accepts read-only queries (the owner approved that; credentials are in v1's committed `docker-compose.yml`; not repeated here). The Metabase tables inside it are a snapshot from about 17-28 September 2025: later charts exist only in the owner's live Metabase.
- Other LAN items the owner has: an Uptime Kuma instance (v1's monitor container pushed to a push monitor there; v3 has an optional `UPTIME_KUMA_PUSH_URL` that is blank), Proxmox, an Unraid box, PBS. Not used by v3.

### 5.2 Secrets (never print them, never commit them)
- Repo has NO secrets. `.env` is git-ignored. `.env.example` has blank secret values on purpose; `sh ops/gen_env.sh [OUT]` writes a `.env` with random hex passwords (prints variable names only); `sh ops/check_env.sh [FILE]` rejects blank, `change-me*`, under 16 characters, non letter/digit values (passwords go inside URLs) and repeated values.
- Secrets: `POSTGRES_PASSWORD` (owner role; db, migrate, restore only), `GRAFANA_RO_PASSWORD`, `COLLECTOR_DB_PASSWORD`, `BACKUP_DB_PASSWORD`, `WATCHDOG_DB_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`, optional `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Changing a role password later: edit `.env`, `docker compose up -d` (the migrate job runs `ALTER ROLE`); `POSTGRES_PASSWORD` itself and `GRAFANA_ADMIN_PASSWORD` only apply at first initialisation (rotate with `ALTER ROLE` / `grafana cli admin reset-admin-password`).
- The local PC `.env` and the VM `.env` are different files with different passwords.
- GitHub: the owner's PC authenticates with an SSH key. The VM has NO GitHub credentials.

## 6. How to verify everything (commands)
Run from the repo root locally, or on the VM inside `~/vatpac-statsV2`.
```bash
docker compose ps                         # backup, collector, db, grafana, watchdog (healthy); migrate exited (0)
sh ops/smoke.sh                           # per-check PASS/FAIL; expects SMOKE PASS (includes: grafana_ro cannot write)
python ops/check_panels.py                # (python3 on the VM) every panel query via Grafana's API; expects "0 errors"; empty panels are fine while data is young
sh ops/soak_report.sh 24                  # PASS needs a full 24 h window, >= 99% of snapshots and p95 cadence <= 17 s; also prints container restart counts
docker compose logs --tail 50 collector   # JSON log lines; a poll about every 30 s (15 s before 09-10-2026) is silent unless something is wrong
```
Direct SQL (note the quoting: do NOT use `$$` inside `sh -c`, the shell expands it):
```bash
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "SELECT status,count(*),max(polled_at) FROM poll_log GROUP BY 1"'
```
Collector health from inside its container (no host port is published): `docker compose exec -T collector python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())"`.

Full automated test run on a scratch Postgres (integration tests TRUNCATE tables; the database name MUST end in `_test`):
```bash
docker run -d --name vp-test -p 127.0.0.1:55436:5432 -e POSTGRES_PASSWORD=x postgres:16.4-bookworm
docker exec vp-test psql -U postgres -c "CREATE DATABASE vatpac_test"
for f in db/migrations/*.sql; do sed '/^-- migrate:down/,$d' "$f" | docker exec -i vp-test psql -U postgres -d vatpac_test -v ON_ERROR_STOP=1 -q; done
docker exec -i vp-test psql -U postgres -d vatpac_test -v ON_ERROR_STOP=1 -v grafana_ro_password=testpw -v collector_password=cpw -v backup_password=bpw -q < db/roles.sql
export VATPAC_TEST_DSN=postgresql://postgres:x@127.0.0.1:55436/vatpac_test VATPAC_TEST_GRAFANA_RO_DSN=postgresql://grafana_ro:testpw@127.0.0.1:55436/vatpac_test
collector/.venv/Scripts/python.exe -m pytest tests collector/tests -q      # expect 326 passed, 1 skipped (07-10-2026; now 371 tests collected: 205 unit, 166 integration)
cd collector && ./.venv/Scripts/python.exe -m ruff check src && ./.venv/Scripts/python.exe -m mypy src && cd ..
python ops/check_size_budget.py collector/src/vatpac; python ops/check_config_drift.py; python grafana/validate_dashboards.py
docker rm -f vp-test
```
`collector/.venv` is git-ignored: recreate with Python 3.11+ and `pip install -r collector/requirements.txt pytest pytest-asyncio ruff mypy`. NOTE: the collector pins numpy 2.3.5 (since 14-10-2026); it needs the VM CPU type `host` or `x86-64-v2-AES`.

## 7. How to deploy and operate

### 7.1 First deploy on a fresh host
```bash
git clone git@github.com:t288matt/vatpac-statsV2.git && cd vatpac-statsV2   # or copy the tree (7.2)
sh ops/gen_env.sh .env && chmod 600 .env
# LAN access: set GRAFANA_BIND to the host LAN IP (default 127.0.0.1 = this host only) and GRAFANA_ROOT_URL
sh ops/check_env.sh .env && docker compose up -d --build
```
Order: db healthy, then `migrate` (dbmate migrations, then `db/roles.sql` with the three password variables, then creates `watchdog_ro`), then collector, grafana, backup, watchdog. Detailed runbooks: `docs/RUNBOOKS/` (deploy-to-vm, restore, feed-outage, stuck-collector, update-reference-files, db-full).

### 7.2 Deploying a new commit to the VM (the VM has no git credentials)
```bash
git archive --format=tar HEAD | ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'     # the VM's .env is not in git, so it is preserved
ssh vatpac-v3 'cd ~/vatpac-statsV2 && sh ops/check_env.sh && docker compose up -d --build'
```
`tar -x` overwrites but does not delete files removed from git. Migrations are forward-only: never edit an applied migration; add a new numbered file (latest on 07-10-2026: `20261007000024`; on 08-10-2026: `20261008000031`).

### 7.3 Routine operations
- Backups: nightly 02:00 UTC `pg_dump -Fc` as `backup_ro` into the `backups` volume, 14 days kept, weekly restore test (`/backups/.last_restore_test`, checked by the watchdog). Manual: `docker compose exec backup backup.sh once`. Restore (guarded): `docker compose exec -e RESTORE_CONFIRM=<db> backup restore.sh <file> <db>`, it restores into `<db>_restoring`, sanity-checks, swaps by renaming, keeps `<db>_old`. Read `docs/RUNBOOKS/restore.md` first.
- Reference files (`config/*.json|txt`): change in git, deploy, then `docker compose kill -s HUP collector` (reload; an invalid file is rejected and the old one stays, `/health` shows `reload_rejected`). Each poll records the file SHA256 in `poll_log`. SHAs on different machines differ if line endings differ (CRLF on the Windows working copy vs LF in git): the VM (LF) and the PC (CRLF) show different hashes for the same content.
- Update a new Australian controller position: add it to `config/controller_callsigns_list.txt`; unlisted look-alike callsigns are recorded in `controller_unlisted_sighting` (and the watchdog alerts once).

### 7.4 What was done to the VM on 07-10-2026 (so you do not undo it or re-meet its traps)
The owner asked: "VM 100 (LAN address omitted here) has been created as a Proxmox clone of 111. Wipe it clean and use it." It presented the original box's SSH host key (verified by fingerprint). On boot the clone had started v1's stack. Removed: all 4 containers (v1 app polling VATSIM, v1 Postgres, `vatsim_monitor` which pushed "OK" to the owner's Uptime Kuma push monitor and could have MASKED a real outage on the original box, and a Tailscale container with a cloned node identity), all images, both named volumes, the home folders `vatpac-stats`, `vatpac-stats-production`, `metabase`, `data`, `tailscale` and `docker-compose.yml`, and the user crontab (v1 DB backups with remote transfer). Left alone: the OS, user `matt`, SSH keys, `get-docker.sh`, `cmoplpete-cleanup.sh`. If the VM is ever re-cloned, re-check cron, Tailscale and monitor pushes first.

## 8. What v3 is

### 8.1 Components (docker-compose.yml, project name `vatpac`)
| Service | Image / build | Role |
|---|---|---|
| `db` | `postgres:16.15-bookworm` (16.4 until 09-10-2026), named volume `pgdata`, `--data-checksums`, `ops/postgres/postgresql.conf` mounted, port NOT published | the database (small box tuning) |
| `migrate` | built from `ops/migrate` (dbmate 2.36.0 + psql; 2.21.0 until 09-10-2026), `restart: "no"` | applies `db/migrations/*.sql`, then `db/roles.sql`, creates `watchdog_ro` |
| `collector` | built from `collector/Dockerfile` (python:3.12-slim, non-root, read-only root, no capabilities) | polls and stores; `/health` and `/metrics` on port 8080 inside the container |
| `grafana` | `grafana/grafana:12.4.12` since 11-10-2026 (ADR-029; this row said `grafana/grafana-oss:11.6.5` from 09-10-2026 and 11.2.2 before), provisioning mounted read-only from `grafana/`, login required, port `${GRAFANA_BIND:-127.0.0.1}:${GRAFANA_PORT:-3300}` | dashboards |
| `backup` | built from `ops/backup` (postgres client) | nightly dump, rotation, restore scripts, weekly restore test, optional `BACKUP_REMOTE` hook |
| `watchdog` | built from `ops/watchdog` (python, psycopg, httpx) | health and DB checks, Telegram alerts, writes `/var/watchdog/watchdog_status.json`; alert-only |
All have `restart: unless-stopped` (except migrate), json-file log rotation (10 MB x 5), memory limits, one internal network `vatpac`. No socket proxy and no Cloudflare service by owner decision.

### 8.2 Collector behaviour (`collector/src/vatpac/`, 10 modules, 1325 lines, budget 1400, no module over 300; 1335 lines on 08-10-2026; on the release branch 14 modules, 1652 lines, budget 1700 per ADR-022)
- `feed.py`: fetch `https://data.vatsim.net/v3/vatsim-data.json` (URL discovered via `https://status.vatsim.net/status.json`, key `data.v3[0]`, fallback to the constant), conditional GET, gzip, 10 s timeout, 3 jittered retries, honours 429 `Retry-After`.
- `main.py`: monotonic scheduler (15 s when written; now `POLL_INTERVAL_SECONDS`, default 30, ADR-024) (aligned to `general.update_timestamp`, skips unchanged snapshots and retries about 2 s later, skips missed ticks instead of bursting); circuit breaker after 5 consecutive failures (poll continues every 60 s, never exits); `apply` wrapped in a 25 s timeout; SIGHUP reloads reference files with validation (rejects a whitelist under 70% of the old size or an invalid polygon; old data stays); daily maintenance loop; a 5-minute `flight_summary` refresh loop.
- `filters.py`: pilot polygon (`config/australian_airspace_polygon.json`, shapely `contains`, edge excluded; pilots with no position pass), controller whitelist (`config/controller_callsigns_list.txt`, exact case-sensitive callsign; **fail closed**: a missing/empty/unreadable file stops startup), near-miss detection (a non-whitelisted controller whose prefix before the first `_` matches a whitelisted prefix, excluding `_OBS`).
- `model.py`: parsing, string sanitising (lone surrogates, control characters), plausibility caps (gs 0..700 kt, altitude -1500..65000 ft; out-of-range becomes NULL and counts in `poll_log.rejected_values`), frequency to integer Hz (199.998 means "no frequency" and becomes NULL), `home_icao` from the trailing 4-letter token of the name.
- `store.py`: one transaction per snapshot; upserts `pilot_session` on `(cid, callsign, logon_time)` (plan fields updated to latest non-null; unchanged rows are not rewritten), inserts `pilot_position` with `ON CONFLICT DO NOTHING`, controller sessions plus rating/frequency change events, `network_snapshot`; bad records are bisected out inside savepoints and counted (a poisoned record never blocks a snapshot).
- `maintenance.py`: create partitions (yesterday to +7 days), drop old ones (as written 10-10-2026: retention 90 days, finalising flights first; since 13-10-2026, issue 89 phase 4, marker 7a71d30, the drop gate instead drops a day only when every session in it is processed by the flight writer and every position of a session with a flight plan lies inside a written flight; it refuses on any doubt with a `DROP REFUSED` log line and a row in `drop_gate_refusal`, drops one day per call, and the collector loops with a 0.5 second pause; retention on the VM is 180 days), trim `poll_log` (400 days), `refresh_flights`.
- `health.py`: `/health` is 200 only if the last real poll succeeded within 60 s AND the newest snapshot is under 120 s old; JSON has `last_ok`, `last_snapshot_ts`, `consecutive_failures`, `breaker_open`, `reload_rejected`, `last_reload_error`, reference-file SHAs. `/metrics` is Prometheus text.
- `settings.py`: pydantic-settings; env names are in `.env.example` and `docs/CONFIG_MAP.md`; `ops/check_config_drift.py` fails CI if settings, compose and `.env.example` disagree.

### 8.3 Data model (full detail: `docs/DATA_DICTIONARY.md`, DDL: `db/migrations/`)
Core tables: `pilot_session(id, cid, callsign, logon_time, UNIQUE(cid,callsign,logon_time), name, home_icao, server, first_seen, last_seen, departure, arrival, aircraft_icao, aircraft_faa, flight_rules, route, remarks, planned_alt, deptime, plan_changed_at, max_gs_kt, max_alt_ft, pilot_rating, military_rating)`; `pilot_position(observed_at, session_id FK, lat, lon, alt_ft, gs_kt, hdg, PK(session_id,observed_at))` partitioned by day `pilot_position_YYYYMMDD`; `controller_session`, `controller_rating_event`, `controller_frequency_event`; `network_snapshot(observed_at PK, pilots_in_region, controllers_in_region, pilots_global, controllers_global)`; `poll_log` (one row per attempt: status ok / skipped_unchanged / error, snapshot_ts, duration_ms, counts, filter counts, rejected_values, reference SHAs, error text); `controller_unlisted_sighting`, `reference_data_change`, `app_setting` (holds `home_icao_prefixes`, default `Y`); `flight_summary(session_id, gap_segment, span_part, cid, start_time, end_time, sample_count, airborne_minutes, max_gs_kt, max_alt_ft, final, computed_at)`.
Lookups (seeded by migrations): `controller_rating`, `facility`, `pilot_rating`, `airline` (62 prefixes), `aircraft_type` (534 rows with category and `is_military`), `region_map`.
Views Grafana reads (*update 08-10-2026: plus `controller_summaries`, migrations 30 and 31*): `flight` (thin view over `flight_summary` joined to `pilot_session`, applies the flight gate), `flight_ext`, `movements`, `cohorts`, `monday_attendance`, `controller_session_merged`, `controller_session_ranked`, `controller_activity_daily`, `controller_activity_summary`, `online_now`, `pilot_latest`, `pilot_online_now`, `controller_online_now`, `network_history`, `data_since`, `data_freshness`, `poll_history`, `near_miss_controllers`, `monitor_freshness`, `home_prefix` and helper functions `icao_is_home`, `icao_region`, `rank_group_of`.
Roles: owner (migrate and restore only), `collector` (DML on its tables, EXECUTE on the SECURITY DEFINER maintenance functions, no DDL), `backup_ro` (`pg_read_all_data`), `grafana_ro` (SELECT on views and lookups only; connection limit 10; read-only default; **a session can switch its own read-only default off, so the real protection is lack of privileges, and the tests/smoke test prove that**), `watchdog_ro` (`pg_monitor` plus three tables).

### 8.4 Definitions that matter (details and v1 differences: `docs/DEFINITIONS.md`)
- Flight = pilot session `(cid, callsign, logon_time)`, split after a 120-minute gap between samples and every 8 h of span (fixed buckets); counts as a "flight" when departure and arrival are non-empty after `upper(btrim(x, E' \t\r\n'))` (circuits, ZZZZ, overflights and one-sample sessions all count, same as v1). Start = first stored sample, end = last; duration = end - start; airborne minutes = sum of gaps where groundspeed >= 60 kt at the earlier sample, each gap capped at 2.5 x `POLL_INTERVAL_SECONDS` (75 s at the 30 s default, ADR-024; this was 40 s = 2.5 x 15 s when written). Reconnects of one flight are merged in the `flight` view (ADR-023) and in-progress flights are excluded from the flight counts (ADR-025).
- Controller session = `(cid, callsign, logon_time)`, start = `logon_time`, end = last sighting; reconnects within 5 minutes merge in the view.
- Online now = seen within the last two snapshots (2 x the poll interval, ADR-024) and within 5 minutes of wall clock (dashboard views).

## 9. Decisions
**The owner's 17 decisions (all resolved):** (1) Milk Run Monday fastest/slowest charts dropped, no sector logic anywhere. (2) Develop on the PC, run 24/7 on a Linux VM (now `vatpac-v3`). (3) LAN only first; Cloudflare later and external. (4) Names and CIDs stored and shown as in v1. (5) Raw positions kept 90 days; sessions forever. (6) Retention charts: owner will export the Metabase definitions. (7) "Top 50 service providers" = hours online over 30 days. (8) REST API: health/status only, data routes retired. (9) Callsign-pattern and frequency exclusion filters removed (inactive in v1; the whitelist keeps ATIS/_OBS out). (10) Store every 15 s sample, compressed (superseded: plain PostgreSQL, no compression, ADR-003). (11) A flight is the same as v1's. (12) No stand-in for the dropped ATC-coverage charts. (13) Watchdog authority: restart and alert only, later reduced to alert-only by owner choice (no Docker access). (14) No backfill. (15) Config-driven region, Australia deployed. (16) v1 is out of scope, the owner manages it. (17) Unlisted look-alike controller callsigns: log and alert once. Also: plain Postgres (not TimescaleDB), Grafana (not Metabase), keep the existing geography filters, frequency matching and sector matching not required (documented), maintainable and watched, built by role agents. Later: movements same as v1 (ADR-018); socket proxy and Cloudflare left out of the repo (ADR-012).
**ADR log:** `docs/DECISIONS.md` (ADR-001 to ADR-018 when written; ADR-019 and ADR-020 added since). The approved plan file `C:\Users\mdora\.claude\plans\idempotent-greeting-horizon.md` on the owner's PC is HISTORICAL (it still mentions TimescaleDB, a socket proxy and a Cloudflare profile, all superseded). `SPEC.md` + `docs/DECISIONS.md` are authoritative.

## 10. Charts
Source of truth for dispositions: `docs/CHART_DEFINITIONS.md` (every one of the 57 live v1 cards) and `docs/DASHBOARDS_SIMPLIFIED.md`. The recovered v1 SQL of all 118 cards (57 live, 61 archived) is in `docs/reference/v1_metabase_cards.json` (fields `id, name, display, archived, query_type, sql`).
- **Rebuilt (36 of 57): KEPT 17, CHANGED 19** (*update 08-10-2026: KEPT 18, CHANGED 18 after the T14 re-check*). Dashboards (`grafana/dashboards/`): `pilots/pilots-now.json`, `pilots/pilots-traffic.json`, `atc/atc-overview.json`, `atc/atc-retention.json` (UNVERIFIED panels), `events/monday-events.json`, `demographics/pilot-origins.json`, `military/military.json`, `monitoring/collector-health.json`. 8 dashboards, 61 query panels plus 9 "Data since go-live" text panels (*update 08-10-2026: 68 query panels and 9 text panels; 5 panels remain screenshot-only*). There are also 11 screenshot-only panels and 14 new panels with no v1 equivalent.
- **Dropped on purpose (21):** frequency matching: cards 41, 43, 98, 133; sector matching: 51, 88, 102, 104, 105, 106, 107; Milk Run Monday fastest/slowest: 62, 86, 132; v1 pipeline monitors: 121, 122, 123, 124, 125, 126, 139. Aircraft-handled metrics also gone with frequency matching.
- **Home airport / region** charts use `home_icao` parsed from the pilot name suffix (12% of names have none: shown as Unknown). Region prefixes come from `app_setting.home_icao_prefixes` (config-driven, default `Y`).
- Charts start EMPTY (no backfill); every dashboard says "Data since <go-live>". Long-window charts (cohorts, 90-day, lapsed/reactivated) fill with time.

## 11. What v1 is and what we learned (so you can answer the owner's questions about it)
v1 (repo `t288matt/vatpac-stats`, public; the owner's old stack): a FastAPI/SQLAlchemy service that every ~90 s fetched the VATSIM feed (and a separate radio "transceivers" feed every ~2 minutes), filtered it (polygon for pilots, whitelist for controllers), stored one raw row per pilot/controller/radio per poll, tracked time in 20 airspace sectors, matched pilots to controllers by radio frequency to measure ATC coverage, and built summary tables through background jobs (flight summaries, controller summaries, an enrichment worker, archiving). Metabase read the database for the charts; a daily integrity container ran SQL checks. About 12.8k lines in `app/` (a 3.6k-line `data_service.py`), 171 Python files and about 60 saved SQL reports in the repo, 505 commits.
Its problems (all evidenced): 40 GB database, 34 GB of it `flights_archive` (99.5M rows) and 3.7 GB transceivers; summaries stalled for months (last flight summary 12-08-2026, 5,966 pending enrichment); duplicates from keying on mutable departure/arrival/route; no primary/foreign keys or unique constraints except surrogate ids; JSONB two-way links; sector-tracking corruption; enrichment retry SQL that never binds; callsign-pattern and frequency filters never applied; controller whitelist failed open; polling every ~92 s of a feed that updates every 15 s (it saw one snapshot in six); an outage looked like an empty feed; timezone and DISTINCT hacks in chart SQL; recurring index corruption; about 170 root scripts and 45 post-mortem docs; tests that needed a live database.
**v1 summary criteria** (what gets into `flight_summaries`): at ingest the pilot must be in the polygon and have a departure and arrival; the selector groups raw rows by `(callsign, cid, departure, arrival)`, waits until the newest row is at least `FLIGHT_COMPLETION_HOURS` old (8 in compose, 14 in config), splits on 120-minute gaps, then upserts a summary. No minimum duration, no minimum samples, no movement check. v1's airport-movements chart counts every summary row.
v1's good ideas that v3 kept: session identity `(cid, callsign, logon_time)`, reconnect merging, airborne = groundspeed >= 60 kt, the polygon and whitelist files, idempotent upserts, config-driven thresholds, health checks. Detailed v1 behaviour reports were produced by read-only audits; the headline numbers are in section 12.

## 12. Evidence log (measured this session; re-measure if you need to claim them)
- VATSIM feed (07-10-2026): about 1.5 MB per snapshot, `Cache-Control: max-age=15`, `update_timestamp` changes every exactly 15 s; about 1,470 pilots and 130 controllers worldwide at 15:00 UTC; separate `atis` array (96 entries) that v1 and v3 ignore; 10 `_OBS` callsigns and 45 controllers on frequency 199.998 appear in the controllers array.
- Filter cross-check (independent re-implementation vs the live database, 07-10-2026 19:34:04 UTC snapshot): 1,629 pilots, 15 inside the polygon, 14 with a flight plan; 242 controllers, 0 whitelisted online. Matched v1's and v3's stored rows exactly. Earlier snapshot: 1,468 pilots to 14 in the polygon, 127 controllers to 1 whitelisted.
- Polygon: 24-point GeoJSON, lon 75 to 163 E, lat -60 to -2; includes Cocos, Lord Howe, Darwin, the Southern Ocean and part of the Indian Ocean; excludes New Zealand, Norfolk, PNG, Fiji, Vanuatu, New Caledonia, Indonesia. 27 whitelisted controller callsigns are outside the polygon (AY, NF, NV, NW, PK prefixes).
- v1 data quality: median flight 101 min (p90 227, p99 452), 94% IFR, 4.5% circuits (dep = arr), route string changes in 44% of sessions, 743 `(cid, callsign)` pairs with several logons in 8 days (max 29), groundspeed outliers to 30,702 kt, altitude -157 to 60,049 ft, 12% of pilot names lack an airport suffix.
- v1 movements: in the last full month (12-07 to 12-08-2026) 10,138 home-region departures counted, 1,197 (12%) with zero airborne time, 227 under 5 minutes, 567 circuits.
- v1 history coverage: raw data only 07-07 to 12-08-2026; summaries stop in 08-2026 and have gaps (April to June 2026 empty).
- Local stack: ingestion at 15 s cadence, about 200 ms per poll; flight_summary refresh about 0.1 s; backup 84 to 92 KB at the start and restore test OK; OOM crash auto-restarted (RestartCount 1); feed outage: process stayed up, error rows recorded, breaker opened, `/health` 503, recovered; watchdog raised CRIT/WARN alerts and RECOVERED messages (logged, no Telegram); soak at 21:19 UTC: 1.77 h covered, 404 of 426 snapshots (94.8%, gaps from deliberate tests), verdict INCOMPLETE.
- Performance of the old live `flight` view (synthetic 10.5M `pilot_position` rows): 12.5 s per query; `monday_attendance` 153 s. After `flight_summary`: 11 ms and 61 ms, results identical (`flight_old EXCEPT flight` empty); full backfill 18 s one-off; refresh of a 12 h window about 100 ms.

## 13. Bugs found by actually running things, and review findings with status

### 13.1 Found at run time (each only visible when the stack ran)
- Grafana could not authenticate: provisioning used `${GRAFANA_RO_PASSWORD}` but compose gave the container `GRAFANA_RO_DB_PASSWORD`. Fixed (`grafana/provisioning/datasources/postgres.yml`).
- Windows line endings (CRLF) in shell scripts and Dockerfiles broke the Linux containers (`set: illegal option -`). Fixed by converting to LF; `.gitattributes` forces LF in git.
- `flight` view used `trim()` which strips spaces only, so a tab in an airport code bypassed the gate. Fixed with `btrim(x, E' \t\r\n')`.
- `store.py` assumed tuple rows (broke with a dict-row connection). Fixed with an explicit `tuple_row` cursor.
- Smoke test looked for "permission denied" but the role is read-only by default (error text differs). Fixed: switch the default off and prove the privilege denial.
- PII checker flagged the feed's compact timestamp (`general.update`) as a phone number. Fixed.
- dbmate sends a file as ONE command: two `CREATE INDEX CONCURRENTLY` in one migration failed. Fixed (one per file, `-- migrate:up transaction:false`).
- **VM crash loop (07-10-2026):** `NumPy was built with baseline optimizations: (X86_V2) but your machine doesn't support: (X86_V2)`. The Proxmox VM's default virtual CPU (QEMU/kvm64) lacks the x86-64-v2 baseline numpy 2.x needs, so shapely could not import and the collector restarted forever. Fixed by pinning `numpy==1.26.4` in `collector/requirements.txt` (commit `d5de461`). The alternative is to set the VM's CPU type to `host` or `x86-64-v2-AES` in Proxmox (needs a VM restart); either is fine, but any new x86 VM or image may hit this again. Watch for it if you add other scientific wheels. **Update 14-10-2026:** the owner set the VM CPU type to `host` in Proxmox (i5-6500, SSE4.2 present), and the collector moved to `numpy==2.3.5` (issue 174); the 1.26.4 pin and the Dependabot numpy ignore are gone. Keep the CPU type `host` or `x86-64-v2-AES` on any rebuilt VM.

### 13.2 Independent code review: all findings and status (reviewer agent, read-only, plus fixes by three agents)
HIGH: H1 `flight` view could not scale (14 s at 8.9M rows): FIXED (`flight_summary`, ADR-016). H2 collector could hang on a DB lock: FIXED (statement/lock/idle timeouts, 25 s apply timeout). H3 one poisoned feed record (lone surrogate) could block all ingest: FIXED (sanitising and bisect-in-savepoint). H4 default install reachable with trivial credentials: FIXED (blank secrets, `check_env`, Grafana bound to 127.0.0.1 by default).
MEDIUM: M1 flights changed after day 90: FIXED (flights finalised before partition drop). M2 restore could destroy the live DB: FIXED (staging DB, exact-name confirm, session check, safety dump, swap). M3 backups on the same disk and no backup healthcheck: healthcheck FIXED; off-box copy hook exists but NOT configured (open). M4 disk sizing: documented 40 GB+ (open: operational). M5 reload rejection only a log line: `/health` fields FIXED; watchdog alert on it NOT written (open). M6 `/health` green on a frozen feed: FIXED. M7 collector ran as superuser: FIXED (least-privilege roles). M8 CI gaps: jobs ADDED, never run (open; *update 08-10-2026: CI is green*). M9 mutable action tags: release gated on CI and tag-restricted, SHAs not pinned (open; *update 08-10-2026: pinned to SHAs, T10*). M10 Grafana hardening: mostly FIXED (external snapshots off, gravatar off, caps dropped, viewers cannot edit, role connection limit); `read_only` root not enabled. M11 silent filter misses: FIXED (callsign strip, malformed counted, whitelist shrink guard). M12 movements count never-airborne sessions: owner decided keep as v1 (ADR-018).
LOW: L1 duplicate SQL files deleted. L2 `pilot_session` bloat: fillfactor 70 and no-op-upsert guard done. L3 partition pinning documented plus a NOTICE. L4 8 h split is an approximation (documented). L5 `online_now` wall-clock guard done. L6 names/CIDs retention: decided, keep forever (ADR-019). L7 restore-test result now checked by the watchdog. L8 DSN building via `make_conninfo`: done in the collector and watchdog (compose still builds a URL, so passwords must be URL-safe). L9 image digests not pinned (open). L10 `pilots_kept` now counts rows actually written. L11 EXECUTE on maintenance functions revoked from PUBLIC. L12 `.env.example` tidied.

## 14. Known limitations and risks
- 24/7 not yet shown on the VM; the soak is the evidence (*update 08-10-2026: still outstanding, TRACKER T1*). The VM host itself (Proxmox) is the owner's responsibility.
- No Docker access for the watchdog, so a running-but-unhealthy container is not auto-restarted.
- Names and CIDs are stored forever and shown on dashboards; any public exposure needs authentication and the owner's check of VATSIM's data-use policy.
- Single database, no replication; the only copies are dumps on the same host until `BACKUP_REMOTE` is configured.
- Timestamps: everything is UTC (`timestamptz`); Grafana is set to UTC. The owner thinks in UK time (GMT/BST) and Sydney time, so state the time zone whenever you give a time.
- The numbers will differ from v1's screenshots: different definitions (airborne time from sample gaps, no duplicates from amended plans), 15 s instead of 92 s sampling (30 s since 09-10-2026), different data period, no backfill.
- `docs/CHART_DEFINITIONS.md` "Test" column says what to check once real data exists (it describes checks, it does not claim they passed).
- The integration suite has one skip (role-dependent). The CI workflows have not run on GitHub (*update 08-10-2026: they have, and are green*).

## 15. Gotchas and tooling quirks (each cost real time)
- Edit tool requires a prior Read of the file (Grep does not count; files written by sub-agents count as unread).
- Writing a literal backslash-r (carriage return escape) through the shell tool mangled files several times (it became a real CR; Python `splitlines()` then split the line and left fragments like `$//' ...` in a Dockerfile). Use `chr(92)`, avoid the sequence, or use `tr`/`sed` in a script file; check results with `od -c` or `repr`.
- `$$` inside `sh -c '...'` is expanded to a PID by the shell; build SQL strings with `chr(NN)` or different quoting.
- `sleep N && ...` as a foreground command can be blocked; use polling loops with short sleeps, or run in the background.
- The auto-mode classifier sometimes returns "no verdict" (transient): retry once, then do other work. It once blocked a live-database read until the owner confirmed.
- Git Bash on Windows: use `/c/Users/...` paths; `make` and `gh` are not installed.
- Python in the venv is 3.11; the Docker image is 3.12: write code for 3.11+.
- `docker kill` counts as a manual stop (no restart); `docker compose up -d` after killing. An OOM or crash is restarted.
- Postgres `trim()` strips spaces only. Postgres `ALTER ROLE ... SET default_transaction_read_only` can be overridden by the session; rely on privileges.
- Passwords are put in URLs: letters and digits only.
- Grafana's `GF_SECURITY_ADMIN_PASSWORD` applies only on first start.
- A cloned VM shares host keys, cron, Tailscale identity and monitor pushes with the original (section 7.4).
- SSH to the VM: `known_hosts` for that IP was reset once (the original had stale entries from an earlier machine).
- v1's Postgres container image cannot be reused for v3; v3 uses `postgres:16.15-bookworm` (16.4 until 09-10-2026; CI still tests on 16.4 until updated).
- Docker Desktop on the owner's PC sometimes needs a manual start; `docker info` failing with a pipe error means it is not running.

## 16. Repo map and where other material lives
```
SPEC.md                      authoritative spec (sections 1-13, incl. public interfaces and acceptance checks)
README.md                    quick start, Makefile table, verified vs not verified
docs/DECISIONS.md            ADR-001..018 + open decisions (now ADR-001..020)
docs/ARCHITECTURE.md  DATA_DICTIONARY.md  DEFINITIONS.md  CONFIG_MAP.md (every v3 variable and every v1 variable kept/changed/dropped)
docs/CHART_DEFINITIONS.md  DASHBOARDS_SIMPLIFIED.md
docs/RUNBOOKS/               deploy-to-vm, restore, feed-outage, stuck-collector, update-reference-files, db-full
docs/reference/v1_metabase_cards.json   recovered v1 card SQL (118 cards)
collector/                   Dockerfile, pyproject.toml, requirements.txt (all versions pinned), src/vatpac/, tests/unit/
db/                          migrations/ (dbmate, forward-only, 20261007000001..24; now ..20261008000031), roles.sql
grafana/                     provisioning/ (datasource, dashboards), dashboards/<folder>/*.json, validate_dashboards.py, check_columns.py
ops/                         check_env.sh gen_env.sh smoke.sh soak_report.sh/.sql check_panels.py check_config_drift.py check_size_budget.py ruff.toml (check_fixture_pii.py was removed 08-10-2026, TRACKER T29); backup/ migrate/ watchdog/ postgres/
tests/                       integration/ (needs Postgres), fixtures/ (anonymised feed snapshot + expected.json), tools/make_fixture.py, README.md
config/                      australian_airspace_polygon.json, controller_callsigns_list.txt, australian_airspace_sectors.geojson (unused by v3)
.github/workflows/           ci.yml, release.yml (manual `publish <commit>`: checks the commit's main CI run, builds and pushes sha-tagged images, issue 173), dependabot.yml
.claude/agents/              role definitions: data-engineer, analytics, sre, qa, reviewer, docs-writer
Makefile                     up, down, logs, ps, config, lint, test, smoke, soak (HOURS=24), backup, backup-test, restore, size, pii (removed 08-10-2026 with the fixture check, TRACKER T29), dev (now also gen-env, check-env, drift)
```
Outside the repo (owner's PC, may be temporary): the v1 working copy and screenshots (`C:\Users\mdora\Pictures\Screenshots`, 3,295 PNGs of which about 50 are the Metabase charts; copies of 34 are in the v1 working copy `docs\metabase_charts\`; they show real names so they were NOT copied into this repo); Claude's scratchpad under `C:\Users\mdora\AppData\Local\Temp\claude\...\scratchpad\` holds throw-away analysis scripts (and an OCR text index of the screenshots), none needed to continue; the historical plan file named in section 9.

## 17. Working with agents (how this session was run, if you want to repeat it)
The lead (main session) wrote `SPEC.md`, then launched role agents in parallel, each owning separate directories and told NOT to commit (the lead commits and pushes). Roles are in `.claude/agents/`. Pattern that worked: data-engineer (db + collector), analytics (grafana + chart docs), SRE (compose, ops, CI), QA (independent tests written from the SPEC's public-interface contract, anonymised fixture), docs-writer, reviewer (read-only; found the scaling and security issues). Lessons: give each agent a self-contained prompt with file ownership and "what you could not verify"; agents hit a usage limit once and stopped mid-work (check disk, then restart with a "resume" prompt); agents wrote CRLF files on Windows; always re-run everything yourself after agents finish (the first real run found 31 failing integration tests and the Grafana password bug); never accept "SQL parsed" as "SQL works" (the first execution on a real Postgres is what found the bugs).

## 18. Owner profile and rules (from the owner's global instructions and this session)
Matt (UK-based; also uses Sydney time; VATPAC staff). Wants: high detail, trade-offs explained, flag every optimisation and risk, warm but direct tone, no excessive caveats; multi-step tasks outlined first and approved before execution (he kept the session in plan mode until he was satisfied, then said "execute the plan"); verification stated up front; new commits not amendments; Telegram (not email) for notifications, plain text, no emoji; never put financial or identity data into forms; never save personal data in memory. He decided to run alert-only watchdog, to leave Cloudflare and the Docker proxy to himself, and that v1 is his. He answers decision questions one at a time and dislikes repetition; the stop hook repeats the unmet-goal message, so reply briefly and only with news.
His global instructions say scheduled/routine tasks should use the Haiku 4.5 model; this session ran on the model the harness provided.

## 19. Things you must NOT do
- Do not touch v1 (original VM or its repo) or the owner's Uptime Kuma, Proxmox, Unraid or PBS.
- Do not commit `.env`, tokens, dumps, backups or internal IP addresses. *Update 08-10-2026: names and CIDs are public and kept forever (ADR-019), so the anonymisation rule and its check `ops/check_fixture_pii.py` were removed (TRACKER T29); the existing fixtures stay anonymised (names `Pilot NNNN`/`Controller NNNN`, CIDs 9000000 to 9999999).*
- (Superseded 11-10-2026 by ADR-029: the 10 dashboards are now public read-only; Grafana itself is still never exposed.) Do not expose Grafana to the internet; do not add Cloudflare or a Docker socket proxy to the repo (owner's choice).
- Do not edit applied migrations; add new ones. Do not rename the public interfaces in `SPEC.md` section 7 without updating the tests.
- Do not claim the goal is met before section 1 items 4 and 5 have evidence.
- Do not redeploy the VM during the soak unless necessary, and say so if you do (it creates a gap in the soak evidence).
- Do not run integration tests against a database that is not named `*_test` (they TRUNCATE).
