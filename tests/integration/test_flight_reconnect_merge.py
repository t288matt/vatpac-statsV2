"""Issue #35 / T41: the `flight` view merges the sessions of one flight the way v1 did.

v1 grouped rows by callsign + CID + departure + arrival and split only on a gap of more than 120 minutes, so a
reconnect with the same callsign and airports was one flight. Expected values are written out by hand.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from helpers import T0, make_session, refresh, run

pytestmark = pytest.mark.integration

CID = 9400000


def flights(conn, cid: int) -> list[dict]:
    refresh(conn)
    return conn.execute("SELECT * FROM flight WHERE cid = %s ORDER BY start_time", (cid,)).fetchall()


def after(samples, minutes: float, n: int, gs: int | None = 250):
    return run(n, start=samples[-1][0] + timedelta(minutes=minutes), gs=gs)


def test_reconnect_within_the_gap_is_one_flight(conn) -> None:
    a = run(40, gs=0)                      # parked 10 minutes, then the pilot reconnects
    b = after(a, 2, 80, gs=250)
    make_session(conn, CID + 1, a)
    sid_b = make_session(conn, CID + 1, b)
    rows = flights(conn, CID + 1)
    assert len(rows) == 1
    r = rows[0]
    assert r["start_time"] == a[0][0] and r["end_time"] == b[-1][0]
    assert r["sample_count"] == 120
    assert r["airborne_minutes"] == pytest.approx(79 * 15 / 60, abs=0.01)
    assert r["session_id"] != sid_b                       # identified by the first part
    assert r["logon_time"] == a[0][0].replace(microsecond=0)


def test_gap_boundary_is_120_minutes_inclusive(conn) -> None:
    a = run(10)
    make_session(conn, CID + 2, a)
    make_session(conn, CID + 2, after(a, 120, 10))          # next sample exactly 120:00 after the last
    assert len(flights(conn, CID + 2)) == 1
    c = run(10, start=T0 + timedelta(days=2))
    make_session(conn, CID + 3, c)
    make_session(conn, CID + 3, run(10, start=c[-1][0] + timedelta(minutes=120, seconds=1)))
    assert len(flights(conn, CID + 3)) == 2


def test_a_changed_plan_is_a_different_flight(conn) -> None:
    a = run(10)
    make_session(conn, CID + 4, a, dep="YSSY", arr="YMML")
    make_session(conn, CID + 4, after(a, 3, 10), dep="YSSY", arr="YBBN")
    rows = flights(conn, CID + 4)
    assert [(r["departure"], r["arrival"]) for r in rows] == [("YSSY", "YMML"), ("YSSY", "YBBN")]


def test_chain_of_three_and_a_break(conn) -> None:
    a = run(10)
    b = after(a, 30, 10)
    c = after(b, 60, 10)
    d = after(c, 125, 10)                                    # more than 120 minutes after c: new flight
    for s in (a, b, c, d):
        make_session(conn, CID + 5, s)
    rows = flights(conn, CID + 5)
    assert len(rows) == 2
    assert (rows[0]["start_time"], rows[0]["end_time"], rows[0]["sample_count"]) == (a[0][0], c[-1][0], 30)
    assert (rows[1]["start_time"], rows[1]["sample_count"]) == (d[0][0], 10)


def test_single_session_is_unchanged_and_other_pilots_do_not_merge(conn) -> None:
    make_session(conn, CID + 6, run(20))
    make_session(conn, CID + 7, run(20))                     # another pilot at the same time
    assert len(flights(conn, CID + 6)) == 1 and len(flights(conn, CID + 7)) == 1


def test_movements_count_a_reconnected_flight_once(conn) -> None:
    a = run(10, gs=0)
    make_session(conn, CID + 8, a)
    make_session(conn, CID + 8, after(a, 1, 10))
    refresh(conn)
    n = conn.execute("SELECT count(*) AS n FROM movements WHERE cid = %s", (CID + 8,)).fetchone()["n"]
    assert n == 2                                            # one departure and one arrival


def test_per_session_parts_stay_available(conn) -> None:
    a = run(10)
    make_session(conn, CID + 9, a)
    make_session(conn, CID + 9, after(a, 1, 10))
    refresh(conn)
    assert conn.execute("SELECT count(*) AS n FROM flight_part WHERE cid = %s", (CID + 9,)).fetchone()["n"] == 2
