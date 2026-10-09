# Owner actions: what only you can do, and exactly how

Written 09-10-2026. Each item says what it is for, the least you have to do, and what Emma (Claude) does so you do not have to. Order is by value. Tracker rows are in docs/TRACKER.md.

## 1. Protect the main branch (issue #44, T37). About 2 minutes.
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

## 4. Decide the Dependabot pull requests (issue #31, T9). About 5 minutes, or say "your call".
Open now: #12 and #11 (Postgres 16.4 to 16.14, the same major version: safe, patch releases), #10 (Grafana 11.2.2 to 11.6.5), #1 (dbmate 2.21 to 2.36). Rules already set: Postgres 17 or 18, numpy 2.x and other major bumps are ignored.
**Recommendation:** merge #11 and #12 after a throwaway-database test run (Emma does it), test #10 against all dashboards with `ops/check_panels.py` on a throwaway Grafana, and test #1 with the migrations on a throwaway database. Nothing is deployed without your go.
**You do:** reply "go" and Emma tests each bump and merges the ones that pass, closing the rest with a reason. (Standing rule: Emma asks before touching pull requests, so this "go" is the permission.)

## 5. Read-only deploy key for the VM (issue #28, T8). Optional, about 5 minutes.
**Why:** today code is copied with `git archive` over ssh, which works. A deploy key would let the VM `git pull` a tag.
**You do:** on the VM run `ssh-keygen -t ed25519 -f ~/.ssh/vatpac_deploy -N ""`, then `cat ~/.ssh/vatpac_deploy.pub`, paste the output at https://github.com/t288matt/vatpac-statsV2/settings/keys under **Add deploy key** with **Allow write access OFF**.
**Recommendation:** skip it. The current method works and needs no credentials on the VM, which is safer. Say "skip" and Emma closes #28 as not needed.

## 6. Rename the two project folders (issue #23, T32). About 5 minutes, only when no Claude session is open in either folder.
**Why:** `VATPAC-Stats V2` (old v1, read-only) and `vatpac-statsV2` (v3) are easy to mix up, and they were confused today.
**You do:** close all Claude sessions, then in File Explorer under `C:\Users\mdora\Claude\` rename `VATPAC-Stats V2` to `VATPAC-Stats-v1-old` and `vatpac-statsV2` to `vatpac-stats-v3`.
**Emma does (next session you open):** update docs/RESUME.md, absolute paths in the docs, the git worktrees, and the Claude memory notes (the memory folder is named after the old path).
**Recommendation:** do it at a quiet moment after this batch of work is deployed, because several agents and worktrees currently use the old paths.

## 7. Clean up the Metabase host (issue #29, T24). About 5 minutes.
**Why:** a 46 MB Metabase backup dump with real names sits in your home folder on the Metabase host, a Metabase container there still runs against the old database, and exported card files sit on your PC outside git.
**You do:** on the Metabase host delete the dump (`rm ~/<dump file>`; the file name is in the issue thread of T24), stop and remove the Metabase container if Metabase is retired (it is), and delete the exported card CSVs on your PC. v3 keeps the card definitions in docs/reference/v1_metabase_cards.json, so nothing is lost.
**Emma does:** close #29 when you confirm.

## Order I suggest
1 (2 minutes), then 4 and 5 by replying in chat, then 2 (one decision), then 3 and 7 when convenient, then 6 last.
