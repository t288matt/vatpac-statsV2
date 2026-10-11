# Public read-only dashboards (Cloudflare Tunnel)

Audience: the owner. Purpose: show the dashboards to anyone on the internet at `https://vatpac.mpengor.com`, read-only, without opening a port on the router. The proxy and the tunnel run on the owner's `docker` machine; this stack only provides Grafana's public dashboards and a script that manages the links.

```
visitor -> Cloudflare (rate limit) -> tunnel (cloudflared, docker machine) -> Traefik (docker machine)
        -> vatpac-proxy (helper container on the docker machine) -> http://192.168.1.222:3300 (Grafana on the v3 VM, LAN address)
```

## What visitors can and cannot do
- Each of the 10 dashboards has its own public link (`/public-dashboards/<token>`). Grafana has no index of them; `python3 ops/grafana_public.py --list --public-url https://vatpac.mpengor.com` prints them.
- A public dashboard runs **only the queries saved on the dashboard**. Visitors cannot send their own SQL, cannot change the time range (each dashboard keeps its own default: 24 hours, 7, 30, 90 or 180 days), cannot edit, and cannot use Explore. Without a login every other API call (`/api/ds/query`, the dashboard and datasource lists) answers 401 (checked 11-10-2026 on a copy of the live Grafana data; SQL sent in the body of a public panel request is ignored).
- A public page exposes the dashboard definition, including the SQL text of each panel, the view names and the datasource name. The views hold no secrets, but treat the SQL as public.
- Panels read views only, through the `grafana_ro` role: SELECT only, 30 s statement limit, at most 10 connections (`db/roles.sql`). The collector uses its own role and connections.
- Pilot and controller names and CIDs are visible. They are public data. The Collector health dashboard is public too (owner decision 11-10-2026).
- The Grafana login page (`/login`) and the admin pages stay reachable from the internet: **owner decision 11-10-2026, no Cloudflare rule blocks them.** The admin account is protected by a long, unique password and Grafana's lock-out after failed attempts. A WAF rule that blocks the admin paths except from the owner's IP is the recommended extra step (section 3, optional).
- Anonymous access stays off. Three settings are never to be changed for this: `GF_AUTH_ANONYMOUS_ENABLED` (false), `GF_USERS_VIEWERS_CAN_EDIT` (false) and `GF_PUBLIC_DASHBOARDS_ENABLED` (true, the default: the links need it). Turning anonymous access on would let any visitor send their own SQL.

## 1. Create the public links (on the VM, after Grafana is running)
```
cd ~/vatpac-statsV2
python3 ops/grafana_public.py --public-url https://vatpac.mpengor.com
```
The script finds Grafana at `GRAFANA_BIND` and `GRAFANA_PORT` from `.env` (on this VM the LAN address, not 127.0.0.1); wait about a minute after a Grafana restart.
It reads the admin login from `.env` (never printed), creates a link for every provisioned dashboard with a fixed time range and no annotations, fixes a link whose settings differ, and prints the 10 URLs. It is safe to run again, and should be run after a new dashboard is added. `--list` changes nothing. `--remove` removes every link, which stops public access at once.

The links live in Grafana's own database (the `grafana_data` volume): they survive restarts and deploys but not a wiped volume; run the script again then (the URLs change). Keep the list of links somewhere you can find it.

Settings in `.env` for the public name: `GRAFANA_ROOT_URL=https://vatpac.mpengor.com/`, `GRAFANA_COOKIE_SECURE=true`, and `GRAFANA_BIND` the VM's LAN address (never `0.0.0.0`; never forward the port on the router). Apply with `docker compose up -d grafana`. After `GRAFANA_COOKIE_SECURE=true` the admin login only works over https, so use the public name.

The admin password is only applied on the first start. Rotate it with `docker compose exec grafana grafana cli admin reset-admin-password '<new>'`. Use a long random unique password; never paste it into a chat, a document or the repository.

## 2. Traefik on the docker machine (a helper container with labels)
Traefik reads Docker labels of containers on its own machine only, and Grafana runs on the v3 VM. So the docker machine (192.168.1.145) runs a tiny forwarder container, `vatpac-proxy`, that carries the usual labels (the same pattern as the other apps) and passes everything on to Grafana at `192.168.1.222:3300`. Traefik's own configuration is not changed and Traefik is not restarted. The folder is `~/vatpac-proxy` with this `docker-compose.yml` (and a README for whoever looks at that machine):
```yaml
services:
  vatpac-proxy:
    image: alpine/socat
    container_name: vatpac-proxy
    # forwards everything it receives on port 3000 to Grafana on the v3 VM (LAN address and port)
    command: tcp-listen:3000,fork,reuseaddr tcp:192.168.1.222:3300
    restart: unless-stopped
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.vatpac.entrypoints=http"
      - "traefik.http.routers.vatpac.rule=Host(`vatpac.mpengor.com`)"
      - "traefik.http.middlewares.vatpac-https-redirect.redirectscheme.scheme=https"
      - "traefik.http.routers.vatpac.middlewares=vatpac-https-redirect"
      - "traefik.http.routers.vatpac-secure.entrypoints=https"
      - "traefik.http.routers.vatpac-secure.rule=Host(`vatpac.mpengor.com`)"
      - "traefik.http.routers.vatpac-secure.tls=true"
      - "traefik.http.routers.vatpac-secure.service=vatpac"
      - "traefik.http.services.vatpac.loadbalancer.server.port=3000"
      - "traefik.docker.network=proxy"
    networks:
      proxy:
    cap_drop:
      - ALL
    read_only: true
    security_opt:
      - no-new-privileges:true

networks:
  proxy:
    external: true
```
Start it with `docker compose up -d` in that folder. Check it on the docker machine: `curl -sk --resolve vatpac.mpengor.com:443:127.0.0.1 https://vatpac.mpengor.com/api/health` answers with Grafana's health (`"database": "ok"`). It has to keep running: if it stops, the public address shows an error and nothing else is affected (`docker compose stop` there is the gentlest way to switch public access off). The VM's address must stay 192.168.1.222 (reserve it on the router). Traefik on that machine does not use its `config.yaml` (the file provider is switched off in `traefik.yaml`), so nothing is added there.

## 3. Cloudflare Tunnel
1. Zero Trust, Networks, Tunnels: open the existing tunnel (or create one) and add a public hostname: `vatpac.mpengor.com`, service type `HTTPS`, URL `<Traefik address on the docker machine>:443`. Under additional settings set the origin server name to `vatpac.mpengor.com` (and "No TLS Verify" only if your Traefik certificate does not cover that name). Cloudflare creates the DNS record.
2. Rate limiting rule (Security, WAF, Rate limiting rules) on `http.host eq "vatpac.mpengor.com"`, for example 100 requests per minute per IP, action Block for 10 minutes. Turn on Bot Fight Mode.
3. Optional, recommended: a custom rule that blocks the admin paths for everyone except the owner's IP. Action Block, expression:
   `http.host eq "vatpac.mpengor.com" and (starts_with(http.request.uri.path, "/login") or starts_with(http.request.uri.path, "/admin") or starts_with(http.request.uri.path, "/profile") or starts_with(http.request.uri.path, "/org") or starts_with(http.request.uri.path, "/datasources") or starts_with(http.request.uri.path, "/api/admin") or starts_with(http.request.uri.path, "/api/user") or starts_with(http.request.uri.path, "/api/org") or starts_with(http.request.uri.path, "/api/datasources") or starts_with(http.request.uri.path, "/api/login")) and not ip.src eq <owner IP>`
   Do not block `/public-dashboards/*`, `/api/public/*` or the static files: the public pages need them. If the owner's home IP changes, update the rule.

## 4. Check it works (from a phone on mobile data, not the home network)
- A public link opens without a login and every panel loads; there is no time picker and no edit button.
- `https://vatpac.mpengor.com/api/search` and `/api/ds/query` answer 401.
- `https://vatpac.mpengor.com/login` shows the login page (or the Cloudflare block page if the optional rule is on).
- On the VM: `docker compose logs grafana` shows no errors; `sh ops/smoke.sh` and `python3 ops/check_panels.py` pass.

## Roll back
- Stop public access at once: `python3 ops/grafana_public.py --remove` (or remove the public hostname from the tunnel). Either is enough on its own.
- The Grafana 12.4 upgrade (11.6.5 before): the data volume is migrated forward on the first start of the newer version and an older version may not read it. Take a copy before upgrading (confirm the volume name with `docker volume ls`): `docker run --rm -v vatpac_grafana_data:/from:ro -v $PWD:/to alpine tar czf /to/grafana_data_pre_12.tgz -C /from .`. To go back, stop Grafana, restore that copy into the volume and pin `grafana/grafana-oss:11.6.5` again.

## Things to watch
- Dashboard queries run on the production database for anyone who loads a page. The `grafana_ro` limits and the Cloudflare rate limit bound the load, but a crowd can slow the dashboards (never the collector). Check `docker stats` and `docker compose logs db` if the dashboards feel slow.
- Keep Grafana up to date: it is exposed to the internet. The image is pinned in `docker-compose.yml`; Dependabot ignores major versions. The 11.6 line ended with 11.6.15 (03-06-2026), so the line in use is 12.4; check Grafana's release notes for security fixes at least monthly.
