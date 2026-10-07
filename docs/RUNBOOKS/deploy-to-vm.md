# Runbook: deploy to the Linux VM

Last reviewed: 07-10-2026. Target: always-on Linux VM (Debian/Ubuntu), 2 vCPU, 4 GB RAM, **40 GB+ SSD**.

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
make smoke                                   # PASS/FAIL per check, non-zero on failure
docker compose logs --tail=50 collector      # polls every ~15 s
docker compose exec backup backup.sh once    # first backup, then `ls` the volume (see restore.md)
```
Open `http://localhost:3300` on the VM (or `http://<GRAFANA_BIND>:3300` from the LAN), log in as the admin user from `.env`. Charts start empty (no backfill).

## 7. Reboot test
`sudo reboot`; after boot `docker compose ps` should show everything back (restart: unless-stopped).

## Updating
```sh
git fetch --tags && git checkout <new tag>
sh ops/check_env.sh                # new releases may add secrets (see .env.example); add any blank ones
docker compose up -d --build     # migrate runs first; collector waits for it
make smoke
```
Roll back by checking out the previous tag. Migrations are forward-only: if one must be undone, restore from backup (`restore.md`).

## Image pinning
Third-party images (`postgres`, `grafana`, `dbmate`, `python`) are pinned by exact version tag. Digest pinning (`image:tag@sha256:...`) is the stricter policy; add digests when you can resolve them with `docker buildx imagetools inspect <image>:<tag>`. Dependabot (docker and docker-compose ecosystems) proposes bumps.
