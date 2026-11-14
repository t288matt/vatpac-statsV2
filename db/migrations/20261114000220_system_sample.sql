-- migrate:up
-- Issue 27, OWNER DECISIONS 14-11-2026: two panels on the Collector health dashboard, database size against VM disk use, and database speed.
-- Grafana reads only the database, so the collector's daily maintenance job records one row per UTC day.
--
--   1. system_sample: the daily record. sampled_on (UTC day) is the key, so a second reading on the same day replaces the first and a
--      day can never hold two rows. The checks refuse a zero or negative size and a disk figure that is not between 0 and the total.
--   2. app_setting 'system_sample_retention_days' (400): the one source of the retention, read through setting_num (no built-in default).
--   3. system_probe_flight_count(): the fixed speed probe, a count of the flights of the last 30 days. SECURITY DEFINER, so the collector
--      needs no right on flight_summary (the integration tests pin that it has none). The collector times three runs and keeps the median;
--      it caps the statement itself (statement_timeout 5 s, set for the probe only), because a statement_timeout set on a function does not
--      limit the statement that calls it.
--   4. record_system_sample(total, used, probe_ms): the one write. It adds pg_database_size(current_database()) and the UTC day, upserts the
--      row, then deletes the rows older than the retention. A failing trim (a missing or non-numeric setting, or a value below 2) is a
--      WARNING and never undoes the day's row. SECURITY DEFINER, so the collector has no right on system_sample either.
--   5. health_sample: the view for Grafana (megabytes, gigabytes, milliseconds). grafana_ro gets it from the view loop in db/roles.sql and
--      has no right on the table.
-- EXECUTE on the two functions is granted to the collector in db/roles.sql.

CREATE TABLE system_sample (
    sampled_on       date PRIMARY KEY,
    sampled_at       timestamptz NOT NULL,
    db_bytes         bigint NOT NULL CHECK (db_bytes > 0),
    disk_total_bytes bigint NOT NULL CHECK (disk_total_bytes > 0),
    disk_used_bytes  bigint NOT NULL CHECK (disk_used_bytes >= 0),
    probe_ms         numeric NOT NULL CHECK (probe_ms >= 0),
    CHECK (disk_used_bytes <= disk_total_bytes)
);
COMMENT ON TABLE system_sample IS
    'One row per UTC day (issue 27): database size, VM disk total and used, and the median time of the fixed speed probe. Written by record_system_sample() from the collector''s daily maintenance; rows older than app_setting system_sample_retention_days are deleted by the same call. Grafana reads the view health_sample, not this table.';

INSERT INTO app_setting (key, value) VALUES ('system_sample_retention_days', '400') ON CONFLICT (key) DO NOTHING;

CREATE FUNCTION system_probe_flight_count()
RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $f$
BEGIN
    RETURN (SELECT count(*) FROM flight_summary WHERE start_time >= now() - interval '30 days');
END
$f$;
REVOKE ALL ON FUNCTION system_probe_flight_count() FROM PUBLIC;
COMMENT ON FUNCTION system_probe_flight_count() IS
    'The fixed speed probe of issue 27: the number of flights that started in the last 30 days. The collector times three calls and records the median. EXECUTE is granted to the collector in db/roles.sql.';

CREATE FUNCTION record_system_sample(p_disk_total bigint, p_disk_used bigint, p_probe_ms double precision)
RETURNS int
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $f$
DECLARE
    d    date := (now() AT TIME ZONE 'UTC')::date;
    keep numeric;
BEGIN
    INSERT INTO system_sample (sampled_on, sampled_at, db_bytes, disk_total_bytes, disk_used_bytes, probe_ms)
    VALUES (d, now(), pg_database_size(current_database()), p_disk_total, p_disk_used, round(p_probe_ms::numeric, 2))
    ON CONFLICT (sampled_on) DO UPDATE
       SET sampled_at = EXCLUDED.sampled_at, db_bytes = EXCLUDED.db_bytes, disk_total_bytes = EXCLUDED.disk_total_bytes,
           disk_used_bytes = EXCLUDED.disk_used_bytes, probe_ms = EXCLUDED.probe_ms;
    BEGIN
        keep := setting_num('system_sample_retention_days');
        IF keep < 2 THEN
            RAISE EXCEPTION 'system_sample_retention_days must be 2 or more, found %', keep;
        END IF;
        DELETE FROM system_sample WHERE sampled_on < d - keep::int;
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'system_sample trim skipped: %', SQLERRM;
    END;
    RETURN 1;
END
$f$;
REVOKE ALL ON FUNCTION record_system_sample(bigint, bigint, double precision) FROM PUBLIC;
COMMENT ON FUNCTION record_system_sample(bigint, bigint, double precision) IS
    'Records today''s (UTC) row of system_sample: the database size read here, the disk figures and the probe time given; a second call on the same day replaces the row. Then deletes the rows older than app_setting system_sample_retention_days (a failing trim only warns). Returns 1. EXECUTE is granted to the collector in db/roles.sql.';

CREATE VIEW health_sample AS
SELECT sampled_on::timestamp AT TIME ZONE 'UTC'                 AS day,
       round(db_bytes / 1048576.0, 1)                           AS database_mb,
       round(disk_used_bytes / 1073741824.0, 2)                 AS disk_used_gb,
       round(disk_total_bytes / 1073741824.0, 2)                AS disk_total_gb,
       probe_ms
  FROM system_sample;
COMMENT ON VIEW health_sample IS
    'The daily system samples for the Collector health dashboard (issue 27): day (UTC midnight), database_mb (MiB), disk_used_gb and disk_total_gb (GiB of the VM disk that holds the Docker volumes), probe_ms (median of three runs of the fixed flight count, in milliseconds). One row per UTC day.';

-- migrate:down
-- Forward-only project: no automatic rollback.
