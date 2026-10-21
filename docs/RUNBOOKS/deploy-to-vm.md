# Runbook: deploy to the Linux VM

> **Note, issue 89 phase 5 (LIVE since 13-10-2026, marker 7a71d30 at 10:05:56 UTC).** This is a dated record. It uses the names as they were when it was written: `flight_summary` meant the old pieces table, `flight_record` the table of finished flights. Migration 20261013000300 (live) dropped the pieces table, `flight_part`, `flight_legacy`, `refresh_flight_summary` and `refresh_flight_summary_sessions`, and renamed `flight_record` to `flight_summary`. Current description: [DATA_DICTIONARY.md](../DATA_DICTIONARY.md) and ADR-030.

Last reviewed 13-10-2026; the commit running in production is 7a71d3054506d5d6e5e0165860de0feb97e0f651 (issue 89 phases 4 and 5, deployed together about 10:03 UTC), release marker of 13-10-2026 10:05:56 UTC. Marker history read from the VM's `.deploy/release.log` on 13-10-2026: b3189ad (11-10-2026 20:35:53 UTC), c70e72f (phase 1, 12-10-2026 17:58:21 UTC), 3d4b5b6 (phase 1b, 13-10-2026 08:01:09 UTC), 38e3958 (phase 3, 13-10-2026 08:14:12 UTC), 7a71d30 (phases 4 and 5, 13-10-2026 10:05:56 UTC). The phase 4 and 5 deploy was done by the owner with a pre-deploy dump (the only way back from phase 5, which is forward-only), a rollback copy of the tree on the VM and the scripted checks of `ops/deploy.sh` (preflight 12 checks, then verify); the plan, the decision tree for each failure and the 24 hour watch list are `DEPLOY_PLAN_PHASE_4_5.md` in the `vatpac-claude-setup` repository. Sections written before 11-10-2026 were not all re-checked line by line against that commit; where a step names 3eb81a3 or deploys 1 and 2 at 520dc3e, read it as history. Target: always-on Linux VM (Debian/Ubuntu), 2 vCPU, 4 GB RAM, **40 GB+ SSD** (the minimum; the production VM has 4 cores, 6 GB RAM and 161 GB free, read 14-10-2026). The production VM is reached as `ssh vatpac-v3` (an alias in the owner's `~/.ssh/config`) and the stack lives in `~/vatpac-statsV2`. Since 14-10-2026 `~/vatpac-statsV2` is a read-only git checkout (deploy key) and the VM runs registry images (overview below and sections 2d to 2f); the `git archive` copy of section 2c is the fallback. Times in this runbook are UTC.

Disk sizing: about 2 to 3 GB of positions at 180 days, plus WAL (up to 2 GB), plus `BACKUP_RETENTION_DAYS` (default 14) dumps on the same disk, plus Docker images, logs and build cache. 20 GB is too tight. Provision 40 GB or more, or lower `BACKUP_RETENTION_DAYS` (fixed in `docker-compose.yml`; for an immediate change use the override file in [CONFIG_MAP](../CONFIG_MAP.md), "Where each setting lives") and copy dumps off-box (`restore.md`, "Off-box copies"). The watchdog checks the filesystems listed in `WATCHDOG_DISK_PATHS` (default `/backups,/`, which is the Docker data-root filesystem that holds the `pgdata` and `backups` named volumes) and warns at `WATCHDOG_DISK_PCT` (80%). If you move a volume onto its own mount, mount that path into the watchdog and add it to the list.

## Overview: how a deploy works (issue 173; run and verified on the production VM on 14-10-2026)

**Status on 14-10-2026, stated plainly.** The production VM now deploys by git checkout plus registry images. Run by the owner on 14-10-2026 and verified: section 2d steps 1 to 10 (finished 22:16 UTC; the 15 stale files were saved to `~/stale-files-14-10-2026.tgz` and removed); one manual run of the release workflow for commit 67c1ca0 (all four images published, tag `sha-67c1ca0`); the registry token and `docker login ghcr.io -u t288matt`; the two `.env` lines (`VATPAC_IMAGE_PREFIX=ghcr.io/t288matt/`, `VATPAC_IMAGE_TAG=sha-67c1ca0`); `docker compose pull` and `docker compose up -d --no-build` (migrate, collector, backup and watchdog recreated; db and grafana untouched); `bash ops/deploy.sh --verify` 10 PASS; release marker 67c1ca0 recorded at 22:29:46 UTC. **Still not run:** a second registry deploy (from one `sha-` tag to another), a rollback to a previous `sha-` tag, and renewing an expired token. Statements about those are marked "not yet run". **Confirmed:** the packages are private (the owner checked the GitHub Packages page on 14-10-2026).

**The idea.** The VM is a read-only git checkout of this repository. The four images that this project builds (collector, watchdog, backup, migrate) are built once by GitHub Actions and stored in a private registry (GHCR), tagged with the commit (`sha-` plus the first 7 characters). The VM pulls them instead of building them. A deploy is:

```sh
git fetch origin && git checkout --detach <commit> && docker compose pull && docker compose up -d --no-build
```

**What stays on the VM tree (mounts, not images):** `grafana/dashboards`, `grafana/provisioning`, `config/`, `db/` (the migrations; the migrate image also holds a copy) and `ops/postgres/postgresql.conf`. A dashboard change still shows within 30 seconds of a checkout, with no image and no Grafana restart. The VM also runs its own `ops/smoke.sh`, `ops/check_panels.py` and `ops/grafana_public.py` from the tree, which is why the tree stays.

**Not ours:** the postgres and grafana images (and the python base image of the builds) come from Docker Hub as before, at the exact versions pinned in `docker-compose.yml`. A move to floating major tags is proposed in issue 176 and is parked, not decided; this page does not assume it.

**Two modes, one compose file.** `VATPAC_IMAGE_PREFIX` and `VATPAC_IMAGE_TAG` in `.env` choose the mode ([../CONFIG_MAP.md](../CONFIG_MAP.md)):

| Mode | `.env` | Image names | Deploy command | Used by |
|---|---|---|---|---|
| Local build | both unset | `vatpac-stats-<name>:local` | `docker compose up -d --build` (`make up`) | PC, CI, dev, first installs, and the rollback on the VM |
| Registry | both set | `ghcr.io/t288matt/vatpac-stats-<name>:sha-<7>` | `docker compose pull`, then `docker compose up -d --no-build` | the production VM since 14-10-2026 |

**On the VM (registry mode) never use `up -d --build`.** It builds from source and tags the result with the registry name, replacing the pulled image, so the VM would run something other than the published image. `--no-build` makes that impossible. Local builds with `--build` are for a PC and for first installs. Setting only one of the two variables fails `sh ops/check_env.sh`.

**First-time set-up order on the VM** (the owner runs every VM command; each step has a check and a way back):

| Step | What | Where described | Status |
|---|---|---|---|
| 1 | Publish the images for the commit to be deployed (release workflow) | "Publishing commit-tagged images" at the end of this page | Run 14-10-2026 (67c1ca0) |
| 2 | Deploy key and git checkout of the live folder (phase P0) | section 2d | Run and verified 14-10-2026 |
| 3 | Registry token, `docker login`, the two `.env` lines, first pull (phase P3) | section 2e | Run and verified 14-10-2026 |
| 4 | Every later deploy | section 2f | Procedure; the second deploy is not yet run |

Owner cards: [../OWNER_ACTIONS.md](../OWNER_ACTIONS.md) cards 15 to 18. Decision record: ADR-031 in [../DECISIONS.md](../DECISIONS.md).

**The old method is kept as a fallback:** section 2c (`git archive | tar -x`) and the "Fallback" notes under Updating and Roll back. It never deletes files, so stale files on the VM survive it; read the warning in 2c before using it.

**A deploy can pull third-party updates (ADR-032).** `docker compose pull` also fetches the newest Postgres 16, Grafana 12 and dbmate 2 images, and they are not reviewed first. After the deploy run `bash ops/deploy.sh --verify` and open Grafana (`/api/health` must answer ok and the dashboards must load); if Grafana moved to a newer minor version, its data volume cannot go back, so keep the copy of the volume taken before the deploy.

**Dependabot and base images.** A merged Dockerfile or base-image bump (Dependabot) only changes main. The VM keeps running the old images until someone publishes the new commit, sets the tag in `.env` and pulls (section 2f).

**Deploy script checks for registry mode (added by pull request 180; merge that pull request first, otherwise these checks do not exist in `ops/deploy.sh`).** `--preflight` in registry mode checks over ssh that the four registry images for the commit exist (`docker manifest inspect`, which also detects an expired token) and that the VM `.env` tag equals `sha-<7>` of the commit being deployed (WARN: the owner sets it first). `--verify` and `--status` show the running image tags and FAIL in registry mode if the VM checkout HEAD does not match the images' revision label or the `.env` tag. `--mark` warns if the VM HEAD differs from the commit and records the image tag in `release.log` (the release file format is unchanged). Exact behaviour is in the usage text of `ops/deploy.sh` once that pull request is merged.

**Housekeeping and prune policy on the VM.**
- Keep the locally built `:local` images: they are the current rollback (see Roll back). Keep them, and the old `grafana/grafana-oss:11.6.5` image, until about 18-10-2026, then decide with the owner.
- Keep the current and the previous `sha-` image set. Remove older ones one by one with `docker image rm <image:tag>` after listing them (`docker images 'ghcr.io/*'`).
- Never run `docker system prune`. Never run `docker volume prune` blindly: named volumes hold the data, backups and Grafana state. The 85 unused anonymous volumes (about 10.6 GB, read 14-10-2026) are a separate cleanup: list them first and check each is anonymous and unused.
- The registry token lasts 90 days (expires about 12-01-2027; the owner's calendar reminder is 05-01-2027). The deploy preflight (pull request 180) fails early if the registry image cannot be read.
- Optional owner tidy: the VM `.env` carries a stale `VATPAC_VERSION=dev` line that nothing reads (`VATPAC_VERSION` is a build argument; the collector health output reports its own `__version__`). It can be deleted.

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

## 2c. FALLBACK: copy the code without git (the method in use on the production VM until section 2d is run)
**No-delete warning.** `tar -x` never deletes anything. Files removed or renamed in git stay on the VM: a stale migration file would still be applied by `migrate`, and a stale module under `collector/src` would still be baked into an image. The VM held 15 such stale files on 14-10-2026 (listed in section 2d). Use this method only when the git checkout of section 2d is not in place or cannot be used, and delete removed files by hand afterwards.

Run from the repository root on the PC, with the commit you want to deploy checked out and committed (`git archive` ships committed files only):
```sh
git archive --format=tar HEAD | ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'
```
The VM's `.env` is not in git, so it is kept. `tar -x` overwrites files but never deletes: if a file was removed or renamed in git, delete it on the VM too (a stale module under `collector/src` would still be baked into the image, and a stale migration file would still be applied). For a first install, create the folder first (`ssh vatpac-v3 'mkdir -p ~/vatpac-statsV2'`), copy, then continue at section 3.

## 2d. Git checkout on the VM (read-only deploy key)
RUN AND VERIFIED on 14-10-2026 (issue 173, phase P0): the owner ran steps 1 to 10, finished 22:16 UTC; `--verify` passed. The text below is the procedure as it was run, kept for rebuilding a VM; the VM is a git checkout now. It replaces the tar copy of section 2c with a git checkout of the live folder, so a deploy is `git fetch && git checkout <commit>` and stale files cannot survive. The owner runs every VM command. Each step says where it runs (PC or VM), with a check after it.

**What was read on the VM on 14-10-2026 (read-only, before the run).** git 2.43.0; 161 GB free; `~/vatpac-statsV2` has no `.git`; no deploy key yet (only the backup key); `ssh -T git@github.com` from the VM reaches GitHub and answers "Permission denied (publickey)", which only means no key is installed. The compose project name is already fixed by `name: vatpac` (docker-compose.yml line 8) and `.env` has no `COMPOSE_PROJECT_NAME`, so the folder name does not affect container, network or volume names. `config/`, `grafana/`, `db/`, `ops/postgres/` and `docker-compose.yml` on the VM are byte-identical to main (git blob hashes compared). Files on the VM that main does not have (15, all stale): `docs/NEXT_SESSION_PROMPT.md`, `docs/RESUME.md`, `ops/audit/logon_precision_impact.py`, `ops/audit/panel_recompute.py`, `ops/audit/throwaway/retention_drill.sql`, `ops/audit/throwaway/synthetic_flights.sql`, `ops/backfill_flight_record.sh`, `ops/compare_flights_89.sql`, `ops/compare_views_89_phase3.sql`, `tests/integration/test_compare_flights.py`, `tests/integration/test_exclude_in_progress.py`, `tests/integration/test_flight_in_progress.py`, `tests/integration/test_flight_summary.py`, `tests/integration/test_flight_writer_equivalence.py`, `tests/test_in_progress_panels.py`. Files that must survive: `.env`, `.env.bak-pre-30s`, `.env.bak-pre-telegram`, `.deploy/` (release marker and log) and `.claude/`.

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

**Deploying after 2d.** Section 2d only turns the live folder into a git checkout. After 2e (done 14-10-2026), deploys on the VM use section 2f (registry mode). This local-build line is only for a PC, or for the VM after the rollback of section 2e step 8 (both image variables unset); never use it while the `VATPAC_IMAGE_*` lines are set:
```sh
cd ~/vatpac-statsV2 && git fetch origin && git checkout --detach <commit> && git status --short && sh ops/check_env.sh && docker compose up -d --build
```
Then `bash ops/deploy.sh --verify` and `--mark <commit>` from the PC as before. Notes: `git checkout` removes files the new commit deleted, which `tar -x` never did. Like `tar -x`, it replaces the single-file mount `ops/postgres/postgresql.conf` with a new file, so a changed postgresql.conf still needs `docker compose restart db`. A checkout refuses to overwrite an untracked file that the new commit adds, which stops the deploy loudly instead of silently. Untracked files (other than `.env*`, `.deploy`, `.claude`) show in the `git status --short` printed by that line; look at it before the build. `docker compose pull` in local-build mode would try to pull `vatpac-stats-<name>:local` from Docker Hub and fail, so do not use it there.

**Roll back (to the tar method).** Nothing else changed.
- VM: `rm -rf ~/vatpac-statsV2/.git`, then use section 2c again.
- VM, optional, not recommended: `tar -xzf ~/stale-files-14-10-2026.tgz -C ~/vatpac-statsV2` restores the stale files.
- VM, to drop the key: `rm ~/.ssh/vatpac_deploy*`. PC (browser): delete the key under Deploy keys in GitHub.
- VM, check the stack is untouched: `cd ~/vatpac-statsV2 && git status` says "not a git repository", and `docker compose ps --format '{{.Name}} {{.Status}}'` shows the same five services with their old uptimes.

**If you prefer swapping folders anyway** (not recommended): copy `.env*`, `.deploy/` and `.claude/` into the clone, run `docker compose config -q` there, rename the live folder to `~/vatpac-statsV2-old` and the clone to `~/vatpac-statsV2`, then `docker compose up -d --force-recreate` (restarts every service including the database; about 45 seconds of snapshots) and `bash ops/deploy.sh --verify`. Way back: rename the folders back and force-recreate again.

## 2e. Registry login and the two `.env` lines (issue 173, phase P3; run and verified on the VM on 14-10-2026)
**Run record, 14-10-2026.** Token created (classic, `read:packages` only, 90 days, expires about 12-01-2027, calendar reminder 05-01-2027); `docker login ghcr.io -u t288matt` done; `.env` lines added with the tag `sha-67c1ca0`; `docker compose pull` and `docker compose up -d --no-build` ran (migrate, collector, backup, watchdog recreated; db and grafana untouched); `--verify` 10 PASS; marker 67c1ca0 recorded 22:29:46 UTC. The steps below are the procedure as followed, for the next time (a new VM, or a renewed token).

Do this after section 2d is done and checked, and after the images for the target commit have been published (see "Publishing commit-tagged images"). The owner runs every command on the VM. Nothing here is typed into chat, committed to git, or written into `.env` except the two non-secret lines in step 4.

**What it needs.** A GitHub personal access token (classic) with ONE scope, `read:packages`. GitHub documents that its container registry only supports a personal access token (classic) (GitHub Docs, "Working with the Container registry", read 14-10-2026), so a fine-grained token cannot be used. Check at creation that only `read:packages` is ticked. Know what it can do: it can read every package the account `t288matt` can reach, until it expires. It cannot push (the release workflow pushes with its own short-lived workflow token, not this one).

1. **PC (browser): create the token.** GitHub > your profile > Settings > Developer settings > Personal access tokens > Tokens (classic) > Generate new token (classic). Note: `vatpac-vm ghcr read`. Expiration: **90 days**. Scope: tick `read:packages` only. Generate and copy it once. Put the expiry date in your calendar now, with a reminder 10 days before (see "When the token expires" below).
2. **VM: log in to the registry.**
```sh
docker login ghcr.io -u t288matt
```
Paste the token at the hidden `Password:` prompt (nothing is shown) and press Enter. The owner types or pastes the token; it is never put on the command line, in chat, in the repository or in `.env`. Check: it prints `Login Succeeded`. Docker stores the credential in `~/.docker/config.json` on the VM (base64, not encrypted): `ls -l ~/.docker/config.json` should show `-rw-------`. Anyone who can read that file can read the packages, so do not copy it.
3. **VM: take a fresh dump first** (a registry deploy cannot undo a migration): `cd ~/vatpac-statsV2 && docker compose exec backup backup.sh once`, then check the new file in `/backups` ([restore.md](restore.md)).
4. **The owner is told the exact two lines before they are added** (rule: no `.env` change without advance notice; the values are never printed back). With `<7>` the first 7 characters of the commit being deployed:
```
VATPAC_IMAGE_PREFIX=ghcr.io/t288matt/
VATPAC_IMAGE_TAG=sha-<7>
```
Neither line is secret. The prefix must end with a slash. The same text is in `.env.example`, commented out. Add them on the VM:
```sh
cd ~/vatpac-statsV2 && printf '\nVATPAC_IMAGE_PREFIX=ghcr.io/t288matt/\nVATPAC_IMAGE_TAG=sha-<7>\n' >> .env && grep '^VATPAC_IMAGE' .env && sh ops/check_env.sh
```
Check: `grep` prints exactly these two lines (it is the only way `.env` is read here; never `cat .env`) and `check_env.sh` passes.
5. **VM: look before pulling.**
```sh
docker compose config --images
```
Check: the four project images show as `ghcr.io/t288matt/vatpac-stats-{migrate,collector,backup,watchdog}:sha-<7>` with the tag you mean, and postgres and grafana are unchanged.
6. **VM: first pull, then start without building.**
```sh
docker compose pull && docker compose up -d --no-build
```
`pull` fetches the project images (their size is not known yet) and checks the others. `up -d --no-build` recreates the services whose image changed. Expect a collector restart of about 45 seconds of snapshots, so do not do it during a soak ([daily-checks.md](daily-checks.md)). `unauthorized` or `denied` at `pull` means the login, the token scope or the package access is wrong; nothing has changed yet, so fix that and repeat. `manifest unknown` means that tag was not published.
7. **PC: verify.** `bash ops/deploy.sh --verify` (all PASS), then open the ten public addresses (on the VM `python3 ops/grafana_public.py --list --public-url https://vpac.mpengor.com` lists them; [public-access.md](public-access.md)) and check each loads. Then `bash ops/deploy.sh --mark <commit>` (it first lists issues without a comment; see "Silent issues" under Updating).
8. **Way back (the rollback for the first registry cycle).** No earlier registry tag exists yet, so the way back is the locally built `:local` images that are still on the VM. VM: remove BOTH lines (`sed -i '/^VATPAC_IMAGE_/d' .env`), then `docker compose up -d --no-build`. Optionally first `git checkout --detach <the commit the local images were built from>` (the previous marker in `.deploy/release.log`) so the tree matches them. `--no-build` is right here because the `:local` images already exist; use `--build` only if they were removed (that is local-build mode, both variables unset). `docker logout ghcr.io` removes the stored credential if wanted. The database is not rolled back by this; restore the step 3 dump only if a migration caused the problem ([restore.md](restore.md)).

**Package privacy: CONFIRMED PRIVATE by the owner on 14-10-2026** (he checked the GitHub Packages page; an anonymous request had also got 403). Not checked: the GHCR storage and transfer quota (the CLI token gets 403 on packages). The pull with the `read:packages` token worked on 14-10-2026.

**When the token expires.** After the expiry date `docker compose pull` fails with `unauthorized` or `denied`. The stack keeps running on the images it already has; only deploys stop. Fix: create a new classic token (`read:packages` only, 90 days), run the `docker login` line of step 2 again, set a new calendar reminder, and delete the old token on GitHub. A deploy that fails at `pull` has changed nothing, because `up -d --no-build` runs only after `pull` succeeds (the two are joined with `&&`). Renewing an expired token has not been done yet (not yet run).

## 2f. Deploy a commit in registry mode (issue 173; amended 15-10-2026: no `.env` edit per deploy, images follow `latest`)
Use after section 2e has been done once. Before deploying read [daily-checks.md](daily-checks.md): a redeploy restarts the collector (about 45 seconds of snapshots) and breaks a 24 hour soak in progress. Every deploy needs the owner's explicit go.

**One-time change on the VM (owner, once, announced first, after the release that moved `latest` has run).** Registry mode is switched by one `.env` line, `VATPAC_IMAGE_PREFIX=ghcr.io/t288matt/`; the tag defaults to `latest`, which the release workflow moves to each published commit. The VM's `.env` currently has `VATPAC_IMAGE_TAG=sha-2f04731` (a pin); while it stays, `make deploy` keeps running that old build. Do the steps in this order, each with its check (the old checkout's `check_env.sh` fails on a prefix-only `.env`, so the checkout moves first):
```sh
cd ~/vatpac-statsV2
git fetch origin && git checkout main && git pull --ff-only
git log -1 --oneline
sed -i '/^VATPAC_IMAGE_TAG=/d' .env
grep '^VATPAC_IMAGE' .env
sh ops/check_env.sh
```
Checks: `git log -1` shows the commit with this change (and `git status --short` is empty); `grep` prints only the `VATPAC_IMAGE_PREFIX` line; `check_env.sh` ends with `check_env: OK`. After that `.env` is never edited per deploy. `make deploy`, `check_env.sh` and the preflight warn loudly if a `sha-` pin is present.

**Order: release first, then deploy.** The release workflow publishes the images and moves `latest` to them; only then does the VM pull. `make deploy` refuses to start anything if the pulled images were not built from the checked-out commit.

1. **PC: publish the commit.** Start the release workflow: Actions > release > Run workflow, leaving `commit` blank to publish whatever main's tip is at the moment the job runs, or `gh workflow run release.yml` (same). To publish an older commit instead (a rollback, or because main's tip is a docs-only commit that skipped CI), give it explicitly: Actions > release > Run workflow with the commit, or `gh workflow run release.yml -f commit=<sha>`; its CI run on main must have passed. It pushes `sha-<7>` for the four images, then, only if all four built, moves `latest` to them. Check: four green `images` jobs and a green `promote` job; the `check` job's log names the exact commit it resolved and published (copy it for steps 2 and 6 below). This click is the approval gate.
2. **PC: preflight.** `bash ops/deploy.sh --preflight <commit>` (reads only; PASS, or a WARN you understand).
3. **VM: fresh dump.** `cd ~/vatpac-statsV2 && docker compose exec backup backup.sh once`, then check the new file in `/backups`. The dump is what matters, because an image cannot undo a migration. Also copy the Grafana data volume now, because `make deploy` pulls and a newer Grafana 12.x cannot be undone by an older image (ADR-032): `docker run --rm -v vatpac_grafana_data:/from:ro -v $HOME:/to alpine tar czf /to/grafana_data_pre_deploy.tgz -C /from .` (the file lands in your home folder, not in the git checkout, so `git status --short` stays empty; confirm the volume name with `docker volume ls`; same command as RUNBOOKS/public-access.md, "Roll back").
4. **VM: deploy.**
```sh
cd ~/vatpac-statsV2 && git status --short && make deploy
```
`git status --short` must print nothing. `make deploy` runs `git pull --ff-only`, `docker compose pull`, a check that the pulled images' revision label is the checkout commit (or an older one with no change to the code baked into the images, so a docs-only commit is fine), then `docker compose up -d --no-build`. If the check fails it stops with "the images for this commit are not published yet: run the release workflow for <commit>, then run make deploy again" and nothing running is changed. It never builds. After a rollback the checkout is detached: run `git checkout main` first. New releases may add settings to `.env.example`: add any new blank secrets before this step. Confirm: `docker compose ps -a migrate` shows `exited (0)` and its logs end with `migrate: done`. A changed `ops/postgres/postgresql.conf` still needs `docker compose restart db`. Without `make`, run the same commands by hand, with `sh ops/check_pulled_images.sh` before the last one.
5. **PC: verify.** `bash ops/deploy.sh --verify` (all PASS), then open the ten public addresses ([public-access.md](public-access.md)).
6. **PC: mark.** `bash ops/deploy.sh --mark <commit>` after commenting on the issues in the commits. The release file is unchanged; `release.log` now records the running collector image's revision in its `images=` line.

**What a change needs in registry mode.** The table "What a change needs" below still applies, with one change: wherever it says `docker compose up -d --build` for collector, watchdog, backup or migrate code, read "publish the commit, set the tag, `docker compose pull`, `docker compose up -d --no-build`". The build arguments `BACKUP_EXTRA_APT_PACKAGES` and `VATPAC_VERSION` are ignored in registry mode: the published backup image has no `rsync`, `ssh` or `rclone`, so an off-box copy hook that needs them ([restore.md](restore.md), [../OWNER_BACKUP_COPY.md](../OWNER_BACKUP_COPY.md)) works only in local-build mode until the tools are added to the published image. Dashboards, provisioning, `config/` and migrations are mounts: a checkout is enough, as before.

**Old images pile up.** Each deploy leaves the previous four images on the VM. After the second registry deploy, the previous `sha-` tag exists locally and is the quick rollback: keep it pulled. Policy for what to keep and remove is in the Overview ("Housekeeping and prune policy"); remove only with the owner's go ([../OWNER_ACTIONS.md](../OWNER_ACTIONS.md), card 18).

**Never `up -d --build` on the VM in registry mode.** It rebuilds locally and retags the registry name, defeating the pull. If someone did, run `docker compose pull` and `docker compose up -d --no-build --force-recreate <service>` to get the published image back.

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
Local-build mode (both image variables unset), as on a PC or a new VM before the registry is set up:
```sh
make up                      # = check_env + docker compose up -d --build
docker compose ps            # migrate shows "exited (0)"; everything else healthy within about 2 minutes
```
Registry mode (the production VM since 14-10-2026): after the `docker login` and the two `.env` lines of section 2e, start with `docker compose pull && docker compose up -d --no-build` instead of `make up` (`make up` builds, which on the VM would replace the pulled image). A `make deploy` target (pull plus `up -d --no-build`, registry mode only) and a refusal of `make up` in registry mode are added by the deploy tooling pull request (separate draft, merged only after review).

## 4a. About sections 4b to 4e
They are dated records of special deploys, written for local-build mode. Keep them as they are. In registry mode read each `docker compose up -d --build` in them as "publish the commit, set the tag, `docker compose pull`, `docker compose up -d --no-build`" (section 2f), and read `git archive` as the git checkout of section 2d.

## 4b. Special case: migration 20261008000100 (whole-second logon_time, T36 / ADR-021)
This migration merges duplicate sessions and rewrites `logon_time` in both session tables. It cannot be undone by its down half (the fractions are gone), so **take a fresh dump first** (`docker compose exec backup backup.sh once`, then check the new file in `/backups`) and keep a copy of the VM's current tree. Stop the old collector before the migration runs (`docker compose stop collector`), so nothing writes while sessions are merged, then `docker compose up -d --build`. Afterwards run `psql -X -f ops/audit/identity_flap.sql` in the db container (both counts must be 0). Roll-back is the restore of the pre-deploy dump (restore.md).

## 4c. Special case: the collector release with the rolling feed archive (T38; issues #63 and #64)
Three things are mandatory for this release. Each one has already cost, or would cost, data.
1. **Stop the collector, then build.** The deploy MUST use `docker compose stop collector` and then `docker compose up -d --build`. The new `archive` volume is created root-owned unless the NEW image (which creates and owns `/archive` for the collector user) is built first; an old or cached image would leave the volume unwritable by the collector and the archive would silently never write (issue #64; the collector keeps polling, so nothing else looks wrong). Never use `docker compose up -d` without `--build` for this release, and never `docker compose restart collector` to pick up a code change (a restart does not rebuild the image). A plain `docker compose restart collector` IS allowed for a hung collector when there is no code change (owner decision 10-10-2026; see stuck-collector.md).
2. **Check the VM clock before deploying** (issue #63): `timedatectl` must show `System clock synchronized: yes` and `NTP service: active`. The collector refuses a snapshot stamped more than `FEED_MAX_FUTURE_SECONDS` (default 120) ahead of the VM clock, so a VM clock more than about 128 s slow makes every poll an error. Fix the clock (`sudo timedatectl set-ntp true`, or ask the owner) before the deploy, not after.
3. **Check the archive after the deploy.** About a minute after the collector starts, `docker compose exec -T collector python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/health').read().decode())"` (the health port is not published; 8080 is the default `HEALTH_PORT`) must show an `archive` block with `enabled: true`, `last_write` set to a time within the last few minutes (and `last_write_age_seconds` small), and error counters at 0. A `last_write` of null, or growing `last_write_age_seconds`, means the archive is not writing: check `docker compose logs --tail=50 collector` and the ownership of the `archive` volume. The watchdog also alerts on an old archive.
Order for this release: fresh dump (`docker compose exec backup backup.sh once`), a copy of the VM's tree for rollback, `timedatectl`, `git archive` copy (section 2c), `docker compose stop collector`, `docker compose up -d --build`, `sh ops/smoke.sh`, `python3 ops/check_panels.py`, restart counts 0, the `/health` archive check above, and `ops/audit/identity_flap.sql` (two zeros).

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
5. **Roll back.** `docker compose up -d --build watchdog` from the previous tree (the extra grants are harmless to leave). To switch the checks off without a rollback, set `WATCHDOG_INTEGRITY_INTERVAL_HOURS` and `WATCHDOG_CROSSCHECK_INTERVAL_DAYS` to `0`. They are fixed in `docker-compose.yml` (ADR-034), so `.env` cannot change them: use a git-ignored `docker-compose.override.yml` ([CONFIG_MAP](../CONFIG_MAP.md), "Where each setting lives") and run `docker compose up -d watchdog`.

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

Which method applies (status 14-10-2026):
- **Registry mode (the production VM since 14-10-2026):** section 2f. On the VM never `up -d --build`.
- **Local-build mode with git** (a PC or a first install; or the VM after the rollback of section 2e step 8):
```sh
git fetch origin && git checkout --detach <new commit>   # a commit (sha); no semver tags
sh ops/check_env.sh                # new releases may add secrets (see .env.example); add any blank ones
docker compose up -d --build     # migrate runs first; collector waits for it
make smoke
```
- **Fallback, no git (the method used on the production VM until 14-10-2026; use only if the git checkout is unusable).** Read the no-delete warning in section 2c first. From the PC:
```sh
git archive --format=tar HEAD | ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'
ssh vatpac-v3 'cd ~/vatpac-statsV2 && sh ops/check_env.sh && docker compose up -d --build'
ssh vatpac-v3 'cd ~/vatpac-statsV2 && sh ops/smoke.sh && python3 ops/check_panels.py'
```
`docker compose up -d --build` (local-build modes) rebuilds the images whose inputs changed, recreates only the services whose image or configuration changed, and starts the `migrate` job before the services that wait for it. On the VM in registry mode the same recreate rule applies to `docker compose up -d --no-build` after the pull. Confirm the migrations ran: `docker compose ps -a migrate` shows `exited (0)`, and `docker compose logs --tail=20 migrate` ends with `migrate: done`. If a new migration did not run, `docker compose up migrate` runs the job on its own (it is idempotent).

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
| `.env` (a collector setting that is still in `.env`; retention periods and alert thresholds are in `docker-compose.yml`, next row) | `docker compose up -d collector` | Yes, about 45 s |
| `docker-compose.yml` | `docker compose up -d` (recreates the services whose definition changed; a new named volume, such as `archive`, is created by the same command) | Only for those services |
| `ops/postgres/postgresql.conf` | `docker compose restart db` | Yes, the database restarts; the collector retries |
| `ops/watchdog/**`, `ops/audit/independent.py`, `ops/audit/invariants.sql` (the watchdog image copies the last two), `ops/backup/**` | `docker compose up -d --build watchdog` or `backup` | No |
| `ops/*.sh`, `ops/*.py`, `grafana/*.py`, `docs/`, `tests/`, `Makefile` | Nothing: they run from the host or are only documentation | No |

## Roll back
Code, dashboards and configuration go back by deploying the previous commit; the database does not go back by itself. Migrations are forward-only. A migration that already ran stays applied after a code rollback; additive migrations (a new view, an index) are harmless to older code. If a migration must be undone, add a new corrective numbered migration, or restore from backup ([restore.md](restore.md)); never edit an applied migration. Images cannot undo a migration either, so the dump taken before the deploy is the way back from a bad one.

**Registry mode.** Go back to a previous build by its `sha-<7>` tag, with no `.env` edit:
```sh
cd ~/vatpac-statsV2 && git checkout --detach <previous commit>
VATPAC_IMAGE_TAG=sha-<previous 7> docker compose up -d --no-build
```
Then `bash ops/deploy.sh --verify`. Warning: that override lasts for that one command only. A later plain `docker compose up -d` returns to `latest`, on the newest images with the old checkout. Return with `git checkout main` and `make deploy`.

**Local-build mode with git.** `git checkout --detach <previous commit>`, then `docker compose up -d --build`, then `sh ops/smoke.sh` and `python3 ops/check_panels.py`. The checkout already removes files the newer commit added.

**Fallback, no git (the method until 14-10-2026).** Note the commit deployed before, then deploy it: `git archive --format=tar <previous commit> | ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'` and `docker compose up -d --build` on the VM. `tar -x` leaves files the newer commit added: delete them by hand. In particular delete a newer migration file you do not want applied **before** running `docker compose up -d`, or `migrate` will apply it.

## Image pinning
Third-party images (`postgres`, `grafana`, `dbmate`, `python`) are pinned by exact version tag. (Issue 176 proposes floating major tags; it is parked and not decided, so this section still describes the exact pins.) Our own four images are tagged by commit (`sha-<7>`), never `latest`. Digest pinning (`image:tag@sha256:...`) is the stricter policy; add digests when you can resolve them with `docker buildx imagetools inspect <image>:<tag>`. Dependabot (docker and docker-compose ecosystems) proposes bumps.

## Publishing commit-tagged images (issue 173, P1; merged in PR 177; run once on 14-10-2026 for commit 67c1ca0, all four images published)
The `release` workflow (`.github/workflows/release.yml`) is started by hand: Actions > release > Run workflow, or `gh workflow run release.yml`. Leave `commit` blank to publish main's current tip (resolved inside the job when it runs, not when you click); give it a commit (full or short SHA) to publish something else instead, e.g. an older commit for a rollback. Whichever commit is used, it must be on main and its `ci` run on main must have passed (a docs-only commit skips CI, so if main's tip is docs-only, give the last code commit explicitly). The workflow checks that, does not repeat CI, and pushes `ghcr.io/t288matt/vatpac-stats-{collector,watchdog,backup,migrate}:sha-<7 characters>` (the full SHA is in the `org.opencontainers.image.revision` label). There is no `latest` and no semver tag. The packages are created private (the repository is private); leave them private. The `migrate` image now contains `db/`, so its migrations always match the code it was built from. `docker-compose.yml` takes these images when `VATPAC_IMAGE_PREFIX` and `VATPAC_IMAGE_TAG` are set (phase P2, merged in PR 178); the `./db` mount is kept. The VM pulls them since 14-10-2026 (section 2e). The first manual run, for commit 67c1ca0, worked: four images, tag `sha-67c1ca0`. The packages were confirmed private by the owner on 14-10-2026.
