# Restore drill (T18)

Date: 08-10-2026, run 08:27 to 08:30 UTC (times taken from `ssh vatpac-v3 date -u`; the VM clock is UTC). The soak test was running; the drill used one dump read and a few short read-only queries.

## Verdict

**PASS.** The newest nightly dump restored cleanly (`pg_restore` exit 0 with `--exit-on-error`) into a new database `vatpac_restore_test`. Every table, view and function present live is present in the restored copy, with identical names. Every restored table holds a plausible subset of the live data, and the migration count matches. The scratch database and temp file were removed afterwards.

## Dump used

| Item | Value |
|---|---|
| Location | `/backups` in the backup container (volume `backups`) |
| Files present | `vatpac_20261007T213402Z.dump` (98,526 bytes), `vatpac_20261008T020000Z.dump` (416,952 bytes) |
| Newest dump | `vatpac_20261008T020000Z.dump`, 416,952 bytes, written 08-10-2026 02:00:00 UTC (archive header: created 2026-10-08 02:00:00 UTC) |
| Format | `pg_dump -Fc` (custom, gzip), 208 TOC entries, taken as the read-only role `backup_ro` |
| Age at drill | about 6 h 28 min |
| Integrity | SHA-256 identical in the backup container and in the db container after the copy (`7ed27775...3751b`) |

Note: `/backups/.last_restore_test` does not exist yet, because the automated restore test only runs after the nightly backup on day 7 (Sunday). This drill is therefore the first end-to-end proof; the automated test has not yet run on this deployment.

## Commands used (no passwords; credentials come from the db container environment)

All from `~/vatpac-statsV2` on the VM, over `ssh vatpac-v3`:

```
docker compose exec -T backup ls -la --time-style=full-iso /backups
docker compose exec -T backup cat /backups/vatpac_20261008T020000Z.dump \
  | docker compose exec -T db sh -c "cat > /tmp/restore_drill.dump"      # streamed; nothing written on the VM host
docker compose exec -T db sh -c "psql -X -tA -U $POSTGRES_USER -d postgres -c 'CREATE DATABASE vatpac_restore_test'"
docker compose exec -T db sh -c "pg_restore --no-owner --no-acl --exit-on-error -U $POSTGRES_USER -d vatpac_restore_test /tmp/restore_drill.dump"
# comparison: the same read-only SELECT script piped to psql -d vatpac and psql -d vatpac_restore_test
docker compose exec -T db sh -c "rm -f /tmp/restore_drill.dump; psql -X -tA -U $POSTGRES_USER -d postgres -c 'DROP DATABASE vatpac_restore_test WITH (FORCE)'"
```

`$POSTGRES_USER` must be expanded inside the container, so the `sh -c` argument has to be in single quotes when typed on a shell (the repeatable, correctly quoted form is in [RUNBOOKS/restore.md](RUNBOOKS/restore.md), "Check a dump by hand"). The restore options (`--no-owner --no-acl --exit-on-error`) are those used by `ops/backup/restore-test.sh` and `restore.sh`. No roles were created and `db/roles.sql` was not run. No warnings or errors were printed.

## Comparison (live read at 08:29 UTC; dump taken 02:00 UTC)

"Newest" is the max of `observed_at` (snapshot, positions, events), `last_seen` (sessions), `computed_at` (flight_summary) or `polled_at` (poll_log), all UTC on 08-10-2026 unless stated.

| Table | Live rows | Restored rows | Live newest | Restored newest |
|---|---:|---:|---|---|
| network_snapshot | 2,589 | 1,040 | 08:28:42 | 01:59:34 |
| pilot_session | 288 | 75 | 08:28:42 | 01:59:34 |
| controller_session | 28 | 8 | 08:28:42 | 01:59:34 |
| flight_summary | 288 | 73 | 08:28:59 | 01:58:48 |
| poll_log | 2,666 | 1,066 | 08:29:00 | 01:59:54 |
| controller_rating_event | 28 | 8 | 08:15:42 | 01:17:34 |
| controller_frequency_event | 47 | 18 | 08:15:42 | 01:18:34 |
| controller_unlisted_sighting | 1 | 0 | n/a | n/a |
| pilot_position (parent) | 71,270 | 18,593 | 08:28:42 | 01:59:34 |
| pilot_position_20261006 | 0 | 0 | none | none |
| pilot_position_20261007 | 10,118 | 10,118 | 07-10 23:59:49 | 07-10 23:59:49 |
| pilot_position_20261008 | 61,152 | 8,475 | 08:28:42 | 01:59:34 |
| pilot_position_20261009 to 20261014 (6 partitions) | 0 each | 0 each | none | none |
| Reference tables: aircraft_type 534, airline 62, app_setting 2, controller_rating 14, facility 7, pilot_rating 7, reference_data_change 2, region_map 21, schema_migrations 11 | equal | equal | n/a | n/a |

Structure: all tables and partitions, 20 views and 8 functions in the public schema have identical names live and restored. `schema_migrations` has 11 rows with latest version 20261007000024 in both. Views are queryable in the restored copy (`flight` returned 64 rows, `online_now` 0).

Reading the numbers: the newest restored row of each table is 6 s to about 1 min 15 s before the dump time (02:00:00 UTC), so the dump captured everything up to its start. The completed day partition `pilot_position_20261007` matches live exactly, which is the strongest like-for-like check. Elsewhere, live counts are higher only by what accrued in the following 6.5 hours. `controller_unlisted_sighting` is 0 in the dump and 1 live; that row presumably arrived after 02:00 (not verified individually).

## Surprising or worth noting

- `/backups/.last_restore_test` is absent: the automated restore test has never run here (first scheduled run is Sunday 11-10-2026 after the 02:00 UTC backup). Anything in the watchdog that reads that file has nothing to read until then.
- Only two dumps exist, so the 14-day rotation has not been exercised yet.
- A database `vatpac_test` is present in the VM Postgres from earlier integration-test work. It was not created by this drill and was left alone; drop it when no longer needed.
- Dumps are small (about 0.4 MB) because the dataset is a day old. Restore took a few seconds, which says little about restore time at 90 days of positions.

## Recommendations

1. Drill monthly, and after any migration that changes table structure or partitioning. Repeat this full comparison, not only the script's sanity check (which looks at `poll_log`, `pilot_session` and `schema_migrations` only).
2. Extend `sanity_check` in `ops/backup/restore-lib.sh` to fail on an empty `network_snapshot` and to compare the table and view lists with an expected list, so the weekly automated test catches a silently truncated dump.
3. Re-time the restore once there are weeks of data; the 90-day `pilot_position` volume will dominate. Record the duration here.
4. A full-disaster restore needs more than the dump contains: roles and passwords (`db/roles.sql` and the secrets in the environment file; dumps omit roles and ACLs), the `migrate` job to re-apply grants (see `docs/RUNBOOKS/restore.md`), the compose and `.env` configuration, rebuilt or pulled container images, and the Grafana volume (dashboards are provisioned from the repo, but users, UI-only changes and data-source secrets are not in the dump).
5. T17 remains the real exposure: dumps exist only on the VM's `backups` volume, so losing the VM loses the database and its backups together. Arrange the off-box copy before treating this drill as a disaster-recovery claim, and keep the destination private (dumps contain real names and CIDs, ADR-019).
6. Add the safe drill commands above to `docs/RUNBOOKS/restore.md` so the next drill is repeatable. *Done 08-10-2026 (documentation audit).*

## Cleanup confirmed (08:29:35 UTC)

- `DROP DATABASE vatpac_restore_test` succeeded. The database list afterwards is postgres, template0, template1, vatpac and vatpac_test (the last pre-existing).
- `/tmp/restore_drill.dump` removed from the db container; no restore or dump files remain in the db container's `/tmp` or the VM host's `/tmp`. Nothing was written inside `~/vatpac-statsV2` and the live database was only read.
