# Runbook: deploy to the Linux VM

> **Note, issue 89 phase 5 (LIVE since 13-10-2026, marker 7a71d30 at 10:05:56 UTC).** This is a dated record. It uses the names as they were when it was written: `flight_summary` meant the old pieces table, `flight_record` the table of finished flights. Migration 20261013000300 (live) dropped the pieces table, `flight_part`, `flight_legacy`, `refresh_flight_summary` and `refresh_flight_summary_sessions`, and renamed `flight_record` to `flight_summary`. Current description: [DATA_DICTIONARY.md](../DATA_DICTIONARY.md) and ADR-030.

Last reviewed 22-10-2026. The commit running in production is shown by `bash ops/deploy.sh --status` (release marker); this page does not name it. Marker history of 11 to 13-10-2026: b3189ad, c70e72f (phase 1), 3d4b5b6 (phase 1b), 38e3958 (phase 3), 7a71d30 (phases 4 and 5, 13-10-2026 10:05:56 UTC). The phase 4 and 5 deploy was done by the owner with a pre-deploy dump (the only way back from phase 5, which is forward-only), a rollback copy of the tree on the VM and the scripted checks of `ops/deploy.sh` (preflight 12 checks, then verify); the plan, the decision tree for each failure and the 24 hour watch list are `DEPLOY_PLAN_PHASE_4_5.md`, kept privately by the owner. Sections written before 11-10-2026 were not all re-checked line by line against that commit; where a step names 3eb81a3 or deploys 1 and 2 at 520dc3e, read it as history. Target: always-on Linux VM (Debian/Ubuntu), 2 vCPU, 4 GB RAM, **40 GB+ SSD** (the minimum; the production VM has 4 cores, 6 GB RAM and 161 GB free, read 14-10-2026). The production VM is reached as `ssh vatpac-v3` (an alias in the owner's `~/.ssh/config`) and the stack lives in `~/vatpac-statsV2`. Since 14-10-2026 `~/vatpac-statsV2` is a read-only git checkout (deploy key) and the VM runs registry images (overview below and sections 2d to 2f); the `git archive` copy of section 2c is the fallback. Times in this runbook are UTC.

Disk sizing: about 2 to 3 GB of positions at 180 days, plus WAL (up to 2 GB), plus `BACKUP_RETENTION_DAYS` (default 14) dumps on the same disk, plus Docker images, logs and build cache. 20 GB is too tight. Provision 40 GB or more, or lower `BACKUP_RETENTION_DAYS` (fixed in `docker-compose.yml`; for an immediate change use the override file in [CONFIG_MAP](../CONFIG_MAP.md), "Where each setting lives") and copy dumps off-box (`restore.md`, "Off-box copies"). The watchdog checks the filesystems listed in `WATCHDOG_DISK_PATHS` (default `/backups,/`, which is the Docker data-root filesystem that holds the `pgdata` and `backups` named volumes) and warns at `WATCHDOG_DISK_PCT` (80%). If you move a volume onto its own mount, mount that path into the watchdog and add it to the list.

## Overview: how a deploy works (issue 173; run and verified on the production VM on 14-10-2026)

**Status on 14-10-2026, stated plainly.** The production VM now deploys by git checkout plus registry images. Run by the owner on 14-10-2026 and verified: section 2d steps 1 to 10 (finished 22:16 UTC; the 15 stale files were saved to `~/stale-files-14-10-2026.tgz` and removed); one manual run of the release workflow for commit 67c1ca0 (all four images published, tag `sha-67c1ca0`); the registry token and `docker login ghcr.io -u t288matt`; the two `.env` lines (`VATPAC_IMAGE_PREFIX=ghcr.io/t288matt/`, `VATPAC_IMAGE_TAG=sha-67c1ca0`); `docker compose pull` and `docker compose up -d --no-build` (migrate, collector, backup and watchdog recreated; db and grafana untouched); `bash ops/deploy.sh --verify` 10 PASS; release marker 67c1ca0 recorded at 22:29:46 UTC. **Still not run:** a second registry deploy (from one `sha-` tag to another), a rollback to a previous `sha-` tag, and renewing an expired token. Statements about those are marked "not yet run". **Confirmed:** the packages are private (the owner checked the GitHub Packages page on 14-10-2026).

**The idea.** The VM is a read-only git checkout of this repository. The five images that this project builds (collector, watchdog, backup, migrate and the ATC stats loader) are built once by GitHub Actions and stored in a private registry (GHCR), tagged with the commit (`sha-` plus the first 7 characters). The VM pulls them instead of building them. The images carry the moving tag `latest` as well as `sha-<7>`, and a deploy is `make deploy` (section 2f):

```sh
make deploy   # git merge --ff-only to the commit the checks passed for, docker compose pull, sh ops/check_pulled_images.sh, then docker compose up -d --no-build
```

To deploy one exact commit instead, see "Roll back" (a `sha-<7>` pin for one command).

**What stays on the VM tree (mounts, not images):** `grafana/dashboards`, `grafana/provisioning`, `config/` and `ops/postgres/postgresql.conf`. The migrations and `db/roles.sql` are not mounts: they come only from the `migrate` image. A dashboard change still shows within 30 seconds of a checkout, with no image and no Grafana restart. The VM also runs its own `ops/smoke.sh`, `ops/check_panels.py` and `ops/grafana_public.py` from the tree, which is why the tree stays.

**Not ours:** the postgres, grafana and dbmate images (and the python base image of the builds) come from Docker Hub on the floating tags in `docker-compose.yml` and the Dockerfiles ([ADR-032](../DECISIONS.md)): `postgres:16-bookworm`, `amacneil/dbmate:2`, `grafana/grafana:12.4`.

**Two modes, one compose file.** `VATPAC_IMAGE_PREFIX` in `.env` chooses the mode; `VATPAC_IMAGE_TAG` is an optional pin ([../CONFIG_MAP.md](../CONFIG_MAP.md)):

| Mode | `.env` | Image names | Deploy command | Used by |
|---|---|---|---|---|
| Local build | prefix unset | `vatpac-stats-<name>:latest` (built on the machine) | `make up` (`docker compose up -d --build`) | PC, CI, dev, first installs, and the rollback on the VM |
| Registry | prefix set (`ghcr.io/t288matt/`); tag unset = `latest` | `ghcr.io/t288matt/vatpac-stats-<name>:latest` (also published as `:sha-<7>`) | `make deploy` | the production VM since 14-10-2026 |

**On the VM (registry mode) never use `up -d --build`.** It builds from source and tags the result with the registry name, replacing the pulled image, so the VM would run something other than the published image. `--no-build` makes that impossible, and `make up` and `make dev` refuse to run when `VATPAC_IMAGE_PREFIX` is set (`ops/image_mode.sh`). Local builds with `--build` are for a PC and for first installs. A `VATPAC_IMAGE_TAG=sha-...` line is a deliberate rollback pin only: `sh ops/check_env.sh`, `make deploy` and `--preflight` warn about it.

**First-time set-up order on the VM** (the owner runs every VM command; each step has a check and a way back):

| Step | What | Where described | Status |
|---|---|---|---|
| 1 | Publish the images for the commit to be deployed (release workflow) | "Publishing commit-tagged images" at the end of this page | Run 14-10-2026 (67c1ca0) |
| 2 | Deploy key and git checkout of the live folder (phase P0) | section 2d | Run and verified 14-10-2026 |
| 3 | Registry token, `docker login`, the two `.env` lines, first pull (phase P3) | section 2e | Run and verified 14-10-2026 |
| 4 | Every later deploy | section 2f | Procedure; the second deploy is not yet run |

Decision record: ADR-031 in [../DECISIONS.md](../DECISIONS.md).

**The old method is kept as a fallback:** section 2c (`git archive | tar -x`) and the "Fallback" notes under Updating and Roll back. It never deletes files, so stale files on the VM survive it; read the warning in 2c before using it.

**A deploy can pull third-party updates (ADR-032).** `docker compose pull` also fetches the newest Postgres 16, Grafana 12 and dbmate 2 images, and they are not reviewed first. After the deploy run `bash ops/deploy.sh --verify` and open Grafana (`/api/health` must answer ok and the dashboards must load); if Grafana moved to a newer minor version, its data volume cannot go back, so keep the copy of the volume taken before the deploy.

**Dependabot and base images.** A merged Dockerfile or base-image bump (Dependabot) only changes main. The VM keeps running the old images until someone publishes the new commit, sets the tag in `.env` and pulls (section 2f).

**Deploy script checks for registry mode.** `--preflight` in registry mode checks over ssh that the five registry images for the commit exist as `sha-<7>` (`docker manifest inspect`, which also detects an expired token) and that the VM `.env` has no `sha-` pin (a pin is a WARN; no tag line means the images follow `latest`). `--verify` and `--status` show the running image tags and FAIL in registry mode if the VM checkout HEAD does not match the images' revision label. The sha-pin warning has one wording in `ops/image_mode.sh`, `ops/check_env.sh` and `--preflight`; the preflight one adds "(the VM's .env pins sha-...)". `--mark` warns if the VM HEAD differs from the commit and records the image tag in `release.log` (the release file format is unchanged). Exact behaviour is in the usage text at the top of `ops/deploy.sh`.

**Housekeeping and prune policy on the VM.**
- Keep the current and the previous `vatpac-stats-*` image set (registry `sha-` tags, or the local `vatpac-stats-<name>:latest` images in local-build mode) so a rollback needs no rebuild or pull.
- Keep the current and the previous `sha-` image set. Remove older ones one by one with `docker image rm <image:tag>` after listing them (`docker images 'ghcr.io/*'`).
- Old rollback copies of the tree (`~/vatpac-statsV2.rollback-*`) and old archives such as `~/grafana_data_pre_*.tgz` (root-owned, so deleting needs `sudo`) are only disk space once the git checkout is in place, because the previous code is one `git checkout` away. List them first (`ls -ld --time-style=long-iso ~/vatpac-statsV2.rollback-* ~/grafana_data_pre_*.tgz`), read the list, then remove each by its exact path with `rm -rf <path>`, never a wildcard, and never the live folder `~/vatpac-statsV2`. Keep them longer if a Grafana or database problem started after the deploy they belong to. Afterwards run `bash ops/deploy.sh --status`.
- Never print or paste `.env`, the Telegram token, the registry token or the Grafana password anywhere, including into a chat with an AI. Never run test suites, scratch databases or throwaway containers on the VM without the caps in [run-tests.md](run-tests.md) (an uncapped test container once took 6.9 GB of 7.9 GB and cost the collector a gap, incident #113). Never delete a Docker volume: `vatpac_pgdata` is the data, `vatpac_backups` holds every backup, `vatpac_grafana_data` the Grafana state, `vatpac_archive` the raw-feed safety copy; `docker compose down -v` deletes them all. Never touch the old v1 system or its folder.
- Never run `docker system prune`. Never run `docker volume prune` blindly: named volumes hold the data, backups and Grafana state. The 85 unused anonymous volumes (about 10.6 GB, read 14-10-2026) are a separate cleanup: list them first and check each is anonymous and unused.
- The registry token lasts 90 days (expires about 12-01-2027; the owner's calendar reminder is 05-01-2027). The deploy preflight (pull request 180) fails early if the registry image cannot be read.
- Optional owner tidy: the VM `.env` carries a stale `VATPAC_VERSION=dev` line that nothing reads (`VATPAC_VERSION` is a build argument; the collector health output reports its own `__version__`). It can be deleted.

## 0 to 2. Prepare the VM and get the code
Not repeated here: see [INSTALL.md](../INSTALL.md) sections 1 and 2 (VM CPU type `host` or `x86-64-v2-AES`, because numpy 2.x crash-loops on import with the default "Common KVM processor"; changing it needs a full stop and start of the VM; installing Docker; the read-only deploy key or token). Section 2d below is the checked procedure for the production VM's git checkout.

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

**Method: adopt the live folder in place; do not swap folders.** Renaming the folder is the risky route. The running `db`, `collector`, `grafana` and `watchdog` containers have `./config`, `./grafana/...` and `./ops/postgres/postgresql.conf` bind-mounted, and a bind mount follows the folder you renamed, not a new one at the old path. `docker compose up -d` would not recreate them (their configuration is unchanged), so they would keep reading the renamed old folder until a `--force-recreate`, which restarts the database and costs about 45 seconds of snapshots. Moving only `.git` into the live folder changes no file, so nothing restarts. Do it when no soak is running (see [daily-checks.md](daily-checks.md)).

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
tr -cd '\0' < ~/stale-files-14-10-2026.txt | wc -c
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

10. **PC: verify.** `bash ops/deploy.sh --verify`, then `bash ops/deploy.sh --status`. Check: all PASS (including `checkout_current`) and the marker unchanged. `--preflight`, `--mark` and `ops/stale_issues.sh` need no change: they `cd ~/vatpac-statsV2` over ssh and read `.deploy/release`, and that folder, its name and the marker are untouched.

**Deploying after 2d.** Section 2d only turns the live folder into a git checkout. After 2e (done 14-10-2026), deploys on the VM use section 2f (registry mode). This local-build line is only for a PC, or for the VM after the rollback of section 2e step 8 (both image variables unset); never use it while the `VATPAC_IMAGE_*` lines are set:
```sh
cd ~/vatpac-statsV2 && git fetch origin && git checkout --detach <commit> && git status --short && sh ops/check_env.sh && docker compose up -d --build
```
Then `bash ops/deploy.sh --verify` and `--mark <commit>` from the PC as before. Notes: `git checkout` removes files the new commit deleted, which `tar -x` never did. Like `tar -x`, it replaces the single-file mount `ops/postgres/postgresql.conf` with a new file, so a changed postgresql.conf still needs `docker compose restart db`. A checkout refuses to overwrite an untracked file that the new commit adds, which stops the deploy loudly instead of silently. Untracked files (other than `.env*`, `.deploy`, `.claude`) show in the `git status --short` printed by that line; look at it before the build. `docker compose pull` in local-build mode would try to pull `vatpac-stats-<name>:latest` from Docker Hub and fail, so do not use it there.

**Roll back (to the copy method of section 2c).** Section 2c builds locally, so first leave registry mode: remove the `VATPAC_IMAGE_` lines from `.env` (`sed -i '/^VATPAC_IMAGE_/d' .env`). Then:
- VM: `rm -rf ~/vatpac-statsV2/.git`, then use section 2c. `bash ops/deploy.sh --verify` fails its `checkout_current` line without a git checkout on the VM, so use `--status` and `ops/smoke.sh` instead.
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
Check: the five project images show as `ghcr.io/t288matt/vatpac-stats-{migrate,collector,backup,watchdog,loader}:sha-<7>` with the tag you mean, and postgres and grafana are unchanged.
6. **VM: first pull, then start without building.**
```sh
docker compose pull && docker compose up -d --no-build
```
`pull` fetches the project images (their size is not known yet) and checks the others. `up -d --no-build` recreates the services whose image changed. Expect a collector restart of about 45 seconds of snapshots, so do not do it during a soak ([daily-checks.md](daily-checks.md)). `unauthorized` or `denied` at `pull` means the login, the token scope or the package access is wrong; nothing has changed yet, so fix that and repeat. `manifest unknown` means that tag was not published.
7. **PC: verify.** `bash ops/deploy.sh --verify` (all PASS), then open the fifteen public addresses (on the VM `python3 ops/grafana_public.py --list --public-url https://vpac.mpengor.com` lists them; [public-access.md](public-access.md)) and check each loads. Then `bash ops/deploy.sh --mark <commit>` (it first lists issues without a comment; see "Silent issues" under Updating; run it from a checkout whose `HEAD` is that commit, see the stale-checkout rule under Updating).
8. **Way back to local builds.** VM: remove the `VATPAC_IMAGE_` lines (`sed -i '/^VATPAC_IMAGE_/d' .env`), `git checkout --detach <the commit to run>`, then `make up` (it builds the five images on the VM and names them `vatpac-stats-<name>:latest`). To go back to an earlier published build instead, stay in registry mode and use "Roll back" below. `docker logout ghcr.io` removes the stored credential if wanted. The database is not rolled back by this; restore the step 3 dump only if a migration caused the problem ([restore.md](restore.md)).

**Package privacy: CONFIRMED PRIVATE by the owner on 14-10-2026** (he checked the GitHub Packages page; an anonymous request had also got 403). Not checked: the GHCR storage and transfer quota (the CLI token gets 403 on packages). The pull with the `read:packages` token worked on 14-10-2026.

**When the token expires.** After the expiry date `docker compose pull` fails with `unauthorized` or `denied`. The stack keeps running on the images it already has; only deploys stop. Fix: create a new classic token (`read:packages` only, 90 days), run the `docker login` line of step 2 again, set a new calendar reminder, and delete the old token on GitHub. A deploy that fails at `pull` has changed nothing, because `up -d --no-build` runs only after `pull` succeeds (the two are joined with `&&`). Renewing an expired token has not been done yet (not yet run).

## 2f. Deploy a commit in registry mode (issue 173; amended 15-10-2026: no `.env` edit per deploy, images follow `latest`)

**One-time `.env` line for the ATC stats loader.** The VM's `.env` needs `STATS_LOADER_DB_PASSWORD` before the first deploy that includes the loader. The owner adds it (the value is never printed), and the steps are in [atc-stats-loader.md](atc-stats-loader.md), section 8. It is not a per-deploy edit.
Use after section 2e has been done once. Before deploying read [daily-checks.md](daily-checks.md): a redeploy restarts the collector (about 45 seconds of snapshots) and breaks a 24 hour soak in progress. Every deploy needs the owner's explicit go.

**Check the VM `.env` for a pin first.** Registry mode is set by one `.env` line, `VATPAC_IMAGE_PREFIX=ghcr.io/t288matt/`; the tag defaults to `latest`, which the release workflow moves to each published commit. Check the VM with `grep '^VATPAC_IMAGE' .env` (never `cat .env`). If it has a `VATPAC_IMAGE_TAG=sha-...` line, remove it, or `make deploy` runs that old build; `check_env.sh` and `make deploy` warn about a pin. To remove it, in this order, each with its check:
```sh
cd ~/vatpac-statsV2
git fetch origin && git checkout main && git pull --ff-only
git log -1 --oneline
sed -i '/^VATPAC_IMAGE_TAG=/d' .env
grep '^VATPAC_IMAGE' .env
sh ops/check_env.sh
```
Checks: `git log -1` shows the commit with this change (and `git status --short` is empty); `grep` prints only the `VATPAC_IMAGE_PREFIX` line; `check_env.sh` ends with `check_env: OK`. After that `.env` is never edited per deploy. `make deploy`, `check_env.sh` and the preflight warn loudly, in the same words, if a `sha-` pin is present.

**Order: release first, then deploy.** The release workflow publishes the images and moves `latest` to them; only then does the VM pull. `make deploy` refuses to start anything if the pulled images were not built from the checked-out commit.

1. **PC: publish the commit.** Since 04-11-2026 (issue 268) this normally happens by itself: when `ci` passes for a push to main, the `release` workflow publishes main's tip. Check Actions > release for a green run for the commit you are deploying (wait for it; it follows the `ci` run). A run for a commit that is no longer main's tip finishes green without publishing, because the newer commit publishes itself. Start it by hand only when no automatic run exists for the commit (a docs-only tip skips CI), for a rollback, or if the automatic run failed. By hand: start the release workflow: Actions > release > Run workflow, leaving `commit` blank to publish whatever main's tip is at the moment the job runs, or `gh workflow run release.yml` (same). To publish an older commit instead (a rollback, or because main's tip is a docs-only commit that skipped CI), give it explicitly: Actions > release > Run workflow with the commit, or `gh workflow run release.yml -f commit=<sha>`; its CI run on main must have passed. It pushes `sha-<7>` for the five images, then, only if all five built, moves `latest` to them. Check: five green `images` jobs and a green `promote` job; the `check` job's log names the exact commit it resolved and published (copy it for steps 2 and 6 below). Publishing is no longer the approval gate: nothing changes on the VM until `make deploy` is run, and that needs the owner's explicit go.
2. **PC: preflight.** `bash ops/deploy.sh --preflight <commit>` (reads only; PASS, or a WARN you understand).
3. **VM: fresh dump.** Since issue 411 `make deploy` takes this dump itself, after the preflight checks and before anything changes (free space on `/` must be at least 1160 MB: a dump of about 160 MB plus a 1000 MB margin; the new dump must be readable and newer than the start). Run `docker compose exec backup backup.sh once` by hand only when you are not using `make deploy`, then check the new file in `/backups`. The dump is what matters, because an image cannot undo a migration. Also copy the Grafana data volume now, because `make deploy` pulls and a newer Grafana 12.x cannot be undone by an older image (ADR-032): `docker run --rm -v vatpac_grafana_data:/from:ro -v $HOME:/to alpine tar czf /to/grafana_data_pre_deploy.tgz -C /from .` (the file lands in your home folder, not in the git checkout, so `git status --short` stays empty; confirm the volume name with `docker volume ls`; same command as RUNBOOKS/public-access.md, "Roll back").
4. **VM: deploy.**
```sh
cd ~/vatpac-statsV2 && git status --short && make deploy
```
`git status --short` must print nothing. `make deploy` first runs the preflight checks (CI green for the commit, images published, VM healthy, disk, the backup window), then a fresh dump; any FAIL stops it before anything changes. Then it runs `git pull --ff-only`, `docker compose pull`, a check that the pulled images' revision label is the checkout commit (or an older one with no change to the code baked into the images, so a docs-only commit is fine), then `docker compose up -d --no-build`. After start it waits up to 300 s for healthy containers, runs `--verify`, and writes the release marker only if verify passed; a silent issue prints a warning but does not stop the marker. The gate needs the GitHub CLI (`gh`) installed and logged in on the VM; without it the CI check cannot answer, so the gate fails and nothing is deployed. On the VM the silent-issue check reads the previous release marker from the checkout itself (no ssh) and asks GitHub for the open issues; if `gh` cannot answer, the marker is still written, with a warning that the check did not run. If the check fails it stops with "the images for this commit are not published yet: run the release workflow for <commit>, then run make deploy again" and nothing running is changed. It never builds. After a rollback the checkout is detached: run `git checkout main` first. New releases may add settings to `.env.example`: add any new blank secrets before this step. Confirm: `docker compose ps -a migrate` shows `exited (0)` and its logs end with `migrate: done`. A changed `ops/postgres/postgresql.conf` still needs `docker compose restart db`. Without `make`, run the same commands by hand, with `sh ops/check_pulled_images.sh` before the last one.
5. **PC: verify.** `bash ops/deploy.sh --verify` (all PASS), then open the fifteen public addresses ([public-access.md](public-access.md)).
6. **PC: mark.** `bash ops/deploy.sh --mark <commit>` after commenting on the issues in the commits, from a checkout whose `HEAD` is that commit (otherwise it refuses). The release file is unchanged; `release.log` now records the running collector image's revision in its `images=` line.

**What a change needs in registry mode.** The table "What a change needs" below still applies, with one change: wherever it says `docker compose up -d --build` for collector, watchdog, backup or migrate code, read "publish the commit, set the tag, `docker compose pull`, `docker compose up -d --no-build`". The build arguments `BACKUP_EXTRA_APT_PACKAGES` and `VATPAC_VERSION` are ignored in registry mode: the published backup image has no `rsync`, `ssh` or `rclone`, so an off-box copy hook that needs them ([restore.md](restore.md), "Off-box copies" there) works only in local-build mode until the tools are added to the published image. Dashboards, provisioning and `config/` are mounts: a checkout is enough, as before. Migrations are not: they travel in the `migrate` image (see "What a change needs" below).

**Old images pile up.** Each deploy leaves the previous five images on the VM. After the second registry deploy, the previous `sha-` tag exists locally and is the quick rollback: keep it pulled. Policy for what to keep and remove is in the Overview ("Housekeeping and prune policy"); remove only with the owner's go.

**Never `up -d --build` on the VM in registry mode.** It rebuilds locally and retags the registry name, defeating the pull. If someone did, run `docker compose pull` and `docker compose up -d --no-build --force-recreate <service>` to get the published image back.

## 3 and 4. Configure and start
Not repeated here: see [INSTALL.md](../INSTALL.md) sections 3 and 4 (generating `.env`, the secrets, the Grafana first-start and password-rotation caveats, `make up`). In registry mode (the production VM since 14-10-2026) start with `make deploy`, never `make up` or `up -d --build`, which would rebuild locally and replace the pulled image (section 2f).

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
Status: DEPLOYED 09-10-2026 at 19:13 UTC; the steps below are the record. Do not repeat any of this without the owner's go (deploy policy). What it changes: the watchdog image (new build context `./ops`, new files `integrity.py`, `independent.py`, `invariants.sql`), two read-only mounts on the watchdog (`archive:/archive:ro` and `./config:/config:ro`), three settings (`WATCHDOG_INTEGRITY_INTERVAL_HOURS=72`, `WATCHDOG_CROSSCHECK_INTERVAL_DAYS=14`, `WATCHDOG_CROSSCHECK_SAMPLES=24`, all with defaults in `docker-compose.yml`, so `.env` needs no edit) and three new `SELECT` grants for `watchdog_ro` (`pilot_position`, `flight_summary`, `reference_data_change`). No migration file, no collector change: collector polling is not interrupted.

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

## 4f. Special case: migrations 20261112000100 to 20261112000170 (`norm_code()` and the two `pilot_session` indexes)
These six migrations add the function `norm_code(text)`, point every view and function at it, and replace the two expression indexes on `pilot_session`. No collector change is needed and the collector is not stopped.
1. **Before the deploy.** Take a fresh dump (`docker compose exec backup backup.sh once`, then check the new file in `/backups`). Do not deploy while the backup is running: `CREATE INDEX CONCURRENTLY` and `DROP INDEX CONCURRENTLY` wait for every transaction that started before them, and a dump is one long transaction. Check for long ones first (read-only): `SELECT pid, now() - xact_start AS age, left(query, 60) FROM pg_stat_activity WHERE xact_start < now() - interval '5 minutes' AND state <> 'idle'`.
2. **What each migration does to the live tables.** `20261112000100` creates the function (instant). `20261112000110` and `20261112000120` build `pilot_session_departure_code_idx` and `pilot_session_arrival_code_idx` with `CREATE INDEX CONCURRENTLY`: the collector keeps inserting and updating `pilot_session` while they build (the build scans the table twice and takes no lock that blocks writes), so there is no polling gap. Each runs outside a transaction and holds one statement, because dbmate sends a file as one command. `20261112000150` replaces the views and two functions (`CREATE OR REPLACE`; each takes a lock for an instant and gives up after 5 seconds rather than queue behind a long panel query). `20261112000160` and `20261112000170` drop the two old indexes with `DROP INDEX CONCURRENTLY`. At every moment between these steps each query has an index, because the old ones are dropped only after the new ones exist and the planner treats both spellings as one expression.
3. **Order with the collector.** `docker compose up -d` runs `migrate` first and recreates the collector only after it exits 0. The old collector keeps polling while the migrations run; it only calls `write_finished_flights` and reads and writes tables, none of which changed signature, so old collector with new schema is safe. The new collector never starts against the old schema. The watchdog image carries `ops/audit/invariants.sql`, which now calls `norm_code`; it also waits for `migrate`, and the old copy of that file still works against the new schema.
4. **Check afterwards.** `docker compose logs --tail=30 migrate` ends with `migrate: done`. Then, as the owner role: `SELECT indexrelid::regclass, indisvalid FROM pg_index WHERE indrelid = 'pilot_session'::regclass` shows the two `_code_idx` indexes with `indisvalid` true and no `_norm_idx` index; `SELECT norm_code(' ymml ')` returns `YMML`; `bash ops/deploy.sh --verify` passes.
5. **If a step fails.** A lock timeout in `20261112000150` leaves the database as it was (the file is one transaction): run `docker compose up migrate` again. If a `CONCURRENTLY` build is interrupted, Postgres leaves an INVALID index with the new name and the next run stops with "already exists" (the migrations deliberately omit `IF NOT EXISTS`, so an invalid index is never skipped): drop it with `DROP INDEX CONCURRENTLY pilot_session_departure_code_idx` (or the arrival one), then `docker compose up migrate`. The old indexes are still in place until their own migration runs, so queries are not slowed meanwhile.
6. **Roll back.** Migrations are forward-only. The old and new definitions return the same rows on every row the collector can store (spaces, lower case, no value); the way back from anything else is the dump taken in step 1, or a new numbered migration that restores the earlier view text.

## 4g. Special case: migrations 20261116000100 to 20261116000300 (the promotions view and its index)
These three migrations create the view `controller_promotion`, drop any leftover `atc_stats_session_promotion_idx` and build it again with `CREATE INDEX CONCURRENTLY` (outside a transaction; the stats loader keeps writing while it builds). Take a fresh dump first and avoid a running backup, as in section 4f. If migration 20261116000300 fails or is cancelled, run `DROP INDEX CONCURRENTLY IF EXISTS atc_stats_session_promotion_idx;` and deploy again. Afterwards `SELECT indisvalid FROM pg_index WHERE indexrelid = 'atc_stats_session_promotion_idx'::regclass` returns true. The new public page `/atc-promotions` also needs its Traefik labels pasted on the Traefik machine ([public-access.md](public-access.md), "Add or rename a short address"), or the navigation link answers 404.

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

**Run `ops/deploy.sh --verify` and `--mark` from a checkout that is at the deployed commit** (the rule is enforced since issue 257). `--verify` counts the integrity checks from the copy of `ops/audit/invariants.sql` in the checkout it runs from (with `python ops/watchdog/integrity.py --names`, the same reader the watchdog uses; it needs only Python), so a stale checkout reports the old count (it showed 21 on 22-10-2026 while production runs 20). Now `--verify` compares the local `HEAD` with the VM checkout's `HEAD` first: if they differ, or either cannot be read, it prints a `STALE CHECKOUT` banner, fails the `checkout_current` line (printed after `vm_reachable`) and stops before the checks that read local files. `--mark <commit>` refuses, writing nothing, unless the local `HEAD` is exactly that commit. `--preflight` and `--status` print `local checkout is at <10 characters>`. Use a checkout of the VM's commit (for `--mark`, of the commit being marked), for example a fresh worktree: `git fetch origin && git worktree add --detach .worktrees/verify <first 10 characters of that commit>`, run the script from inside it, then `git worktree remove .worktrees/verify`. The banner prints this command with the right commit. Do not use `origin/main` for this: it may have moved on since the release (a second pull request merged between the deploy and the verify), and a worktree of it would fail the same check. `--verify` and `--mark` also FAIL when there are uncommitted or untracked files under `ops/` (`--verify` shows it as `checkout_clean`, after `checkout_current`; `--mark` prints a DIRTY CHECKOUT banner and writes nothing), because those files are what the script runs, whatever `HEAD` says: commit them, stash them, or use a clean worktree at the VM's commit. `--preflight` and `--status` do not check this.

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

**Silent issues (built 11-10-2026, issue #104).** After a verified deploy, `bash ops/deploy.sh --mark <commit>` first lists every issue named in the commits since the previous marker and refuses to mark if any has no comment newer than its newest commit: comment on each (what shipped, the commit, how it was verified), then mark again. If `gh` fails, or no previous marker can be read, it refuses too; the first-ever mark on a VM therefore needs `--allow-silent-issues`. `--allow-silent-issues` marks anyway and prints a warning. `--mark` accepts the commit as 7 to 39 characters or the full 40, and the marker is read by one shared reader (`ops/release_common.sh`, also used by `ops/stale_issues.sh`), which takes 7 to 40 hexadecimal characters. The GitHub CLI is found from `GH_BIN`, then `DEPLOY_GH`, then the `PATH`. To look without marking: `bash ops/stale_issues.sh` (since the last marker) or `--since <sha|date>`; it is read-only and exits 1 when it finds any. The guard is built and awaiting review; the deploy of it still needs the owner's go. **Closing keywords:** a pull request description that says `Closes #N` (or `Fixes`, `Resolves`) closes that issue the moment the pull request merges, even when only part of it is done, and a merge leaves no comment on it. Write `Refs #N` for partial work and close the issue by hand once it is deployed and verified; on 21-10-2026 a "Closes #201" in a description closed an issue whose main part was still open, and it had to be reopened.

## What a change needs (rebuild, restart or reload)

| What changed | What to do | Interrupts polling? |
|---|---|---|
| `collector/src/**`, `collector/requirements.txt`, `collector/Dockerfile` | `docker compose up -d --build` (rebuilds the collector image and recreates the container) | Yes, about 45 s |
| `db/migrations/*.sql` (new numbered file; never edit an applied one) | The migration travels in the `migrate` image. Registry mode: `make deploy` pulls the image built for the commit and `migrate` applies it. Local-build mode: `docker compose build migrate`, then `docker compose up -d`; without the build the old image runs the old migrations. If it did not run, `docker compose up migrate`. Then `sh ops/smoke.sh` | No |
| `db/roles.sql` (in the `migrate` image like the migrations) | Pull or build the image as for a migration, then `docker compose up -d`: `migrate` re-applies the roles and grants (all five roles, `watchdog_ro` included). `ops/migrate/migrate.sh` passes the five password variables the file requires, and the file and the script are in the same image, so they always match. **Never run this file by hand against the live database**: it resets the live role passwords to whatever you pass | No |
| `ops/migrate/**` (the migrate image itself) | Local-build mode: `docker compose up -d --build`. Registry mode: `make deploy` | No |
| `grafana/dashboards/**/*.json` | Nothing to restart: the provider rescans the read-only mount every 30 seconds. Reload the browser page | No |
| `grafana/provisioning/**` (datasource or provider settings) | `docker compose restart grafana` (provisioning is read at start-up) | No |
| `config/*.json`, `config/*.txt` (polygon, whitelist) | No rebuild: `docker compose kill -s HUP collector` (see [update-reference-files.md](update-reference-files.md)) | No |
| `.env` (a password) | `docker compose up -d` (the `migrate` job runs `ALTER ROLE`; `POSTGRES_PASSWORD` and `GRAFANA_ADMIN_PASSWORD` only apply at first start, see [INSTALL.md](../INSTALL.md) section 3) | The services using the changed value are recreated |
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
Third-party images (`postgres`, `grafana`, `dbmate`, `python`) use the most floating tag their registry publishes ([ADR-032](../DECISIONS.md)): a major tag where one exists (`postgres:16-bookworm`, `amacneil/dbmate:2`), `grafana/grafana:12.4` (Docker Hub has no bare-major tag for it), `python:3.14-slim`; a new major version is a deliberate edit. Our own five images are published as `sha-<7>` and `latest` (the release workflow moves `latest` only after all five are built). Digest pinning (`image:tag@sha256:...`) is the stricter policy; add digests when you can resolve them with `docker buildx imagetools inspect <image>:<tag>`. Dependabot (docker and docker-compose ecosystems) proposes bumps.

## Publishing commit-tagged images (issue 173, P1; merged in PR 177; run once on 14-10-2026 for commit 67c1ca0, all four images published)
The `release` workflow (`.github/workflows/release.yml`) runs by itself after every successful `ci` run for a push to main and publishes that commit only if it is still main's tip (a stale run stops at the `check` job with a notice and publishes nothing, so `latest` never moves backwards). It does not deploy: the VM only changes when the owner runs `make deploy`. It can still be started by hand: Actions > release > Run workflow, or `gh workflow run release.yml`. Leave `commit` blank to publish main's current tip (resolved inside the job when it runs, not when you click); give it a commit (full or short SHA) to publish something else instead, e.g. an older commit for a rollback. Whichever commit is used, it must be on main and its `ci` run on main must have passed (a docs-only commit skips CI, so if main's tip is docs-only, give the last code commit explicitly). The workflow checks that, does not repeat CI, and pushes `ghcr.io/t288matt/vatpac-stats-{collector,watchdog,backup,migrate,loader}:sha-<7 characters>` (the full SHA is in the `org.opencontainers.image.revision` label). There is no semver tag. After all five images are pushed, a final `promote` job moves `latest` to them (a re-tag, no rebuild); a failed build leaves `latest` where it was. The packages are created private (the repository is private); leave them private. The `migrate` image contains `db/` (migrations and `roles.sql`) and nothing is mounted over it, so the migrations that run always match the code the image was built from. `docker-compose.yml` takes these images when `VATPAC_IMAGE_PREFIX` is set (the tag defaults to `latest`, `VATPAC_IMAGE_TAG` is an optional pin). The VM pulls them since 14-10-2026 (section 2e). The first manual run, for commit 67c1ca0, worked: four images, tag `sha-67c1ca0`. The packages were confirmed private by the owner on 14-10-2026.
