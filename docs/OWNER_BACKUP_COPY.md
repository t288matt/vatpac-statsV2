# Owner card: set up the off-VM backup copy on the Proxmox host

Written 10-10-2026. Follows the style of [OWNER_ACTIONS.md](OWNER_ACTIONS.md). Decision: ADR-026 item 7 in [DECISIONS.md](DECISIONS.md). Times are UTC. Nothing on this card has been run: it was written from the scripts and a read-only look at the VM, and every place where I could not prove a step is marked **UNVERIFIED**. The steps are not in letter order. The real order is: **A1 to A4, B1, B2, A5, B3, B4, then C, D, E.** (A5 needs the public key made in B1.) Stop at the first surprise.

## Why

Today the nightly database dumps sit in a Docker volume on the VM's own disk (`BACKUP_REMOTE` in the VM's `.env` is empty, checked 10-10-2026). If that disk dies or the VM is corrupted, the data and every backup go together. This copies each nightly dump to a folder on the Proxmox host's disk, using the hook that already exists in the backup container (`BACKUP_REMOTE`, `ops/backup/backup.sh`).

What it covers and what it does not:
- It covers: a dead or corrupted VM disk, or a deleted VM.
- It does NOT cover loss of the Proxmox host itself (same physical machine). The Proxmox Backup Server job and its offsite sync stay the cover for that, and **this card does not replace them and does not replace the still-untested restore of the PBS copy** (issue #26, item 3 of OWNER_ACTIONS.md). Do that test too.
- It does NOT copy Grafana's own data (the `grafana_data` volume: public links and admin password); that is not in `ops/backup` at all. To rebuild it: `docker compose up -d grafana`, then `python3 ops/grafana_public.py --public-url https://vpac.mpengor.com`. The tokens are stable (the first 32 hex characters of sha256 of `vatpac-public:` followed by the dashboard uid), so the same addresses return. Keep your own copy of the links list (card 9 in OWNER_ACTIONS.md).
- The dumps contain real names and CIDs (public data, ADR-019, but keep them private). That is why the Proxmox user is locked down below.

## The least you have to do

Sections A to C need you (the Proxmox root login and the VM's sudo password; I have neither). Section D is typing one line into the VM's `.env`. Everything after that I do, once you say go. You never paste a private key anywhere: the key pair is made on the VM and the private half never leaves it. Only the PUBLIC half (a line starting `ssh-ed25519`) is copied to Proxmox, and that is safe to paste.

**Wait for deploy 1 first (planned 11-10-2026).** The watchdog on the VM today has no `backup_remote` check (it arrives in fix-ops), so a failing copy would be silent. You can do sections A and B before the deploy; sections C to F are best after it. This is also a change to the VM (an image rebuild of the backup container), so it needs your explicit go.

Placeholders used below: `PVE_HOST` is the Proxmox host's name or LAN address; `VM_ADDR` is the VM's LAN address (both known to you; this card deliberately does not write them down). User `vatpacbackup`, folder `/srv/vatpac-backups`; change them if you like but keep them the same everywhere. Give the VM a fixed LAN address (a DHCP reservation or a static address) before you start: `from="VM_ADDR"` in section A silently stops the copies if the VM's address ever changes.

## A. On the Proxmox host (as root, in the Proxmox shell). About 10 minutes.

1. **Check there is space and choose the disk.** A dump is about 5 MB today (the newest on 10-10-2026 was 5.2 MB), 14 days of them is under 100 MB, and the database grows slowly (ADR-026 estimates 1.3 to 1.8 GB total), so a few GB is plenty. But the Proxmox root disk is often small: look first.
```sh
df -h /srv /
```
   If `/srv` is on the small root disk, pick a folder on a bigger separate disk or dataset instead (ideally one with a quota) and use that path everywhere below: a full root disk can stop Proxmox.
2. **Check rsync and rrsync exist.**
```sh
command -v rsync rrsync || ls /usr/bin/rrsync /usr/share/doc/rsync/scripts/rrsync* 2>&1
rrsync -h 2>&1 | head -20
```
   If rsync is missing: `apt-get install -y rsync`. `rrsync` ships with the Debian rsync package (UNVERIFIED for your Proxmox version). The help text must list `-wo` (write-only). If the program is elsewhere, use that path in step 5. If `-wo` is not offered, stop and tell me (a read-write rrsync would let a stolen key read the backups).
3. **Create a restricted user, with no password.**
```sh
adduser --disabled-password --gecos "" vatpacbackup
```
   Leave its login shell as the default (`/bin/bash` or `/bin/sh`). Do NOT set it to `nologin`: sshd runs a forced command through the user's shell, so `nologin` would block the copy. The restriction comes from the key (step 5), not from the shell.
4. **Create the folder and the ssh folder, private to that user.**
```sh
install -d -m 700 -o vatpacbackup -g vatpacbackup /srv/vatpac-backups
install -d -m 700 -o vatpacbackup -g vatpacbackup /home/vatpacbackup/.ssh
```
5. **Wait for the public key from section B**, then install it as ONE line (replace `VM_ADDR`, and paste the public key text where shown). The options in front are what make the key safe:
```sh
cat > /home/vatpacbackup/.ssh/authorized_keys <<'EOF'
restrict,from="VM_ADDR",command="/usr/bin/rrsync -wo /srv/vatpac-backups" ssh-ed25519 AAAA...PASTE-THE-PUBLIC-KEY-HERE vatpac-backup
EOF
chown vatpacbackup:vatpacbackup /home/vatpacbackup/.ssh/authorized_keys
chmod 600 /home/vatpacbackup/.ssh/authorized_keys
```
   Important limit: write-only is not append-only. A stolen key, or a compromised VM, can still overwrite files already in the folder or fill the disk with junk. So put the folder on a disk or dataset where filling it cannot hurt Proxmox itself (not a small root filesystem), or give the folder a quota. What each option does: `restrict` turns off a terminal, port forwarding, agent forwarding, X11 and the user's start-up scripts; `from=` allows this key only from the VM's address; `command=` means that whatever the client asks for, only `rrsync` runs, and `-wo` lets it write files into `/srv/vatpac-backups` only (no reading back, no escaping the folder). Check your sshd allows key logins for this user (`grep -i '^AllowUsers\|^PubkeyAuthentication' /etc/ssh/sshd_config`; no output is fine).
6. **Optional but recommended: stop the folder growing for ever.** The backup hook only adds files; nothing deletes them on the Proxmox side (the VM keeps 14 days locally and does not touch the copy). A nightly clean-up of copies older than 30 days:
```sh
cat > /etc/cron.d/vatpac-backup-prune <<'EOF'
30 3 * * * vatpacbackup [ "$(find /srv/vatpac-backups -maxdepth 1 -name '*.dump' -mtime -31 | wc -l)" -ge 7 ] && find /srv/vatpac-backups -maxdepth 1 -name '*.dump' -mtime +30 -delete
EOF
chmod 644 /etc/cron.d/vatpac-backup-prune
```
The condition is a floor: nothing is deleted unless at least 7 dumps from the last 31 days exist, so if the VM dies or the copy stops, the last good copies are not pruned away. The line has no percent characters on purpose (cron treats them as line breaks). Failed transfers can leave stray temporary files named like `.vatpac_20261010T020000Z.dump.XXXXXX` that this prune does not match; look for them now and then with `ls -la /srv/vatpac-backups` and delete any old ones by hand.

## B. On the VM: make the key pair and capture the host key. About 5 minutes.

Run from your PC: `ssh vatpac-v3`. The key goes in a folder outside the project folder, so a deploy never touches it. (A file `~/.ssh/vatpac_backup_key` already exists on the VM, dated 22-08-2025. I do not know what it is for; do not reuse or delete it. The new key has a different name and folder on purpose.)

1. **Make the key pair (no passphrase, because it must work unattended).**
```sh
mkdir -p ~/vatpac-backup-keys && chmod 755 ~/vatpac-backup-keys
ssh-keygen -t ed25519 -N "" -C "vatpac-backup" -f ~/vatpac-backup-keys/id_ed25519
cat ~/vatpac-backup-keys/id_ed25519.pub
```
   Copy the one line printed by `cat` (it starts `ssh-ed25519`). That is the PUBLIC key: take it to section A step 5. Never print or paste the other file, `id_ed25519` (no `.pub`).
2. **Record Proxmox's host key** so the container can check it is talking to the right machine (not trust-on-first-use inside a container):
```sh
ssh-keyscan -t ed25519 PVE_HOST > ~/vatpac-backup-keys/known_hosts
ssh-keygen -lf ~/vatpac-backup-keys/known_hosts
```
   Compare the fingerprint it prints with the one on Proxmox (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`, run on the Proxmox host). They must match exactly. If you use the host's address in `.env` below, scan and use that same address here.
3. **Test the key BEFORE locking it down** (do section A step 5 first). From the VM, two tests:
```sh
ssh -i ~/vatpac-backup-keys/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$HOME/vatpac-backup-keys/known_hosts vatpacbackup@PVE_HOST
```
   You must NOT get a shell prompt. Expect it to end immediately with an `rrsync` error or silence (UNVERIFIED exact wording). Second test, which proves the restriction: run the same command with `ls` added at the end (`... vatpacbackup@PVE_HOST ls`). It must print an rrsync refusal (a message such as "SSH_ORIGINAL_COMMAND does not run rsync"), and NEVER a directory listing. If you see a listing, the forced command is not applied: remove the key line on Proxmox and tell me. `Permission denied (publickey)` means the key or the `from=` address is wrong: fix that first. If you ever do get a shell, remove the key line on Proxmox at once and tell me.
4. **Make the private key readable by the backup container.** The container runs as the `postgres` user, uid 999 (checked on the VM), not as `matt`, and ssh refuses a key that other users can read. So the key must be owned by uid 999 with mode 600. That needs your sudo password:
```sh
sudo chown 999:999 ~/vatpac-backup-keys/id_ed25519
sudo chmod 600 ~/vatpac-backup-keys/id_ed25519
ls -ln ~/vatpac-backup-keys
```
   `ls -ln` should show the `id_ed25519` line with `-rw-------` and owner `999`. After this, `matt` can no longer read the private key directly (good, but note `matt` is in the `docker` group, so the key could still be read through Docker: the chown protects against accidents, not against a hostile local user); `known_hosts` and `id_ed25519.pub` stay readable. (UNVERIFIED: that uid 999 inside the container maps to 999 on the host; checked only that `id` in the running backup container reports `uid=999(postgres)`; Docker does not remap users in this stack as far as the compose file shows.)

## C. Wiring (I do this, with your go)

1. **A compose override on the VM** that mounts that folder read-only into the backup container. I create `~/vatpac-statsV2/docker-compose.override.yml` (compose reads it automatically; it exists only on the VM, is not in git, and the `git archive` deploy never deletes it. Note 14-10-2026: once the VM is a git checkout (deploy-to-vm.md section 2d) this untracked file shows in `git status --short`, which the deploy steps expect to be empty; add it with `echo docker-compose.override.yml >> .git/info/exclude`. A checkout never deletes an untracked file):
```yaml
services:
  backup:
    volumes:
      - /home/matt/vatpac-backup-keys:/run/keys:ro
```
2. **Tools in the image.** The stock backup image has neither `rsync` nor `ssh` (checked in the running container on 10-10-2026). In the VM's `.env` I edit the EXISTING empty line `BACKUP_EXTRA_APT_PACKAGES=` (do not add a second copy) to `BACKUP_EXTRA_APT_PACKAGES=rsync openssh-client` (a non-secret line; I will not print the file), then run `docker compose up -d --build backup`. The backup container is rebuilt and recreated and the collector is not touched (the idempotent `migrate` job may run again, because the backup service waits for it). The build downloads packages from the internet (UNVERIFIED that the VM can reach the Debian mirrors; it can reach the internet for the feed). **Note 14-10-2026 (issue 173): this step as written no longer works on the production VM.** The VM runs in registry mode (`VATPAC_IMAGE_PREFIX` and `VATPAC_IMAGE_TAG` set since 14-10-2026): the backup image comes from the registry, the build argument `BACKUP_EXTRA_APT_PACKAGES` is ignored, and `docker compose up -d --build backup` is not allowed there (it rebuilds locally and replaces the pulled image). The published image therefore has no `rsync` or `ssh`. **OPEN DECISION for the owner (not decided here):** either (a) add `rsync` and `openssh-client` to `ops/backup/Dockerfile` so the release workflow publishes them (a code change and a new publish), or (b) use a copy method that needs nothing the image lacks, or runs outside the image. Do not run this step until one is chosen.

## D. You type the BACKUP_REMOTE line into the VM's `.env`

Edit the file yourself (`nano ~/vatpac-statsV2/.env`) and edit the EXISTING empty `BACKUP_REMOTE=` line (do not add a second one) so that it reads exactly this one line, with `PVE_HOST` replaced by the name or address you used for `ssh-keyscan`. Do not paste the file into chat.
```sh
BACKUP_REMOTE='rsync -t --timeout=600 -e "ssh -i /run/keys/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/run/keys/known_hosts -o ConnectTimeout=30" "$BACKUP_FILE" vatpacbackup@PVE_HOST:'
```
Notes on the format, from `ops/backup/backup.sh` (`run_remote`): the value is run as `sh -c "$BACKUP_REMOTE"` with the variable `BACKUP_FILE` set to the new dump's full path, under a 3600-second timeout, once after every successful, validated dump. Keep `"$BACKUP_FILE"` in double quotes. The outer single quotes stop `.env` reading the `$`. The trailing `PVE_HOST:` with nothing after the colon means the root folder of the forced rrsync, `/srv/vatpac-backups` (UNVERIFIED for your rrsync version; the test in section E shows it).

Check what the container will actually run (this prints only the command, which contains no secret):
```sh
cd ~/vatpac-statsV2 && docker compose exec -T backup sh -c 'printf "%s\n" "$BACKUP_REMOTE"'
```
It must show the line above with `$BACKUP_FILE` still literal (UNVERIFIED that compose does not expand it; if it prints an empty string where `$BACKUP_FILE` should be, tell me and I will change the quoting).

## E. Test, and what you should see

1. After I have rebuilt (section C) and you have edited `.env` (section D): `docker compose up -d backup` (recreates the backup container with the new setting; no code change, no build needed beyond C). Once deploy 1 is live, use `docker compose up -d backup watchdog` instead, so the watchdog also switches the `backup_remote` check on. Before deploy 1 do not recreate the watchdog; it gains nothing from it.
2. Take a backup now and watch the hook:
```sh
cd ~/vatpac-statsV2 && make backup
```
   (`make backup` runs `backup.sh once`; run it in an interactive ssh session because it uses `docker compose exec` without `-T`.) Expected output ends with `backup: ok <size>`, `backup: running BACKUP_REMOTE hook`, then `backup: BACKUP_REMOTE ok`. The failure line is `backup: BACKUP_REMOTE FAILED (the local backup is intact)`, preceded by the ssh or rsync error text.
3. The marker: `docker compose exec backup cat /backups/.last_remote_success` shows a UTC time just now.
4. On Proxmox: `ls -l /srv/vatpac-backups` shows the new `vatpac_<date>T<time>Z.dump`, owned by `vatpacbackup`, mode `-rw-r--r--` (the file takes the source mode; `rrsync` does not allow `--chmod`). That is fine because the folder is mode 700 and owned by `vatpacbackup`, so nobody else can reach the file.
5. Prove it restores once: copy it back (any way you like) and run it through the normal restore into a scratch name ([RUNBOOKS/restore.md](RUNBOOKS/restore.md), "Restore into a scratch database").

What the watchdog will report:
- The check is called `backup_remote`. It is switched on automatically when `BACKUP_REMOTE` is non-empty. With the marker present it is OK ("last off-box copy N h ago"); with none yet it says "no off-box copy yet" and stays OK until 26 hours after the watchdog first ran, then goes to WARN; it also goes to WARN when the marker is more than 26 hours old (`WATCHDOG_BACKUP_MAX_HOURS`). The Telegram message is a plain sentence ending `Technical: backup_remote ...` ([RUNBOOKS/alerts.md](RUNBOOKS/alerts.md)).
- A failed copy is otherwise only in the container log: `docker compose logs --tail=50 backup | grep BACKUP_REMOTE`. It never fails the local backup.
- **Not deployed yet:** this check exists on main but not on the VM. Until deploy 1, a broken copy raises no alert.

## F. Taking it out again

1. VM `.env`: set `BACKUP_REMOTE=` (empty) and `BACKUP_EXTRA_APT_PACKAGES=` (empty). Then, on the production VM (registry mode), `docker compose up -d --no-build backup watchdog` (never `--build` there; the empty build argument has no effect on the published image anyway). In local-build mode `docker compose up -d --build backup watchdog`. The check switches itself off (it reports "off-box copy not configured").
2. Remove the override: `rm ~/vatpac-statsV2/docker-compose.override.yml`, then `docker compose up -d backup`.
3. Delete the key: `sudo rm -r ~/vatpac-backup-keys` (the private key is owned by uid 999, hence sudo).
4. Proxmox: delete the user and its keys with `deluser --remove-home vatpacbackup`, remove `/etc/cron.d/vatpac-backup-prune`, and decide deliberately about `/srv/vatpac-backups`: it holds real backups and is not removed by `deluser`. Delete it only when you no longer want them.
5. If you only want to stop trusting the key quickly, delete its line from `/home/vatpacbackup/.ssh/authorized_keys` on Proxmox. That alone ends the copying.

## First run checklist (things only a real run can prove)
Tick these on the first run; any failure means stop and tell me.
- [ ] `rrsync` on the Proxmox host exists and accepts every option the client sends (`-t`, `--timeout`, and the others rsync adds itself): the E2 hook line shows `BACKUP_REMOTE ok`, not a "invalid rsync-command syntax or options" error.
- [ ] The empty-path destination `vatpacbackup@PVE_HOST:` lands the file in `/srv/vatpac-backups` end to end.
- [ ] The key is accepted from the VM's address (B3 first test gives no shell and no `Permission denied`).
- [ ] The forced command refuses other commands (B3 second test: refusal message, never a listing).
- [ ] The fingerprint from `ssh-keyscan` matches the one shown on Proxmox (B2).
- [ ] `ssh` inside the container can read the chowned key (E2 does not say `Permission denied` or `unprotected private key`).
- [ ] `$BACKUP_FILE` survives Compose: the D `printf` check shows it literal.
- [ ] The copy arrives with the right owner and mode: `vatpacbackup`, `-rw-r--r--`, in a folder of mode 700 (E4).
- [ ] The copy restores into a scratch database (E5), and the `backup_remote` marker `.last_remote_success` is fresh (E3).

## Open points

- **Cap on pre-deploy dumps (ADR-026 item 7): NOT DECIDED.** Every deploy takes an extra dump with `backup.sh once` (12 accumulated in two days), and that path also runs the hook, so each one is copied to Proxmox as well and the 30-day prune above is the only limit. The cap itself is recommended but not confirmed by you; decide it separately.
- Whether the rsync destination form, the `-wo` option, the quoting of `$BACKUP_FILE`, and the key ownership work together is **UNVERIFIED** until section E has been run once. I have no way to try it from here without changing the VM or the host.
- A permanent fix for key ownership (a compose `user:` or a startup copy) would be a code change and is not part of this card.
- This card is reference for [OWNER_ACTIONS.md](OWNER_ACTIONS.md); the generic hook is described in [RUNBOOKS/restore.md](RUNBOOKS/restore.md), "Off-box copies".

## What I did and did not check

Checked against the repository: `ops/backup/backup.sh` `run_remote` (variable `BACKUP_FILE`, `sh -c`, 3600 s timeout, marker `/backups/.last_remote_success`, failure only logged), `ops/backup/Dockerfile` (`EXTRA_APT_PACKAGES` build argument, user `postgres`), `docker-compose.yml` (backup service `BACKUP_REMOTE`, build argument from `BACKUP_EXTRA_APT_PACKAGES`, `cap_drop: ALL`, no key mount; watchdog `BACKUP_REMOTE_ENABLED: ${BACKUP_REMOTE:+1}`), `ops/watchdog/watchdog.py` `check_backup_remote`, `.env.example`, restore.md "Off-box copies", ADR-026 item 7. Checked on the VM, read-only, on 10-10-2026: the running backup container is uid 999, has no `rsync` or `ssh`, `/backups` is owned by postgres; `BACKUP_REMOTE` is empty; the deployed `backup.sh` has `run_remote` and the marker; the deployed compose file has no `BACKUP_REMOTE_ENABLED`; `make` is installed; no `docker-compose.override.yml` exists; a pre-existing `~/.ssh/vatpac_backup_key` exists (not read). Not run anywhere: every Proxmox command, every key command, the rebuild and the hook.
