# Review: docs-backup-copy (tip ad58f20), 10-10-2026

Independent reviewer. Docs only: docs/OWNER_BACKUP_COPY.md, an index row in docs/README.md, item 8 in docs/OWNER_ACTIONS.md. Read-only review (VM read-only, official sources read). Nothing on the Proxmox host was run.

Verdict: DO NOT MERGE AS IT STANDS. Approve once finding 1 is fixed (one option removed); findings 2 to 5 should be fixed in the same pass. The rest of the card checks out.

## Findings, ranked

1. BLOCKER. docs/OWNER_BACKUP_COPY.md:107 (the BACKUP_REMOTE line) uses `--chmod=F600`. rrsync refuses it: `chmod` is not in rrsync's table of allowed long options (read in rsync v3.2.7 and v3.4.1, `support/rrsync`; Debian bookworm ships rsync 3.2.7). An unknown option makes rrsync stop with "invalid rsync-command syntax or options", so every copy would fail (only a log line, and before deploy 1 there is no alert). Correct line, with the option removed:
   `BACKUP_REMOTE='rsync -t --timeout=600 -e "ssh -i /run/keys/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/run/keys/known_hosts -o ConnectTimeout=30" "$BACKUP_FILE" vatpacbackup@PVE_HOST:'`
   Line 126 (E4) should then expect `-rw-r--r--` (new files take the source mode under the user's umask); that is fine because /srv/vatpac-backups is mode 700 and owned by vatpacbackup. `-t` and `--timeout` are allowed (same table; `t` is in the allowed short options).
2. MEDIUM. :54-60 (the 30-day prune) deletes every dump older than 30 days with no floor. If the VM is dead or the copy has failed for over 30 days, the prune removes the last good copies, exactly when they are needed. Safer, only prune while enough recent files exist:
   `30 3 * * * vatpacbackup [ "$(find /srv/vatpac-backups -maxdepth 1 -name '*.dump' -mtime -31 | wc -l)" -ge 7 ] && find /srv/vatpac-backups -maxdepth 1 -name '*.dump' -mtime +30 -delete`
   (no percent characters, which cron would break). Also say that failed transfers can leave stray `.vatpac_*` temp files that the prune does not match.
3. MEDIUM. :53 and :22-28: "no reading back, no escaping the folder" is right, but write-only is not append-only: a stolen key (or a compromised VM) can overwrite files already in the folder and fill the Proxmox disk. Add one sentence saying so. Choose a folder on a disk where filling cannot hurt Proxmox itself (not a small root filesystem), or use a quota.
4. LOW-MEDIUM, clarity. :22-52: A5 needs the key from B1, so "do the steps in order" cannot be followed literally. State the real order: A1-A4, B1, B2, A5, B3, B4, then C. In B3 (:79-83) add a second test that proves the restriction: `ssh ... vatpacbackup@PVE_HOST ls` must print an rrsync refusal ("SSH_ORIGINAL_COMMAND does not run rsync", from the rrsync source), never a directory listing.
5. LOW. :101 and :119: `docker compose up -d --build backup` also starts `migrate` again (backup depends on it; idempotent), and `up -d backup watchdog` recreates the watchdog needlessly before deploy 1. Reword "only the backup container" to "the backup container (the idempotent migrate job may run again)". In C/D say "edit the existing empty BACKUP_REMOTE= and BACKUP_EXTRA_APT_PACKAGES= lines in .env, do not add second copies". If the VM's LAN address can change (DHCP), `from="VM_ADDR"` would silently stop the copies; advise a fixed address.
6. INFORMATION. :84-90: sudo works only with a password on the VM (`sudo -n true` answers "a password is required"; matt is in the sudo group), consistent with the card. matt is also in the docker group, so the key could still be read through Docker; the chown protects against accidents, not a hostile local user.

## Claims checked

Repository and VM (all confirmed):
- backup.sh `run_remote`: `[ -n "$BACKUP_REMOTE" ]`, `BACKUP_FILE="$1" timeout 3600 sh -c "$BACKUP_REMOTE"`, marker `/backups/.last_remote_success`, failure only logged; called only after the dump is validated and moved into place under its final name.
- Dockerfile: `ARG EXTRA_APT_PACKAGES`; the apt install runs as root before `USER postgres`. Compose passes `${BACKUP_EXTRA_APT_PACKAGES:-}`, `BACKUP_REMOTE: ${BACKUP_REMOTE:-}`, `cap_drop: ALL`, only the `backups` volume, and the watchdog `BACKUP_REMOTE_ENABLED: ${BACKUP_REMOTE:+1}`. The VM's deployed compose file lacks BACKUP_REMOTE_ENABLED, so there is no watchdog check today (the card says so).
- check_backup_remote behaviour (off message, "no off-box copy yet" until the limit, WARN after 26 h) matches the card.
- Live VM: container uid=999(postgres), HOME=/var/lib/postgresql; no rsync, ssh or rrsync in the image; /backups owned by postgres; .env has empty `BACKUP_REMOTE=` and empty `BACKUP_EXTRA_APT_PACKAGES=` lines; no docker-compose.override.yml; ~/vatpac-backup-keys does not exist; ~/.ssh/vatpac_backup_key (22-08-2025) exists (listed only, never read) and the card never touches, reuses or deletes it. F3 `sudo rm -r ~/vatpac-backup-keys` targets only the new folder.
- ADR-026 item 7 and restore.md "Off-box copies" are consistent; the cap on pre-deploy dumps is correctly left undecided.

Proxmox and OpenSSH:
- rrsync is `/usr/bin/rrsync` in the Debian bookworm rsync package (Debian file list): confirmed. It is a python3 script.
- `-wo` exists and blocks reading ("reading from write-only server is not allowed"): confirmed (rrsync source, man page).
- Destination `PVE_HOST:` (empty path) arrives as `.`, which rrsync maps to its root directory: confirmed from source; the end-to-end result still needs one real run.
- authorized_keys: `restrict` disables pty, port, agent and X11 forwarding and ~/.ssh/rc; `from=` matches host name or IP; `command=` ignores the client's command and sets SSH_ORIGINAL_COMMAND: confirmed (sshd man page).
- The forced command runs through the user's login shell (`-c`), so `nologin` would break it: confirmed (sshd man page).
- StrictModes: ~/.ssh, authorized_keys and the home folder must not be writable by others; the card's 700/600 and adduser's default home satisfy this: confirmed.
- Container ssh: the key must be owned by the running uid with mode 600; a read-only bind mount with uid 999 satisfies that after the chown; no user remapping is configured. Consistent with ssh's rules, unproven without a run.
- `adduser --disabled-password` leaves `*` in the password field, which sshd accepts for key logins on Debian: believed correct, not proven here.
- Single-quoted `.env` values keep `$BACKUP_FILE` literal: consistent with Compose env-file rules; the card's own `printf` check (section D) is the proof. Unverifiable without a run.

## Only a real run can prove
rrsync accepting the exact options the client sends (finding 1 is the known failure; others may exist); the empty-path destination; sshd accepting the key from the VM's address; ssh in the container reading the chowned key; `$BACKUP_FILE` surviving Compose; the VM reaching the Debian mirrors for the image build; the fingerprint match; the files appearing with the expected owner and mode.

## Safety summary
The private key never leaves the VM and is never printed; the Proxmox key is write-only to one folder and tied to the VM's address; no step reads, reuses or deletes ~/.ssh/vatpac_backup_key; sudo is limited to chown, chmod and rm of the new key folder; lock-out risk is nil (the Proxmox user is new, the owner's logins are untouched). Style: British English, DD-MM-YYYY, UTC, no emoji, no real addresses or secrets in the three files.

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>

# Re-check at tip 520dc3e (10-10-2026)

Verdict: APPROVE. `git diff ad58f20..520dc3e` touches only docs/OWNER_BACKUP_COPY.md.
- Finding 1 fixed: the BACKUP_REMOTE line (line 108) is now `rsync -t --timeout=600 -e "ssh ..." "$BACKUP_FILE" vatpacbackup@PVE_HOST:`. Every option left (`-t`, `--timeout`, and `-e`, which is client-side only) is accepted by rrsync (checked against its option table earlier). E4 now expects `-rw-r--r--` with the reason.
- Finding 2 fixed: the cron line is `30 3 * * * vatpacbackup [ "$(find /srv/vatpac-backups -maxdepth 1 -name '*.dump' -mtime -31 | wc -l)" -ge 7 ] && find ... -mtime +30 -delete`; no percent character anywhere in it, valid under cron's sh; the floor and stray temp-file note are explained.
- Finding 3 fixed: the write-only-is-not-append-only sentence and the separate-disk or quota advice are in section A1 and A5.
- Finding 4 fixed: the real order (A1 to A4, B1, B2, A5, B3, B4, then C, D, E) is stated at the top and agrees with the numbering and with B3 "do section A step 5 first"; the second B3 test (`ls` must give an rrsync refusal, never a listing) is added and matches rrsync source.
- Finding 5 fixed: migrate re-run noted; edit the EXISTING empty lines in .env; fixed VM address advice. E1 is correct both ways: `docker compose up -d backup` before deploy 1, `docker compose up -d backup watchdog` after.
- Finding 6 fixed: the docker-group caveat is in B4.
- New First run checklist: accurate and complete against the list of things only a real run can prove (rrsync options, empty-path destination, key accepted, forced command, fingerprint, container key read, `$BACKUP_FILE` literal, owner and mode, restore and marker).
- No new error found. One cosmetic point, not required: the checklist quotes "a invalid" (should be "an invalid").

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>
