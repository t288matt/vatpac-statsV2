# Install a new instance from the source code

*Note 14-10-2026 (issue 173): a new section 4b describes the registry mode that the production VM has used since 14-10-2026, and section 2 describes the read-only deploy key. Everything else below is the local-build install, last reviewed 10-10-2026 against the deployed commit 3eb81a3 (the `.env.example`, `Makefile`, `docker-compose.yml` and `ops/` scripts at that commit). Written for a person with a clean Linux machine and nothing from the owner's VM. Walked through on 21-10-2026 on a Windows 11 PC (Docker Desktop, Git Bash, local-build mode, commit c072491, no earlier data): sections 3 to 6 worked, with the corrections marked "Walk-through 21-10-2026" below and the Windows notes in section 11. The Linux server path was not re-run. Anything not checked is marked UNVERIFIED.*

The result is the same stack that runs in production: PostgreSQL 16, a collector that reads the public VATSIM feed every 30 seconds, Grafana dashboards, a nightly backup, and a watchdog. A new instance starts with NO history and NO backfill: every chart starts empty at your go-live and fills as data arrives. The 30 to 90 day charts need that much history before they mean anything.

## 1. What you need
- A Linux machine that stays on (Debian or Ubuntu are what the project uses), 2 vCPU, 4 GB RAM, **40 GB or more of disk** (about 2 to 3 GB of positions at 180 days, plus WAL, 14 days of backups, images and logs; 20 GB is too tight). Sizing source: [RUNBOOKS/deploy-to-vm.md](RUNBOOKS/deploy-to-vm.md). **On Proxmox or QEMU set the VM CPU type to `host` (or `x86-64-v2-AES`), not the default "Common KVM processor"**: the collector uses numpy 2.x, which needs the x86-64-v2 CPU baseline (SSE4.2, POPCNT) and otherwise crash-loops on import.
- Docker Engine and the Docker Compose plugin (`docker compose version` must show v2.20 or newer). Install: `curl -fsSL https://get.docker.com | sudo sh`, then `sudo usermod -aG docker "$USER"`, log out and in again, and check `docker compose version`. On a VM, check the CPU type after a change with `grep -m1 -ow sse4_2 /proc/cpuinfo` (it must print `sse4_2`); a changed type needs a full stop and start of the VM, not a reboot.
- A synchronised clock. Check `timedatectl`: it must say `System clock synchronized: yes`. The collector rejects a feed snapshot dated more than `FEED_MAX_FUTURE_SECONDS` (120) ahead of the clock, and a clock that is more than about two minutes slow breaks polling.
- Outbound internet access to `data.vatsim.net` and `status.vatsim.net` (HTTPS), and for the image builds.
- `git`, `sh` (POSIX shell), `make`, `openssl` (used by `ops/gen_env.sh` when present), `curl` (for the Docker install line), `sudo` (for the Docker install) and Python 3.11 or newer on the machine, for the helper scripts. On a Windows PC (development only, never production) `make` is not installed and `psql` is not on the PATH of Git Bash; the stack needs neither: see section 11.
- Access to the repository (it is private: you need a read credential from the owner). The method planned for servers is a read-only GitHub deploy key (section 2). UNVERIFIED: the exact clone method you will be given.

## 2. Get the code
```sh
git clone <repository url> vatpac-statsV2
cd vatpac-statsV2
git checkout <the release tag or commit the owner gives you>
```
Run every later command from this folder. Do not use the tip of `main` unless the owner says so: it can contain reviewed work that is not yet in production.

**Deploy key (for a server that keeps itself up to date).** Instead of a personal login, give the machine a read-only deploy key: `ssh-keygen -t ed25519 -f ~/.ssh/vatpac_deploy -N ""`, add the `.pub` line in the repository's Settings > Deploy keys with **Allow write access OFF**, and clone with `GIT_SSH_COMMAND="ssh -i ~/.ssh/vatpac_deploy -o IdentitiesOnly=yes" git clone git@github.com:t288matt/vatpac-statsV2.git`. The full, checked procedure (adopting an existing folder, and the checks after each step) is section 2d of [RUNBOOKS/deploy-to-vm.md](RUNBOOKS/deploy-to-vm.md). Run on the production VM on 14-10-2026. Alternative to a deploy key: HTTPS with a fine-grained personal access token (Contents: read-only, this repository only); never store the token in the remote URL, use a credential helper. To deploy a fixed commit rather than a moving branch, use `git fetch origin && git checkout --detach <commit>` (there are no version tags).

## 3. Configure `.env`
All settings live in one file, `.env`, which is not in git. `.env.example` lists every setting with its default; the secrets are **blank on purpose**, and the stack refuses to start until they are set.

```sh
sh ops/gen_env.sh      # writes .env (mode 600) from .env.example with a random value for every blank secret; refuses to overwrite an existing .env
sh ops/check_env.sh    # rejects blank, change-me*, shorter than 16 characters, repeated, or non letter-and-digit secrets (a quoted or `export`ed value is read without the quotes, as every `.env` reader does)
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
- `GRAFANA_BIND` (default `127.0.0.1`, this machine only) and `GRAFANA_PORT` (3300). For access from your LAN set `GRAFANA_BIND` to the machine's LAN address, or to `0.0.0.0` if the address comes from DHCP (Docker can start before the address exists, and a bind to a missing address fails); then firewall it (section 8).
- `GRAFANA_ROOT_URL` (default `http://localhost:3300/`) and `GRAFANA_COOKIE_SECURE` (`false`; `true` only when users reach Grafana over https).
- `GRAFANA_ADMIN_USER` (default `admin`), `POSTGRES_DB` and `POSTGRES_USER` (default `vatpac`).
- `HOME_ICAO_PREFIXES` (default `Y`, the Australian airport prefix), `POSITION_RETENTION_DAYS` (180, a literal in `docker-compose.yml` since 19-10-2026; production has run 180 since 13-10-2026, issue 142, to give a six-month window in which a rule mistake can be spotted and airborne time recalculated, and time to build and rehearse the new drop gate without a deadline (the drop gate also keeps any day it cannot reconcile); ADR-026, ADR-030), `BACKUP_RETENTION_DAYS` (14), `BACKUP_TIME_UTC` (02:00).
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`: leave blank unless you want alerts (section 7).

**Changing a password later.** `GRAFANA_ADMIN_PASSWORD` only applies on Grafana's first start (empty `grafana_data`); changing it in `.env` afterwards does nothing. To rotate it: `docker compose exec grafana grafana cli admin reset-admin-password '<new password>'`, then update `.env` to match. For the other passwords, edit `.env` and run `docker compose up -d` (the migrate job runs `ALTER ROLE ... PASSWORD`); `POSTGRES_PASSWORD` is read by Postgres only at first initialisation, so rotate it with `ALTER ROLE` inside the database as well.

Everything else has a working default. The full list with meanings is [CONFIG_MAP.md](CONFIG_MAP.md).

The airspace polygon and the controller whitelist that decide what is kept from the feed are the files in `config/` (`australian_airspace_polygon.json`, `controller_callsigns_list.txt`). They are in the repository and mounted read-only; change them only with [RUNBOOKS/update-reference-files.md](RUNBOOKS/update-reference-files.md).

## 4. First start
```sh
make up            # = sh ops/check_env.sh .env, then docker compose up -d --build (first installs and PCs; on a registry-mode VM see 4b)
docker compose ps
```
Without `make`: `sh ops/check_env.sh .env` then `docker compose up -d --build`. The first build downloads images and compiles the collector; allow several minutes.

**Starting again over an old stack (Walk-through 21-10-2026).** `docker compose down` keeps the named volumes (`pgdata`, `grafana_data`, `backups`, `watchdog_status`), and they belong to the project name `vatpac`, so a new `.env` on the same machine silently reuses them. Postgres reads `POSTGRES_PASSWORD` only when it first creates the data directory, so the new random passwords will not match the old database, and the old database still has the old table layout. For a genuinely fresh start on a development machine, delete the old volumes first: `docker compose down -v` removes ALL of this stack's volumes (the database, Grafana state, backups and the archive), so use it only when none of that data matters. Then run `sh ops/gen_env.sh` again and start as above.

### 4b. Optional: run published images instead of building (the production VM since 14-10-2026)
The default above builds the four project images on the machine (`VATPAC_IMAGE_PREFIX` unset, names `vatpac-stats-<name>:latest`). The production VM runs images that GitHub Actions builds and publishes to a private registry, named `ghcr.io/t288matt/vatpac-stats-<name>:latest` (also kept as `:sha-<first 7 characters of the commit>`). You switch with one `.env` line, `VATPAC_IMAGE_PREFIX=ghcr.io/t288matt/` (nothing secret; described in [CONFIG_MAP.md](CONFIG_MAP.md)), and a registry login with a read-only token (`docker login ghcr.io`, steps in section 2e of the deploy runbook). Then start with `make deploy` (or `docker compose pull && docker compose up -d --no-build`) instead of `make up`, and never use `up -d --build` there (it rebuilds locally and replaces the pulled image). Two consequences: the build arguments `BACKUP_EXTRA_APT_PACKAGES` and `VATPAC_VERSION` are ignored in this mode, and a new instance needs the owner to give you a token and a published commit tag. The registry is private, so a stranger cannot use this mode. Source of truth: [RUNBOOKS/deploy-to-vm.md](RUNBOOKS/deploy-to-vm.md).

What happens, in order:
1. `db` (PostgreSQL 16, data checksums on, a named volume `pgdata`) starts and becomes healthy.
2. The one-shot `migrate` container runs **dbmate** over every SQL file in `db/migrations/`, then applies `db/roles.sql` (it creates the least-privilege roles from the passwords in `.env`) and the read grants for the watchdog role, then exits. In `docker compose ps`, `migrate` showing `exited (0)` is correct and expected. It runs again on every `docker compose up`; re-running changes nothing.
3. `collector`, `grafana`, `backup` and `watchdog` start once `migrate` has finished.

**Grafana 12.x and an existing `grafana_data` volume:** the image is `grafana/grafana:12.4`, so a `docker compose pull` can bring a newer 12.4.x patch version. The first start of a newer Grafana version (12.x minors included) migrates the `grafana_data` volume one way. There is no downgrade: an older Grafana cannot read the upgraded volume without the tarball taken before the upgrade. If you upgrade an install from before Grafana 12 (production did on 11-10-2026), copy the volume first (the command is in RUNBOOKS/public-access.md, "Roll back") and keep the copy until you are sure. A fresh install with an empty volume is not affected.

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

**What day zero looks like (Walk-through 21-10-2026).** Within ten minutes of the first start `ops/smoke.sh` ended with SMOKE PASS and `ops/check_panels.py` reported 0 errors, with a bit under half of the roughly 80 dashboard queries returning rows and the rest empty. That is normal: errors are what matter, empty panels fill as data arrives.

### 5b. Optional, owner only: compare a new instance with production
Both commands only read. From the repository root, dump the structure of the new instance and of production with `ops/schema_dump.sql` and compare the two files:
```sh
docker compose exec -T db psql -U vatpac -d vatpac -P pager=off -A -F" | " < ops/schema_dump.sql > schema_local.txt
ssh vatpac-v3 'cd ~/vatpac-statsV2 && docker compose exec -T db psql -U vatpac -d vatpac -P pager=off -A -F" | "' < ops/schema_dump.sql > schema_prod.txt
diff schema_local.txt schema_prod.txt
```
On 21-10-2026 the new instance and production differed in exactly three places, none of them a real difference: the `version` column of `schema_migrations` is `varchar(128)` on production (created by an older dbmate) and plain `varchar` on a new install; the number of daily `pilot_position` partitions depends on how long the database has run; and the settings of the `grafana_ro` role are listed in a different order. Anything else is worth looking at. Delete the two files afterwards (they are not for git).

## 6. First backup and a test restore
```sh
docker compose exec backup backup.sh once   # or: make backup     writes vatpac_<UTC stamp>.dump into the backups volume
make backup-test                            # restores the newest dump into a scratch database and checks it, then drops it
```
**On a brand-new instance the first command fails (Walk-through 21-10-2026).** The backup script refuses a first dump smaller than `BACKUP_MIN_FIRST_BYTES` (default 1 MiB, a guard against keeping an empty backup), and a database that is only minutes old is about 130 KB. It prints `FAILED content check: first dump is ... bytes, under the minimum ...`, moves the file aside as `vatpac_<stamp>.dump.rejected` in the backups volume, and `make backup-test` then says `no backup found`. For the first backup of a new instance lower the limit for that one run, then continue as normal:
```sh
docker compose exec -e BACKUP_MIN_FIRST_BYTES=100000 backup backup.sh once
docker compose exec backup restore-test.sh      # = make backup-test; expect: OK /backups/vatpac_<stamp>.dump poll_log=<n> pilot_session=<n> flight_summary=0
```
Once the database is bigger than 1 MiB the default limit passes by itself, so the nightly backups need no change. The restore check also fails with `poll_log is empty` or `pilot_session is empty` if nothing has been stored yet (nobody was in the airspace in the first minutes); wait a few minutes and run it again. `RESTORE_SANITY_ALLOW_EMPTY=1` waives only that row-count rule and is meant for a new install (`ops/backup/restore-lib.sh`); the walk-through did not need it. You can delete the `.rejected` file from the first attempt.
The restore check counts `poll_log`, `pilot_session` and `flight_summary` (the table of finished flights, which the 29 migrations create together with `drop_gate_refusal` and the `write_finished_flights` function; nothing is run by hand). It does not require `flight_summary` to hold rows, and a flight is written only 8 hours after its last position, so it is empty on a new install at first. The backup service also runs by itself at `BACKUP_TIME_UTC` and keeps `BACKUP_RETENTION_DAYS` days. The `backups` volume is on the same disk as the database, so set up a copy somewhere else (the `BACKUP_REMOTE` setting, [RUNBOOKS/restore.md](RUNBOOKS/restore.md) "Off-box copies"). A backup you have never restored is not a backup: the full restore procedure and the restore drill are in [RUNBOOKS/restore.md](RUNBOOKS/restore.md). `make backup-test` only checks that the main tables exist and hold rows, so run it after the collector has stored its first snapshots. UNVERIFIED on a fresh instance.

## 7. Optional: Telegram alerts and an outside heartbeat
- **Alerts.** The watchdog already runs and writes its verdict to a status file; it sends Telegram messages only when you set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. Create a bot with Telegram's BotFather and find your chat id yourself (outside this repository), put both values in `.env`, then `docker compose up -d --build watchdog`. What each message means: [RUNBOOKS/alerts.md](RUNBOOKS/alerts.md). Never paste the token anywhere. UNVERIFIED: the BotFather steps are Telegram's, not the project's.
- **Outside heartbeat.** The watchdog runs on the same machine, so a dead machine sends nothing. `ops/heartbeat/outside_heartbeat.py` is meant to run on a different computer every 5 minutes (a scheduled task or cron job) and messages you if Grafana's `/api/health` stops answering. Usage: `python outside_heartbeat.py --url http://<machine>:3300/api/health --settings <json file> --state <state file>`. It reads the Telegram token and chat id from the `env` block of the JSON settings file (keys `TELEGRAM_BOT_TOKEN` and `TELEGRAM_MATT_CHAT_ID`, a name from the owner's setup). Details: [RUNBOOKS/outside-heartbeat.md](RUNBOOKS/outside-heartbeat.md). UNVERIFIED on another machine.
- An optional `UPTIME_KUMA_PUSH_URL` setting exists for an Uptime Kuma monitor; see [CONFIG_MAP.md](CONFIG_MAP.md).

## 8. Network exposure
The default is safe: Grafana listens on the loopback address only, the database and the collector are not published. **Do not expose Grafana to the internet by accident**. Production shares the dashboards read-only through Grafana public dashboards and a Cloudflare Tunnel (anonymous access stays off); this is optional and not part of a basic install. It is described in [RUNBOOKS/public-access.md](RUNBOOKS/public-access.md) (names and CIDs are shown on the dashboards). If you open it to a LAN, set `GRAFANA_BIND` to the LAN address and firewall the port (`GRAFANA_PORT`) to the addresses that need it. Any tunnel or reverse proxy is yours to add; if it terminates https, set `GRAFANA_ROOT_URL` and `GRAFANA_COOKIE_SECURE=true`. Never publish the database or Docker's socket. Firewall notes: section 5 of deploy-to-vm.md.

## 9. After the first day: updating and keeping it safe
- Update: [RUNBOOKS/deploy-to-vm.md](RUNBOOKS/deploy-to-vm.md) ("Updating", "What a change needs", "Roll back"). Take a fresh dump before every update, then deploy: in local-build mode `docker compose up -d --build` (never `docker compose restart` for a code change); in registry mode (the production VM) `git checkout --detach <commit>`, set the tag, `docker compose pull`, `docker compose up -d --no-build` (runbook section 2f; never `--build` there). Then `make smoke` and `python3 ops/check_panels.py`. Migrations are forward-only; never edit one that has been applied.
- Backups and restore: [RUNBOOKS/restore.md](RUNBOOKS/restore.md). Disk full: [RUNBOOKS/db-full.md](RUNBOOKS/db-full.md). Collector stuck: [RUNBOOKS/stuck-collector.md](RUNBOOKS/stuck-collector.md). Feed down: [RUNBOOKS/feed-outage.md](RUNBOOKS/feed-outage.md).
- Reboot test: restart the machine once and confirm everything returns by itself (containers restart automatically); deploy-to-vm.md section 7.
- Never run `db/roles.sql` by hand against a running instance: it resets the role passwords.

## 10. Stopping and starting
`make down` (or `docker compose down`) stops the stack and KEEPS the volumes (data, Grafana state, backups, archive). Do not add `-v` unless you mean to delete all data.

## 11. Setting up on a Windows PC (development only, never production)
Walked through on 21-10-2026 on Windows 11. Production stays on Linux; this is for trying the stack or testing a change.
- **You need:** Git for Windows (its Git Bash provides `sh` and `openssl`), Docker Desktop with the WSL 2 backend, and Python 3.11 or newer (`python3` must work in Git Bash). Run every command in this guide from Git Bash, in the repository folder.
- **`make` is not installed and not needed.** Every `make` target in this guide is a plain command:

| Instead of | Run |
|---|---|
| `make gen-env`, `make check-env` | `sh ops/gen_env.sh`, `sh ops/check_env.sh .env` |
| `make up` | `sh ops/check_env.sh .env` then `docker compose up -d --build` |
| `make smoke` | `sh ops/smoke.sh` |
| `make backup` | `docker compose exec backup backup.sh once` (see the first-backup note in section 6) |
| `make backup-test` | `docker compose exec backup restore-test.sh` |
| `make down` | `docker compose down` |

- **`psql` is not needed on the PC** to run the stack: the commands above run `psql` inside the containers. Windows installs the PostgreSQL client in `C:\Program Files\PostgreSQL\<version>\bin`, which Git Bash does not have on its PATH. Only some of the repository's own integration tests look for `psql` there, and they skip when it cannot be found; [RUNBOOKS/run-tests.md](RUNBOOKS/run-tests.md) section 1b has the one `PATH` line that fixes it.
- **Docker Desktop will not start and says `com.docker.build: exit status 1`** (often after "failed to fetch extensions"). A leftover `com.docker.build.exe` from an earlier crash is holding the build service's pipe, so every restart fails; restarting Docker Desktop or running `wsl --shutdown` does not clear it. Quit Docker Desktop, end that `com.docker.build` process in Task Manager (its start time is older than Docker Desktop's), then start Docker Desktop again. Do NOT use "Reset to factory defaults": it deletes every image, container and volume.
- **Git Bash rewrites container paths.** An argument that starts with `/` (for example `restore.sh /backups/file.dump`) is turned into a Windows path (`C:/Program Files/Git/backups/...`) before Docker sees it, and the command says the file is not found. Put `MSYS_NO_PATHCONV=1` in front of that one command, or write the path with a double slash (`//backups/file.dump`).
- **Ports:** Grafana is on `http://localhost:3300`. Nothing else is published.
