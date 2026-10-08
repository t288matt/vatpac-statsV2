"""M7 least privilege: collector and backup_ro roles from db/roles.sql, SECURITY DEFINER maintenance
functions, plus the online_now wall-clock guard and the movements comment.

The role tests skip when the roles have not been created (db/roles.sql not applied).
"""
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration

MAINT_FUNCS = [
    "ensure_pilot_position_partitions(int, date)",
    "drop_old_pilot_position_partitions(int, date)",
    "refresh_flight_summary(interval)",
    "refresh_materialized_views()",
    "refresh_flight_summary_sessions(bigint[], boolean)",
]


def role_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 AS x FROM pg_roles WHERE rolname = %s", (name,)).fetchone() is not None


@pytest.fixture()
def as_collector(conn):
    if not role_exists(conn, "collector"):
        pytest.skip("role collector not created (apply db/roles.sql)")
    conn.execute("SET ROLE collector")
    try:
        yield conn
    finally:
        conn.execute("RESET ROLE")


@pytest.fixture()
def as_backup(conn):
    if not role_exists(conn, "backup_ro"):
        pytest.skip("role backup_ro not created (apply db/roles.sql)")
    conn.execute("SET ROLE backup_ro")
    try:
        yield conn
    finally:
        conn.execute("RESET ROLE")


def test_roles_are_unprivileged(conn) -> None:
    for name in ("collector", "backup_ro"):
        if not role_exists(conn, name):
            pytest.skip(f"role {name} not created")
        row = conn.execute("SELECT rolsuper, rolcreaterole, rolcreatedb, rolcanlogin FROM pg_roles "
                           "WHERE rolname = %s", (name,)).fetchone()
        assert row["rolcanlogin"] and not (row["rolsuper"] or row["rolcreaterole"] or row["rolcreatedb"])


def test_grafana_ro_limits(conn) -> None:
    if not role_exists(conn, "grafana_ro"):
        pytest.skip("role grafana_ro not created")
    row = conn.execute("SELECT rolconnlimit, rolconfig FROM pg_roles WHERE rolname = 'grafana_ro'").fetchone()
    assert row["rolconnlimit"] == 10 and "work_mem=16MB" in row["rolconfig"]


def test_grafana_ro_timezone_is_pinned_to_utc(conn) -> None:
    """T27 item 4: `ALTER ROLE grafana_ro SET timezone = 'UTC'` is stored in pg_db_role_setting and applies to a
    fresh session of the role. Skips when the role is absent (db/roles.sql not applied); CI applies roles.sql."""
    if not role_exists(conn, "grafana_ro"):
        pytest.skip("role grafana_ro not created (apply db/roles.sql)")
    row = conn.execute("SELECT rolconfig FROM pg_roles WHERE rolname = 'grafana_ro'").fetchone()
    # Postgres stores the canonical GUC name ("TimeZone=UTC"), so compare case-insensitively.
    assert "timezone=utc" in [c.lower() for c in row["rolconfig"]]
    # Same catalog the task names: the setting is a role-level default (setdatabase = 0), not per-database.
    n = conn.execute("SELECT count(*) AS n FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole "
                     "WHERE r.rolname = 'grafana_ro' AND s.setdatabase = 0 "
                     "AND 'timezone=utc' = ANY (SELECT lower(c) FROM unnest(s.setconfig) AS c)").fetchone()["n"]
    assert n == 1


def test_collector_can_write_data_and_run_maintenance(as_collector) -> None:
    c = as_collector
    c.execute("SELECT ensure_pilot_position_partitions(2, DATE '2026-10-07')")
    sid = c.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
        "VALUES (9400001,'COL1','2026-10-07 00:00+00','2026-10-07 00:00+00','2026-10-07 00:01+00','YSSY','YMML') "
        "RETURNING id").fetchone()["id"]
    c.execute("INSERT INTO pilot_position (observed_at, session_id, gs_kt) VALUES ('2026-10-07 00:00+00', %s, 250)", (sid,))
    c.execute("INSERT INTO network_snapshot VALUES (now(),1,1,1,1)")
    c.execute("INSERT INTO app_setting (key, value) VALUES ('x_test','1') ON CONFLICT (key) DO UPDATE SET value = '2'")
    c.execute("DELETE FROM poll_log WHERE polled_at < now() - interval '400 days'")
    assert c.execute("SELECT refresh_flight_summary('100 years') AS n").fetchone()["n"] >= 0
    c.execute("SELECT refresh_materialized_views()")
    c.execute("SELECT drop_old_pilot_position_partitions(90, DATE '2026-10-07')")


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE collector_probe (x int)",
        "DROP TABLE airline",
        "ALTER TABLE pilot_session ADD COLUMN y int",
        "TRUNCATE pilot_session CASCADE",
        "SELECT * FROM flight_summary",
        "SELECT * FROM flight",
        "INSERT INTO airline (prefix, name) VALUES ('ZZZ','x')",
        "DROP TABLE pilot_position_20261007",
        "CREATE TABLE pilot_position_20991231 PARTITION OF pilot_position "
        "FOR VALUES FROM ('2099-12-31') TO ('2100-01-01')",
        "SELECT refresh_flight_summary_sessions(ARRAY[1]::bigint[])",
    ],
    ids=["create", "drop", "alter", "truncate", "read-summary", "read-view", "write-lookup", "drop-partition",
         "create-partition", "internal-helper"],
)
def test_collector_has_no_ddl_or_extra_access(conn, sql: str) -> None:
    if not role_exists(conn, "collector"):
        pytest.skip("role collector not created")
    conn.execute("SELECT ensure_pilot_position_partitions(1, DATE '2026-10-07')")
    conn.execute("SET ROLE collector")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(sql)
    finally:
        conn.execute("RESET ROLE")


def test_maintenance_functions_are_not_public(conn) -> None:
    for fn in MAINT_FUNCS:
        acl = conn.execute("SELECT proacl::text AS a FROM pg_proc WHERE oid = %s::regprocedure", (fn,)).fetchone()["a"]
        # NULL = default privileges (EXECUTE for PUBLIC); a PUBLIC grant shows as "=X/owner"
        assert acl is not None and "{=X" not in acl and ",=X" not in acl, fn
    for fn in ("ensure_pilot_position_partitions", "drop_old_pilot_position_partitions",
               "refresh_flight_summary", "refresh_materialized_views"):
        row = conn.execute("SELECT prosecdef, proconfig FROM pg_proc WHERE proname = %s", (fn,)).fetchone()
        assert row["prosecdef"] is True and "search_path=public" in row["proconfig"]


def test_backup_role_reads_everything_writes_nothing(as_backup) -> None:
    c = as_backup
    for table in ("pilot_session", "pilot_position", "flight_summary", "poll_log"):
        c.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchall()
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("INSERT INTO app_setting (key, value) VALUES ('bk','1')")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("CREATE TABLE backup_probe (x int)")


def test_online_now_is_empty_when_the_newest_snapshot_is_stale(conn) -> None:
    conn.execute("INSERT INTO network_snapshot VALUES (now() - interval '10 minutes',1,1,1,1)")
    conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
        "VALUES (9400002,'STALE', date_trunc('second', now() - interval '1 hour'), now() - interval '1 hour', now() - interval '10 minutes')")
    assert conn.execute("SELECT count(*) AS n FROM online_now").fetchone()["n"] == 0
    conn.execute("INSERT INTO network_snapshot VALUES (now() - interval '10 seconds',1,1,1,1)")
    conn.execute("UPDATE pilot_session SET last_seen = now() - interval '10 seconds'")
    assert conn.execute("SELECT count(*) AS n FROM online_now").fetchone()["n"] == 1


def test_movements_comment_documents_semantics(conn) -> None:
    c = conn.execute("SELECT obj_description('movements'::regclass, 'pg_class') AS c").fetchone()["c"]
    assert c and "never became airborne" in c
