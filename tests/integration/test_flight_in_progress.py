"""Issue #38 / T46: `flight.is_in_progress` tells flights still connected from completed ones.

In progress means a session of the (merged) flight is online now, by the online_now definition: seen within the last
two snapshots of the newest snapshot, which must be recent. The existing columns are unchanged (last column added).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from test_flight_summary import refresh
from test_flight_view import make_session, run

pytestmark = pytest.mark.integration

CID = 9700000


def set_up_clock(conn) -> datetime:
    """30 s polling and a newest snapshot 5 s ago; returns the snapshot time."""
    conn.execute("INSERT INTO app_setting (key, value) VALUES ('poll_interval_seconds', '30') "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    t = datetime.now(timezone.utc) - timedelta(seconds=5)
    conn.execute("INSERT INTO network_snapshot VALUES (%s,1,1,1,1)", (t,))
    return t


def flag(conn, cid: int) -> list[bool]:
    refresh(conn)
    rows = conn.execute("SELECT is_in_progress FROM flight WHERE cid = %s ORDER BY start_time", (cid,)).fetchall()
    return [r["is_in_progress"] for r in rows]


def session_ending(t: datetime, age_s: int, n: int = 40, gs: int | None = 250):
    """n samples 15 s apart whose last one is age_s seconds before t."""
    return run(n, start=t - timedelta(seconds=age_s + 15 * (n - 1)), gs=gs)


def test_still_connected_flight_is_in_progress(conn) -> None:
    t = set_up_clock(conn)
    make_session(conn, CID + 1, session_ending(t, 0))
    assert flag(conn, CID + 1) == [True]


def test_flight_that_ended_is_not_in_progress(conn) -> None:
    t = set_up_clock(conn)
    make_session(conn, CID + 2, session_ending(t, 120))     # last seen 2 minutes before the newest snapshot
    assert flag(conn, CID + 2) == [False]


def test_window_is_two_polls(conn) -> None:
    t = set_up_clock(conn)
    make_session(conn, CID + 3, session_ending(t, 59))
    make_session(conn, CID + 4, session_ending(t, 61))
    assert flag(conn, CID + 3) == [True]
    assert flag(conn, CID + 4) == [False]


def test_merged_reconnect_flight_is_in_progress_when_the_later_session_is_online(conn) -> None:
    t = set_up_clock(conn)
    first = session_ending(t, 600, n=40)
    later = run(20, start=t - timedelta(seconds=15 * 19 + 5))
    make_session(conn, CID + 5, first)
    make_session(conn, CID + 5, later)
    assert flag(conn, CID + 5) == [True]               # one merged flight, its newest session is online


def test_nothing_is_in_progress_when_the_collector_is_stale(conn) -> None:
    conn.execute("INSERT INTO app_setting (key, value) VALUES ('poll_interval_seconds', '30') "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    t = datetime.now(timezone.utc) - timedelta(minutes=30)
    conn.execute("INSERT INTO network_snapshot VALUES (%s,1,1,1,1)", (t,))
    make_session(conn, CID + 6, session_ending(t, 0))
    assert flag(conn, CID + 6) == [False]


def test_flag_is_the_last_column_and_the_others_are_unchanged(conn) -> None:
    t = set_up_clock(conn)
    make_session(conn, CID + 7, session_ending(t, 0))
    refresh(conn)
    cols = list(conn.execute("SELECT * FROM flight WHERE cid = %s", (CID + 7,)).fetchone())
    assert cols[-1] == "is_in_progress"
    assert cols[:3] == ["session_id", "gap_segment", "span_part"] and cols[-2] == "touches_home_region"
    assert len(cols) == 32
