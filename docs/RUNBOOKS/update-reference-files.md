# Runbook: update the polygon or controller whitelist

Files (mounted read-only at `/app/config`): `config/australian_airspace_polygon.json` and `config/controller_callsigns_list.txt`. The collector loads them at start and reloads on SIGHUP. An invalid new file is rejected, the old one stays in use, and an alert is raised. A missing, unreadable or empty whitelist stops start-up (fail closed).

## Steps
1. Edit the file in git on your workstation (never on the VM). Keep a copy of the old one.
2. Validate locally: polygon must be a closed GeoJSON Polygon (lon,lat order, lon -180..180, lat -90..90); whitelist lines are `CALLSIGN, FREQ` or `CALLSIGN`, `#` comments allowed, case-sensitive.
3. Commit, push, tag if you version deployments.
4. On the VM: `git pull` (or `git checkout <tag>`).
5. Reload without restart:
   ```sh
   docker compose kill -s HUP collector
   docker compose logs --tail=30 collector      # expect "reloaded" and the new SHA-256, or a clear rejection
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
