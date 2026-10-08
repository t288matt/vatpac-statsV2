# Runbook: update the polygon or controller whitelist

Files (mounted read-only at `/app/config`): `config/australian_airspace_polygon.json` and `config/controller_callsigns_list.txt`. The collector loads them at start and reloads on SIGHUP. An invalid new file, or a whitelist that shrinks to under 70% of the old one, is rejected: the old one stays in use, an error is logged and `/health` shows `reload_rejected` above 0 (the watchdog does not send a Telegram alert for this yet, so check the logs yourself). A missing, unreadable or empty whitelist stops start-up (fail closed).

## Steps
1. Edit the file in git on your workstation (never on the VM). Keep a copy of the old one.
2. Validate locally: polygon must be a closed GeoJSON Polygon (lon,lat order, lon -180..180, lat -90..90); whitelist lines are `CALLSIGN, FREQ` or `CALLSIGN`, `#` comments allowed, case-sensitive.
3. Commit, push, tag if you version deployments.
4. Put the new files on the VM. The production VM has no git, so from the PC: `git archive --format=tar HEAD | ssh vatpac-v3 'cd ~/vatpac-statsV2 && tar -x'` (see [deploy-to-vm.md](deploy-to-vm.md)); on a host with git, `git pull` (or `git checkout <tag>`). `config/` is mounted into the container, so no rebuild is needed.
5. Reload without restart:
   ```sh
   docker compose kill -s HUP collector
   docker compose logs --tail=30 collector      # expect "reference data reloaded" and the new SHA-256, or "reference reload rejected"
   ```
   If the change is not picked up (bind mounts of single files can hold a stale inode after an editor replaces the file), `docker compose restart collector`.
6. Verify:
   ```sh
   docker compose exec db psql -U vatpac -d vatpac -c \
     "SELECT changed_at, file, old_sha, new_sha FROM reference_data_change ORDER BY id DESC LIMIT 5"
   docker compose exec db psql -U vatpac -d vatpac -c \
     "SELECT polygon_sha, whitelist_sha FROM poll_log ORDER BY id DESC LIMIT 1"
   ```
   The newest `poll_log` SHAs must equal the new `reference_data_change` rows.

## Notes
- Near-miss controllers (a similar prefix but not listed) are in `controller_unlisted_sighting`; the watchdog alerts on the first sighting of each. To accept one, add it to the whitelist as above.
- A polygon change affects only new snapshots; stored history is not re-filtered.
- The SHA-256 is of the raw file bytes, so the same text with Windows line endings (CRLF) hashes differently from LF. Git forces LF (`.gitattributes`), so a deployed file is LF; do not hand-edit on Windows and copy across.
