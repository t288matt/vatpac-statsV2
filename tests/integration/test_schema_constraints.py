"""SPEC s6 constraints. Run against a real PostgreSQL 16.4 on 07-10-2026 (all pass)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from helpers import partition_for

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)


def new_session(conn, cid=9100001, callsign="TEST1", logon=T0, first=T0, last=T0) -> int:
    return conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (cid, callsign, logon, first, last),
    ).fetchone()["id"]


def test_pilot_session_unique_key(conn) -> None:
    new_session(conn)
    with pytest.raises(psycopg.errors.UniqueViolation):
        new_session(conn)


def test_pilot_session_new_logon_time_is_new_session(conn) -> None:
    new_session(conn)
    later = T0 + timedelta(hours=1)
    new_session(conn, logon=later, first=later, last=later)
    assert conn.execute("SELECT count(*) AS n FROM pilot_session").fetchone()["n"] == 2


def test_pilot_session_last_seen_not_before_first_seen(conn) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        new_session(conn, first=T0, last=T0 - timedelta(seconds=1))


def test_pilot_session_last_seen_equal_first_seen_ok(conn) -> None:
    new_session(conn, first=T0, last=T0)


def test_pilot_session_requires_key_columns(conn) -> None:
    with pytest.raises(psycopg.errors.NotNullViolation):
        conn.execute(
            "INSERT INTO pilot_session (callsign, logon_time, first_seen, last_seen) "
            "VALUES ('X', now(), now(), now())"
        )


@pytest.mark.parametrize("hdg,ok", [(0, True), (360, True), (180, True), (361, False), (-1, False)])
def test_heading_range(conn, hdg: int, ok: bool) -> None:
    sid = new_session(conn)
    partition_for(conn, T0)

    def insert() -> None:
        conn.execute(
            "INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) "
            "VALUES (%s,%s,-33.9,151.2,1000,100,%s)",
            (T0, sid, hdg),
        )

    if ok:
        insert()
    else:
        with pytest.raises(psycopg.errors.CheckViolation):
            insert()


def test_position_primary_key(conn) -> None:
    sid = new_session(conn)
    partition_for(conn, T0)
    sql = (
        "INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) "
        "VALUES (%s,%s,-33.9,151.2,1000,100,90)"
    )
    conn.execute(sql, (T0, sid))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(sql, (T0, sid))


def test_position_requires_existing_session(conn) -> None:
    partition_for(conn, T0)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO pilot_position (observed_at, session_id, lat, lon) VALUES (%s, 999999, 0, 0)",
            (T0,),
        )


def test_position_without_partition_is_rejected(conn) -> None:
    sid = new_session(conn)
    far = datetime(2031, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(psycopg.errors.CheckViolation):  # "no partition of relation found for row"
        conn.execute("INSERT INTO pilot_position (observed_at, session_id) VALUES (%s,%s)", (far, sid))


def test_controller_session_unique_and_check(conn) -> None:
    ins = (
        "INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen) "
        "VALUES (9100002,'ML_APP',%s,%s,%s)"
    )
    conn.execute(ins, (T0, T0, T0))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(ins, (T0, T0, T0))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen) "
            "VALUES (9100003,'ML_APP',%s,%s,%s)",
            (T0, T0, T0 - timedelta(seconds=1)),
        )


def test_poll_log_status_check(conn) -> None:
    for status in ("ok", "skipped_unchanged", "error"):
        conn.execute("INSERT INTO poll_log (status) VALUES (%s)", (status,))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO poll_log (status) VALUES ('bogus')")


def test_network_snapshot_primary_key(conn) -> None:
    sql = ("INSERT INTO network_snapshot (observed_at, pilots_in_region, controllers_in_region, "
           "pilots_global, controllers_global) VALUES (%s, %s, 0, 0, 0)")
    conn.execute(sql, (T0, 1))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(sql, (T0, 2))


def test_expected_objects_exist(conn) -> None:
    rows = conn.execute(
        "SELECT table_name AS t FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    present = {r["t"] for r in rows}
    # materialized views are not in information_schema.tables
    present |= {r["m"] for r in conn.execute("SELECT matviewname AS m FROM pg_matviews").fetchall()}
    for name in (
        "pilot_session", "pilot_position", "controller_session", "controller_rating_event",
        "controller_frequency_event", "network_snapshot", "poll_log", "controller_unlisted_sighting",
        "reference_data_change", "controller_rating", "facility", "pilot_rating", "airline",
        "aircraft_type", "region_map", "flight", "controller_session_merged",
        "movements", "cohorts", "online_now", "data_freshness",
    ):
        assert name in present, name
