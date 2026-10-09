"""ADR-024: the 'seen within the last two snapshots' windows are 2 x POLL_INTERVAL_SECONDS (published to app_setting)."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

BASE = 9600000


def set_interval(conn, seconds: int) -> None:
    conn.execute("INSERT INTO app_setting (key, value) VALUES ('poll_interval_seconds', %s) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(seconds),))


def snapshot_now(conn) -> None:
    conn.execute("INSERT INTO network_snapshot VALUES (now() - interval '5 seconds',1,1,1,1)")


def pilot(conn, cid: int, age_s: int) -> None:
    conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
        "VALUES (%s, %s, date_trunc('second', now() - interval '1 hour'), now() - interval '1 hour', "
        "now() - interval '5 seconds' - make_interval(secs => %s))", (cid, f"P{cid}", age_s))


def online(conn, cid: int) -> bool:
    return conn.execute("SELECT count(*) AS n FROM online_now WHERE cid = %s", (cid,)).fetchone()["n"] == 1


@pytest.mark.parametrize("interval", [15, 30, 45])
def test_online_now_window_is_twice_the_poll_interval(conn, interval: int) -> None:
    set_interval(conn, interval)
    snapshot_now(conn)
    w = 2 * interval
    ages = (0, interval, w - 1, w, w + 1)
    for k, age in enumerate(ages):
        pilot(conn, BASE + interval * 10 + k, age)
    got = [online(conn, BASE + interval * 10 + k) for k in range(len(ages))]
    assert got == [True, True, True, False, False]


@pytest.mark.parametrize("interval", [15, 30])
def test_controller_is_open_window_is_twice_the_poll_interval(conn, interval: int) -> None:
    set_interval(conn, interval)
    snapshot_now(conn)
    w = 2 * interval
    for k, age in enumerate((interval, w - 1, w + 1)):
        conn.execute(
            "INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen) "
            "VALUES (%s, 'ML_TWR', date_trunc('second', now() - interval '1 hour') + make_interval(secs => %s), "
            "now() - interval '1 hour', now() - interval '5 seconds' - make_interval(secs => %s))",
            (BASE + 500 + interval * 10 + k, k, age))
    rows = conn.execute("SELECT is_open FROM controller_session_merged WHERE cid BETWEEN %s AND %s ORDER BY cid",
                        (BASE + 500 + interval * 10, BASE + 500 + interval * 10 + 2)).fetchall()
    assert [r["is_open"] for r in rows] == [True, True, False]


def test_without_the_setting_nobody_is_online(conn) -> None:
    conn.execute("DELETE FROM app_setting WHERE key = 'poll_interval_seconds'")
    snapshot_now(conn)
    pilot(conn, BASE + 900, 0)
    assert not online(conn, BASE + 900)


def test_poll_cadence_view_and_collector_sync(conn) -> None:
    from types import SimpleNamespace
    from vatpac.maintenance import sync_settings
    sync_settings(conn, SimpleNamespace(home_icao_prefixes="Y", airborne_gap_cap_seconds=75, poll_interval_seconds=30.0))
    assert float(conn.execute("SELECT poll_interval_s FROM poll_cadence").fetchone()["poll_interval_s"]) == 30.0
