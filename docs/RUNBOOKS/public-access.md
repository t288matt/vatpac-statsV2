# Public read-only dashboards (Cloudflare Tunnel)

Audience: the owner. Purpose: show the dashboards to anyone on the internet at `https://vatpac.mpengor.com`, read-only, without opening a port on the router. The proxy and the tunnel run on the owner's `docker` machine; this stack only provides Grafana's public dashboards and a script that manages the links.

```
visitor -> Cloudflare (rate limit) -> tunnel (cloudflared, docker machine) -> Traefik (docker machine)
        -> http://192.168.1.222:3300 (Grafana on the v3 VM, LAN address)
```

## What visitors can and cannot do
- Each of the 10 dashboards has its own public link (`/public-dashboards/<token>`). Grafana has no index of them; `python3 ops/grafana_public.py --list --public-url https://vatpac.mpengor.com` prints them.
- A public dashboard runs **only the queries saved on the dashboard**. Visitors cannot send their own SQL, cannot change the time range (each dashboard keeps its own default: 24 hours, 7, 30, 90 or 180 days), cannot edit, and cannot use Explore. Without a login every other API call (`/api/ds/query`, the dashboard and datasource lists) answers 401 (checked 11-10-2026 on a copy of the live Grafana data; SQL sent in the body of a public panel request is ignored).
- Panels read views only, through the `grafana_ro` role: SELECT only, 30 s statement limit, at most 10 connections (`db/roles.sql`). The collector uses its own role and connections.
- Pilot and controller names and CIDs are visible. They are public data. The Collector health dashboard is public too (owner decision 11-10-2026).
- The Grafana login page (`/login`) and the admin pages stay reachable from the internet: **owner decision 11-10-2026, no Cloudflare rule blocks them.** The admin account is protected by a long, unique password and Grafana's lock-out after failed attempts. A WAF rule that blocks the admin paths except from the owner's IP is the recommended extra step (section 3, optional).
- Anonymous access stays off. Two settings are never to be changed for this: `GF_AUTH_ANONYMOUS_ENABLED` (false) and `GF_USERS_VIEWERS_CAN_EDIT` (false). Turning anonymous access on would let any visitor send their own SQL.

## 1. Create the public links (on the VM, after Grafana is running)
```
cd ~/vatpac-statsV2
python3 ops/grafana_public.py --public-url https://vatpac.mpengor.com
```
It reads the admin login from `.env` (never printed), creates a link for every provisioned dashboard with a fixed time range and no annotations, fixes a link whose settings differ, and prints the 10 URLs. It is safe to run again, and should be run after a new dashboard is added. `--list` changes nothing. `--remove` removes every link, which stops public access at once.

The links live in Grafana's own database (the `grafana_data` volume): they survive restarts and deploys but not a wiped volume; run the script again then (the URLs change). Keep the list of links somewhere you can find it.

Settings in `.env` for the public name: `GRAFANA_ROOT_URL=https://vatpac.mpengor.com/`, `GRAFANA_COOKIE_SECURE=true`, and `GRAFANA_BIND` the VM's LAN address (never `0.0.0.0`; never forward the port on the router). Apply with `docker compose up -d grafana`. After `GRAFANA_COOKIE_SECURE=true` the admin login only works over https, so use the public name.

The admin password is only applied on the first start. Rotate it with `docker compose exec grafana grafana cli admin reset-admin-password '<new>'`. Use a long random unique password; never paste it into a chat, a document or the repository.

## 2. Traefik on the docker machine (file provider)
Traefik reads Docker labels of containers on its own host only, so a route to a container on another machine is a file. Enable the file provider once (`--providers.file.directory=/dynamic --providers.file.watch=true` and mount the folder), then add `/dynamic/vatpac.yml`:
```yaml
http:
  routers:
    vatpac-secure:
      entryPoints: [https]
      rule: Host(`vatpac.mpengor.com`)
      tls: {}
      service: vatpac
  services:
    vatpac:
      loadBalancer:
        servers:
          - url: http://192.168.1.222:3300
```
The `https` entry point name matches the Jellyfin example; use your own entry point name if it differs.

## 3. Cloudflare Tunnel
1. Zero Trust, Networks, Tunnels: open the existing tunnel (or create one) and add a public hostname: `vatpac.mpengor.com`, service type `HTTPS`, URL `<Traefik address on the docker machine>:443`. Under additional settings set the origin server name to `vatpac.mpengor.com` (and "No TLS Verify" only if your Traefik certificate does not cover that name). Cloudflare creates the DNS record.
2. Rate limiting rule (Security, WAF, Rate limiting rules) on `http.host eq "vatpac.mpengor.com"`, for example 100 requests per minute per IP, action Block for 10 minutes. Turn on Bot Fight Mode.
3. Optional, recommended: a custom rule that blocks the admin paths for everyone except the owner's IP. Action Block, expression:
   `http.host eq "vatpac.mpengor.com" and (starts_with(http.request.uri.path, "/login") or starts_with(http.request.uri.path, "/admin") or starts_with(http.request.uri.path, "/profile") or starts_with(http.request.uri.path, "/org") or starts_with(http.request.uri.path, "/datasources") or starts_with(http.request.uri.path, "/api/admin") or starts_with(http.request.uri.path, "/api/user") or starts_with(http.request.uri.path, "/api/org") or starts_with(http.request.uri.path, "/api/datasources")) and not ip.src eq <owner IP>`
   Do not block `/public-dashboards/*`, `/api/public/*` or the static files: the public pages need them. If the owner's home IP changes, update the rule.

## 4. Check it works (from a phone on mobile data, not the home network)
- A public link opens without a login and every panel loads; there is no time picker and no edit button.
- `https://vatpac.mpengor.com/api/search` and `/api/ds/query` answer 401.
- `https://vatpac.mpengor.com/login` shows the login page (or the Cloudflare block page if the optional rule is on).
- On the VM: `docker compose logs grafana` shows no errors; `sh ops/smoke.sh` and `python3 ops/check_panels.py` pass.

## Roll back
- Stop public access at once: `python3 ops/grafana_public.py --remove` (or remove the public hostname from the tunnel). Either is enough on its own.
- The Grafana 12.4 upgrade (11.6.5 before): the data volume is migrated forward on the first start of the newer version and an older version may not read it. Take a copy before upgrading: `docker run --rm -v vatpac_grafana_data:/from:ro -v $PWD:/to alpine tar czf /to/grafana_data_pre_12.tgz -C /from .`. To go back, stop Grafana, restore that copy into the volume and pin `grafana/grafana-oss:11.6.5` again.

## Things to watch
- Dashboard queries run on the production database for anyone who loads a page. The `grafana_ro` limits and the Cloudflare rate limit bound the load, but a crowd can slow the dashboards (never the collector). Check `docker stats` and `docker compose logs db` if the dashboards feel slow.
- Keep Grafana up to date: it is exposed to the internet. The image is pinned in `docker-compose.yml`; Dependabot ignores major versions. 11.6 is out of support (last patch 03-06-2026), so the line in use is 12.4.
