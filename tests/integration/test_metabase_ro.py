"""Issue 288: `metabase_ro` is a copy of `grafana_ro` (db/roles.sql): SELECT on views and lookups only, nothing else.

Uses VATPAC_TEST_METABASE_RO_DSN if set; otherwise SET ROLE metabase_ro on the superuser connection.
"""
from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration

RAW = ["pilot_session", "pilot_position", "controller_session", "controller_rating_event",
       "controller_frequency_event", "network_snapshot", "poll_log", "controller_unlisted_sighting",
       "reference_data_change", "flight_summary", "app_setting"]
LOOKUPS = ["controller_rating", "facility", "pilot_rating", "airline", "aircraft_type", "region_map"]


def _views(conn) -> list[str]:
    return [r["n"] for r in conn.execute(
        "SELECT viewname AS n FROM pg_views WHERE schemaname = 'public' "
        "UNION SELECT matviewname FROM pg_matviews WHERE schemaname = 'public' ORDER BY 1").fetchall()]


@pytest.fixture()
def ro(conn):
    dsn = os.environ.get("VATPAC_TEST_METABASE_RO_DSN")
    if dsn:
        with psycopg.connect(dsn, autocommit=True) as c:
            yield c
    else:
        conn.execute("SET ROLE metabase_ro")
        try:
            yield conn
        finally:
            conn.execute("RESET ROLE")


def test_can_select_every_view_and_lookup(conn, ro) -> None:
    names = _views(conn)
    assert len(names) >= 20, names
    for name in names + LOOKUPS:
        ro.execute(f"SELECT * FROM {name} LIMIT 1").fetchall()


@pytest.mark.parametrize("name", RAW)
def test_cannot_select_raw_tables(ro, name: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro.execute(f"SELECT 1 FROM {name} LIMIT 1")


@pytest.mark.parametrize(
    "sql",
    ["INSERT INTO airline (prefix, name) VALUES ('ZZZ', 'Test')", "UPDATE airline SET name = 'x'", "DELETE FROM airline",
     "TRUNCATE poll_log", "CREATE TABLE metabase_ro_probe (x int)", "DROP TABLE airline", "ALTER TABLE airline ADD COLUMN y int"],
    ids=["insert", "update", "delete", "truncate", "create", "drop", "alter"],
)
def test_cannot_write_or_ddl(ro, sql: str) -> None:
    with pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
        ro.execute(sql)
    ro.execute("SET default_transaction_read_only = off")  # a session can switch the default off: the privilege layer must hold
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro.execute(sql)


def test_role_settings_match_grafana_ro(conn) -> None:
    rows = {r["rolname"]: r for r in conn.execute(
        "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin, rolconnlimit, rolconfig FROM pg_roles "
        "WHERE rolname IN ('grafana_ro', 'metabase_ro')").fetchall()}
    assert set(rows) == {"grafana_ro", "metabase_ro"}, "role metabase_ro missing (created by the migrate job)"
    m, g = rows["metabase_ro"], rows["grafana_ro"]
    assert m["rolcanlogin"] and not (m["rolsuper"] or m["rolcreaterole"] or m["rolcreatedb"])
    assert m["rolconnlimit"] == g["rolconnlimit"] == 10
    assert sorted(m["rolconfig"]) == sorted(g["rolconfig"])
    cfg = [c.lower() for c in m["rolconfig"]]
    assert "statement_timeout=30s" in cfg and "default_transaction_read_only=on" in cfg


def test_privilege_list_equals_grafana_ro(conn) -> None:
    """Every relation, column and function privilege that grafana_ro has, metabase_ro has, and the reverse."""
    def privs(role: str) -> list[tuple]:
        rel = conn.execute(
            "SELECT c.relname, p FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN unnest(ARRAY['SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER']) p "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r','p','v','m','S') AND has_table_privilege(%s, c.oid, p) ORDER BY 1, 2", (role,))
        fn = conn.execute("SELECT p.oid::regprocedure::text AS relname, 'EXECUTE' AS p FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                          "WHERE n.nspname = 'public' AND has_function_privilege(%s, p.oid, 'EXECUTE') ORDER BY 1", (role,))
        sch = conn.execute("SELECT 'schema' AS relname, p FROM unnest(ARRAY['USAGE','CREATE']) p "
                           "WHERE has_schema_privilege(%s, 'public', p) ORDER BY 2", (role,))
        return [tuple(r.values()) for q in (rel, fn, sch) for r in q.fetchall()]
    g, m = privs("grafana_ro"), privs("metabase_ro")
    assert m == g and len(g) > 20
    assert all(p == "SELECT" for name, p in m if name != "schema" and "(" not in name)
