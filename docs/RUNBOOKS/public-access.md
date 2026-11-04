# Public read-only dashboards (Cloudflare Tunnel)

Last reviewed 11-10-2026 against commit b3189ad and the VM. Audience: the owner. Purpose: show the dashboards to anyone on the internet at `https://vpac.mpengor.com`, read-only, without opening a port on the router. The hostname `vpac.mpengor.com` is the owner's choice (changed from `vatpac.mpengor.com` so it does not appear in searches for "vatpac"; confirmed by the owner). DNS, Cloudflare and the Traefik machine are outside this repository and cannot be checked from it or from the VM. The proxy and the tunnel run on the owner's `docker` machine; this stack only provides Grafana's public dashboards and a script that manages the links.

```
visitor -> Cloudflare (rate limit, Bot Fight Mode) -> Cloudflare Tunnel (named Home; connector on the Traefik machine) -> Traefik (docker machine)
        -> vatpac-proxy (helper container on the docker machine) -> http://<VM_LAN_IP>:3300 (Grafana on the v3 VM, LAN address)
```

## What visitors can and cannot do
- Each of the 12 dashboards has its own public link (`/public-dashboards/<token>`). Grafana has no index of them; `python3 ops/grafana_public.py --list --public-url https://vpac.mpengor.com` prints them. Each also has a short address (section 1, table).
- A public dashboard runs **only the queries saved on the dashboard**. Visitors cannot send their own SQL, cannot change the time range (each dashboard keeps its own fixed default: 24 hours, 7, 30, 90 or 180 days), cannot edit, and cannot use Explore. Without a login every other API call (`/api/ds/query`, the dashboard and datasource lists) answers 401 (checked 11-10-2026 on a copy of the live Grafana data; SQL sent in the body of a public panel request is ignored).
- The Departure board panel on "Pilots: online now" is fixed to YSSY: public dashboards do not fill text-box variables, so the airport box was removed. The hidden `go_live` variable is a constant (07-10-2026 21:38 UTC), because public dashboards cannot run variable queries: as a query it showed a blank "Data since" and an "Invalid panel id" toast (fixed 14-10-2026). A public page exposes the dashboard definition, including the SQL text of each panel, the view names and the datasource name. The views hold no secrets, but treat the SQL as public.
- Panels read views only, through the `grafana_ro` role: SELECT only, 30 s statement limit, at most 10 connections (`db/roles.sql`). The collector uses its own role and connections.
- Pilot and controller names and CIDs are visible. They are public data. The Collector health dashboard is public too (owner decision 11-10-2026).
- The Grafana login page (`/login`) and the admin pages stay reachable from the internet: **owner decision 11-10-2026, no Cloudflare rule blocks them.** The admin account is protected by a long, unique password and Grafana's lock-out after failed attempts. The optional WAF rule that would block the admin paths (section 3, step 3) was offered and **declined by the owner**.
- Anonymous access stays off. Three settings are never to be changed for this: `GF_AUTH_ANONYMOUS_ENABLED` (false), `GF_USERS_VIEWERS_CAN_EDIT` (false) and `GF_PUBLIC_DASHBOARDS_ENABLED` (true, the default: the links need it). Turning anonymous access on would let any visitor send their own SQL.

## 1. Create the public links (on the VM, after Grafana is running)
```
cd ~/vatpac-statsV2
python3 ops/grafana_public.py --public-url https://vpac.mpengor.com
```
The script finds Grafana at `GRAFANA_BIND` and `GRAFANA_PORT` from `.env` (on this VM the LAN address, not 127.0.0.1); wait about a minute after a Grafana restart.
It finds Grafana at `GRAFANA_BIND` and `GRAFANA_PORT` from `.env` (on this VM the LAN address). It is on the VM tree since commit b3189ad (11-10-2026, 20:35 UTC), where it replaced the 10 links once with the stable ones. It reads the admin login from `.env` (never printed), creates a link for every provisioned dashboard with a fixed time range and no annotations, fixes a link whose settings differ, and prints the 10 URLs. It is safe to run again, and should be run after a new dashboard is added. `--list` changes nothing. `--remove` removes every link, which stops public access at once.

The links live in Grafana's own database (the `grafana_data` volume): they survive restarts and deploys. After a wiped volume, run the script again: the tokens are stable, so the same links come back. 

**Rebuilding the public links after the VM is rebuilt (issue 173; the checkout and registry steps were run on 14-10-2026, a whole-VM rebuild has not been).** Get the code as a git checkout (deploy-to-vm.md section 2d, or restore.md "Rebuilding a whole VM"), start the stack in either mode (local build, or registry images after `docker login ghcr.io`), wait for Grafana to be healthy, then run the script above from the checkout. The script is part of the tree, not of an image, so a checkout of the live commit is enough. A registry deploy does not touch the `grafana_data` volume or the links.

### Short addresses
| Short address | Dashboard |
|---|---|
| `/live` | Pilots: online now |
| `/traffic` | Pilots: traffic and aircraft |
| `/new-pilots` | Pilots: onboarding and lapsed pilots |
| `/origins` | Demographics: pilot origins and cohorts |
| `/atc` | ATC: controllers |
| `/atc-retention` | ATC: retention and reactivation |
| `/atc-hours` | ATC: position hours |
| `/pilots` | Pilots: records and totals |
| `/monday` | Events: Monday event |
| `/military` | Military: aircraft and routes |
| `/monthly` | Management: people online |
| `/health` | Monitoring: collector and data freshness |

Each short address answers a redirect (302) to `https://vpac.mpengor.com/public-dashboards/<token>`, and the dashboard then loads; the address bar shows the long address afterwards (Grafana accepts only a 32-hex-character token in its own address; words, short hex and dashes are rejected). The token of a dashboard is fixed: the first 32 hex characters of sha256 of `vatpac-public:` plus the dashboard uid, so the long links do not change when the links are recreated. `--list` prints each link with its short name.

**Add or rename a short address.** (1) Edit `SHORT_NAMES` in `ops/grafana_public.py` (a test requires every provisioned dashboard to have one). (2) Run `python3 ops/grafana_public.py --traefik-labels --host vpac.mpengor.com` on the VM. (3) On the Traefik machine paste the printed block into the `labels:` list of `vatpac-proxy` in `~/vatpac-proxy/docker-compose.yml` (replacing the old short-name block), run `docker compose up -d` there, and update the README in that folder (appendix). (4) Keep the owner's private copies of those files in step. Without the labels the short address answers 404 and the long link still works.


Settings in `.env`: production has `GRAFANA_BIND=0.0.0.0` since 15-10-2026 (it was the LAN address <VM_LAN_IP>; Docker could start before DHCP had given that address, the bind failed and Grafana stayed down for 8 hours on 14-10-2026, issue 164; the LAN and public addresses still work), `GRAFANA_PORT=3300`, `GRAFANA_ROOT_URL=http://<VM_LAN_IP>:3300/` and `GRAFANA_COOKIE_SECURE=false` (public pages work with these; the admin login works over the LAN). For admin login through the public name instead, set `GRAFANA_ROOT_URL=https://vpac.mpengor.com/` and `GRAFANA_COOKIE_SECURE=true`. `GRAFANA_BIND` must be the VM's LAN address (never `0.0.0.0`; never forward the port on the router). Apply with `docker compose up -d grafana`. After `GRAFANA_COOKIE_SECURE=true` the admin login only works over https, so use the public name.

The admin password is only applied on the first start. Rotate it with `docker compose exec grafana grafana cli admin reset-admin-password '<new>'`. Use a long random unique password; never paste it into a chat, a document or the repository.

## 2. The Traefik machine (<TRAEFIK_HOST_IP>)
**Outside this repository.** Everything on this machine, and Cloudflare (the tunnel named Home, the hostname route, the rate limit rule, Bot Fight Mode), cannot be verified from the VM or from this repository; this section is the written record. The owner keeps private copies of the compose file and README (`docker-compose.yml`, `README.md`) on his own PC; they may still say `vatpac.mpengor.com` (the hostname changed to `vpac.mpengor.com`), so update them and the machine to match this page.

What runs there for us: only the folder `~/vatpac-proxy`, one container `vatpac-proxy` (image `alpine/socat`, command `tcp-listen:3000,fork,reuseaddr tcp:<VM_LAN_IP>:3300`, `read_only`, `cap_drop: ALL`, `no-new-privileges`, no published ports, joins the external Docker network `proxy`, `restart: unless-stopped`). Traefik's own configuration is unchanged and Traefik was not restarted; that Traefik does NOT use `config.yaml` (the file provider is commented out in `traefik.yaml`). Only the public address depends on this container.

How to check, stop, start and troubleshoot: see the README in the appendix. If the machine is replaced, rebuild three things: the folder with the compose file below and its README, the Docker network `proxy`, and the Cloudflare hostname route to `https://<that machine>:443`.

### The compose file
Traefik reads Docker labels of containers on its own machine only, and Grafana runs on the v3 VM. So the docker machine (<TRAEFIK_HOST_IP>) runs a tiny forwarder container, `vatpac-proxy`, that carries the usual labels (the same pattern as the other apps) and passes everything on to Grafana at `<VM_LAN_IP>:3300`. Traefik's own configuration is not changed and Traefik is not restarted. The folder is `~/vatpac-proxy` with this `docker-compose.yml` (and a README for whoever looks at that machine):
```yaml
services:
  vatpac-proxy:
    image: alpine/socat
    container_name: vatpac-proxy
    # forwards everything it receives on port 3000 to Grafana on the v3 VM (LAN address and port)
    command: tcp-listen:3000,fork,reuseaddr tcp:<VM_LAN_IP>:3300
    restart: unless-stopped
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.vatpac.entrypoints=http"
      - "traefik.http.routers.vatpac.rule=Host(`vpac.mpengor.com`)"
      - "traefik.http.middlewares.vatpac-https-redirect.redirectscheme.scheme=https"
      - "traefik.http.routers.vatpac.middlewares=vatpac-https-redirect"
      - "traefik.http.routers.vatpac-secure.entrypoints=https"
      - "traefik.http.routers.vatpac-secure.rule=Host(`vpac.mpengor.com`)"
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
Start it with `docker compose up -d` in that folder. Check it on the docker machine: `curl -sk --resolve vpac.mpengor.com:443:127.0.0.1 https://vpac.mpengor.com/api/health` answers with Grafana's health (`"database": "ok"`). It has to keep running: if it stops, the public address shows an error and nothing else is affected (`docker compose stop` there is the gentlest way to switch public access off). The VM's address must stay <VM_LAN_IP> (reserve it on the router). Traefik on that machine does not use its `config.yaml` (the file provider is switched off in `traefik.yaml`), so nothing is added there.

## 3. Cloudflare Tunnel
1. Zero Trust, Networks, Tunnels: open the existing tunnel (here named Home; its connector runs on the Traefik machine) and add a public hostname: `vpac.mpengor.com`, service type `HTTPS`, URL `<Traefik address on the docker machine>:443`. Under additional settings set the origin server name to `vpac.mpengor.com` (and "No TLS Verify" only if your Traefik certificate does not cover that name). Cloudflare creates the DNS record.
2. Rate limiting rule (Security, WAF, Rate limiting rules) on `http.host eq "vpac.mpengor.com"`, for example 100 requests per minute per IP, action Block for 10 minutes. Turn on Bot Fight Mode.
3. Optional, NOT adopted (owner decision 11-10-2026, so the admin login page stays reachable from the internet): a custom rule that blocks the admin paths for everyone except the owner's IP. Action Block, expression:
   `http.host eq "vpac.mpengor.com" and (starts_with(http.request.uri.path, "/login") or starts_with(http.request.uri.path, "/admin") or starts_with(http.request.uri.path, "/profile") or starts_with(http.request.uri.path, "/org") or starts_with(http.request.uri.path, "/datasources") or starts_with(http.request.uri.path, "/api/admin") or starts_with(http.request.uri.path, "/api/user") or starts_with(http.request.uri.path, "/api/org") or starts_with(http.request.uri.path, "/api/datasources") or starts_with(http.request.uri.path, "/api/login")) and not ip.src eq <owner IP>`
   Do not block `/public-dashboards/*`, `/api/public/*` or the static files: the public pages need them. If the owner's home IP changes, update the rule.

## 4. Check it works (from a phone on mobile data, not the home network)
- Each short address, for example `https://vpac.mpengor.com/live`, answers 302 to the long link and the dashboard loads (`curl -sI https://vpac.mpengor.com/live`).
- A public link opens without a login and every panel loads; there is no time picker and no edit button.
- `https://vpac.mpengor.com/api/search`, `/api/datasources`, `/api/user` and a POST to `/api/ds/query` answer 401, and plain http redirects to https.
- `https://vpac.mpengor.com/login` shows the login page (or the Cloudflare block page if the optional rule is on).
- On the VM: `docker compose logs grafana` shows no errors; `sh ops/smoke.sh` and `python3 ops/check_panels.py` pass.

## Roll back
- Stop public access at once: `python3 ops/grafana_public.py --remove` (or remove the public hostname from the tunnel). Either is enough on its own.
- The Grafana 12.4.12 upgrade (11.6.5 before; deployed 11-10-2026 at about 19:40 UTC, commit 848526c): the image is now `grafana/grafana`, because the `grafana-oss` image stops at 11.6.5. The data volume is migrated forward on the first start and an older version may not read it. It was tested first on a copy of the live volume. Rollback copies on the VM: `~/grafana_data_pre_12.tgz` (the volume) and `~/vatpac-statsV2.rollback-11-10-grafana` (the tree). To take such a copy (confirm the volume name with `docker volume ls`; here `vatpac_grafana_data`): `docker run --rm -v vatpac_grafana_data:/from:ro -v $PWD:/to alpine tar czf /to/grafana_data_pre_12.tgz -C /from .`. To go back to 11.6.5, stop Grafana, restore that copy into the volume and pin `grafana/grafana-oss:11.6.5` again. A rollback within 12.x (ADR-032) is the volume copy taken just before that deploy plus the previous image digest (recorded as `thirdparty=` in `.deploy/release.log` by `deploy.sh --mark`); `~/grafana_data_pre_12.tgz` predates 12 and cannot restore a 12.x volume.

## Things to watch
- Keep copies outside this repository, because the public address depends on settings that live elsewhere: (1) the output of `python3 ops/grafana_public.py --list --public-url <public URL>` (the links live only in the `grafana_data` volume, which is not in the nightly backup; they can be rebuilt with the same tokens, and the copy lets you check the rebuilt addresses match); (2) the Traefik and `vatpac-proxy` files, and notes of the Cloudflare tunnel, hostname and rate-limit rule (screenshots are enough; never save the tunnel token in a document).
- Reserve the VM's LAN address in the router (a DHCP reservation for its network card). Grafana listens on that address and the proxy points at it; if the router hands it to another device, the public dashboards and the LAN login stop.
- Rotate the Grafana admin password now and then (the login page is reachable from the internet), and after anyone else has seen it: see the admin password step in section 1, and edit `GRAFANA_ADMIN_PASSWORD` in the VM's `.env` to match.
- Harden SSH on the VM: key login only (`PasswordAuthentication no`, `PermitRootLogin no` in a file under `/etc/ssh/sshd_config.d/`, test a NEW session before closing the old one) and install `fail2ban`. Password guessing from inside the LAN is otherwise not stopped.
- Dashboard queries run on the production database for anyone who loads a page. The `grafana_ro` limits and the Cloudflare rate limit bound the load, but a crowd can slow the dashboards (never the collector). Check `docker stats` and `docker compose logs db` if the dashboards feel slow.
- Keep Grafana up to date: it is exposed to the internet. The image is `grafana/grafana:12.4` in `docker-compose.yml`, a floating minor tag (ADR-032; Docker Hub has no bare-major tag for this image, so it floats on minor rather than major as Postgres and dbmate do): patch updates arrive with each `docker compose pull`, and a new minor or major version is a manual, tested step (Dependabot refuses major versions of `grafana/grafana`). The 11.6 line ended with 11.6.15 (03-06-2026); check Grafana's release notes for security fixes at least monthly.

## Appendix: README for `~/vatpac-proxy` on the Traefik machine
Copy this text to `~/vatpac-proxy/README.md` there (the compose file there holds 81 labels: 11 base lines plus 7 labels and a comment per dashboard) (this repository holds the source of truth).
```
# vatpac-proxy (on the Traefik machine, <TRAEFIK_HOST_IP>)

What it is: a tiny forwarder container so that Traefik can serve the VATPAC Stats Grafana at https://vpac.mpengor.com.
Grafana runs on another machine (the v3 VM, <VM_LAN_IP>, port 3300), and Traefik only reads Docker labels of containers
on its own machine. This container carries the usual labels and forwards everything it receives on port 3000 to
http://<VM_LAN_IP>:3300 (alpine/socat). Traefik's own config (traefik.yaml, config.yaml) is not touched.

Chain: visitor -> Cloudflare -> Cloudflare Tunnel (hostname vpac.mpengor.com -> https://<TRAEFIK_HOST_IP>:443)
       -> Traefik -> vatpac-proxy (this container) -> Grafana on <VM_LAN_IP>:3300 -> public dashboards only.

What visitors can reach: only the 12 public dashboards (links: python3 ops/grafana_public.py --list on the VM) and
Grafana's login page. They cannot send their own queries.

Short addresses: /live /traffic /new-pilots /origins /atc /atc-retention /atc-hours /pilots /monday /military /monthly /health redirect to the
public dashboard links (Traefik labels in docker-compose.yml, generated by ops/grafana_public.py --traefik-labels on the VM;
the links have fixed tokens, so they only change if the dashboards change). Full description and roll back: docs/RUNBOOKS/public-access.md
in the vatpac-statsV2 repository (private, GitHub t288matt/vatpac-statsV2).

Check it:   curl -sk --resolve vpac.mpengor.com:443:127.0.0.1 https://vpac.mpengor.com/api/health
            (expect JSON with "database": "ok")
Logs:       docker logs vatpac-proxy      (a forwarder: normally silent)
Stop public access at once (nothing is deleted):   cd ~/vatpac-proxy && docker compose stop
Start again:                                         cd ~/vatpac-proxy && docker compose up -d

If the public address shows an error: (1) is this container Up (docker ps)? (2) can this machine reach the VM:
curl http://<VM_LAN_IP>:3300/api/health  (3) is the VM's address still <VM_LAN_IP> (it should be reserved on the
router)? (4) is the tunnel up in Cloudflare Zero Trust, Networks, Tunnels?

Needs: Docker network proxy (external, as for the other apps), the Cloudflare hostname above, the VM at <VM_LAN_IP>.
Written 11-10-2026.
```
