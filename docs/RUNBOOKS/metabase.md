# Runbook: Metabase (ad-hoc questions beside Grafana)

Added by issue 288 (owner instruction 06-11-2026). Metabase is for ad-hoc questions; it does not replace Grafana (decision on issue 280: Grafana stays). Not deployed until the owner gives the deploy go. Rehearsed on 06-11-2026 on throwaway containers on the owner's PC (section 9), never on the VM.

## 1. What is added

| Piece | What it is |
|---|---|
| `metabase` service | `metabase/metabase:v0.64.5.4` (the open-source image), memory cap 1792 MB (`-Xmx1g` for the Java heap), 1.5 CPUs, `restart: unless-stopped`, healthcheck on `/api/health`. Published on `METABASE_BIND`:`METABASE_PORT` (default `127.0.0.1:3400`). No proxy label, so it is not reachable from outside the LAN; making it public is a separate owner decision. |
| `metabase-db` service | `postgres:16-bookworm` with a 256 MB cap and the named volume `metabase_db_data`. It holds Metabase's own settings, users, saved questions and the saved connection. It is not the vatpac database and is on a private network (`vatpac-metabase`, no outside route) shared only with `metabase` and `backup`. |
| role `metabase_ro` | Created by `db/roles.sql` (run by the `migrate` job, never by hand): the same rights and limits as `grafana_ro`. |
| backup | The `backup` service also dumps `metabase-db` to `metabase_<UTC stamp>.mbdump` in the `backups` volume (section 5). |

Secrets, all in `.env` only (generate with `sh ops/gen_env.sh`, check with `sh ops/check_env.sh`): `METABASE_RO_PASSWORD` (the database role), `METABASE_DB_PASSWORD` (the application database), `METABASE_ENCRYPTION_KEY` (encrypts the saved connection details). Settings: `METABASE_PORT` (3400), `METABASE_BIND`, `METABASE_SITE_URL`. See [../CONFIG_MAP.md](../CONFIG_MAP.md).

Image policy: ADR-032 floats third-party images, but Metabase publishes no bare-major tag, and a new Metabase version migrates its application database in a way a downgrade cannot undo. So the tag is an exact version, changed only on purpose (section 7).

Licence: the image is Metabase's open-source edition (AGPL). Used internally on the LAN it needs no licence purchase; embedding it in a public product or using the enterprise features would be a separate decision.

## 2. Install (deploy)

A deploy needs the owner's explicit go, one per deploy (WA 10). Fresh dump first (`docker compose exec backup backup.sh once`, check the new file in `/backups`). Before the deploy the owner adds three lines to the VM's `.env` (values are never printed or pasted into a chat): `METABASE_RO_PASSWORD`, `METABASE_DB_PASSWORD` and `METABASE_ENCRYPTION_KEY` (`openssl rand -hex 24` each, letters and digits only), and, for LAN access, `METABASE_BIND=<the VM's LAN address>`. Compose refuses to start any service until all three secrets are set, because `migrate` reads `METABASE_RO_PASSWORD` and `backup` reads `METABASE_DB_PASSWORD`; `sh ops/check_env.sh` (part of `make up` and the deploy preflight) names a missing one.

Then the usual deploy (deploy-to-vm.md). `migrate` creates `metabase_ro` on its first run; `docker compose up -d` starts `metabase-db` and `metabase`. The first start of Metabase creates its tables and takes one to two minutes (the healthcheck allows two). Check:

```sh
docker compose ps metabase metabase-db          # both healthy
curl -fsS http://127.0.0.1:3400/api/health      # {"status":"ok"}
```

## 3. First-run set-up (the owner does this; passwords are entered by the owner only)

Open `http://<the VM's LAN address>:3400/`, choose the language, and create the admin account (a strong password, kept in the owner's password manager). Tell Metabase not to share usage data. Then add the data:

1. Admin settings, Databases, Add database, type PostgreSQL.
2. Host `db`, port `5432`, database name `vatpac` (the value of `POSTGRES_DB`), username `metabase_ro`, password `METABASE_RO_PASSWORD` from `.env`, SSL off (the connection stays inside the Docker network).
3. Do not tick "Allow data editing"/model actions; leave "Let Metabase sync" on. Save. It lists 28 tables: the 22 views and the 6 lookups, and nothing else. If it lists more, stop and tell the engineer.
4. Delete the "Sample Database" entry (it is only demo data).
5. Create further accounts for the people who need them (Admin settings, People). Anyone who can log in can run SQL on the views (section 8).

## 4. What Metabase may and may not see

`metabase_ro` has what `grafana_ro` has and no more: SELECT on every view and materialized view in `public` (re-granted on every deploy, so new views appear) and on the six lookup tables; no base table (`pilot_session`, `pilot_position`, `controller_session`, `flight_summary`, `poll_log` and the rest); no INSERT, UPDATE, DELETE, TRUNCATE or DDL; cannot create in `public`; connection limit 10, `statement_timeout` 30 s, `default_transaction_read_only = on`, `work_mem` 16 MB, JIT off, time zone UTC. A person can switch the read-only default off in their own session, so the real guard is that the role has no write privileges, which the tests check with the default switched off (`tests/integration/test_metabase_ro.py`).

The views do expose personal data: the `flight` view carries pilot names, VATSIM ids, callsigns and the flight plan remarks (see [../DATA_DICTIONARY.md](../DATA_DICTIONARY.md)), the same as the Grafana dashboards show to a logged-in user. The owner decides who gets a Metabase account. Metabase has its own copy of the saved connection (encrypted with `METABASE_ENCRYPTION_KEY`) and of every saved question; it never holds a copy of the vatpac data.

## 5. Backup

The nightly `backup` run (02:00 UTC, `BACKUP_TIME_UTC`) now also writes `metabase_<UTC stamp>.mbdump` (`pg_dump -Fc` of the application database, about 1 MB, validated with `pg_restore -l`) to the `backups` volume. Rules:

- The name ends `.mbdump`, never `.dump`, so the vatpac size rule, key-table check, rotation, the container healthcheck and the watchdog backup-age check never see it.
- Kept `BACKUP_RETENTION_DAYS` (14) days, never fewer than `BACKUP_MIN_KEEP` (3) files.
- A failed Metabase dump is logged (`backup: FAILED metabase dump`) and never changes the vatpac backup or its exit status; a failed vatpac dump never stops the Metabase dump. The watchdog does not watch the Metabase dump or the Metabase container; check the log line in `docker compose logs backup` after the first night.
- Not in the dump: `METABASE_ENCRYPTION_KEY`. It lives only in `.env`: keep a copy of `.env` as before (never in git or a chat). Without the key a restored Metabase works, but its saved database connection cannot be read, so the owner re-enters the `metabase_ro` password in Admin settings, Databases.
- The `backup` service reaches `metabase-db` as the `metabase` user with `METABASE_DB_PASSWORD`.

## 6. Restore

Restore the application database from a `.mbdump` (settings, users, saved questions come back; vatpac data is untouched):

```sh
cd ~/vatpac-statsV2
docker compose stop metabase
docker compose exec backup ls -1t /backups | grep mbdump | head       # pick the file
# restore into a fresh database, then swap by name (the old one stays as metabase_old until you are happy)
docker compose exec backup sh -c '
  export PGPASSWORD="$METABASE_PGPASSWORD" PGHOST=metabase-db PGUSER=metabase
  createdb metabase_new &&
  pg_restore -d metabase_new --no-owner /backups/<FILE>.mbdump'
```

Then rename (`ALTER DATABASE metabase RENAME TO metabase_old; ALTER DATABASE metabase_new RENAME TO metabase;` from `docker compose exec backup sh -c 'PGPASSWORD="$METABASE_PGPASSWORD" psql -h metabase-db -U metabase -d postgres'`) and `docker compose start metabase`. If the volume is lost entirely: `docker compose up -d metabase-db`, wait until healthy, `pg_restore` into the empty `metabase` database with `--clean --if-exists --no-owner`, then start `metabase`. If there is no usable dump at all, Metabase simply starts empty and the owner repeats section 3; no vatpac data is lost. The restore was rehearsed on a throwaway (section 9).

## 7. Upgrade

Metabase migrates its application database on the first start of a newer version, and the migration cannot be undone. So: take a fresh dump (`docker compose exec backup backup.sh once`), copy the volume (`docker run --rm -v vatpac_metabase_db_data:/d -v "$PWD":/o alpine tar czf /o/metabase_db_pre_upgrade.tgz -C /d .`, keep it until the new version is confirmed), read the release notes, rehearse the new tag on a throwaway first (section 9), then edit the tag in `docker-compose.yml` (and `EXPECTED_THIRD_PARTY` in `tests/test_compose_image_vars.py`), `docker compose pull metabase`, `docker compose up -d metabase`, and check `/api/health` and one saved question. To go back after a failed upgrade: stop `metabase`, restore the pre-upgrade `.mbdump` or volume copy (section 6), put the old tag back, start.

## 8. Roll back (remove Metabase)

```sh
cd ~/vatpac-statsV2
docker compose stop metabase metabase-db && docker compose rm -f metabase metabase-db
docker volume rm vatpac_metabase_db_data        # deleting data: only after the owner agrees; keep the last .mbdump first
docker compose exec db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c 'DROP ROLE metabase_ro'   # the role owns nothing, so this is enough
```

Run the `DROP ROLE` only after the Metabase service is removed (a live connection blocks it). Revert the commit that added the service and `db/roles.sql` block before the next deploy; if `db/roles.sql` still contains the block, the next `migrate` run creates the role again. Grafana, the collector and the backup of the vatpac database are unaffected either way. Delete the three `METABASE_` secrets from `.env` afterwards (until the revert is deployed, compose still wants them).

## 9. How it was tested (06-11-2026, throwaway containers named `mbtest-*` on the owner's PC, bound to 127.0.0.1, removed afterwards)

- PostgreSQL 16.15 with all migrations and `db/roles.sql` applied by the production `migrate` image. `metabase_ro` and `grafana_ro` have identical privilege lists (28 relations, all SELECT only; `diff` of the two lists is empty); `metabase_ro` cannot read any base table and cannot INSERT, UPDATE, DELETE, TRUNCATE or CREATE, also with the read-only default switched off (`tests/integration/test_metabase_ro.py`, run with the rest of the integration suite in CI).
- The real image `metabase/metabase:v0.64.5.4`, with the compose settings (caps, `-Xmx1g`, `no-new-privileges`, its own Postgres): `/api/health` returned `{"status":"ok"}`, the set-up page loaded, the database was added as `metabase_ro`, sync found exactly the 22 views and 6 lookups, a native SQL question on a view ran in the UI, and `select` on a base table, `delete` and `show statement_timeout` (30s) gave the expected results. Findings that changed the compose file: with `cap_drop: [ALL]` alone the image fails to start (`su: can't set groups`), so five capabilities are added back (CHOWN, SETUID, SETGID, DAC_OVERRIDE, FOWNER); and at 1536 MB the container used about 1.43 GiB at idle, so the cap is 1792 MB.
- `backup.sh once` against the throwaway wrote the `.mbdump`; `pg_restore` of it into a scratch database returned the users and the saved database entry.
- Not tested on the VM, and not tested under real query load.
