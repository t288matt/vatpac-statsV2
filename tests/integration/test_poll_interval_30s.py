"""ADR-024: polling every 30 s. The 'seen within the last two snapshots' window is 60 s, the airborne gap cap is 75 s."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

BASE = 9600000


def snapshot_now(conn) -> None:
    conn.execute("INSERT INTO network_snapshot VALUES (now() - interval '5 seconds',1,1,1,1)")


def pilot(conn, cid: int, age_s: int) -> None:
    conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
        "VALUES (%s, %s, date_trunc('second', now() - interval '1 hour'), now() - interval '1 hour', now() - interval '5 seconds' - make_interval(secs => %s))",
        (cid, f"P{cid}", age_s))


def online(conn, cid: int) -> bool:
    return conn.execute("SELECT count(*) AS n FROM online_now WHERE cid = %s", (cid,)).fetchone()["n"] == 1


def test_online_now_window_is_60_seconds(conn) -> None:
    snapshot_now(conn)
    for k, age in enumerate((0, 30, 59, 60, 61)):
        pilot(conn, BASE + k, age)
    assert [online(conn, BASE + k) for k in range(5)] == [True, True, True, False, False]


def test_controller_is_open_window_is_60_seconds(conn) -> None:
    snapshot_now(conn)
    for k, age in enumerate((30, 59, 61)):
        conn.execute(
            "INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen) "
            "VALUES (%s, 'ML_TWR', date_trunc('second', now() - interval '1 hour') + make_interval(secs => %s), now() - interval '1 hour', now() - interval '5 seconds' - make_interval(secs => %s))",
            (BASE + 10 + k, k, age))
    rows = conn.execute("SELECT cid, is_open FROM controller_session_merged WHERE cid BETWEEN %s AND %s ORDER BY cid",
                        (BASE + 10, BASE + 12)).fetchall()
    assert [r["is_open"] for r in rows] == [True, True, False]


def test_airborne_gap_cap_setting_is_75() -> None:
    from vatpac.settings import Settings
    assert Settings.model_fields["airborne_gap_cap_seconds"].default == 75
    assert Settings.model_fields["poll_interval_seconds"].default == 30.0


def test_airborne_gap_cap_seeded_value(conn) -> None:
    assert conn.execute("SELECT value FROM app_setting WHERE key = 'airborne_gap_cap_seconds'").fetchone()["value"] == "75"
