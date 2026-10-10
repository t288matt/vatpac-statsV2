# If Claude is unavailable: a card for the owner

Written 10-10-2026 for Matt. It describes the system as deployed on the VM on 10-10-2026 (the tree shipped from commit 3eb81a3 on 09-10-2026). All times are UTC. The aim is to keep the stack running and the data safe until Claude is back. If in doubt, do less: a quiet system is better than a broken one. Wherever this card says "not run here", the command was checked against the repository scripts but not executed by the writer.

## 1. What it is, and where it runs

VATPAC Stats v3 polls the public VATSIM feed every 30 seconds, stores the pilots inside the Australian airspace and the controllers on a callsign whitelist in PostgreSQL, and shows them as Grafana dashboards. It runs 24/7 in Docker on an always-on Linux VM on your Proxmox host.

| Piece | What it does |
|---|---|
| `db` | PostgreSQL 16, the data (volume `vatpac_pgdata`) |
| `collector` | polls the feed every 30 s and writes to the database |
| `grafana` | the dashboards, on port 3300 of the VM's LAN address |
| `backup` | one database dump a night at 02:00, keeps 14 days (volume `vatpac_backups`), plus a restore test on Sundays |
| `watchdog` | checks everything every minute and sends Telegram messages; it only alerts, it restarts nothing |
| `migrate` | a one-off job that applies database changes at start-up; `exited (0)` is normal |

Reach the VM from your PC with `ssh vatpac-v3` (user matt, no sudo). The project folder on the VM is `~/vatpac-statsV2`; run every `docker compose` command from there. The VM holds a copy of the code but is not a git checkout. The Grafana login is the admin user and the password in the `.env` file in that folder: never paste or print the contents of `.env`.

The old v1 system is still running on a different VM (alias `vatpac`). Leave it alone.

## 2. How to tell it is healthy

1. **Telegram.** Quiet is good. A message is sent only when something changes (about two minutes after a problem starts, and again when it clears). Today's messages begin `vatpac-stats:` followed by `WARN`, `CRIT` or `RECOVERED` and a check name. After the next deploy they become plain sentences beginning `VATPAC Stats:` with a final `Technical:` line. What each message means: [RUNBOOKS/alerts.md](RUNBOOKS/alerts.md).
2. **The outside heartbeat.** A Task Scheduler task on your PC (`VatpacV3Heartbeat`) opens Grafana's health page every 5 minutes. After two failures in a row it sends `VATPAC v3 FAILED ...` and when Grafana answers again `VATPAC v3 RECOVERED ...`. It only runs while the PC is on. If you stay silent for a day, that is a reason to look, not proof of health. Details: [RUNBOOKS/outside-heartbeat.md](RUNBOOKS/outside-heartbeat.md).
3. **Grafana.** Open `http://<VM LAN address>:3300` (the address is in the outside-heartbeat runbook). Open the dashboard "Monitoring: collector and data freshness". Healthy means snapshots arriving about every 30 seconds.
4. **On the VM** (checked read-only on 10-10-2026, all of this matched):
   ```sh
   ssh vatpac-v3
   cd ~/vatpac-statsV2
   docker compose ps
   ```
   Expect `db`, `collector`, `grafana`, `backup` and `watchdog` all `healthy`, and `migrate` as `Exited (0)`. Then `sh ops/smoke.sh` should end with `SMOKE PASS` (not run here; documented in [RUNBOOKS/daily-checks.md](RUNBOOKS/daily-checks.md)).

Known and harmless on 10-10-2026: the watchdog reports one `data_integrity` warning about a 6-minute gap caused by the power cut on 09-10-2026. It clears itself on 12-10-2026 at about 15:42.

## 3. The first three things to check when it is not healthy

1. **Is the VM up and are the containers running?** `ssh vatpac-v3`, then `docker compose ps`. If you cannot reach the VM at all, look at it in Proxmox (is the VM running, does it have a disk and network?). After a power cut the stack comes back by itself (tested twice); give it five minutes.
2. **Is VATSIM itself down?** From the VM: `curl -sI https://data.vatsim.net/v3/vatsim-data.json | head -5` (from [RUNBOOKS/feed-outage.md](RUNBOOKS/feed-outage.md)). If that fails too, wait. Nothing is wrong on our side and there is nothing to fix; the outage shows as a gap.
3. **Is the disk full?** `df -h` on the VM. A full disk stops the database. Steps: [RUNBOOKS/db-full.md](RUNBOOKS/db-full.md) (that file is being rewritten for a later release; its advice to remove old partitions is for an engineer, not for this card: if the disk is full, stop and ask for help).

Then read the logs of the service the alert names: `docker compose logs --tail=100 <service>`. A stuck collector is covered in [RUNBOOKS/stuck-collector.md](RUNBOOKS/stuck-collector.md).

## 4. Restarting a stopped container safely

Check first: `docker compose ps -a`. Docker restarts a crashed container by itself (`restart: unless-stopped`), so a container that is still down has usually been stopped on purpose or is failing to start; read its log before touching it.

- **Any service other than the collector** (`grafana`, `watchdog`, `backup`, `db`): `docker compose up -d <service>`. Do not restart `db` while a restore is running.
- **The collector.** The safe sequence is the one the deploy runbook insists on: stop it, then bring it up with a build.
  ```sh
  docker compose stop collector
  docker compose up -d --build
  ```
  Not run here (it would interrupt live polling). Traps:
  - **Never `docker compose restart collector`**, and never plain `docker compose up -d` without `--build` to bring back the collector after a code change. The deploy runbook ([RUNBOOKS/deploy-to-vm.md](RUNBOOKS/deploy-to-vm.md), section 4c) records an incident where an old or cached image left the archive volume unwritable and nothing looked wrong. With no code change the sequence above rebuilds from cache and is harmless.
  - [RUNBOOKS/stuck-collector.md](RUNBOOKS/stuck-collector.md) still lists `docker compose restart collector` as step 1 for a stuck collector. The two runbooks disagree; this card follows the stricter rule. Ask the next Claude session to reconcile them.
  - Every collector restart costs about 45 seconds of snapshots. That is fine; just do not repeat it in a loop.
  - Before a planned restart of the whole VM, pause the outside heartbeat: `Disable-ScheduledTask -TaskName VatpacV3Heartbeat` in PowerShell on your PC, and `Enable-ScheduledTask -TaskName VatpacV3Heartbeat` afterwards.
- After any restart: `docker compose ps` (all healthy, restart counts 0) and `sh ops/smoke.sh`.
- Do not deploy new code while Claude is unavailable. Run what is there.

## 5. Backups: take one, find them, restore

**Where they are.** Inside the `backup` container at `/backups` (the Docker volume `vatpac_backups` on the VM's own disk). One file a night at 02:00, named `vatpac_<date>T<time>Z.dump`, kept 14 days. On 10-10-2026 the newest was `vatpac_20261010T020000Z.dump`, 5.2 MB.
```sh
docker compose exec backup ls -lh /backups
docker compose exec backup cat /backups/.last_success /backups/.last_restore_test
```
(Run on the VM on 10-10-2026: the first worked. `.last_restore_test` does not exist until the first Sunday restore test, 11-10-2026; each line reads `<time> OK|FAIL <detail>`.)

**Where the second copy is.** There is no off-box copy made by the stack itself yet (`BACKUP_REMOTE` is empty in the VM's `.env` as checked on 10-10-2026). The cover against losing the VM disk is your Proxmox Backup Server job for the VM (tracker T17; a restore of that copy has not been tested, owner action in [OWNER_ACTIONS.md](OWNER_ACTIONS.md) item 3). Never boot a restored copy of the v1 clone VM on the LAN.

**Take a backup now:** `make backup` (this is `docker compose exec backup backup.sh once`; `make` is installed on the VM). Check it with the `ls` command above. Not run here, to avoid a dump on the live database for a documentation task; the same command is the first step of every deploy.

**Prove the newest backup restores:** `make backup-test` (restores into a scratch database `vatpac_restore_test`, checks it, drops it). Not run here.

**Restore.** Read [RUNBOOKS/restore.md](RUNBOOKS/restore.md) first. The commands below are copied from it and match `ops/backup/restore.sh` and the Makefile.

*A. Look at a backup without touching live data (safe, any time):*
```sh
make restore FILE=/backups/vatpac_20261007T020000Z.dump TARGET=vatpac_restore CONFIRM=vatpac_restore
docker compose exec db psql -U vatpac -d vatpac_restore -c "SELECT count(*) FROM poll_log"
```
Use a real file name from the `ls` output. `CONFIRM` must equal `TARGET` exactly. The script refuses system databases, refuses to run if `<target>_old` exists, and leaves the live database alone.

*B. Disaster recovery, replacing the live database (only if the live database is lost or corrupt):*
1. `docker compose stop collector grafana watchdog` (the restore refuses if anything else is connected).
2. `make restore FILE=/backups/<file>.dump TARGET=vatpac CONFIRM=vatpac` (`vatpac` is the default database name; use `POSTGRES_DB` from `.env` if it was changed). The old database is kept as `vatpac_old` and a safety dump of it is saved in `/backups/` as `*.prerestore`.
3. `docker compose up migrate` (re-applies roles and grants, which the dump does not carry).
4. `docker compose up -d`, then `make smoke`.
5. Only when you are satisfied, drop the old copy (it uses disk until then): `docker compose exec db psql -U vatpac -d postgres -c 'DROP DATABASE "vatpac_old"'`.
6. Data between the backup and the failure is gone; there is no backfill. Note the gap.

Restores were drilled on 08-10-2026 ([RESTORE_DRILL.md](RESTORE_DRILL.md)); the exact B sequence has not been run end to end on the live volume, so go slowly and read each message.

**Copying a dump to your PC** (an extra safeguard): `docker compose cp backup:/backups/<file>.dump .` on the VM, then `scp` it to the PC. Not run here and not in a runbook as written; `docker compose cp` is documented for the other direction. Dumps contain real names and CIDs (public data, but keep them private).

## 6. Who to ask for help

- **Claude.** Start a new session in the folder `C:\Users\mdora\Claude\vatpac-statsV2` (not the folder `VATPAC-Stats V2`, which is the old v1 system) and paste the prompt in [NEXT_SESSION_PROMPT.md](NEXT_SESSION_PROMPT.md). It tells the session what to read first ([RESUME.md](RESUME.md), then [TRACKER.md](TRACKER.md)).
- **The repository and its issues:** https://github.com/t288matt/vatpac-statsV2 (the open issues list is the live backlog).
- **A human engineer:** any engineer who knows Docker and PostgreSQL can work from [RUNBOOKS/](RUNBOOKS/) and [ARCHITECTURE.md](ARCHITECTURE.md). The repository names no other person or support contact. I could not verify one, so add a name here if you have one.
- **VATSIM outages:** `https://status.vatsim.net/status.json`.

## 7. What NOT to do

- **Never run `db/roles.sql` against the live database.** Roles are cluster-wide; running it with other passwords resets the live role passwords and breaks the stack. (The `migrate` job applies it safely on its own.)
- **Never run `docker system prune`, `docker volume prune` or `docker compose down -v`, and never delete a Docker volume.** `vatpac_pgdata` is the data, `vatpac_backups` holds every backup, `vatpac_grafana_data` the Grafana state, `vatpac_archive` the raw-feed safety copy. Several rollback copies sit in the VM's home folder (`vatpac-statsV2.rollback-*`) and a volume `vatpac_grafana_data_pre_bumps`; leave them for the owner's planned clean-up.
- **Never run test suites, scratch databases or throwaway containers on the VM** without memory caps. An uncapped test container took 6.9 GB of the 7.9 GB on 08-10-2026 and cost the collector a gap (tracker T39).
- **Never print or paste `.env`**, the Telegram token or the Grafana password anywhere, including into a chat with an AI.
- **Do not expose Grafana to the internet** without a login layer.
- **Do not edit files on the VM by hand** (the next deploy overwrites them, and `.env` is the only file meant to differ). Do not `git pull` there; it is not a git checkout.
- **Do not touch or stop the old v1 VM** (alias `vatpac`) or the folder `VATPAC-Stats V2` on your PC.
- **Do not drop partitions or delete rows by hand** to free space; ask first (db-full runbook).
- **Do not deploy** anything while Claude is unavailable.

## 8. Where things stand that you might trip over (10-10-2026)

- The code on `main` is ahead of what is deployed (backup and restore hardening, Telegram wording, collector hardening). The deploy plan is [DEPLOY_PLAN_REVIEW_FIXES.md](DEPLOY_PLAN_REVIEW_FIXES.md). If the runbooks describe a feature your VM does not show, check [../CHANGELOG.md](../CHANGELOG.md) for whether it is deployed. In particular [RUNBOOKS/restore.md](RUNBOOKS/restore.md) and [RUNBOOKS/alerts.md](RUNBOOKS/alerts.md) already describe the newer behaviour; the commands on this card work with both.
- The daily digest email and the Claude desktop app: the digest only runs while the Claude desktop app is open.
