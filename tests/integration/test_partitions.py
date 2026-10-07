"""SPEC s6 partition lifecycle. Run against a real PostgreSQL 16.4 on 07-10-2026 (all pass).

Part A (schema level) needs only SQL. Part B exercises vatpac.maintenance; SPEC s7 names no
functions for it, so those tests look for conventional names and skip with a clear reason.
Proposed contract (to add to SPEC s7): ensure_partitions(conn, today: date, days_ahead: int = 7) and
drop_old_partitions(conn, today: date, retention_days: int).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from helpers import partition_for

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)

SESSION_SQL = (
    "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
    "VALUES (%s,%s,%s,%s,%s) RETURNING id"
)


def partitions(conn) -> set[str]:
    rows = conn.execute(
        "SELECT c.relname AS p FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "WHERE i.inhparent = 'pilot_position'::regclass"
    ).fetchall()
    return {r["p"] for r in rows}


# ------------------------------------------------------------------ A: schema level
def test_partition_create_and_route(conn) -> None:
    name = partition_for(conn, NOW)
    assert name == "pilot_position_20261007" and name in partitions(conn)
    sid = conn.execute(SESSION_SQL, (9100001, "TEST1", NOW, NOW, NOW)).fetchone()["id"]
    conn.execute("INSERT INTO pilot_position (observed_at, session_id, lat, lon) VALUES (%s,%s,0,0)", (NOW, sid))
    assert conn.execute(f"SELECT count(*) AS n FROM {name}").fetchone()["n"] == 1


def test_partition_drop_removes_its_rows_only(conn) -> None:
    old = NOW - timedelta(days=100)
    p_old, p_new = partition_for(conn, old), partition_for(conn, NOW)
    sid = conn.execute(SESSION_SQL, (9100001, "TEST1", old, old, NOW)).fetchone()["id"]
    for t in (old, NOW):
        conn.execute("INSERT INTO pilot_position (observed_at, session_id) VALUES (%s,%s)", (t, sid))
    conn.execute(f"DROP TABLE {p_old}")
    assert p_old not in partitions(conn) and p_new in partitions(conn)
    assert conn.execute("SELECT count(*) AS n FROM pilot_position").fetchone()["n"] == 1


def test_create_partition_is_idempotent(conn) -> None:
    partition_for(conn, NOW)
    partition_for(conn, NOW)
    assert len([p for p in partitions(conn) if p.endswith("20261007")]) == 1


# ------------------------------------------------------------------ B: maintenance module
def maint(*names):
    mod = pytest.importorskip("vatpac.maintenance")
    for n in names:
        if hasattr(mod, n):
            return getattr(mod, n)
    pytest.skip(f"vatpac.maintenance has none of {names}; SPEC s7 does not name its public functions")


def test_maintenance_creates_today_plus_seven(conn) -> None:
    ensure = maint("ensure_partitions", "create_partitions")
    ensure(conn, date(2026, 10, 7), 7)
    want = {f"pilot_position_{date(2026, 10, 7) + timedelta(days=i):%Y%m%d}" for i in range(0, 8)}
    assert want <= partitions(conn)
    ensure(conn, date(2026, 10, 7), 7)  # second run: no error, no duplicates
    assert want <= partitions(conn)


def test_maintenance_drops_old_partitions_without_open_session(conn) -> None:
    drop = maint("drop_old_partitions", "drop_expired_partitions")
    old_day = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)  # 128 days old
    recent = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    p_old, p_recent = partition_for(conn, old_day), partition_for(conn, recent)
    drop(conn, date(2026, 10, 7), 90)
    assert p_old not in partitions(conn) and p_recent in partitions(conn)


def test_maintenance_keeps_old_partition_with_open_session(conn) -> None:
    drop = maint("drop_old_partitions", "drop_expired_partitions")
    old_day = datetime(2026, 6, 1, 12, tzinfo=timezone.utc)
    p_old = partition_for(conn, old_day)
    still_online = datetime(2026, 10, 7, 11, tzinfo=timezone.utc)  # session open: last_seen is recent
    sid = conn.execute(SESSION_SQL, (9100009, "TESTOPEN", old_day, old_day, still_online)).fetchone()["id"]
    conn.execute("INSERT INTO pilot_position (observed_at, session_id) VALUES (%s,%s)", (old_day, sid))
    drop(conn, date(2026, 10, 7), 90)
    assert p_old in partitions(conn)
