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

Reach the VM from your PC with `ssh vatpac-v3` (user matt, no sudo). The project folder on the VM is `~/vatpac-statsV2`; run every `docker compose` command from there. Since 14-10-2026 the folder is a read-only git checkout that runs registry images (section 7b); before that it was a plain copy of the code. The Grafana login is the admin user and the password in the `.env` file in that folder: never paste or print the contents of `.env`.

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
- **The collector.** Since 14-10-2026 the VM runs published images (registry mode). The safe sequence for a new image is: `docker compose pull`, then `docker compose stop collector`, then `docker compose up -d --no-build`. Not run here (it would interrupt live polling). Traps:
  - **Never use `up -d --build` on the VM.** In registry mode it rebuilds from source and gives the result the registry name, replacing the image you pulled. After a `pull`, `docker compose up -d --no-build` is correct. Local builds with `--build` are for a PC and first installs only.
  - **Never use `docker compose restart collector` to pick up a code change**: a restart keeps the old image. The old incident in [RUNBOOKS/deploy-to-vm.md](RUNBOOKS/deploy-to-vm.md) section 4c (a cached image left the archive volume unwritable) was a local-build problem; with a pulled image the equivalent mistake is forgetting `pull`, so check `docker compose config --images` shows the tag you mean.
  - [RUNBOOKS/stuck-collector.md](RUNBOOKS/stuck-collector.md) still lists `docker compose restart collector` as step 1 for a stuck collector. Owner decision 10-10-2026: a plain `docker compose restart collector` is fine for a stuck collector when there is no code change; for a code change pull the new image and use `up -d --no-build`.
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

**Where the second copy is.** There is no off-box copy made by the stack itself yet (`BACKUP_REMOTE` is empty in the VM's `.env` as checked on 10-10-2026). The cover against losing the VM disk is your Proxmox Backup Server job for the VM (tracker T17; a restore of that copy has not been tested, owner action in [OWNER_ACTIONS.md](OWNER_ACTIONS.md) item 3). When test-restoring the Proxmox backup of this VM, remove its network device before starting it (it is a clone of the v1 machine and would clash on the LAN); see [OWNER_ACTIONS.md](OWNER_ACTIONS.md) item 3.

**Take a backup now:** `make backup` (this is `docker compose exec backup backup.sh once`; `make` is installed on the VM). Check it with the `ls` command above. Not run here, to avoid a dump on the live database for a documentation task; the same command is the first step of every deploy.

**Prove the newest backup restores:** `make backup-test` (restores into a scratch database `vatpac_restore_test`, checks it, drops it). Not run here.

**Restore.** Read [RUNBOOKS/restore.md](RUNBOOKS/restore.md) first. The commands below are copied from it and match `ops/backup/restore.sh` and the Makefile.

*A. Look at a backup without touching live data (safe, any time):*
```sh
make restore FILE=/backups/vatpac_20261007T020000Z.dump TARGET=vatpac_restore CONFIRM=vatpac_restore
docker compose exec db psql -U vatpac -d vatpac_restore -c "SELECT count(*) FROM poll_log"
```
Use a real file name from the `ls` output. These `make` targets use `docker compose exec` without `-T`, so run them in an interactive `ssh vatpac-v3` session, not as a one-line `ssh vatpac-v3 'make ...'` (not tested). After deploy 1 (planned 11-10-2026) `restore.sh` also refuses a dump whose newest poll data is more than 48 hours older than the time in the dump's file name (`RESTORE_MAX_POLL_AGE_HOURS`; the setting is explained in [RUNBOOKS/restore.md](RUNBOOKS/restore.md)). If it refuses because the dump is stale and you know why (for example a long outage before that night), ask for help or read restore.md first. The version running on the VM today has no such check. `CONFIRM` must equal `TARGET` exactly. The script refuses system databases, refuses to run if `<target>_old` exists, and leaves the live database alone.

*B. Disaster recovery, replacing the live database (only if the live database is lost or corrupt):*
1. `docker compose stop collector grafana watchdog` (the restore refuses if anything else is connected).
2. `make restore FILE=/backups/<file>.dump TARGET=vatpac CONFIRM=vatpac` (`vatpac` is the default database name; use `POSTGRES_DB` from `.env` if it was changed). The old database is kept as `vatpac_old` and a safety dump of it is saved in `/backups/` as `*.prerestore`.
3. `docker compose up migrate` (re-applies roles and grants, which the dump does not carry).
4. `docker compose up -d` (no code change, so no build is needed), then `make smoke`. Keep this order: step 3 must come before the smoke test, because the deployed `ops/smoke.sh` contains a `DELETE FROM poll_log` that is only safe once `migrate` has restored the correct grants (issue #86).
5. Only when you are satisfied, drop the old copy (it uses disk until then): `docker compose exec db psql -U vatpac -d postgres -c 'DROP DATABASE "vatpac_old"'`.
6. Data between the backup and the failure is gone; there is no backfill. Note the gap.

Restores were drilled on 08-10-2026; the exact B sequence has not been run end to end on the live volume, so go slowly and read each message.

**Copying a dump to your PC** (an extra safeguard): `docker compose cp backup:/backups/<file>.dump .` on the VM, then `scp` it to the PC. Not run here and not in a runbook as written; `docker compose cp` is documented for the other direction. Dumps contain real names and CIDs (public data, but keep them private).

## 6. Who to ask for help

- **Claude.** Start a new session in the folder `C:\Users\mdora\Claude\vatpac-statsV2` (not the folder `VATPAC-Stats V2`, which is the old v1 system) and paste the prompt in `NEXT_SESSION_PROMPT.md` (a Claude working file, kept outside the repo). It tells the session what to read first (`RESUME.md`, also a Claude working file kept outside the repo).
- **The repository and its issues:** https://github.com/t288matt/vatpac-statsV2 (the open issues list is the live backlog).
- **A human engineer:** any engineer who knows Docker and PostgreSQL can work from [RUNBOOKS/](RUNBOOKS/) and [ARCHITECTURE.md](ARCHITECTURE.md). The repository names no other person or support contact. I could not verify one, so add a name here if you have one.
- **VATSIM outages:** `https://status.vatsim.net/status.json`.

## 7. What NOT to do

- **Never run `db/roles.sql` against the live database.** Roles are cluster-wide; running it with other passwords resets the live role passwords and breaks the stack. (The `migrate` job applies it safely on its own.)
- **Never run `docker system prune`, `docker volume prune` or `docker compose down -v`, and never delete a Docker volume.** `vatpac_pgdata` is the data, `vatpac_backups` holds every backup, `vatpac_grafana_data` the Grafana state, `vatpac_archive` the raw-feed safety copy. Several rollback copies sit in the VM's home folder (`vatpac-statsV2.rollback-*`) and a volume `vatpac_grafana_data_pre_bumps`; leave them for the owner's planned clean-up.
- **Never run test suites, scratch databases or throwaway containers on the VM** without memory caps. An uncapped test container took 6.9 GB of the 7.9 GB on 08-10-2026 and cost the collector a gap (tracker T39).
- **Never print or paste `.env`**, the Telegram token or the Grafana password anywhere, including into a chat with an AI.
- **Do not expose Grafana itself to the internet.** Only the 10 published read-only dashboards are public (ADR-029, `https://vpac.mpengor.com`, through Cloudflare and Traefik). Never forward the Grafana port on the router, never bind it to `0.0.0.0`, never let visitors run their own queries, and keep anonymous access OFF (`GF_AUTH_ANONYMOUS_ENABLED=false`). To take the public address down in an emergency, see [RUNBOOKS/public-access.md](RUNBOOKS/public-access.md) (Roll back).
- **Do not edit files on the VM by hand** (the next deploy overwrites them, and `.env` is the only file meant to differ). Do not `git pull` or `git reset --hard` there; deploys use `git checkout --detach <commit>` (section 7b). The checkout is read-only (deploy key) and must stay clean: `git status --short` prints nothing.
- **Do not touch or stop the old v1 VM** (alias `vatpac`) or the folder `VATPAC-Stats V2` on your PC.
- **Do not drop partitions or delete rows by hand** to free space; ask first (db-full runbook).
- **Do not deploy** anything while Claude is unavailable.

## 7b. Deploying by hand, and the registry token (steps run once on 14-10-2026; a second deploy and a rollback to a previous tag are not yet run)

This card still says: do not deploy while Claude is unavailable. If you decide to anyway, or you must roll back, use the numbered steps in [RUNBOOKS/deploy-to-vm.md](RUNBOOKS/deploy-to-vm.md) sections 2e and 2f (deploy) and "Roll back". The short version, from `~/vatpac-statsV2` on the VM, with a fresh dump first (`docker compose exec backup backup.sh once`):

- Update to a commit that has been published: `git fetch origin`, `git checkout --detach <commit>`, change the one line `VATPAC_IMAGE_TAG=sha-<first 7 characters>` in `.env`, then `docker compose config --images`, `docker compose pull`, `docker compose up -d --no-build`, then `bash ops/deploy.sh --verify` from the PC.
- Roll back, first cycle (14-10-2026): no earlier registry tag exists, so use the locally built images still on the VM. Remove BOTH `VATPAC_IMAGE_*` lines from `.env` (`sed -i '/^VATPAC_IMAGE_/d' .env`; with only one the stack refuses to start), then `docker compose up -d --no-build`. After a second registry deploy, roll back by checking out the previous commit and setting the previous `sha-` tag, then `pull` and `up -d --no-build`. Images cannot undo a database migration: if a migration caused the problem, restore the dump ([RUNBOOKS/restore.md](RUNBOOKS/restore.md)).
- Publishing a commit (needed before it can be pulled) is done on GitHub: Actions > release > Run workflow. Only commits on main with a green CI run can be published.
- **If `docker compose pull` says `unauthorized` or `denied`**, the registry token has expired (it lasts 90 days). The stack keeps running; only deploys are blocked. Create a new classic token with only `read:packages` and run `docker login ghcr.io -u t288matt` on the VM and paste the token at the hidden `Password:` prompt (nothing is shown), then press Enter. Never paste the token into a chat. Details: section 2e of the deploy runbook.
- Never use `--build` on the VM in registry mode, and never delete just one of the two `VATPAC_IMAGE_*` lines: with only one set the stack refuses to start (`sh ops/check_env.sh`). The token expires about 12-01-2027 (reminder 05-01-2027).

## 8. Where things stand that you might trip over (10-10-2026)

- The code on `main` is ahead of what is deployed (backup and restore hardening, Telegram wording, collector hardening). If the runbooks describe a feature your VM does not show, check [../CHANGELOG.md](../CHANGELOG.md) for whether it is deployed. In particular [RUNBOOKS/restore.md](RUNBOOKS/restore.md) and [RUNBOOKS/alerts.md](RUNBOOKS/alerts.md) already describe the newer behaviour; the commands on this card work with both.
- The daily digest email and the Claude desktop app: the digest only runs while the Claude desktop app is open.
