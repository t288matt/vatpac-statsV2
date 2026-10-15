# VM boot: wait for the IPv4 address before Docker starts (issue 164)

Applies to the production VM `vatpac-v3` (Ubuntu 24.04, systemd 255, interface `ens18`, DHCP reservation 192.168.1.222).
Also apply it to a rebuilt VM. Every command below runs on the VM and needs `sudo`. The owner runs them.

## Why

Grafana binds `192.168.1.222:3300` only. If Docker starts before DHCP has given that address, the bind fails and Docker
never retries (`restart: unless-stopped` does not cover it). Evidence from the 14-10-2026 boot (UTC): network-online at
08:59:10, docker start 08:59:11, bind failure 08:59:14, IPv4 address acquired 09:00:09. The cause: netplan's own wait
rule treats `ens18` as online once it is routable, and the router's IPv6 address makes it routable before IPv4 arrives.
`ops/vm/20-wait-for-ipv4.conf` adds a rule that waits for the IPv4 address. Full reasoning is in that file.

## Install

1. Before: record the current state (read-only).
   `systemctl show systemd-networkd-wait-online -p DropInPaths --value`
   Check: it lists `/run/systemd/system/systemd-networkd-wait-online.service.d/10-netplan.conf` and no file of ours.
2. Copy the file from the repo checkout (`~/vatpac-statsV2`, at a commit that contains it):
   `sudo install -D -m 644 ~/vatpac-statsV2/ops/vm/20-wait-for-ipv4.conf /etc/systemd/system/systemd-networkd-wait-online.service.d/20-wait-for-ipv4.conf`
3. Reload systemd: `sudo systemctl daemon-reload`
4. Check the unit now has three checks, ours last:
   `systemctl cat systemd-networkd-wait-online | grep ExecStart`
   Expected: `ExecStart=`, then `-i ens18:degraded`, then `--any -o routable -i ens18`, then `--interface=ens18 --ipv4 --timeout=120`.
5. Check the new rule passes now (the VM has its address): `sudo systemctl restart systemd-networkd-wait-online; echo $?`
   Expected: `0`. Restarting this service does not touch the network or Docker.

## Prove it (planned reboot, owner runs it, after a quiet period)

1. Take a fresh dump first (`bash ops/deploy.sh --status` shows the newest dump age).
2. `sudo reboot`. Wait about three minutes.
3. Within a few minutes, with nobody running a command: `docker ps` shows six containers healthy, and
   `curl -s http://192.168.1.222:3300/api/health` answers.
4. `docker inspect vatpac-grafana-1` shows the `vatpac` network and port 3300 published, not just `healthy`.
5. `journalctl -b -u docker --no-pager | grep -c "bind host port"` prints `0`.
6. Public dashboards load from outside (Cloudflare address).
7. Timeline check: `journalctl -b --no-pager -o short-precise | grep -E "DHCPv4 address|Reached target network-online|Starting docker.service"`.
   The DHCPv4 line must come BEFORE "Reached target Network is Online".

## If Grafana is still down after a boot

`cd ~/vatpac-statsV2 && docker compose up -d --no-build grafana` (never `make up`: in registry mode it would build locally).
Then check `docker inspect` as in step 4 above. The failed-start state can leave Grafana "healthy" with no network; recreate it:
`docker compose up -d --no-build --force-recreate grafana`.

## Undo

`sudo rm /etc/systemd/system/systemd-networkd-wait-online.service.d/20-wait-for-ipv4.conf && sudo systemctl daemon-reload`
Then `systemctl cat systemd-networkd-wait-online | grep ExecStart` shows only netplan's checks again.
