# Owner actions: what only you can do, and exactly how

Written 09-10-2026, updated 12-10-2026 (cards 9 to 14 are the owner actions left after the public-dashboard deploy of 11-10-2026; the live commit is b3189ad). Each item says what it is for, the least you have to do, and what Emma (Claude) does so you do not have to. Order is by value. Tracker rows are in docs/TRACKER.md.

## 1. Protect the main branch (issue #44, T37). WON'T DO: branch protection is not available on the GitHub plan in use; issue #44 closed 09-10-2026 (19:28 UTC). Kept below as a record only.
**Why:** `main` is what gets deployed. Nothing stops a mistaken force push or deleting it.
**You do:**
1. Open https://github.com/t288matt/vatpac-statsV2/settings/rules and choose **New ruleset**, then **New branch ruleset**.
2. Name it `protect-main`. Enforcement status: **Active**.
3. Under Target branches choose **Add target**, then **Include default branch**.
4. Tick **Restrict deletions** and **Block force pushes**. Leave everything else off (do not require pull requests or status checks; the workflow pushes to `main` and reads CI afterwards).
5. Click **Create**.
If GitHub says rulesets are not available for a private repository on your plan, stop: the fallback is care (no force pushes to `main`), which is already the practice. Tell Emma and the issue is closed as "not available on this plan".
**Emma does:** test it with a harmless force push to a scratch branch (never `main`), then close #44.

## 2. Outside heartbeat monitor (issue #17, T19). DONE by Emma on 09-10-2026.
A Task Scheduler job on your PC checks Grafana every 5 minutes and messages you on Telegram after two failures and on recovery. Tested with a simulated outage. See docs/RUNBOOKS/outside-heartbeat.md. Nothing for you to do. Close #17 when you are happy.

## 3. Restore test of the Proxmox backup of VM 100 (issue #26, T17). About 30 minutes, in Proxmox.
**Why:** a backup you have never restored is a hope, not a backup.
**Safety rule:** VM 100 is a clone of the old v1 machine. Never boot a restored copy on the LAN: it carries the old host keys, cron jobs, Tailscale identity and monitor pushes.
**You do (Proxmox web page):**
1. Storage, your Proxmox Backup Server, find the newest backup of **VM 100**, click **Restore**.
2. Restore to a **new unused VM ID** (for example 9100). Do not tick "Start after restore".
3. Open the new VM's **Hardware**, select the network device, and **remove it** (or untick "Connected"). The VM must have no network.
4. Start it from the console, log in, and run `docker compose ps` in `~/vatpac-statsV2`; the stack should start and the database should answer. A quick check: `docker compose exec db psql -U vatpac -d vatpac -c "select count(*) from pilot_session"`.
5. Shut it down and delete the scratch VM.
**Emma does:** record the result and close #26 when you tell her the outcome.

## 4. Dependabot pull requests (issue #31, T9, T48). DONE: tested, applied and DEPLOYED 09-10-2026 at 13:46 to 13:47 UTC (commit 8905d85); issue #31 closed.
Postgres 16.15 (db and backup), Grafana 11.6.5 (history: Grafana has run 12.4.12 since 11-10-2026) and dbmate 2.36.0 were tested on throwaway containers (all pass) and committed to main directly (commit aecec21, because the repo token cannot merge pull requests); the four PRs were closed with a note. Deploying them recreated the db and grafana containers (see docs/RUNBOOKS/deploy-to-vm.md section 4d).
**You do:** nothing. Postgres 17 or 18, Grafana 13, numpy 2.x and other major bumps stay ignored.
**Emma did:** the deploy (fresh dump first) and the checks (tracker T48).

## 5. Read-only deploy key for the VM (issue #28, T8). WON'T DO: owner decision 09-10-2026, issue closed as not planned. Kept below as a record only.
**Why:** today code is copied with `git archive` over ssh, which works. A deploy key would let the VM `git pull` a tag.
**You do:** on the VM run `ssh-keygen -t ed25519 -f ~/.ssh/vatpac_deploy -N ""`, then `cat ~/.ssh/vatpac_deploy.pub`, paste the output at https://github.com/t288matt/vatpac-statsV2/settings/keys under **Add deploy key** with **Allow write access OFF**.
**Recommendation:** skip it. The current method works and needs no credentials on the VM, which is safer. Say "skip" and Emma closes #28 as not needed.

## 6. Rename the two project folders (issue #23, T32). About 5 minutes, only when no Claude session is open in either folder.
**Why:** `VATPAC-Stats V2` (old v1, read-only) and `vatpac-statsV2` (v3) are easy to mix up, and they were confused today.
**You do:** close all Claude sessions, then in File Explorer under `C:\Users\mdora\Claude\` rename `VATPAC-Stats V2` to `VATPAC-Stats-v1-old` and `vatpac-statsV2` to `vatpac-stats-v3`.
**Emma does (next session you open):** update docs/RESUME.md, absolute paths in the docs, the git worktrees, and the Claude memory notes (the memory folder is named after the old path).
**Recommendation:** do it at a quiet moment after this batch of work is deployed, because several agents and worktrees currently use the old paths.

## 7. Clean up the Metabase host (issue #29, T24). WON'T DO: owner decision 09-10-2026, issue closed as not planned. Kept below as a record only.
**Why:** a 46 MB Metabase backup dump with real names sits in your home folder on the Metabase host, a Metabase container there still runs against the old database, and exported card files sit on your PC outside git.
**You do:** on the Metabase host delete the dump (`rm ~/<dump file>`; the file name is in the issue thread of T24), stop and remove the Metabase container if Metabase is retired (it is), and delete the exported card CSVs on your PC. v3 keeps the card definitions in docs/reference/v1_metabase_cards.json, so nothing is lost.
**Emma does:** close #29 when you confirm.

## 8. Off-VM backup copy on the Proxmox host (ADR-026 item 7)
One-time set-up on the Proxmox host and the VM, step by step: [OWNER_BACKUP_COPY.md](OWNER_BACKUP_COPY.md). It does not replace item 3 (the untested PBS restore). The deploys are done (11-10-2026), so it can be done now.

## 9. Keep a copy of the public links list (new 11-10-2026). About 5 minutes.
**Why:** the 10 public dashboard links live only in the `grafana_data` volume, which is not in the nightly backup. They can be rebuilt (the tokens are stable), but a copy lets you check that the rebuilt addresses match.
**You do:** on the VM, in `~/vatpac-statsV2`, run `python3 ops/grafana_public.py --list --public-url https://vpac.mpengor.com` and save the output in your password manager or a private note. **Emma does:** nothing.

## 10. Keep copies of the Cloudflare and Traefik settings (new 11-10-2026). About 15 minutes.
**Why:** the public address depends on settings outside this repository. If the Traefik machine or the Cloudflare account is lost, the address is down until they are rebuilt.
**You do:** keep a second copy of the Traefik and `vatpac-proxy` files (you hold them in `vatpac-claude-setup\traefik`), and note the Cloudflare Tunnel, hostname and rate-limit rule settings (screenshots are enough; never save the tunnel token in a document). The layout is in RUNBOOKS/public-access.md. **Emma does:** nothing.

## 11. Rotate the Grafana admin password (new 11-10-2026). About 10 minutes.
**Why:** the admin login page is reachable from the internet (owner decision 11-10-2026), so the admin password is the one lock that matters. Rotate it now and then, and after anyone else has seen it.
**You do:** on the VM run `docker compose exec grafana grafana cli admin reset-admin-password '<new password>'`, then edit `GRAFANA_ADMIN_PASSWORD` in the VM's `.env` to match (letters and digits only, long). Tell Emma first so the checks that use it are not left failing (deploy-to-vm.md section 3). **Emma does:** re-run `python3 ops/check_panels.py` afterwards.

## 12. DHCP reservation for the Grafana address (new 11-10-2026). About 5 minutes, in the router.
**Why:** Grafana binds to the VM's LAN address (192.168.1.222) and the Traefik proxy points at it. If the router gives that address to another device, or the VM a new one, the public dashboards and the LAN login stop.
**You do:** in the router's DHCP settings reserve 192.168.1.222 for the VM's network card (its MAC address is under the VM's Hardware in Proxmox). **Emma does:** nothing.

## 13. Harden SSH on the VM and install fail2ban (new 11-10-2026). About 20 minutes, needs sudo on the VM.
**Why:** password login over SSH is NOT disabled on the VM, and fail2ban is NOT installed. The VM holds the data and the `.env` secrets. The Grafana port is not reachable from the internet, but password guessing from inside the LAN would not be stopped.
**You do:** with your own sudo, confirm your key login works, then set `PasswordAuthentication no` (and `PermitRootLogin no`) in a file under `/etc/ssh/sshd_config.d/`, reload SSH and test a NEW session before closing the old one; then `sudo apt install fail2ban`. **Emma does:** check from the `vatpac-v3` login that key login still works.

## 14. Delete the old rollback copies on the VM (new 12-10-2026). About 10 minutes, about 18-10-2026.
**Why:** each deploy leaves a full copy of the previous tree on the VM so that a rollback is possible. After a week of clean running they only take disk space. The Grafana volume copy holds the old admin settings. Nothing in the repository depends on them.
**You do:** on the VM, FIRST list them and read the list: `ls -ld --time-style=long-iso ~/vatpac-statsV2.rollback-* ~/grafana_data_pre_12.tgz` and `docker volume ls | grep pre_bumps`. On 12-10-2026 the list was these seven folders and one archive: `vatpac-statsV2.rollback-09-10-bumps`, `-09-10-card65`, `-09-10-integrity`, `-09-10-mig150`, `-11-10-all`, `-11-10-all2`, `-11-10-grafana`, and the file `grafana_data_pre_12.tgz` (owned by root, so it needs sudo to delete). Check the names against that list, then remove each with `rm -rf <path>` (never a wildcard). Do not delete the live folder `~/vatpac-statsV2` or any volume whose name does not end in `_pre_bumps`. The volume `vatpac_grafana_data_pre_bumps` named in the 10-10-2026 notes was not checked on 12-10-2026; list it before deciding.
**Keep them longer if:** you have a Grafana or database problem that started after 11-10-2026; the copies are the quickest way back. **Emma does:** re-run `ops/deploy.sh --status` afterwards and note the freed space.

## Order I suggest
Items 1, 4, 5 and 7 are decided or done. Still open: 3 (the Proxmox restore test, when convenient), then the new cards 9 to 13 (public links copy, Cloudflare and Traefik copies, admin password rotation, DHCP reservation, SSH hardening and fail2ban), card 14 (delete the old rollback copies, about 18-10-2026), then 6 (the folder rename) last.
