# Install a new instance from the source code

*Last reviewed 10-10-2026 against the deployed commit 3eb81a3 (the `.env.example`, `Makefile`, `docker-compose.yml` and `ops/` scripts at that commit). Written for a person with a clean Linux machine and nothing from the owner's VM. Nobody has followed this page on a fresh machine, because the owner's PC has no working Docker; every command was checked against the repository only. Anything not checked is marked UNVERIFIED.*

The result is the same stack that runs in production: PostgreSQL 16, a collector that reads the public VATSIM feed every 30 seconds, Grafana dashboards, a nightly backup, and a watchdog. A new instance starts with NO history and NO backfill: every chart starts empty at your go-live and fills as data arrives. The 30 to 90 day charts need that much history before they mean anything.

## 1. What you need
- A Linux machine that stays on (Debian or Ubuntu are what the project uses), 2 vCPU, 4 GB RAM, **40 GB or more of disk** (about 2 to 3 GB of positions at 90 days, plus WAL, 14 days of backups, images and logs; 20 GB is too tight). Sizing source: [RUNBOOKS/deploy-to-vm.md](RUNBOOKS/deploy-to-vm.md).
- Docker Engine and the Docker Compose plugin (`docker compose version` must show v2.20 or newer). Install steps: section 1 of deploy-to-vm.md (`curl -fsSL https://get.docker.com | sudo sh`, then add your user to the `docker` group and log in again).
- A synchronised clock. Check `timedatectl`: it must say `System clock synchronized: yes`. The collector rejects a feed snapshot dated more than `FEED_MAX_FUTURE_SECONDS` (120) ahead of the clock, and a clock that is more than about two minutes slow breaks polling.
- Outbound internet access to `data.vatsim.net` and `status.vatsim.net` (HTTPS), and for the image builds.
- `git`, `sh` (POSIX shell), `make`, `openssl` (used by `ops/gen_env.sh` when present), `curl` (for the Docker install line), `sudo` (for the Docker install) and Python 3.11 or newer on the machine, for the helper scripts.
- Access to the repository (it is private: you need a read credential from the owner). UNVERIFIED: the exact clone method you will be given.

## 2. Get the code
```sh
git clone <repository url> vatpac-statsV2
cd vatpac-statsV2
git checkout <the release tag or commit the owner gives you>
```
Run every later command from this folder. Do not use the tip of `main` unless the owner says so: it can contain reviewed work that is not yet in production.

## 3. Configure `.env`
All settings live in one file, `.env`, which is not in git. `.env.example` lists every setting with its default; the secrets are **blank on purpose**, and the stack refuses to start until they are set.

```sh
sh ops/gen_env.sh      # writes .env (mode 600) from .env.example with a random value for every blank secret; refuses to overwrite an existing .env
sh ops/check_env.sh    # rejects blank, change-me*, shorter than 16 characters, repeated, or non letter-and-digit secrets
```
(`make gen-env` and `make check-env` do the same.) Without `sh`, copy `.env.example` to `.env`, `chmod 600 .env`, and fill each blank secret yourself with a different random value of letters and digits only, 16 characters or more (they are placed inside database connection URLs). Example generator: `openssl rand -hex 24`. Never commit `.env`; never paste its values into a chat or ticket.

**Required (generated for you):**

| Setting | What it is |
|---|---|
| `POSTGRES_PASSWORD` | Database owner; used by the db, migrate and restore only. Read by Postgres at first initialisation only |
| `COLLECTOR_DB_PASSWORD` | Database role `collector` (writes data) |
| `GRAFANA_RO_PASSWORD` | Database role `grafana_ro` (read-only, used by Grafana) |
| `BACKUP_DB_PASSWORD` | Database role `backup_ro` (read-only, used by pg_dump) |
| `WATCHDOG_DB_PASSWORD` | Database role `watchdog_ro` (read-only, used by the watchdog) |
| `GRAFANA_ADMIN_PASSWORD` | Grafana admin login; applies only on Grafana's FIRST start |

**Worth reviewing before the first start:**
- `GRAFANA_BIND` (default `127.0.0.1`, this machine only) and `GRAFANA_PORT` (3300). For access from your LAN set `GRAFANA_BIND` to the machine's LAN address; then firewall it (section 8).
- `GRAFANA_ROOT_URL` (default `http://localhost:3300/`) and `GRAFANA_COOKIE_SECURE` (`false`; `true` only when users reach Grafana over https).
- `GRAFANA_ADMIN_USER` (default `admin`), `POSTGRES_DB` and `POSTGRES_USER` (default `vatpac`).
- `HOME_ICAO_PREFIXES` (default `Y`, the Australian airport prefix), `POSITION_RETENTION_DAYS` (90), `BACKUP_RETENTION_DAYS` (14), `BACKUP_TIME_UTC` (02:00).
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`: leave blank unless you want alerts (section 7).

Everything else has a working default. The full list with meanings is [CONFIG_MAP.md](CONFIG_MAP.md).

The airspace polygon and the controller whitelist that decide what is kept from the feed are the files in `config/` (`australian_airspace_polygon.json`, `controller_callsigns_list.txt`). They are in the repository and mounted read-only; change them only with [RUNBOOKS/update-reference-files.md](RUNBOOKS/update-reference-files.md).

## 4. First start
```sh
make up            # = sh ops/check_env.sh .env, then docker compose up -d --build
docker compose ps
```
Without `make`: `sh ops/check_env.sh .env` then `docker compose up -d --build`. The first build downloads images and compiles the collector; allow several minutes.

What happens, in order:
1. `db` (PostgreSQL 16, data checksums on, a named volume `pgdata`) starts and becomes healthy.
2. The one-shot `migrate` container runs **dbmate** over every SQL file in `db/migrations/`, then applies `db/roles.sql` (it creates the least-privilege roles from the passwords in `.env`) and the read grants for the watchdog role, then exits. In `docker compose ps`, `migrate` showing `exited (0)` is correct and expected. It runs again on every `docker compose up`; re-running changes nothing.
3. `collector`, `grafana`, `backup` and `watchdog` start once `migrate` has finished.

Within about two minutes `db`, `collector`, `grafana`, `backup` and `watchdog` should all be `healthy`. If `migrate` is not `exited (0)`, read `docker compose logs migrate`.

Grafana login: open `http://localhost:3300` (or `http://<GRAFANA_BIND>:3300`), user `GRAFANA_ADMIN_USER`, password `GRAFANA_ADMIN_PASSWORD` from `.env` (read it from the file; do not paste it anywhere). Anonymous access is off. Dashboards and the data source are provisioned from `grafana/` by the container; there is nothing to import.

## 5. Check that it works
```sh
make smoke                                  # or: sh ops/smoke.sh   ends with SMOKE PASS
python3 ops/check_panels.py                 # runs every dashboard query through Grafana; expect 0 errors (EMPTY panels are fine while data is young)
docker compose logs --tail=20 collector     # an ok poll about every 30 seconds
```
`ops/smoke.sh` waits up to four minutes for containers to settle, then checks container health, the collector `/health`, a recent `ok` row in `poll_log`, Grafana's health, and that the Grafana role cannot write. The collector's `/health` and `/metrics` are on port 8080 inside the Docker network only and are not published; the smoke test reaches them for you. The database is not published either (the dev overlay `make dev` publishes it on `127.0.0.1:55432` only).

Charts: the "Monitoring: collector and data freshness" dashboard shows whether data is arriving. The rest fill over the first hours and days. Routine checks afterwards: [RUNBOOKS/daily-checks.md](RUNBOOKS/daily-checks.md).

## 6. First backup and a test restore
```sh
docker compose exec backup backup.sh once   # or: make backup     writes vatpac_<UTC stamp>.dump into the backups volume
make backup-test                            # restores the newest dump into a scratch database and checks it, then drops it
```
The backup service also runs by itself at `BACKUP_TIME_UTC` and keeps `BACKUP_RETENTION_DAYS` days. The `backups` volume is on the same disk as the database, so set up a copy somewhere else (the `BACKUP_REMOTE` setting, [RUNBOOKS/restore.md](RUNBOOKS/restore.md) "Off-box copies"). A backup you have never restored is not a backup: the full restore procedure and the restore drill are in [RUNBOOKS/restore.md](RUNBOOKS/restore.md). `make backup-test` only checks that the main tables exist and hold rows, so run it after the collector has stored its first snapshots. UNVERIFIED on a fresh instance.

## 7. Optional: Telegram alerts and an outside heartbeat
- **Alerts.** The watchdog already runs and writes its verdict to a status file; it sends Telegram messages only when you set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. Create a bot with Telegram's BotFather and find your chat id yourself (outside this repository), put both values in `.env`, then `docker compose up -d --build watchdog`. What each message means: [RUNBOOKS/alerts.md](RUNBOOKS/alerts.md). Never paste the token anywhere. UNVERIFIED: the BotFather steps are Telegram's, not the project's.
- **Outside heartbeat.** The watchdog runs on the same machine, so a dead machine sends nothing. `ops/heartbeat/outside_heartbeat.py` is meant to run on a different computer every 5 minutes (a scheduled task or cron job) and messages you if Grafana's `/api/health` stops answering. Usage: `python outside_heartbeat.py --url http://<machine>:3300/api/health --settings <json file> --state <state file>`. It reads the Telegram token and chat id from the `env` block of the JSON settings file (keys `TELEGRAM_BOT_TOKEN` and `TELEGRAM_MATT_CHAT_ID`, a name from the owner's setup). Details: [RUNBOOKS/outside-heartbeat.md](RUNBOOKS/outside-heartbeat.md). UNVERIFIED on another machine.
- An optional `UPTIME_KUMA_PUSH_URL` setting exists for an Uptime Kuma monitor; see [CONFIG_MAP.md](CONFIG_MAP.md).

## 8. Network exposure
The default is safe: Grafana listens on the loopback address only, the database and the collector are not published. **Do not expose Grafana to the internet by accident**: the public, read-only setup through Grafana public dashboards and a Cloudflare Tunnel is described in [RUNBOOKS/public-access.md](RUNBOOKS/public-access.md) (names and CIDs are shown on the dashboards). If you open it to a LAN, set `GRAFANA_BIND` to the LAN address and firewall the port (`GRAFANA_PORT`) to the addresses that need it. Any tunnel or reverse proxy is yours to add; if it terminates https, set `GRAFANA_ROOT_URL` and `GRAFANA_COOKIE_SECURE=true`. Never publish the database or Docker's socket. Firewall notes: section 5 of deploy-to-vm.md.

## 9. After the first day: updating and keeping it safe
- Update: [RUNBOOKS/deploy-to-vm.md](RUNBOOKS/deploy-to-vm.md) ("Updating", "What a change needs", "Roll back"). Take a fresh dump before every update, stop the collector, then `docker compose up -d --build` (never `docker compose restart` for a code change), then `make smoke` and `python3 ops/check_panels.py`. Migrations are forward-only; never edit one that has been applied.
- Backups and restore: [RUNBOOKS/restore.md](RUNBOOKS/restore.md). Disk full: [RUNBOOKS/db-full.md](RUNBOOKS/db-full.md). Collector stuck: [RUNBOOKS/stuck-collector.md](RUNBOOKS/stuck-collector.md). Feed down: [RUNBOOKS/feed-outage.md](RUNBOOKS/feed-outage.md).
- Reboot test: restart the machine once and confirm everything returns by itself (containers restart automatically); deploy-to-vm.md section 7.
- Never run `db/roles.sql` by hand against a running instance: it resets the role passwords.

## 10. Stopping and starting
`make down` (or `docker compose down`) stops the stack and KEEPS the volumes (data, Grafana state, backups, archive). Do not add `-v` unless you mean to delete all data.
