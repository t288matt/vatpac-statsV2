# Runbook: deploy to the Linux VM

Last reviewed: 07-10-2026. Target: always-on Linux VM (Debian/Ubuntu), 2 vCPU, 4 GB RAM, 20 GB+ SSD.

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
```sh
cp .env.example .env
chmod 600 .env
```
Edit `.env`: set every `change-me` password. Use URL-safe values (they sit inside connection URLs): `openssl rand -hex 24`. Set `GRAFANA_ROOT_URL`, and `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` if you want alerts. Never commit `.env`.

## 4. Start
```sh
docker compose up -d --build
docker compose ps            # migrate shows "exited (0)"; everything else healthy within about 2 minutes
```

## 5. Firewall
Only Grafana (`GRAFANA_PORT`, default 3300) is published. The database is not published.
```sh
sudo ufw default deny incoming
sudo ufw allow OpenSSH
sudo ufw allow from <your LAN CIDR> to any port 3300 proto tcp
sudo ufw enable
```
Docker publishes ports by editing iptables directly and can bypass ufw. If the VM has a public interface, bind Grafana to the LAN address instead, or put the VM behind a cloud firewall. Do not expose Grafana directly to the internet. Any tunnel or reverse proxy (for example Cloudflare) is set up externally by the owner and is not configured in this repo; real pilot and controller names are visible on the dashboards, so put a login in front of any public exposure.

## 6. Verify
```sh
make smoke                                   # PASS/FAIL per check, non-zero on failure
docker compose logs --tail=50 collector      # polls every ~15 s
docker compose exec backup backup.sh once    # first backup, then `ls` the volume (see restore.md)
```
Open `http://<vm>:3300`, log in as the admin user from `.env`. Charts start empty (no backfill).

## 7. Reboot test
`sudo reboot`; after boot `docker compose ps` should show everything back (restart: unless-stopped).

## Updating
```sh
git fetch --tags && git checkout <new tag>
docker compose up -d --build     # migrate runs first; collector waits for it
make smoke
```
Roll back by checking out the previous tag. Migrations are forward-only: if one must be undone, restore from backup (`restore.md`).
