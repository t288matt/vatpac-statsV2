"""Issue #89 (#138): QA sign-off. Pins the behaviour of design gaps that no earlier test covered.

Each test names the gap number from the QA scenario list.
Only behaviour judged acceptable is pinned here. No product code is touched.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from drop_gate_helpers import unreconciled
from test_flight_writer import BASE, HOUR, MIN, extend, flown, last_seen, mk, records, snap, times, write

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration


def test_gap1_the_test_setup_airborne_cap_is_40_seconds_and_a_missed_poll_counts_that_cap(conn: Any) -> None:
    """40 s is only the value the test setup (conftest TEST_GAP_CAP) writes where the collector would; live the collector sets 75 s and a fresh database has no row (issues 200, 238). With 40, a 60 s gap at 250 kt counts 40 s."""
    assert conn.execute("SELECT value FROM app_setting WHERE key = 'airborne_gap_cap_seconds'").fetchone()["value"] == "40"
    ts = [BASE, BASE + MIN, BASE + 2 * MIN]                          # two 60 s gaps (a missed 30 s poll)
    mk(conn, 800, ts)
    snap(conn, ts[-1] + 9 * HOUR)
    assert write(conn) == 1
    (r,) = records(conn, 800)
    assert float(r["airborne_minutes"]) == pytest.approx(80 / 60, abs=0.01)   # two 60 s gaps, each capped at 40 s; the last sample counts 0


def test_gap5_a_stray_position_beyond_last_seen_is_left_and_keeps_its_day_unreconciled(conn: Any) -> None:
    ts = times(BASE, 20)
    sid = mk(conn, 801, ts)
    stray = ts[-1] + timedelta(seconds=30)
    conn.execute("INSERT INTO pilot_position (observed_at, session_id, gs_kt) VALUES (%s, %s, 100)", (stray, sid))
    snap(conn, ts[-1] + 9 * HOUR)
    assert write(conn) == 1
    (r,) = records(conn, 801)
    assert r["sample_count"] == 20 and flown(conn, sid) == ts[-1]
    snap(conn, ts[-1] + 30 * HOUR)
    assert write(conn) == 0                                          # the session is not picked again: last_seen did not rise
    assert flown(conn, sid) == ts[-1]
    assert [s for s, _ in unreconciled(conn, BASE, BASE + 24 * HOUR)] == [sid]   # fail safe: the gate keeps the day, it does not lose data


def test_gap8_a_session_whose_last_seen_rose_without_new_positions_is_marked_and_makes_no_second_flight(conn: Any) -> None:
    ts = times(BASE, 20)
    sid = mk(conn, 802, ts)
    snap(conn, ts[-1] + 9 * HOUR)
    assert write(conn) == 1
    new_seen = ts[-1] + 10 * HOUR
    conn.execute("UPDATE pilot_session SET last_seen = %s WHERE id = %s", (new_seen, sid))   # seen again, but no position stored
    snap(conn, new_seen + 9 * HOUR)
    assert write(conn) == 0
    assert flown(conn, sid) == new_seen and len(records(conn, 802)) == 1
    assert write(conn) == 0                                                         # a second call changes nothing


@pytest.mark.parametrize("cap", [0, -5])
def test_gap9_a_cap_below_one_writes_nothing_and_touches_nothing(conn: Any, cap: int) -> None:
    sid = mk(conn, 803, times(BASE, 20))
    snap(conn, BASE + 20 * HOUR)
    assert write(conn, cap) == 0 and records(conn) == [] and flown(conn, sid) is None


def test_gap9_a_null_cap_returns_zero_and_writes_nothing(conn: Any) -> None:
    sid = mk(conn, 804, times(BASE, 20))
    snap(conn, BASE + 20 * HOUR)
    assert conn.execute("SELECT write_finished_flights(NULL) AS n").fetchone()["n"] == 0
    assert records(conn) == [] and flown(conn, sid) is None


def test_gap15b_the_cap_counts_pilots_examined_in_a_round_so_idle_pilots_let_one_call_write_more_than_the_cap(conn: Any) -> None:
    """Cap 2, one conflicting pilot (picked first, oldest) and three quiet ones: round 1 writes one, round 2 writes two more.
    A call can therefore write up to ten rounds of the cap, never more; the work of one call stays bounded."""
    ts = times(BASE, 20)
    a = mk(conn, 810, ts)
    for i, cid in enumerate((811, 812, 813)):
        mk(conn, cid, times(BASE + (i + 1) * HOUR, 20), callsign=f"VOZ{i}", logon=BASE + (i + 1) * HOUR)
    conn.execute("INSERT INTO flight_summary (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, "
                 "logon_time, sample_count) VALUES (%s, %s, %s, 810, 'QFA1', 'YSSY', 'YMML', ARRAY[%s::bigint], %s, 20)",
                 (a, ts[0], ts[-1], a, ts[0]))
    snap(conn, BASE + 40 * HOUR)
    n = write(conn, 2)
    assert n == 3 and n <= 10 * 2
    assert flown(conn, a) is None                                    # the conflict pilot is still unmarked


def test_gap15d_a_session_arriving_late_within_120_minutes_of_a_written_flight_is_a_new_flight_and_the_written_one_is_left(conn: Any) -> None:
    """Written rows are never changed: a session with a logon inside the 120 minute window of an already written flight
    (only possible if its data arrived after the flight was written) starts its own flight."""
    t1 = times(BASE, 20)
    first = mk(conn, 820, t1)
    snap(conn, t1[-1] + 9 * HOUR)
    assert write(conn) == 1
    (before,) = records(conn, 820)
    t2 = times(t1[-1] + 30 * MIN, 20)
    second = mk(conn, 820, t2, logon=t2[0])
    snap(conn, t2[-1] + 9 * HOUR)
    assert write(conn) == 1
    rows = records(conn, 820)
    assert len(rows) == 2 and rows[0] == before and rows[0]["session_ids"] == [first] and rows[1]["session_ids"] == [second]


def test_gap11_sessions_with_different_callsigns_overlapping_in_time_never_join(conn: Any) -> None:
    mk(conn, 830, times(BASE, 60), callsign="QFA1")
    mk(conn, 830, times(BASE + 20 * MIN, 60), callsign="QFA2", logon=BASE + 20 * MIN)
    snap(conn, BASE + 20 * HOUR)
    assert write(conn) == 2
    assert [len(r["session_ids"]) for r in records(conn, 830)] == [1, 1]


def test_gap6_a_null_remarks_in_the_last_session_wins_over_earlier_text_but_route_keeps_the_latest_non_null(conn: Any) -> None:
    t1, t2 = times(BASE, 20), times(BASE + 40 * MIN, 20)
    mk(conn, 840, t1, remarks="RMK/EARLY", route="DCT")
    mk(conn, 840, t2, logon=t2[0], remarks=None, route=None)
    snap(conn, BASE + 20 * HOUR)
    assert write(conn) == 1
    (r,) = records(conn, 840)
    assert r["remarks"] is None and r["route"] == "DCT"
