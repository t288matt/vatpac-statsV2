# Runbook: refresh the data dictionary

[DATA_DICTIONARY.md](../DATA_DICTIONARY.md) describes the schema. Refresh it after any migration, or when the live database is changed.

## Steps
1. Dump the live schema (read-only; the script sets `default_transaction_read_only = on`, only reads the catalogue, prints no secret). From your machine, in the repository root:
   ```sh
   ssh vatpac-v3 'cd ~/vatpac-statsV2 && docker compose exec -T db psql -U vatpac -d vatpac -P pager=off -A -F" | "' < ops/schema_dump.sql > schema_dump.txt
   ```
   The file is piped over ssh, so nothing is copied to the VM. `ops/schema_dump.sql` has twelve numbered sections: applied migrations; relations with comments; table columns; view columns; constraints; indexes; partitions; which relations each view reads; functions and grants; table and view grants; roles; settings and lookup sizes.
2. Check `schema_migrations` (section 1) against `ls db/migrations`. Migrations are applied in file-name order; if the VM is behind or ahead of the repository, say so in the dictionary header and report it.
3. Compare each section with the dictionary and edit the file. For view rules read the definition: `SELECT pg_get_viewdef('public.<view>'::regclass, true);` (same read-only session).
4. Update the dated line at the top ("Checked against ... up to <latest migration> on DD-MM-YYYY") and the object counts.
5. Run `PYTHONPATH=collector/src python -m pytest -q -m "not integration" tests/test_data_dictionary_tests.py`. It fails when a table, view or function created by a migration is missing from the dictionary, or when the header names a migration other than the latest.
6. Delete `schema_dump.txt` (it is not for git).

Do not run `db/roles.sql` or any migration to "check" the schema; both write.
