# Owner actions: what only you can do, and exactly how

Written 09-10-2026, updated 12-10-2026 (cards 9 to 14 are the owner actions left after the public-dashboard deploy of 11-10-2026; the live commit is b3189ad). Updated 14-10-2026: cards 15 to 18 are the owner steps for issue 173 (git checkout and registry images). Cards 15, 16 and 17 were run on 14-10-2026 (see each card); card 18 is open. Each item says what it is for, the least you have to do, and what Emma (Claude) does so you do not have to. Order is by value.

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
**You do:** nothing. Postgres 17 or 18 and Grafana 13 stay ignored. numpy is no longer ignored (14-10-2026): a numpy bump PR needs the VM CPU type to stay `host` or `x86-64-v2-AES`.
**Emma did:** the deploy (fresh dump first) and the checks (tracker T48).

## 5. Read-only deploy key for the VM (issue #28, T8). WON'T DO: owner decision 09-10-2026, issue closed as not planned. Kept below as a record only. Note 14-10-2026: superseded by issue 173; the deploy key is now card 15 below.
**Why:** today code is copied with `git archive` over ssh, which works. A deploy key would let the VM `git pull` a commit.
**You do:** on the VM run `ssh-keygen -t ed25519 -f ~/.ssh/vatpac_deploy -N ""`, then `cat ~/.ssh/vatpac_deploy.pub`, paste the output at https://github.com/t288matt/vatpac-statsV2/settings/keys under **Add deploy key** with **Allow write access OFF**.
**Recommendation:** skip it. The current method works and needs no credentials on the VM, which is safer. Say "skip" and Emma closes #28 as not needed.

## 6. Rename the two project folders (issue #23, T32). About 5 minutes, only when no Claude session is open in either folder.
**Why:** `VATPAC-Stats V2` (old v1, read-only) and `vatpac-statsV2` (v3) are easy to mix up, and they were confused today.
**You do:** close all Claude sessions, then in File Explorer under `C:\Users\mdora\Claude\` rename `VATPAC-Stats V2` to `VATPAC-Stats-v1-old` and `vatpac-statsV2` to `vatpac-stats-v3`.
**Emma does (next session you open):** update absolute paths in the docs, the git worktrees, and the Claude memory notes (the memory folder is named after the old path).
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
**Why:** Grafana listens on all addresses of the VM (`GRAFANA_BIND=0.0.0.0`) and the Traefik proxy points at the VM's LAN address (192.168.1.222). If the router gives that address to another device, or the VM a new one, the public dashboards and the LAN login stop.
**You do:** in the router's DHCP settings reserve 192.168.1.222 for the VM's network card (its MAC address is under the VM's Hardware in Proxmox). **Emma does:** nothing.

## 13. Harden SSH on the VM and install fail2ban (new 11-10-2026). About 20 minutes, needs sudo on the VM.
**Why:** password login over SSH is NOT disabled on the VM, and fail2ban is NOT installed. The VM holds the data and the `.env` secrets. The Grafana port is not reachable from the internet, but password guessing from inside the LAN would not be stopped.
**You do:** with your own sudo, confirm your key login works, then set `PasswordAuthentication no` (and `PermitRootLogin no`) in a file under `/etc/ssh/sshd_config.d/`, reload SSH and test a NEW session before closing the old one; then `sudo apt install fail2ban`. **Emma does:** check from the `vatpac-v3` login that key login still works.

## 14. Delete the old rollback copies on the VM (new 12-10-2026). About 10 minutes, about 18-10-2026.
**Why:** each deploy leaves a full copy of the previous tree on the VM so that a rollback is possible. After a week of clean running they only take disk space. The Grafana volume copy holds the old admin settings. Nothing in the repository depends on them.
**You do:** on the VM, FIRST list them and read the list: `ls -ld --time-style=long-iso ~/vatpac-statsV2.rollback-* ~/grafana_data_pre_12.tgz` and `docker volume ls | grep pre_bumps`. On 12-10-2026 the list was these seven folders and one archive: `vatpac-statsV2.rollback-09-10-bumps`, `-09-10-card65`, `-09-10-integrity`, `-09-10-mig150`, `-11-10-all`, `-11-10-all2`, `-11-10-grafana`, and the file `grafana_data_pre_12.tgz` (owned by root, so it needs sudo to delete). Check the names against that list, then remove each with `rm -rf <path>` (never a wildcard). Do not delete the live folder `~/vatpac-statsV2` or any volume whose name does not end in `_pre_bumps`. The volume `vatpac_grafana_data_pre_bumps` also exists on the VM (seen with `docker volume ls` on 12-10-2026); list it and decide separately.
**Keep them longer if:** you have a Grafana or database problem that started after 11-10-2026; the copies are the quickest way back. **Emma does:** re-run `ops/deploy.sh --status` afterwards and note the freed space.

## 15. Deploy key and git checkout on the VM (issue 173, phase P0). DONE 14-10-2026 (finished 22:16 UTC, verified). Kept as the procedure for a rebuilt VM.
**Why:** the VM is deployed by copying files (`tar -x`), which never deletes anything; the VM already holds 15 stale files, among them old migration-comparison scripts. A read-only git checkout replaces that, so a deploy is a `git checkout <commit>` and stale files cannot survive.
**You do:** follow the ten numbered steps of section 2d in docs/RUNBOOKS/deploy-to-vm.md. In short: make a key on the VM (`ssh-keygen -t ed25519 -f ~/.ssh/vatpac_deploy -N ""`), paste only the `.pub` line at https://github.com/t288matt/vatpac-statsV2/settings/keys under **Add deploy key** with **Allow write access OFF**, clone beside the live folder, compare, remove the 15 stale files (they are saved in a tar first), and move the `.git` folder into the live folder. Nothing restarts. Not during a soak.
**Emma does:** reads the VM before and after (read-only), runs `bash ops/deploy.sh --verify` and `--status`, and records the result.
**Way back:** `rm -rf ~/vatpac-statsV2/.git`; the old method works again (runbook section 2d, Roll back).

## 16. Registry token and its 90 day reminder (issue 173, phase P3). DONE 14-10-2026 (token expires about 12-01-2027, reminder set for 05-01-2027). Renew by the same steps.
**Why:** the VM needs to read the private images from GHCR. GitHub's container registry only accepts a classic personal access token (GitHub Docs, read 14-10-2026), so a fine-grained token will not work.
**You do:** on GitHub, Settings > Developer settings > Personal access tokens > Tokens (classic) > Generate new token (classic); expiration **90 days**; tick **only** `read:packages`. Copy it once. Put the expiry date in your calendar with a reminder 10 days earlier. On the VM run `docker login ghcr.io -u t288matt` and paste the token at the hidden `Password:` prompt (nothing is shown), then press Enter. Never put the token in chat, in the repository or in `.env`; Docker keeps it in `~/.docker/config.json` on the VM (check `ls -l` shows `-rw-------`).
**Know this:** the token can read every package your account can reach. When it expires, deploys fail with `unauthorized` or `denied`; the running stack is not affected. Renew: make a new token, repeat the login, delete the old token.
**Emma does:** tells you the exact commands first, never sees the token, and checks `Login Succeeded` from what you report.
**Checked:** you confirmed on the GitHub Packages page on 14-10-2026 that the packages are Private.

## 17. The two `.env` lines for registry mode (issue 173, phase P3). DONE 14-10-2026 (`VATPAC_IMAGE_TAG=sha-67c1ca0`; marker 67c1ca0 at 22:29:46 UTC).
**Why:** these two lines switch the VM from building images to pulling the published ones. Emma must tell you the exact lines before they are added, and never prints your `.env`.
**The lines** (nothing secret in them; `<7>` is the first 7 characters of the commit being deployed):
`VATPAC_IMAGE_PREFIX=ghcr.io/t288matt/` and `VATPAC_IMAGE_TAG=sha-<7>`
**You do:** after card 16, take a fresh dump (`docker compose exec backup backup.sh once`), add the two lines (the command is in runbook section 2e, step 4), then `docker compose config --images`, `docker compose pull`, `docker compose up -d --no-build`. The collector restarts once (about 45 seconds of snapshots).
**Emma does:** `bash ops/deploy.sh --verify`, the ten public addresses, then `--mark` after commenting on the issues.
**Way back (first cycle):** no earlier registry tag exists, so "set the previous tag" does not work yet. Delete BOTH lines (`sed -i '/^VATPAC_IMAGE_/d' .env`; one alone fails `check_env.sh`) and run `docker compose up -d --no-build`: the locally built `:local` images are still on the VM. From the second registry deploy the previous `sha-` tag also works. Never `up -d --build` while the lines are set.

## 18. Tidy old rollback folders and images (new 14-10-2026, after cards 14 to 17 have been used). About 15 minutes.
**Why:** with a git checkout the previous code is always one `git checkout` away, so the full tree copies (`vatpac-statsV2.rollback-*`, card 14) are no longer needed. Each registry deploy also leaves the previous four images on the VM, and Docker holds 85 unused anonymous volumes (about 10.6 GB, read 14-10-2026).
**You do:** card 14 for the folders. For images, list first: `docker images 'ghcr.io/*'`, keep the current and the previous `sha-` tag, and remove older ones one by one with `docker image rm <image:tag>`. Keep the locally built `:local` images (they are the current rollback) and the old `grafana/grafana-oss:11.6.5` image until about 18-10-2026, then decide. Do NOT run `docker system prune`, and never `docker volume prune` blindly: named volumes hold the data, the backups and Grafana state. The unused anonymous volumes are a separate cleanup: list them, check each is anonymous and unused, ask Emma first.
**Optional tidy:** the VM `.env` has a stale `VATPAC_VERSION=dev` line that nothing reads; you can delete it.
**Emma does:** reads the lists and checks `--status` afterwards.

## Order I suggest
Items 1, 4, 5 and 7 are decided or done. Cards 15, 16 and 17 (issue 173) are done; card 18 comes last. Still open: 3 (the Proxmox restore test, when convenient), then the new cards 9 to 13 (public links copy, Cloudflare and Traefik copies, admin password rotation, DHCP reservation, SSH hardening and fail2ban), card 14 (delete the old rollback copies, about 18-10-2026), then 6 (the folder rename) last.
