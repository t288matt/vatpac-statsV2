# Runbook: deploy to the Linux VM

> **Note, issue 89 phase 5 (LIVE since 13-10-2026, marker 7a71d30 at 10:05:56 UTC).** This is a dated record. It uses the names as they were when it was written: `flight_summary` meant the old pieces table, `flight_record` the table of finished flights. Migration 20261013000300 (live) dropped the pieces table, `flight_part`, `flight_legacy`, `refresh_flight_summary` and `refresh_flight_summary_sessions`, and renamed `flight_record` to `flight_summary`. Current description: [DATA_DICTIONARY.md](../DATA_DICTIONARY.md) and ADR-030.

Last reviewed 13-10-2026; the commit running in production is 7a71d3054506d5d6e5e0165860de0feb97e0f651 (issue 89 phases 4 and 5, deployed together about 10:03 UTC), release marker of 13-10-2026 10:05:56 UTC. Marker history read from the VM's `.deploy/release.log` on 13-10-2026: b3189ad (11-10-2026 20:35:53 UTC), c70e72f (phase 1, 12-10-2026 17:58:21 UTC), 3d4b5b6 (phase 1b, 13-10-2026 08:01:09 UTC), 38e3958 (phase 3, 13-10-2026 08:14:12 UTC), 7a71d30 (phases 4 and 5, 13-10-2026 10:05:56 UTC). The phase 4 and 5 deploy was done by the owner with a pre-deploy dump (the only way back from phase 5, which is forward-only), a rollback copy of the tree on the VM and the scripted checks of `ops/deploy.sh` (preflight 12 checks, then verify); the plan, the decision tree for each failure and the 24 hour watch list are `DEPLOY_PLAN_PHASE_4_5.md` in the `vatpac-claude-setup` repository. Sections written before 11-10-2026 were not all re-checked line by line against that commit; where a step names 3eb81a3 or deploys 1 and 2 at 520dc3e, read it as history. Target: always-on Linux VM (Debian/Ubuntu), 2 vCPU, 4 GB RAM, **40 GB+ SSD**. The production VM is reached as `ssh vatpac-v3` (an alias in the owner's `~/.ssh/config`) and the stack lives in `~/vatpac-statsV2`. That VM has the git program installed (version 2.43.0 on 10-10-2026) but `~/vatpac-statsV2` is not a git checkout and the VM holds no GitHub credentials, so code is deployed with `git archive` (section 2c). Times in this runbook are UTC.

Disk sizing: about 2 to 3 GB of positions at 90 days, plus WAL (up to 2 GB), plus `BACKUP_RETENTION_DAYS` (default 14) dumps on the same disk, plus Docker images, logs and build cache. 20 GB is too tight. Provision 40 GB or more, or set `BACKUP_RETENTION_DAYS=7` and copy dumps off-box (`restore.md`, "Off-box copies"). The watchdog checks the filesystems listed in `WATCHDOG_DISK_PATHS` (default `/backups,/`, which is the Docker data-root filesystem that holds the `pgdata` and `backups` named volumes) and warns at `WATCHDOG_DISK_PCT` (80%). If you move a volume onto its own mount, mount that path into the watchdog and add it to the list.

## 0. VM CPU type (Proxmox or QEMU)
Set the VM CPU type to `host` (or `x86-64-v2-AES`), not the default "Common KVM processor": the collector uses numpy 2.x, which needs the x86-64-v2 baseline (SSE4.2, POPCNT) and otherwise crash-loops on import (07-10-2026). Check on the VM: `grep -m1 -ow sse4_2 /proc/cpuinfo` prints `sse4_2`. Changing the type needs a full stop and start of the VM, not a reboot.

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

## 2b. Pin a release (by commit)
```sh
git fetch origin && git checkout --detach <commit>   # deploy a commit (sha), not a moving branch; there are no semver tags (issue 173)
```

## 2c. Copy the code without git (the method in use on the production VM)
Run from the repository root on the PC, with the commit you want to deploy checked out and committed (`git archive` ships committed files only):
```sh
git archive --format=tar HEAD | ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'
```
The VM's `.env` is not in git, so it is kept. `tar -x` overwrites files but never deletes: if a file was removed or renamed in git, delete it on the VM too (a stale module under `collector/src` would still be baked into the image, and a stale migration file would still be applied). For a first install, create the folder first (`ssh vatpac-v3 'mkdir -p ~/vatpac-statsV2'`), copy, then continue at section 3.

## 2d. Git checkout on the VM (read-only deploy key)
DRAFT (issue 173, phase P0; prepared 14-10-2026, not yet run): 2d is a draft until run, and until then the VM is still not a git checkout. Replaces the tar copy of section 2c with a git checkout of the live folder, so a deploy is `git fetch && git checkout <commit>` and stale files cannot survive. The owner runs every VM command. Each step says where it runs (PC or VM), with a check after it.

**What was read on the VM on 14-10-2026 (read-only).** git 2.43.0; 161 GB free; `~/vatpac-statsV2` has no `.git`; no deploy key yet (only the backup key); `ssh -T git@github.com` from the VM reaches GitHub and answers "Permission denied (publickey)", which only means no key is installed. The compose project name is already fixed by `name: vatpac` (docker-compose.yml line 8) and `.env` has no `COMPOSE_PROJECT_NAME`, so the folder name does not affect container, network or volume names. `config/`, `grafana/`, `db/`, `ops/postgres/` and `docker-compose.yml` on the VM are byte-identical to main (git blob hashes compared). Files on the VM that main does not have (15, all stale): `docs/NEXT_SESSION_PROMPT.md`, `docs/RESUME.md`, `ops/audit/logon_precision_impact.py`, `ops/audit/panel_recompute.py`, `ops/audit/throwaway/retention_drill.sql`, `ops/audit/throwaway/synthetic_flights.sql`, `ops/backfill_flight_record.sh`, `ops/compare_flights_89.sql`, `ops/compare_views_89_phase3.sql`, `tests/integration/test_compare_flights.py`, `tests/integration/test_exclude_in_progress.py`, `tests/integration/test_flight_in_progress.py`, `tests/integration/test_flight_summary.py`, `tests/integration/test_flight_writer_equivalence.py`, `tests/test_in_progress_panels.py`. Files that must survive: `.env`, `.env.bak-pre-30s`, `.env.bak-pre-telegram`, `.deploy/` (release marker and log) and `.claude/`.

**Order matters:** step 4 (it accepts GitHub's host key) must run before step 5.

**Method: adopt the live folder in place; do not swap folders.** Renaming the folder is the risky route. The running `db`, `collector`, `grafana` and `watchdog` containers have `./config`, `./db`, `./grafana/...` and `./ops/postgres/postgresql.conf` bind-mounted, and a bind mount follows the folder you renamed, not a new one at the old path. `docker compose up -d` would not recreate them (their configuration is unchanged), so they would keep reading the renamed old folder until a `--force-recreate`, which restarts the database and costs about 45 seconds of snapshots. Moving only `.git` into the live folder changes no file, so nothing restarts. Do it when no soak is running (see [daily-checks.md](daily-checks.md)).

1. **VM: check the starting point.**
```sh
cd ~/vatpac-statsV2 && git --version && df -h / | tail -1 && ls -d .git 2>&1 | head -1 && head -3 .deploy/release
```
Check: git 2.43 or newer, over 20 GB free, `.git` reported missing, and a `commit=` line (the deployed commit; called `<deployed>` below).

2. **VM: make the read-only deploy key.**
```sh
ssh-keygen -t ed25519 -f ~/.ssh/vatpac_deploy -N "" -C "vatpac-vm-deploy-readonly" && cat ~/.ssh/vatpac_deploy.pub
```
Check: one line starting `ssh-ed25519`. Copy only that public line (the file without `.pub` never leaves the VM).

3. **PC (browser): add it to GitHub.** Repository t288matt/vatpac-statsV2, Settings > Deploy keys > Add deploy key; title `vatpac-vm 14-10-2026`; paste the line; leave **Allow write access OFF**; Add key. Check: the list shows the key as read-only.

4. **VM: test the key.**
```sh
GIT_SSH_COMMAND="ssh -i ~/.ssh/vatpac_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" git ls-remote git@github.com:t288matt/vatpac-statsV2.git HEAD
```
Check: prints one commit hash and `HEAD`. "Permission denied" means step 3 was not saved.

5. **VM: clone beside the live folder (the live folder is not touched) and check out the deployed commit.**
```sh
GIT_SSH_COMMAND="ssh -i ~/.ssh/vatpac_deploy -o IdentitiesOnly=yes" git clone git@github.com:t288matt/vatpac-statsV2.git ~/vatpac-statsV2-git
git -C ~/vatpac-statsV2-git checkout --detach <deployed>
```
Check: `git -C ~/vatpac-statsV2-git log --oneline -1` shows `<deployed>`.

6. **VM: compare the clone with the live folder.**
```sh
diff -rq ~/vatpac-statsV2-git ~/vatpac-statsV2 -x .git -x '.env*' -x .deploy -x .claude -x __pycache__ -x '*.pyc'
```
Check: the only lines are `Only in /home/matt/vatpac-statsV2/...` for the 15 stale files above. Any `Files ... differ`, or an `Only in` for a file not in that list, means stop and report it: it is a hand edit on the VM that the next step would lose.

7. **VM: save, then remove, the stale files.** A stale migration file would be applied by `migrate`; removing them is the point of this change.
```sh
cd ~/vatpac-statsV2
git -C ~/vatpac-statsV2-git ls-files -z | sort -z > ~/main-files.txt
find . -type f -not -path './.git/*' -not -path './.deploy/*' -not -path './.claude/*' -not -path '*/__pycache__/*' -not -name '*.pyc' -not -name '.env*' -print0 | sed -z 's#^\./##' | sort -z > ~/live-files.txt
comm -z -13 ~/main-files.txt ~/live-files.txt > ~/stale-files-14-10-2026.txt
tr -cd ' ' < ~/stale-files-14-10-2026.txt | wc -c
tar -czf ~/stale-files-14-10-2026.tgz --null -T ~/stale-files-14-10-2026.txt && xargs -0 rm -f < ~/stale-files-14-10-2026.txt
rm -f ~/main-files.txt ~/live-files.txt
```
Check: the count says 15 (stop if it does not, and send the list). The 15 was counted against main; the clone is at the deployed commit, so the count can differ if a file was deleted on main after the deployed commit: compare against the checked-out commit, not main. Then the step 6 diff prints nothing.

8. **VM: move the git data into the live folder.**
```sh
mv ~/vatpac-statsV2-git/.git ~/vatpac-statsV2/.git && rm -rf ~/vatpac-statsV2-git
cd ~/vatpac-statsV2 && printf '.deploy/\n.claude/\n' >> .git/info/exclude
git config core.sshCommand "ssh -i ~/.ssh/vatpac_deploy -o IdentitiesOnly=yes"
git status --short
```
Check: `git status --short` prints nothing (`.env*` is in `.gitignore`). If it lists modified files, stop (line endings or a hand edit); do not run `git reset --hard`.

9. **VM: confirm the stack did not change.**
```sh
cd ~/vatpac-statsV2 && docker compose config -q && docker compose ps --format '{{.Name}} {{.Status}}'
```
Check: no error; the same five services with their old uptimes (nothing restarted).

10. **PC: verify.** `bash ops/deploy.sh --verify`, then `bash ops/deploy.sh --status`. Check: all PASS and the marker unchanged. `--preflight`, `--mark` and `ops/stale_issues.sh` need no change: they `cd ~/vatpac-statsV2` over ssh and read `.deploy/release`, and that folder, its name and the marker are untouched.

**New deploy line (from the next deploy).** On the VM, with the commit chosen on the PC:
```sh
cd ~/vatpac-statsV2 && git fetch origin && git checkout --detach <commit> && git status --short && sh ops/check_env.sh && docker compose up -d --build
```
Then `bash ops/deploy.sh --verify` and `--mark <commit>` from the PC as before. Notes: `git checkout` removes files the new commit deleted, which `tar -x` never did. Like `tar -x`, it replaces the single-file mount `ops/postgres/postgresql.conf` with a new file, so a changed postgresql.conf still needs `docker compose restart db`. A checkout refuses to overwrite an untracked file that the new commit adds, which stops the deploy loudly instead of silently. Untracked files (other than `.env*`, `.deploy`, `.claude`) show in the `git status --short` printed by that line; look at it before the build.

**Registry mode (issue 173, P2; compose side built, not yet used on the VM).** `docker-compose.yml` takes the image of migrate, collector, backup and watchdog from two optional `.env` variables, `VATPAC_IMAGE_PREFIX` and `VATPAC_IMAGE_TAG` (docs/CONFIG_MAP.md). The owner must be told the exact two lines before they are added (they are in `.env.example`, commented out); until then nothing changes.
- Local-build mode (both unset; today): the deploy line above, unchanged.
- Registry mode (both set, after P3): `cd ~/vatpac-statsV2 && git fetch origin && git checkout --detach <commit> && docker compose config --images && docker compose pull && docker compose up -d --no-build`, where the tag in `.env` is `sha-<first 7 characters of <commit>>` and that commit was published by the release workflow. `config --images` first shows the resolved names (check they are the ghcr.io names and the tag you mean). After `pull` the images exist; `--no-build` is a safety net that guarantees nothing is built locally (without it, a missing image would be built from source). `docker compose pull` in local-build mode would try to pull `vatpac-stats-<name>:local` from Docker Hub and fail, so do not use it there. Set both variables or neither: `sh ops/check_env.sh` fails when only one is set.

**Roll back (to the tar method).** Nothing else changed.
- VM: `rm -rf ~/vatpac-statsV2/.git`, then use section 2c again.
- VM, optional, not recommended: `tar -xzf ~/stale-files-14-10-2026.tgz -C ~/vatpac-statsV2` restores the stale files.
- VM, to drop the key: `rm ~/.ssh/vatpac_deploy*`. PC (browser): delete the key under Deploy keys in GitHub.
- VM, check the stack is untouched: `cd ~/vatpac-statsV2 && git status` says "not a git repository", and `docker compose ps --format '{{.Name}} {{.Status}}'` shows the same five services with their old uptimes.

**If you prefer swapping folders anyway** (not recommended): copy `.env*`, `.deploy/` and `.claude/` into the clone, run `docker compose config -q` there, rename the live folder to `~/vatpac-statsV2-old` and the clone to `~/vatpac-statsV2`, then `docker compose up -d --force-recreate` (restarts every service including the database; about 45 seconds of snapshots) and `bash ops/deploy.sh --verify`. Way back: rename the folders back and force-recreate again.

## 3. Configure
`.env.example` has **blank secrets on purpose**: the stack will not start with trivial credentials.
```sh
sh ops/gen_env.sh          # writes .env (mode 600) with random hex passwords; prints only the variable names
# or: cp .env.example .env, chmod 600 .env, and fill the blanks yourself
sh ops/check_env.sh        # rejects blank, change-me*, under 16 characters, repeated, or non letter/digit values
```
`make up` runs `check_env` first. Secrets: `POSTGRES_PASSWORD` (owner; used by the db, migrate and restore only), `GRAFANA_RO_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`, `WATCHDOG_DB_PASSWORD`, `COLLECTOR_DB_PASSWORD` (role `collector`), `BACKUP_DB_PASSWORD` (role `backup_ro`). They sit inside connection URLs, so they must be letters and digits only. Then set `GRAFANA_ROOT_URL`, and `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` if you want alerts. Never commit `.env`.

Two caveats:
- `GRAFANA_ADMIN_PASSWORD` only applies on Grafana's **first start** (empty `grafana_data`). Changing it in `.env` later does nothing. To rotate: `docker compose exec grafana grafana cli admin reset-admin-password '<new password>'`, then update `.env` to match.
- Changing the other passwords: edit `.env`, then `docker compose up -d` (the migrate job runs `ALTER ROLE ... PASSWORD`; `POSTGRES_PASSWORD` itself is only read by Postgres at first initialisation, so rotate it with `ALTER ROLE` inside the database as well).

## 4. Start
```sh
make up                      # = check_env + docker compose up -d --build
docker compose ps            # migrate shows "exited (0)"; everything else healthy within about 2 minutes
```

## 4b. Special case: migration 20261008000100 (whole-second logon_time, T36 / ADR-021)
This migration merges duplicate sessions and rewrites `logon_time` in both session tables. It cannot be undone by its down half (the fractions are gone), so **take a fresh dump first** (`docker compose exec backup backup.sh once`, then check the new file in `/backups`) and keep a copy of the VM's current tree. Stop the old collector before the migration runs (`docker compose stop collector`), so nothing writes while sessions are merged, then `docker compose up -d --build`. Afterwards run `psql -X -f ops/check_identity_flap.sql` in the db container (every count must be 0). Roll-back is the restore of the pre-deploy dump (restore.md).

## 4c. Special case: the collector release with the rolling feed archive (T38; issues #63 and #64)
Three things are mandatory for this release. Each one has already cost, or would cost, data.
1. **Stop the collector, then build.** The deploy MUST use `docker compose stop collector` and then `docker compose up -d --build`. The new `archive` volume is created root-owned unless the NEW image (which creates and owns `/archive` for the collector user) is built first; an old or cached image would leave the volume unwritable by the collector and the archive would silently never write (issue #64; the collector keeps polling, so nothing else looks wrong). Never use `docker compose up -d` without `--build` for this release, and never `docker compose restart collector` to pick up a code change (a restart does not rebuild the image). A plain `docker compose restart collector` IS allowed for a hung collector when there is no code change (owner decision 10-10-2026; see stuck-collector.md).
2. **Check the VM clock before deploying** (issue #63): `timedatectl` must show `System clock synchronized: yes` and `NTP service: active`. The collector refuses a snapshot stamped more than `FEED_MAX_FUTURE_SECONDS` (default 120) ahead of the VM clock, so a VM clock more than about 128 s slow makes every poll an error. Fix the clock (`sudo timedatectl set-ntp true`, or ask the owner) before the deploy, not after.
3. **Check the archive after the deploy.** About a minute after the collector starts, `docker compose exec -T collector python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())"` (the health port is not published; 8080 is the default `HEALTH_PORT`) must show an `archive` block with `enabled: true`, `last_write` set to a time within the last few minutes (and `last_write_age_seconds` small), and error counters at 0. A `last_write` of null, or growing `last_write_age_seconds`, means the archive is not writing: check `docker compose logs --tail=50 collector` and the ownership of the `archive` volume. The watchdog also alerts on an old archive.
Order for this release: fresh dump (`docker compose exec backup backup.sh once`), a copy of the VM's tree for rollback, `timedatectl`, `git archive` copy (section 2c), `docker compose stop collector`, `docker compose up -d --build`, `sh ops/smoke.sh`, `python3 ops/check_panels.py`, restart counts 0, the `/health` archive check above, and `ops/check_identity_flap.sql` (three zeros).

## 4d. Special case: the Dependabot image bumps (T48; issue #31)
Status 09-10-2026: DEPLOYED at 13:46 UTC (commit 8905d85) and verified; the steps below are the record of how it was done. The Grafana volume was copied first (a throwaway container with `--memory 256m --memory-swap 256m --cpus 0.5` running `cp -a` from the volume into a new one).
Postgres 16.4 to 16.15 (`db` and `ops/backup`), Grafana 11.2.2 to 11.6.5 and dbmate 2.21.0 to 2.36.0 were tested on throwaway containers, not on the live stack. Deploying them recreates the containers: `docker compose up -d --build` recreates `db` (same major version 16, the `pgdata` volume is reused, about 10 to 30 seconds of database downtime), `grafana` (about a minute; `grafana_data` is upgraded in place on first start, so copy the volume first if rollback matters) and rebuilds `backup` and `migrate`. Do it in a quiet period, take a fresh dump first (`docker compose exec backup backup.sh once`), then run `sh ops/smoke.sh` and `python3 ops/check_panels.py`. Never let the `backup` image tag differ from the `db` image tag.

## 4d2. Special case: Grafana 11.6.5 to 12.4.12 (DEPLOYED 11-10-2026 at about 19:40 UTC, commit 848526c)
The image changed from `grafana/grafana-oss:11.6.5` to `grafana/grafana:12.4.12` (the OSS edition; the 11.6 line ended with 11.6.15 on 03-06-2026 and `grafana-oss` stops at 11.6.5). It was tested first on a copy of the live `grafana_data` volume. Rollback copies on the VM: `~/grafana_data_pre_12.tgz` (the volume) and `~/vatpac-statsV2.rollback-11-10-grafana` (the tree). To go back: stop Grafana, restore the volume copy, restore the tree, pin `grafana/grafana-oss:11.6.5`, `docker compose up -d grafana`; the upgraded volume cannot be read by the older version. Afterwards the public links were created (public-access.md). The fixed `ops/grafana_public.py` reached the VM tree with commit b3189ad (20:35 UTC the same evening).

## 4e. Special case: the scheduled integrity checks (T16, issue #20; DEPLOYED 09-10-2026 at 19:13 UTC)
Status: DEPLOYED 09-10-2026 at 19:13 UTC (see tracker T16); the steps below are the record. Do not repeat any of this without the owner's go (deploy policy, [../TRACKER.md](../TRACKER.md)). What it changes: the watchdog image (new build context `./ops`, new files `integrity.py`, `independent.py`, `invariants.sql`), two read-only mounts on the watchdog (`archive:/archive:ro` and `./config:/config:ro`), three settings (`WATCHDOG_INTEGRITY_INTERVAL_HOURS=72`, `WATCHDOG_CROSSCHECK_INTERVAL_DAYS=14`, `WATCHDOG_CROSSCHECK_SAMPLES=24`, all with defaults in `docker-compose.yml`, so `.env` needs no edit) and three new `SELECT` grants for `watchdog_ro` (`pilot_position`, `flight_summary`, `reference_data_change`). No migration file, no collector change: collector polling is not interrupted.

**Timing (done: it was deployed before that time and the warning appeared).** The first sweep runs on the first watchdog cycle and will find the 6 minute snapshot gap of the power cut of 09-10-2026 (15:35:57 to 15:41:57 UTC, tracker T51) while it is inside the 3 day window. If deployed before 12-10-2026 15:42 UTC, expect one `WARN data_integrity: 1 of 18 invariants report violations: snapshot gaps over 5 minutes (1)` and a `RECOVERED` up to 6 hours after the gap leaves the window. That is a live proof that the alert path works; deploy after that time for a clean start (no message).

Steps (all from `~/vatpac-statsV2` on the VM, after the `git archive` copy of the tree, section 2c):
1. **Fresh dump and rollback copy.** `docker compose exec backup backup.sh once`; check the new file in `/backups`; copy the current tree (`cp -a ~/vatpac-statsV2 ~/vatpac-statsV2.rollback-<dd-mm>`).
2. **Migrate job first.** `docker compose up -d --build migrate`. It re-applies the roles and the `watchdog_ro` grants and exits 0; nothing restarts. Check: `docker compose ps -a migrate` shows `exited (0)`, and
   ```sh
   echo "SELECT t FROM unnest(ARRAY['pilot_position','flight_summary','reference_data_change']) t WHERE has_table_privilege('watchdog_ro', t, 'SELECT')" |      docker compose exec -T db sh -c 'psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At'
   ```
   prints all three names. Never run `db/roles.sql` by hand.
3. **Rebuild the watchdog only.** `docker compose up -d --build watchdog`. Only the watchdog is recreated (its status volume is kept, so the schedule state survives).
4. **Checks afterwards.**
   - `docker compose ps`: `watchdog` is `healthy`, `collector` has the same start time as before (not restarted).
   - `docker compose logs --tail=50 watchdog | grep -i integrity`: lines for the invariant sweep and the archive cross-check on the first cycle (the cross-check takes about 25 s).
   - `docker compose exec -T watchdog cat /var/watchdog/watchdog_status.json`: `data_integrity` and `feed_crosscheck` present, `state.integrity.<name>.last_run` set. Expect `feed_crosscheck` ok (24 snapshots match, or fewer if the archive is young).
   - `could not run (InsufficientPrivilege)` means step 2 did not apply the grants: repeat it.
   - `sh ops/smoke.sh` ends `SMOKE PASS`; `python3 ops/check_panels.py` reports 0 errors.
5. **Roll back.** `docker compose up -d --build watchdog` from the previous tree (the extra grants are harmless to leave). To switch the checks off without a rollback, set `WATCHDOG_INTEGRITY_INTERVAL_HOURS=0` and `WATCHDOG_CROSSCHECK_INTERVAL_DAYS=0` in `.env` and `docker compose up -d watchdog`.

Runbook for the checks themselves: [integrity-checks.md](integrity-checks.md).

## 5. Network exposure and firewall
Only Grafana is published, and by default **only on loopback**: `127.0.0.1:${GRAFANA_PORT:-3300}`. The database is not published. To reach Grafana from the LAN, opt in explicitly in `.env`:
```sh
GRAFANA_BIND=<VM_LAN_IP>    # the VM's LAN address (preferred), or 0.0.0.0 for every interface
GRAFANA_PORT=3300
GRAFANA_COOKIE_SECURE=false  # keep false for plain http on a LAN; set true only behind https/TLS
```
then `docker compose up -d grafana`. Prefer a specific LAN IP over `0.0.0.0`. Combine with the firewall:
```sh
sudo ufw default deny incoming
sudo ufw allow OpenSSH
sudo ufw allow from <your LAN CIDR> to any port 3300 proto tcp
sudo ufw enable
```
Docker publishes ports by editing iptables directly and can bypass ufw. If the VM has a public interface, bind Grafana to the LAN address instead of `0.0.0.0`, or put the VM behind a cloud firewall. Do not publish Grafana's port directly to the internet. The public read-only setup (Grafana public dashboards behind a Cloudflare Tunnel and a reverse proxy on another machine) is described in [public-access.md](public-access.md). When the proxy terminates TLS, set `GRAFANA_ROOT_URL=https://...` and `GRAFANA_COOKIE_SECURE=true`.

Public read-only access (since 11-10-2026): Grafana public dashboards, managed by `ops/grafana_public.py`, behind Cloudflare and a Traefik helper on another machine; see [public-access.md](public-access.md). Grafana still listens on the LAN address only, and the router forwards nothing.

Grafana is also hardened with: external snapshots disabled, Gravatar disabled, viewers cannot edit, anonymous access and sign-up off, all Linux capabilities dropped, `no-new-privileges`. (A read-only root filesystem for Grafana is not enabled; it has not been validated.)

## 6. Verify
```sh
make smoke                                   # PASS/FAIL per check, non-zero on failure (without make: sh ops/smoke.sh)
docker compose logs --tail=50 collector      # polls every 30 s (POLL_INTERVAL_SECONDS)
docker compose exec backup backup.sh once    # first backup, then `ls` the volume (see restore.md)
```
`python3 ops/check_panels.py` runs every dashboard query through Grafana's own API; expect `0 errors` (empty panels are fine while data is young). It reads `.env` and never prints the password.

Open `http://localhost:3300` on the VM (or `http://<GRAFANA_BIND>:3300` from the LAN), log in as the admin user from `.env`. Charts start empty (no backfill).

## 7. Reboot test
`sudo reboot`; after boot `docker compose ps` should show everything back (restart: unless-stopped).

## Updating
Before deploying read [daily-checks.md](daily-checks.md): **a redeploy restarts the collector, which costs about 45 seconds of snapshots, and breaks a 24 hour soak that is in progress.** Do not redeploy while a soak is running unless it is necessary, and say so if you do.

With git on the host:
```sh
git fetch origin && git checkout --detach <new commit>   # a commit (sha); no semver tags
sh ops/check_env.sh                # new releases may add secrets (see .env.example); add any blank ones
docker compose up -d --build     # migrate runs first; collector waits for it
make smoke
```
Without git (production VM), from the PC:
```sh
git archive --format=tar HEAD | ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'
ssh vatpac-v3 'cd ~/vatpac-statsV2 && sh ops/check_env.sh && docker compose up -d --build'
ssh vatpac-v3 'cd ~/vatpac-statsV2 && sh ops/smoke.sh && python3 ops/check_panels.py'
```
`docker compose up -d --build` rebuilds the images whose inputs changed, recreates only the services whose image or configuration changed, and starts the `migrate` job before the services that wait for it. Confirm the migrations ran: `docker compose ps -a migrate` shows `exited (0)`, and `docker compose logs --tail=20 migrate` ends with `migrate: done`. If a new migration did not run, `docker compose up migrate` runs the job on its own (it is idempotent).

**Silent issues (built 11-10-2026, issue #104).** After a verified deploy, `bash ops/deploy.sh --mark <commit>` first lists every issue named in the commits since the previous marker and refuses to mark if any has no comment newer than its newest commit: comment on each (what shipped, the commit, how it was verified), then mark again. If `gh` fails, or no previous marker can be read, it refuses too; the first-ever mark on a VM therefore needs `--allow-silent-issues`. `--allow-silent-issues` marks anyway and prints a warning. To look without marking: `bash ops/stale_issues.sh` (since the last marker) or `--since <sha|date>`; it is read-only and exits 1 when it finds any. The guard is built and awaiting review; the deploy of it still needs the owner's go.

## What a change needs (rebuild, restart or reload)

| What changed | What to do | Interrupts polling? |
|---|---|---|
| `collector/src/**`, `collector/requirements.txt`, `collector/Dockerfile` | `docker compose up -d --build` (rebuilds the collector image and recreates the container) | Yes, about 45 s |
| `db/migrations/*.sql` (new numbered file; never edit an applied one) | `docker compose up -d`: `migrate` applies it (`./db` is mounted, no rebuild); if it did not run, `docker compose up migrate`. Then `sh ops/smoke.sh` | No |
| `db/roles.sql` (mounted like the migrations) | `docker compose up -d`: `migrate` re-applies the roles and grants. **Never run this file by hand against the live database**: it resets the live role passwords to whatever you pass | No |
| `ops/migrate/**` (the migrate image itself) | `docker compose up -d --build` | No |
| `grafana/dashboards/**/*.json` | Nothing to restart: the provider rescans the read-only mount every 30 seconds. Reload the browser page | No |
| `grafana/provisioning/**` (datasource or provider settings) | `docker compose restart grafana` (provisioning is read at start-up) | No |
| `config/*.json`, `config/*.txt` (polygon, whitelist) | No rebuild: `docker compose kill -s HUP collector` (see [update-reference-files.md](update-reference-files.md)) | No |
| `.env` (a password) | `docker compose up -d` (the `migrate` job runs `ALTER ROLE`; `POSTGRES_PASSWORD` and `GRAFANA_ADMIN_PASSWORD` only apply at first start, see section 3) | The services using the changed value are recreated |
| `.env` (a collector setting) | `docker compose up -d collector` | Yes, about 45 s |
| `docker-compose.yml` | `docker compose up -d` (recreates the services whose definition changed; a new named volume, such as `archive`, is created by the same command) | Only for those services |
| `ops/postgres/postgresql.conf` | `docker compose restart db` | Yes, the database restarts; the collector retries |
| `ops/watchdog/**`, `ops/audit/independent.py`, `ops/audit/invariants.sql` (the watchdog image copies the last two), `ops/backup/**` | `docker compose up -d --build watchdog` or `backup` | No |
| `ops/*.sh`, `ops/*.py`, `grafana/*.py`, `docs/`, `tests/`, `Makefile` | Nothing: they run from the host or are only documentation | No |

## Roll back
Code, dashboards and configuration go back by deploying the previous commit; the database does not go back by itself.
1. Note the commit that was deployed before (`git log --oneline`), then deploy it: `git archive --format=tar <previous commit> | ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'`, then `docker compose up -d --build` on the VM.
2. Delete files the newer commit added, because `tar -x` leaves them behind. In particular delete a newer migration file you do not want applied **before** running `docker compose up -d`, or `migrate` will apply it.
3. Migrations are forward-only. A migration that already ran stays applied after a code rollback; additive migrations (a new view, an index) are harmless to older code. If a migration must be undone, add a new corrective numbered migration, or restore from backup (`restore.md`); never edit an applied migration.
4. Verify with `sh ops/smoke.sh` and `python3 ops/check_panels.py`.

With git on the host, check out the previous commit (sha) instead and run `docker compose up -d --build`.

## Image pinning
Third-party images (`postgres`, `grafana`, `dbmate`, `python`) are pinned by exact version tag. Digest pinning (`image:tag@sha256:...`) is the stricter policy; add digests when you can resolve them with `docker buildx imagetools inspect <image>:<tag>`. Dependabot (docker and docker-compose ecosystems) proposes bumps.

## Publishing commit-tagged images (issue 173, P1; built, not yet run)
The `release` workflow (`.github/workflows/release.yml`) is started by hand: Actions > release > Run workflow, or `gh workflow run release.yml -f commit=<sha>`. The commit must be on main and its `ci` run on main must have passed (a docs-only commit skips CI, so pick the last code commit). The workflow checks that, does not repeat CI, and pushes `ghcr.io/t288matt/vatpac-stats-{collector,watchdog,backup,migrate}:sha-<7 characters>` (the full SHA is in the `org.opencontainers.image.revision` label). There is no `latest` and no semver tag. The packages are created private (the repository is private); leave them private. The `migrate` image now contains `db/`, so its migrations always match the code it was built from. Nothing pulls these images yet: the stack still builds on the VM (`docker-compose.yml` keeps the `:local` names and the `./db` mount) until phase P2.
