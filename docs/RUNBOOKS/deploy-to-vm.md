# Runbook: deploy to the Linux VM

Last reviewed: 09-10-2026. Target: always-on Linux VM (Debian/Ubuntu), 2 vCPU, 4 GB RAM, **40 GB+ SSD**. The production VM is reached as `ssh vatpac-v3` (an alias in the owner's `~/.ssh/config`) and the stack lives in `~/vatpac-statsV2`. That VM has no git and no GitHub credentials, so code is deployed with `git archive` (section 2c). Times in this runbook are UTC.

Disk sizing: about 2 to 3 GB of positions at 90 days, plus WAL (up to 2 GB), plus `BACKUP_RETENTION_DAYS` (default 14) dumps on the same disk, plus Docker images, logs and build cache. 20 GB is too tight. Provision 40 GB or more, or set `BACKUP_RETENTION_DAYS=7` and copy dumps off-box (`restore.md`, "Off-box copies"). The watchdog checks the filesystems listed in `WATCHDOG_DISK_PATHS` (default `/backups,/`, which is the Docker data-root filesystem that holds the `pgdata` and `backups` named volumes) and warns at `WATCHDOG_DISK_PCT` (80%). If you move a volume onto its own mount, mount that path into the watchdog and add it to the list.

## 1. Install Docker
```sh
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # log out and in again
docker compose version            # needs v2.20 or newer
```

## 2. Get the code (private repo)
Preferred: a read-only **deploy key**.
```sh
ssh-keygen -t ed25519 -f ~/.ssh/vatpac_deploy -N ""
cat ~/.ssh/vatpac_deploy.pub      # GitHub: repo Settings > Deploy keys > Add (leave write access OFF)
GIT_SSH_COMMAND="ssh -i ~/.ssh/vatpac_deploy -o IdentitiesOnly=yes" \
  git clone git@github.com:t288matt/<repo>.git vatpac-stats && cd vatpac-stats
```
Alternative: HTTPS with a fine-grained personal access token (Contents: read-only, this repo only). Do not store the token in the remote URL; use a credential helper.

## 2b. Pin a release
```sh
git fetch --tags && git checkout v1.0.0   # deploy tags, not a moving branch
```

## 2c. Copy the code without git (the method in use on the production VM)
Run from the repository root on the PC, with the commit you want to deploy checked out and committed (`git archive` ships committed files only):
```sh
git archive --format=tar HEAD | ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'
```
The VM's `.env` is not in git, so it is kept. `tar -x` overwrites files but never deletes: if a file was removed or renamed in git, delete it on the VM too (a stale module under `collector/src` would still be baked into the image, and a stale migration file would still be applied). For a first install, create the folder first (`ssh vatpac-v3 'mkdir -p ~/vatpac-statsV2'`), copy, then continue at section 3.

## 3. Configure
`.env.example` has **blank secrets on purpose**: the stack will not start with trivial credentials.
```sh
sh ops/gen_env.sh          # writes .env (mode 600) with random hex passwords; prints only the variable names
# or: cp .env.example .env, chmod 600 .env, and fill the blanks yourself
sh ops/check_env.sh        # rejects blank, change-me*, under 16 characters, repeated, or non letter/digit values
```
`make up` runs `check_env` first. Secrets: `POSTGRES_PASSWORD` (owner; used by the db, migrate and restore only), `GRAFANA_RO_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`, `WATCHDOG_DB_PASSWORD`, `COLLECTOR_DB_PASSWORD` (role `collector`), `BACKUP_DB_PASSWORD` (role `backup_ro`). They sit inside connection URLs, so they must be letters and digits only. Then set `GRAFANA_ROOT_URL`, and `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` if you want alerts. Never commit `.env`.

Two caveats:
- `GRAFANA_ADMIN_PASSWORD` only applies on Grafana's **first start** (empty `grafana_data`). Changing it in `.env` later does nothing. To rotate: `docker compose exec grafana grafana cli admin reset-admin-password '<new password>'`, then update `.env` to match.
- Changing the other passwords: edit `.env`, then `docker compose up -d` (the migrate job runs `ALTER ROLE ... PASSWORD`; `POSTGRES_PASSWORD` itself is only read by Postgres at first initialisation, so rotate it with `ALTER ROLE` inside the database as well).

## 4. Start
```sh
make up                      # = check_env + docker compose up -d --build
docker compose ps            # migrate shows "exited (0)"; everything else healthy within about 2 minutes
```

## 4b. Special case: migration 20261008000100 (whole-second logon_time, T36 / ADR-021)
This migration merges duplicate sessions and rewrites `logon_time` in both session tables. It cannot be undone by its down half (the fractions are gone), so **take a fresh dump first** (`docker compose exec backup backup.sh once`, then check the new file in `/backups`) and keep a copy of the VM's current tree. Stop the old collector before the migration runs (`docker compose stop collector`), so nothing writes while sessions are merged, then `docker compose up -d --build`. Afterwards run `psql -X -f ops/check_identity_flap.sql` in the db container (every count must be 0). Roll-back is the restore of the pre-deploy dump (restore.md).

## 4c. Special case: the collector release with the rolling feed archive (T38; issues #63 and #64)
Three things are mandatory for this release. Each one has already cost, or would cost, data.
1. **Stop the collector, then build.** The deploy MUST use `docker compose stop collector` and then `docker compose up -d --build`. The new `archive` volume is created root-owned unless the NEW image (which creates and owns `/archive` for the collector user) is built first; an old or cached image would leave the volume unwritable by the collector and the archive would silently never write (issue #64; the collector keeps polling, so nothing else looks wrong). Never use `docker compose up -d` without `--build` for this release, and never `docker compose restart collector`.
2. **Check the VM clock before deploying** (issue #63): `timedatectl` must show `System clock synchronized: yes` and `NTP service: active`. The collector refuses a snapshot stamped more than `FEED_MAX_FUTURE_SECONDS` (default 120) ahead of the VM clock, so a VM clock more than about 128 s slow makes every poll an error. Fix the clock (`sudo timedatectl set-ntp true`, or ask the owner) before the deploy, not after.
3. **Check the archive after the deploy.** About a minute after the collector starts, `docker compose exec -T collector python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())"` (the health port is not published; 8080 is the default `HEALTH_PORT`) must show an `archive` block with `enabled: true`, `last_write` set to a time within the last few minutes (and `last_write_age_seconds` small), and error counters at 0. A `last_write` of null, or growing `last_write_age_seconds`, means the archive is not writing: check `docker compose logs --tail=50 collector` and the ownership of the `archive` volume. The watchdog also alerts on an old archive.
Order for this release: fresh dump (`docker compose exec backup backup.sh once`), a copy of the VM's tree for rollback, `timedatectl`, `git archive` copy (section 2c), `docker compose stop collector`, `docker compose up -d --build`, `sh ops/smoke.sh`, `python3 ops/check_panels.py`, restart counts 0, the `/health` archive check above, and `ops/check_identity_flap.sql` (three zeros).

## 4d. Special case: the Dependabot image bumps (T48; issue #31)
Status 09-10-2026: DEPLOYED at 13:46 UTC (commit 8905d85) and verified; the steps below are the record of how it was done. The Grafana volume was copied first (a throwaway container with `--memory 256m --memory-swap 256m --cpus 0.5` running `cp -a` from the volume into a new one).
Postgres 16.4 to 16.15 (`db` and `ops/backup`), Grafana 11.2.2 to 11.6.5 and dbmate 2.21.0 to 2.36.0 were tested on throwaway containers, not on the live stack. Deploying them recreates the containers: `docker compose up -d --build` recreates `db` (same major version 16, the `pgdata` volume is reused, about 10 to 30 seconds of database downtime), `grafana` (about a minute; `grafana_data` is upgraded in place on first start, so copy the volume first if rollback matters) and rebuilds `backup` and `migrate`. Do it in a quiet period, take a fresh dump first (`docker compose exec backup backup.sh once`), then run `sh ops/smoke.sh` and `python3 ops/check_panels.py`. Never let the `backup` image tag differ from the `db` image tag.

## 4e. Special case: the scheduled integrity checks (T16, issue #20; owner's go needed)
Status: DEPLOYED 09-10-2026 at 19:13 UTC (see tracker T16); the steps below are the record. Do not repeat any of this without the owner's go (deploy policy, [../TRACKER.md](../TRACKER.md)). What it changes: the watchdog image (new build context `./ops`, new files `integrity.py`, `independent.py`, `invariants.sql`), two read-only mounts on the watchdog (`archive:/archive:ro` and `./config:/config:ro`), three settings (`WATCHDOG_INTEGRITY_INTERVAL_HOURS=72`, `WATCHDOG_CROSSCHECK_INTERVAL_DAYS=14`, `WATCHDOG_CROSSCHECK_SAMPLES=24`, all with defaults in `docker-compose.yml`, so `.env` needs no edit) and three new `SELECT` grants for `watchdog_ro` (`pilot_position`, `flight_summary`, `reference_data_change`). No migration file, no collector change: collector polling is not interrupted.

**Owner decision needed on timing.** The first sweep runs on the first watchdog cycle and will find the 6 minute snapshot gap of the power cut of 09-10-2026 (15:35:57 to 15:41:57 UTC, tracker T51) while it is inside the 3 day window. If deployed before 12-10-2026 15:42 UTC, expect one `WARN data_integrity: 1 of 18 invariants report violations: snapshot gaps over 5 minutes (1)` and a `RECOVERED` up to 6 hours after the gap leaves the window. That is a live proof that the alert path works; deploy after that time for a clean start (no message).

Steps (all from `~/vatpac-statsV2` on the VM, after the `git archive` copy of the tree, section 2c):
1. **Fresh dump and rollback copy.** `docker compose exec backup backup.sh once`; check the new file in `/backups`; copy the current tree (`cp -a ~/vatpac-statsV2 ~/vatpac-statsV2.rollback-<dd-mm>`).
2. **Migrate job first.** `docker compose up -d --build migrate`. It re-applies the roles and the `watchdog_ro` grants and exits 0; nothing restarts. Check: `docker compose ps -a migrate` shows `exited (0)`, and
   ```sh
   echo "SELECT t FROM unnest(ARRAY['pilot_position','flight_summary','reference_data_change']) t WHERE has_table_privilege('watchdog_ro', t, 'SELECT')" |      docker compose exec -T db sh -c 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At'
   ```
   prints all three names. Never run `db/roles.sql` by hand.
3. **Rebuild the watchdog only.** `docker compose up -d --build watchdog`. Only the watchdog is recreated (its status volume is kept, so the schedule state survives).
4. **Checks afterwards.**
   - `docker compose ps`: `watchdog` is `healthy`, `collector` has the same start time as before (not restarted).
   - `docker compose logs --tail=50 watchdog | grep -i integrity`: lines for the invariant sweep and the archive cross-check on the first cycle (the cross-check takes about 25 s).
   - `docker compose exec -T watchdog cat /var/watchdog/watchdog_status.json`: `data_integrity` and `feed_crosscheck` present, `state.integrity.<name>.last_run` set. Expect `feed_crosscheck` ok (24 snapshots match, or fewer if the archive is young).
   - `could not run (InsufficientPrivilege)` means step 2 did not apply the grants: repeat it.
   - `sh ops/smoke.sh` ends `SMOKE PASS`; `python3 ops/check_panels.py` reports 0 errors.
5. **Roll back.** `docker compose up -d --build watchdog` from the previous tree (the extra grants are harmless to leave). To switch the checks off without a rollback, set `WATCHDOG_INTEGRITY_INTERVAL_HOURS=0` and `WATCHDOG_CROSSCHECK_INTERVAL_DAYS=0` in `.env` and `docker compose up -d watchdog`.

Runbook for the checks themselves: [integrity-checks.md](integrity-checks.md).

## 4f. Special case: plain-language Telegram messages (T56, issue #79; owner's go needed)
What it changes: the watchdog image only (new file `ops/watchdog/messages.py`, copied by the Dockerfile; a small change in `watchdog.py`). No migration, no setting, no `.env` edit, no change to the collector, so polling is not interrupted. Alert logic, the log text and the status-file detail strings keep their technical wording (the status file gains a `bad_since` entry per active alert, used for "the alert was active for ...").

Steps (from `~/vatpac-statsV2` on the VM, after the `git archive` copy of the tree, section 2c):
1. **Rollback copy.** `cp -a ~/vatpac-statsV2 ~/vatpac-statsV2.rollback-<dd-mm>` before copying the new tree over it.
2. **Rebuild the watchdog only.** `docker compose up -d --build --no-deps watchdog`. `--no-deps` keeps Compose from touching anything the watchdog depends on; only the watchdog is recreated, its status volume is kept, the collector is not touched.
3. **Checks afterwards.**
   - `docker compose ps`: `watchdog` is `healthy`; `collector` has the same start time as before.
   - `docker compose logs --tail=30 watchdog`: no `Traceback`, no `could not format the alert`.
   - `docker compose exec -T watchdog python -c "import messages; print(messages.telegram_text('WARN disk: / 82% used'))"` prints a `VATPAC Stats: heads up` message with a `Technical:` last line (prints to the terminal only; sends nothing).
   - The status file kept its state across the rebuild. Before the rebuild run `docker compose exec -T watchdog python -c "import json; s=json.load(open('/var/watchdog/watchdog_status.json'))['state']; print(sorted(s), s.get('alerts'))"` and note the output; after the rebuild run it again: the same keys (`alerts`, `first_run`, `integrity`, `known_near_miss`) and the same active alerts must be there (for example `data_integrity` while the power-cut gap is in the window), so no duplicate WARN is sent. `bad_since` is new and appears after the first cycle.
   - `sh ops/smoke.sh` ends `SMOKE PASS`.
4. **Live proof.** The next real alert arrives in the new form and the owner confirms it. Do not send a test message to the owner's chat without asking.
5. **Roll back.** Restore the previous tree and `docker compose up -d --build --no-deps watchdog`. The extra `bad_since` entry in the status file is ignored by the old code.

## 5. Network exposure and firewall
Only Grafana is published, and by default **only on loopback**: `127.0.0.1:${GRAFANA_PORT:-3300}`. The database is not published. To reach Grafana from the LAN, opt in explicitly in `.env`:
```sh
GRAFANA_BIND=<VM_LAN_IP>    # the VM's LAN address (preferred), or 0.0.0.0 for every interface
GRAFANA_PORT=3300
GRAFANA_COOKIE_SECURE=false  # keep false for plain http on a LAN; set true only behind https/TLS
```
then `docker compose up -d grafana`. Prefer a specific LAN IP over `0.0.0.0`. Combine with the firewall:
```sh
sudo ufw default deny incoming
sudo ufw allow OpenSSH
sudo ufw allow from <your LAN CIDR> to any port 3300 proto tcp
sudo ufw enable
```
Docker publishes ports by editing iptables directly and can bypass ufw. If the VM has a public interface, bind Grafana to the LAN address instead of `0.0.0.0`, or put the VM behind a cloud firewall. Do not expose Grafana directly to the internet. Any tunnel or reverse proxy (for example Cloudflare) is set up externally by the owner and is not configured in this repo; real pilot and controller names are visible on the dashboards, so put a login in front of any public exposure. When the proxy terminates TLS, set `GRAFANA_ROOT_URL=https://...` and `GRAFANA_COOKIE_SECURE=true`.

Grafana is also hardened with: external snapshots disabled, Gravatar disabled, viewers cannot edit, anonymous access and sign-up off, all Linux capabilities dropped, `no-new-privileges`. (A read-only root filesystem for Grafana is not enabled; it has not been validated.)

## 6. Verify
```sh
make smoke                                   # PASS/FAIL per check, non-zero on failure (without make: sh ops/smoke.sh)
docker compose logs --tail=50 collector      # polls every 30 s (POLL_INTERVAL_SECONDS)
docker compose exec backup backup.sh once    # first backup, then `ls` the volume (see restore.md)
```
`python3 ops/check_panels.py` runs every dashboard query through Grafana's own API; expect `0 errors` (empty panels are fine while data is young). It reads `.env` and never prints the password.

Open `http://localhost:3300` on the VM (or `http://<GRAFANA_BIND>:3300` from the LAN), log in as the admin user from `.env`. Charts start empty (no backfill).

## 7. Reboot test
`sudo reboot`; after boot `docker compose ps` should show everything back (restart: unless-stopped).

## Updating
Before deploying read [daily-checks.md](daily-checks.md): **a redeploy restarts the collector, which costs about 45 seconds of snapshots, and breaks a 24 hour soak that is in progress.** Do not redeploy while a soak is running unless it is necessary, and say so if you do.

With git on the host:
```sh
git fetch --tags && git checkout <new tag>
sh ops/check_env.sh                # new releases may add secrets (see .env.example); add any blank ones
docker compose up -d --build     # migrate runs first; collector waits for it
make smoke
```
Without git (production VM), from the PC:
```sh
git archive --format=tar HEAD | ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'
ssh vatpac-v3 'cd ~/vatpac-statsV2 && sh ops/check_env.sh && docker compose up -d --build'
ssh vatpac-v3 'cd ~/vatpac-statsV2 && sh ops/smoke.sh && python3 ops/check_panels.py'
```
`docker compose up -d --build` rebuilds the images whose inputs changed, recreates only the services whose image or configuration changed, and starts the `migrate` job before the services that wait for it. Confirm the migrations ran: `docker compose ps -a migrate` shows `exited (0)`, and `docker compose logs --tail=20 migrate` ends with `migrate: done`. If a new migration did not run, `docker compose up migrate` runs the job on its own (it is idempotent).

## What a change needs (rebuild, restart or reload)

| What changed | What to do | Interrupts polling? |
|---|---|---|
| `collector/src/**`, `collector/requirements.txt`, `collector/Dockerfile` | `docker compose up -d --build` (rebuilds the collector image and recreates the container) | Yes, about 45 s |
| `db/migrations/*.sql` (new numbered file; never edit an applied one) | `docker compose up -d`: `migrate` applies it (`./db` is mounted, no rebuild); if it did not run, `docker compose up migrate`. Then `sh ops/smoke.sh` | No |
| `db/roles.sql` (mounted like the migrations) | `docker compose up -d`: `migrate` re-applies the roles and grants. **Never run this file by hand against the live database**: it resets the live role passwords to whatever you pass | No |
| `ops/migrate/**` (the migrate image itself) | `docker compose up -d --build` | No |
| `grafana/dashboards/**/*.json` | Nothing to restart: the provider rescans the read-only mount every 30 seconds. Reload the browser page | No |
| `grafana/provisioning/**` (datasource or provider settings) | `docker compose restart grafana` (provisioning is read at start-up) | No |
| `config/*.json`, `config/*.txt` (polygon, whitelist) | No rebuild: `docker compose kill -s HUP collector` (see [update-reference-files.md](update-reference-files.md)) | No |
| `.env` (a password) | `docker compose up -d` (the `migrate` job runs `ALTER ROLE`; `POSTGRES_PASSWORD` and `GRAFANA_ADMIN_PASSWORD` only apply at first start, see section 3) | The services using the changed value are recreated |
| `.env` (a collector setting) | `docker compose up -d collector` | Yes, about 45 s |
| `docker-compose.yml` | `docker compose up -d` (recreates the services whose definition changed; a new named volume, such as `archive`, is created by the same command) | Only for those services |
| `ops/postgres/postgresql.conf` | `docker compose restart db` | Yes, the database restarts; the collector retries |
| `ops/watchdog/**`, `ops/audit/independent.py`, `ops/audit/invariants.sql` (the watchdog image copies the last two), `ops/backup/**` | `docker compose up -d --build watchdog` or `backup` | No |
| `ops/*.sh`, `ops/*.py`, `grafana/*.py`, `docs/`, `tests/`, `Makefile` | Nothing: they run from the host or are only documentation | No |

## Roll back
Code, dashboards and configuration go back by deploying the previous commit; the database does not go back by itself.
1. Note the commit that was deployed before (`git log --oneline`), then deploy it: `git archive --format=tar <previous commit> | ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'`, then `docker compose up -d --build` on the VM.
2. Delete files the newer commit added, because `tar -x` leaves them behind. In particular delete a newer migration file you do not want applied **before** running `docker compose up -d`, or `migrate` will apply it.
3. Migrations are forward-only. A migration that already ran stays applied after a code rollback; additive migrations (a new view, an index) are harmless to older code. If a migration must be undone, add a new corrective numbered migration, or restore from backup (`restore.md`); never edit an applied migration.
4. Verify with `sh ops/smoke.sh` and `python3 ops/check_panels.py`.

With git on the host, check out the previous tag instead and run `docker compose up -d --build`.

## Image pinning
Third-party images (`postgres`, `grafana`, `dbmate`, `python`) are pinned by exact version tag. Digest pinning (`image:tag@sha256:...`) is the stricter policy; add digests when you can resolve them with `docker buildx imagetools inspect <image>:<tag>`. Dependabot (docker and docker-compose ecosystems) proposes bumps.
