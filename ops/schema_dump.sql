-- Read-only schema dump for refreshing docs/DATA_DICTIONARY.md (docs/RUNBOOKS/data-dictionary.md).
-- Run from your machine (the file is read locally and piped over ssh; no secret is printed, nothing is copied to the VM):
--   ssh vatpac-v3 'cd ~/vatpac-statsV2 && docker compose exec -T db psql -U vatpac -d vatpac -P pager=off -A -F" | "' < ops/schema_dump.sql
-- It only reads the catalogue. The first line makes the session refuse any write.
SET default_transaction_read_only = on;

\echo == 1 applied migrations
SELECT version FROM schema_migrations ORDER BY 1;

\echo == 2 relations (r table, p partitioned table, v view, m materialised view) with comments
SELECT c.relname, c.relkind, obj_description(c.oid) AS comment
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm') AND NOT c.relispartition
ORDER BY c.relkind DESC, c.relname;

\echo == 3 table columns: type, nullable, default, comment
SELECT c.relname, a.attnum, a.attname, format_type(a.atttypid, a.atttypmod) AS type,
       NOT a.attnotnull AS nullable, pg_get_expr(d.adbin, d.adrelid) AS "default", col_description(c.oid, a.attnum) AS comment
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
ORDER BY c.relname, a.attnum;

\echo == 4 view columns (one line per view)
SELECT c.relname, string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum) AS columns
FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('v', 'm')
GROUP BY 1 ORDER BY 1;

\echo == 5 constraints (primary, unique, foreign, check); partitions excluded
SELECT conrelid::regclass AS "table", conname, contype, pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE connamespace = 'public'::regnamespace AND conrelid::regclass::text NOT LIKE 'pilot_position\_2%'
ORDER BY 1, 3, 2;

\echo == 6 indexes; partitions excluded
SELECT tablename, indexname, indexdef FROM pg_indexes
WHERE schemaname = 'public' AND tablename NOT LIKE 'pilot_position\_2%' ORDER BY 1, 2;

\echo == 7 partitioning: key, number of daily partitions, first and last
SELECT pg_get_partkeydef('pilot_position'::regclass) AS partition_key;
SELECT count(*) AS partitions, min(c.relname) AS first_partition, max(c.relname) AS last_partition
FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'pilot_position'::regclass;

\echo == 8 which relations each view reads
SELECT DISTINCT v.relname AS view, d.relname AS reads
FROM pg_rewrite r
JOIN pg_depend dp ON dp.objid = r.oid AND dp.classid = 'pg_rewrite'::regclass
JOIN pg_class v ON v.oid = r.ev_class JOIN pg_class d ON d.oid = dp.refobjid
WHERE v.relkind = 'v' AND v.relnamespace = 'public'::regnamespace AND d.relnamespace = 'public'::regnamespace
  AND d.relname <> v.relname AND d.relkind IN ('r', 'p', 'v') AND d.relname NOT LIKE 'pilot_position\_2%'
ORDER BY 1, 2;

\echo == 9 functions: arguments, result, security definer, grants
SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS arguments, pg_get_function_result(p.oid) AS returns,
       p.prosecdef AS security_definer, p.proacl AS grants, obj_description(p.oid, 'pg_proc') AS comment
FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace ORDER BY 1;

\echo == 10 table and view grants (partitions excluded)
SELECT grantee, table_name, string_agg(privilege_type, ',' ORDER BY privilege_type) AS privileges
FROM information_schema.role_table_grants
WHERE table_schema = 'public' AND grantee <> 'vatpac' AND table_name NOT LIKE 'pilot_position\_2%'
GROUP BY 1, 2 ORDER BY 1, 2;

\echo == 11 roles: login, connection limit, settings, memberships
SELECT rolname, rolcanlogin, rolconnlimit, rolconfig FROM pg_roles
WHERE rolname IN ('vatpac', 'collector', 'grafana_ro', 'backup_ro', 'watchdog_ro') ORDER BY 1;
SELECT r.rolname AS member, m.rolname AS member_of FROM pg_auth_members am
JOIN pg_roles r ON r.oid = am.member JOIN pg_roles m ON m.oid = am.roleid
WHERE r.rolname IN ('collector', 'grafana_ro', 'backup_ro', 'watchdog_ro') ORDER BY 1, 2;

\echo == 12 settings, triggers, storage options, lookup sizes
SELECT key, value FROM app_setting ORDER BY 1;
SELECT count(*) AS materialised_views FROM pg_matviews;
SELECT tgrelid::regclass AS "table", tgname FROM pg_trigger WHERE NOT tgisinternal;
SELECT relname, reloptions FROM pg_class WHERE relnamespace = 'public'::regnamespace AND reloptions IS NOT NULL;
SELECT (SELECT count(*) FROM airline) AS airline, (SELECT count(*) FROM aircraft_type) AS aircraft_type,
       (SELECT count(*) FILTER (WHERE is_military) FROM aircraft_type) AS military,
       (SELECT count(*) FILTER (WHERE in_v1_card65_list) FROM aircraft_type) AS card65,
       (SELECT count(*) FROM region_map) AS region_map, (SELECT count(*) FROM controller_rating) AS controller_rating,
       (SELECT count(*) FROM facility) AS facility, (SELECT count(*) FROM pilot_rating) AS pilot_rating;
