"""Issue #89 (#138): two DOCUMENTED differences from v1, found in the live v1 comparison of 13-10-2026. These tests pin them so nobody
"fixes" them by accident. Both follow from the plan being a SESSION attribute in v3 (docs/DESIGN_89_FINAL.md 1.4, "Storage" and
"Amended plan" rows): pilot_session holds one departure/arrival, the latest non-null, and plan_changed_at says when it last changed.
"""
from __future__ import annotations

from typing import Any

import pytest

from test_flight_writer import BASE, HOUR, mk, records, snap, times, write

pytestmark = pytest.mark.integration


def test_a_plan_changed_inside_one_connection_is_one_flight_under_the_final_plan(conn: Any) -> None:
    """v1 keys a flight summary on (callsign, cid, departure, arrival), so a pilot who amends the arrival halfway through one connection
    gets one v1 summary per plan (the key includes departure and arrival; seen live as the 'amended plan' duplicates in comparison B of 13-10-2026, docs/qa_89/README.md section 2f). v3 never splits inside a connection: the session holds
    the final plan, so there is ONE flight, with every position of the session, under the final plan."""
    ts = times(BASE, 90)                                                        # 90 minutes, one connection
    sid = mk(conn, 950, ts, dep="YSSY", arr="YBBN", plan_changed_at=ts[45])      # narrative only: the schema keeps just the final plan, so the test sets it and plan_changed_at; imagine YMML first, amended to YBBN halfway
    snap(conn, ts[-1] + 8 * HOUR)
    assert write(conn) == 1
    (r,) = records(conn, 950)
    assert (r["departure"], r["arrival"]) == ("YSSY", "YBBN")                    # the final plan; no YMML flight exists
    assert r["session_ids"] == [sid] and r["sample_count"] == 90                 # all positions of the session, before and after the change
    assert (r["start_time"], r["end_time"]) == (ts[0], ts[-1])


def test_positions_stored_before_the_plan_was_filed_stay_in_the_flight(conn: Any) -> None:
    """v1 drops rows that have no flight plan at ingest, so its flight starts when the plan appears. v3 stores every in-region position
    and the plan is a session attribute, so the positions from before the plan was filed are in the flight: start_time and sample_count
    include them (about 7% of sessions get their plan more than 5 minutes after connecting, DESIGN_89_FINAL 1.7)."""
    ts = times(BASE, 60)
    sid = mk(conn, 951, ts, dep=None, arr=None)                                  # connected and flying with no plan at all ...
    conn.execute("UPDATE pilot_session SET departure = 'YSSY', arrival = 'YMML', plan_changed_at = %s WHERE id = %s",
                 (ts[20], sid))                                                  # ... the plan arrives 20 minutes in
    snap(conn, ts[-1] + 8 * HOUR)
    assert write(conn) == 1
    (r,) = records(conn, 951)
    assert r["start_time"] == ts[0] and r["start_time"] < ts[20]                 # starts at the first stored position, not at the plan
    assert r["sample_count"] == 60                                               # includes the 20 positions stored before the plan existed
    assert (r["departure"], r["arrival"]) == ("YSSY", "YMML")
