"""SPEC s6: `grafana_ro` has SELECT on views and lookups, nothing else. UNEXECUTED.

Uses VATPAC_TEST_GRAFANA_RO_DSN if set; otherwise SET ROLE grafana_ro on the superuser connection.
"""
from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration

VIEWS = ["flight", "controller_session_merged", "controller_activity_daily", "movements", "cohorts",
         "online_now", "data_freshness"]
LOOKUPS = ["controller_rating", "facility", "pilot_rating", "airline", "aircraft_type", "region_map"]
RAW = ["pilot_session", "pilot_position", "controller_session", "controller_rating_event",
       "controller_frequency_event", "network_snapshot", "poll_log", "controller_unlisted_sighting",
       "reference_data_change"]


@pytest.fixture()
def ro(conn):
    ro_dsn = os.environ.get("VATPAC_TEST_GRAFANA_RO_DSN")
    if ro_dsn:
        with psycopg.connect(ro_dsn, autocommit=True) as c:
            yield c
    else:
        conn.execute("SET ROLE grafana_ro")
        try:
            yield conn
        finally:
            conn.execute("RESET ROLE")


@pytest.mark.parametrize("name", VIEWS + LOOKUPS)
def test_can_select_views_and_lookups(ro, name: str) -> None:
    ro.execute(f"SELECT * FROM {name} LIMIT 1").fetchall()


@pytest.mark.parametrize("name", RAW)
def test_cannot_select_raw_tables(ro, name: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro.execute(f"SELECT 1 FROM {name} LIMIT 1")


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO airline (prefix, name) VALUES ('ZZZ', 'Test')",
        "UPDATE airline SET name = 'x'",
        "DELETE FROM airline",
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
        "VALUES (1,'X',now(),now(),now())",
        "TRUNCATE poll_log",
        "CREATE TABLE grafana_ro_probe (x int)",
        "DROP TABLE airline",
        "ALTER TABLE airline ADD COLUMN y int",
    ],
    ids=["insert-lookup", "update-lookup", "delete-lookup", "insert-raw", "truncate", "create", "drop", "alter"],
)
def test_cannot_write_or_ddl(ro, sql: str) -> None:
    # Two layers: the role defaults to read-only transactions, and (the real guarantee) it lacks the
    # privileges. A role can switch its own default off, so prove the privilege layer too.
    with pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
        ro.execute(sql)
    ro.execute("SET default_transaction_read_only = off")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro.execute(sql)


def test_role_is_not_superuser_and_cannot_create_roles(conn) -> None:
    row = conn.execute("SELECT rolsuper, rolcreaterole, rolcreatedb, rolcanlogin FROM pg_roles "
                       "WHERE rolname = 'grafana_ro'").fetchone()
    assert row is not None, "role grafana_ro missing (created by the migrate job)"
    assert not row["rolsuper"] and not row["rolcreaterole"] and not row["rolcreatedb"]
