"""Issue #89 phase 1 (#137): flight_record against today's `flight` view (comparison A of design section 5.2, in miniature).

Where the old rule and the new rule agree, the rows must be identical on every shared column; where they differ, they must
differ in exactly the ways section 5.2 lists as expected (8 hour parts of one session, a join decided by logon_time). Anything
else would be a bug in the move, which is what ops/compare_flights_89.sql looks for on the real data.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from test_flight_writer import BASE, HOUR, MIN, mk, snap, times, write

pytestmark = pytest.mark.integration

SHARED = ("session_id, cid, callsign, departure, arrival, logon_time, start_time, end_time, sample_count, airborne_minutes, "
          "max_gs_kt, max_alt_ft, name, home_icao, server, aircraft_icao, aircraft_faa, flight_rules, route, remarks, "
          "planned_alt, deptime, pilot_rating, military_rating")


def both(conn: Any, cids: list[int]) -> tuple[list[dict], list[dict]]:
    """Run the pieces refresh (which also runs the writer) and return (old view rows, new table rows) for the pilots."""
    conn.execute("SELECT refresh_flight_summary(NULL)")
    old = conn.execute(f"SELECT {SHARED} FROM flight WHERE cid = ANY(%s) AND NOT is_in_progress ORDER BY cid, start_time", (cids,)).fetchall()  # noqa: S608
    new = conn.execute(f"SELECT {SHARED} FROM flight_record WHERE cid = ANY(%s) ORDER BY cid, start_time", (cids,)).fetchall()  # noqa: S608
    return old, new


def test_ordinary_flights_and_a_reconnect_join_are_identical_in_old_and_new(conn: Any) -> None:
    ts = times(BASE, 40, 15)
    mk(conn, 200, ts, gs=[0] * 5 + [250] * 35, name="Pat", route="DCT", deptime="0900", aircraft_icao="B738", pilot_rating=1)
    a = times(BASE, 30)
    mk(conn, 201, a)
    mk(conn, 201, times(a[-1] + 30 * MIN, 20), logon=a[-1] + 29 * MIN, route="XYZ", deptime="1000", aircraft_icao="A320")
    mk(conn, 202, times(BASE + HOUR, 15, 30), callsign="JST9", dep="YBBN", arr="YPPH", gs=0)
    snap(conn, BASE + 24 * HOUR)
    old, new = both(conn, [200, 201, 202])
    assert len(new) == 3 and old == new


def test_expected_difference_1_a_session_longer_than_8_hours_is_two_old_rows_and_one_new(conn: Any) -> None:
    ts = times(BASE, 61, 600)                                   # 10 hours
    mk(conn, 210, ts)
    snap(conn, ts[-1] + 8 * HOUR)
    conn.execute("SELECT refresh_flight_summary(NULL)")
    old = conn.execute("SELECT * FROM flight WHERE cid = 210 ORDER BY start_time").fetchall()
    parts = conn.execute("SELECT span_part, sample_count FROM flight_summary WHERE cid = 210 ORDER BY span_part").fetchall()
    new = conn.execute("SELECT * FROM flight_record WHERE cid = 210").fetchall()
    assert len(parts) == 2 and parts[1]["span_part"] > 0                 # how the owner recognises it: span_part > 0 in the pieces
    assert len(old) == 2 and len(new) == 1
    assert sum(r["sample_count"] for r in old) == new[0]["sample_count"] == 61


def test_expected_difference_2_a_120_minute_silence_inside_a_session_is_two_old_rows_and_one_new(conn: Any) -> None:
    a = times(BASE, 20, 15)
    b = times(a[-1] + timedelta(hours=3), 20, 15)
    mk(conn, 216, a + b)
    snap(conn, b[-1] + 8 * HOUR)
    conn.execute("SELECT refresh_flight_summary(NULL)")
    assert conn.execute("SELECT count(*) AS n FROM flight WHERE cid = 216").fetchone()["n"] == 2
    assert conn.execute("SELECT max(gap_segment) AS g FROM flight_summary WHERE cid = 216").fetchone()["g"] == 2
    assert conn.execute("SELECT count(*) AS n FROM flight_record WHERE cid = 216").fetchone()["n"] == 1


def test_expected_difference_3_a_join_decided_by_logon_time_is_two_old_rows_and_one_new(conn: Any) -> None:
    first = times(BASE, 30)
    logon = first[-1] + 100 * MIN                               # within 120 minutes of the previous end ...
    mk(conn, 211, first)
    mk(conn, 211, times(first[-1] + 150 * MIN, 10), logon=logon)   # ... but the first position is 150 minutes after it
    snap(conn, BASE + 24 * HOUR)
    conn.execute("SELECT refresh_flight_summary(NULL)")
    assert conn.execute("SELECT count(*) AS n FROM flight WHERE cid = 211").fetchone()["n"] == 2
    assert conn.execute("SELECT count(*) AS n FROM flight_record WHERE cid = 211").fetchone()["n"] == 1


def test_expected_difference_4_new_plan_fields_are_frozen_the_old_view_reads_them_live(conn: Any) -> None:
    mk(conn, 212, times(BASE, 20), route="DCT")
    snap(conn, BASE + 24 * HOUR)
    conn.execute("SELECT refresh_flight_summary(NULL)")
    conn.execute("UPDATE pilot_session SET route = 'CHANGED LATER' WHERE cid = 212")
    assert conn.execute("SELECT route FROM flight WHERE cid = 212").fetchone()["route"] == "CHANGED LATER"
    assert conn.execute("SELECT route FROM flight_record WHERE cid = 212").fetchone()["route"] == "DCT"


def test_expected_difference_5_airborne_is_rounded_once_not_per_piece(conn: Any) -> None:
    ts = times(BASE, 61, 600)                                   # two old pieces, each rounded to 2 decimals
    mk(conn, 213, ts)
    snap(conn, ts[-1] + 8 * HOUR)
    conn.execute("SELECT refresh_flight_summary(NULL)")
    old = conn.execute("SELECT sum(airborne_minutes) AS m FROM flight WHERE cid = 213").fetchone()["m"]
    new = conn.execute("SELECT airborne_minutes AS m FROM flight_record WHERE cid = 213").fetchone()["m"]
    assert abs(float(old) - float(new)) <= 0.011                             # at most 0.01 per extra old piece


def test_the_independent_count_new_sample_count_equals_the_positions_of_gate_passing_sessions(conn: Any) -> None:
    """The check section 5.2 adds because the writer was built from the same SQL as the view."""
    mk(conn, 214, times(BASE, 25))
    mk(conn, 215, times(BASE, 17), dep=None, arr=None)
    snap(conn, BASE + 24 * HOUR)
    assert write(conn) == 1
    total = conn.execute("SELECT sum(sample_count) AS n FROM flight_record").fetchone()["n"]
    positions = conn.execute(
        "SELECT count(*) AS n FROM pilot_position p JOIN pilot_session s ON s.id = p.session_id "
        "WHERE p.observed_at <= s.flown_upto AND NULLIF(btrim(s.departure, E' \\t\\r\\n'), '') IS NOT NULL "
        "AND NULLIF(btrim(s.arrival, E' \\t\\r\\n'), '') IS NOT NULL").fetchone()["n"]
    assert total == positions == 25
