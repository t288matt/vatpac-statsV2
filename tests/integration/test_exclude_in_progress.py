"""Issue #38 / T46 / ADR-025: flights still connected are left out of the flight counts, live panels keep them.

The exclusion lives in `flight_ext` (so `movements` and `cohorts` inherit it) and in the SQL of the panels that read
`flight` directly. Sessions are inserted synthetically; the panel SQL is read from the dashboard JSON and executed.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import REPO

from test_flight_in_progress import CID, session_ending, set_up_clock
from test_flight_summary import refresh
from test_flight_view import make_session, run

pytestmark = pytest.mark.integration

DASH = REPO / "grafana" / "dashboards"
BASE = CID + 100                      # keeps these CIDs apart from test_flight_in_progress


def panel_sql(path: str, title: str) -> str:
    dash = json.loads((DASH / path).read_text(encoding="utf-8"))
    p = next(p for p in dash["panels"] if p.get("title") == title)
    days = int(str(p.get("timeFrom", "30d")).rstrip("d"))
    return (p["targets"][0]["rawSql"]
            .replace("$__timeFrom()", f"(now() - interval '{days} days')")
            .replace("$__timeTo()", "now()")
            .replace("${airport:sqlstring}", "'YSSY'"))


def two_flights(conn):
    """One flight still connected and one that ended 2 minutes before the newest snapshot, both YSSY-YMML."""
    t = set_up_clock(conn)
    make_session(conn, BASE + 1, session_ending(t, 0))        # in progress
    make_session(conn, BASE + 2, session_ending(t, 120))      # finished
    refresh(conn)
    return t


def cids(rows) -> set[int]:
    return {r["cid"] for r in rows}


# ---------------------------------------------------------------- views
def test_flight_ext_leaves_out_the_in_progress_flight_and_keeps_the_finished_one(conn) -> None:
    two_flights(conn)
    assert cids(conn.execute("SELECT cid FROM flight_ext").fetchall()) == {BASE + 2}
    assert cids(conn.execute("SELECT cid FROM flight").fetchall()) == {BASE + 1, BASE + 2}   # flight itself keeps both


def test_movements_drop_both_the_departure_and_the_arrival_of_an_in_progress_flight(conn) -> None:
    two_flights(conn)
    rows = conn.execute("SELECT cid, movement_type FROM movements ORDER BY 1, 2").fetchall()
    assert [(r["cid"], r["movement_type"]) for r in rows] == [(BASE + 2, "arrival"), (BASE + 2, "departure")]


def test_cohorts_inherit_the_exclusion(conn) -> None:
    two_flights(conn)
    assert cids(conn.execute("SELECT cid FROM cohorts").fetchall()) == {BASE + 2}


def test_reconnected_flight_is_excluded_whole_when_its_newest_session_is_online(conn) -> None:
    t = set_up_clock(conn)
    make_session(conn, BASE + 3, session_ending(t, 600))
    make_session(conn, BASE + 3, run(20, start=t - timedelta(seconds=15 * 19 + 5)))
    refresh(conn)
    assert conn.execute("SELECT count(*) AS n FROM flight WHERE cid = %s", (BASE + 3,)).fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM flight_ext WHERE cid = %s", (BASE + 3,)).fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM movements WHERE cid = %s", (BASE + 3,)).fetchone()["n"] == 0


def test_flight_ext_keeps_its_columns(conn) -> None:
    two_flights(conn)
    cols = list(conn.execute("SELECT * FROM flight_ext LIMIT 1").fetchone())
    assert cols[0] == "session_id" and cols[-1] == "has_remarks" and len(cols) == 31


# ---------------------------------------------------------------- panels
def test_count_panel_over_flight_ext_leaves_out_the_in_progress_flight(conn) -> None:
    two_flights(conn)
    rows = conn.execute(panel_sql("pilots/pilots-traffic.json", "Busiest days by flights (last 6 months)")).fetchall()
    assert sum(r["total_flights"] for r in rows) == 1


def test_count_panel_over_movements_counts_the_finished_flight_only(conn) -> None:
    two_flights(conn)
    rows = conn.execute(panel_sql("pilots/pilots-traffic.json", "Busiest home-region airports (last 30 days)")).fetchall()
    assert {r["airport"]: (r["departures"], r["arrivals"]) for r in rows} == {"YSSY": (1, 0), "YMML": (0, 1)}


def test_count_panel_reading_flight_directly_leaves_out_the_in_progress_flight(conn) -> None:
    two_flights(conn)
    rows = conn.execute(panel_sql("pilots/pilots-traffic.json", "Busiest days by pilots online (last 6 months)")).fetchall()
    assert sum(r["unique_pilots"] for r in rows) == 1


def test_a_finished_flight_is_counted_when_nothing_is_in_progress(conn) -> None:
    t = set_up_clock(conn)
    make_session(conn, BASE + 4, session_ending(t, 120))
    refresh(conn)
    rows = conn.execute(panel_sql("pilots/pilots-traffic.json", "Busiest days by pilots online (last 6 months)")).fetchall()
    assert sum(r["unique_pilots"] for r in rows) == 1


# ---------------------------------------------------------------- live panels and a stale collector
def test_departure_board_still_shows_the_in_progress_flight(conn) -> None:
    two_flights(conn)
    rows = conn.execute(panel_sql("pilots/pilots-now.json", "Departure board")).fetchall()
    by_flight = {r["Flight"]: r["Status"] for r in rows}
    assert by_flight[f"TEST{BASE + 1}"] == "AIRBORNE"          # connected now, shown although excluded from the counts
    assert by_flight[f"TEST{BASE + 2}"] == "LOGGED OFF"


def test_pilots_online_now_view_still_lists_the_in_progress_flight(conn) -> None:
    two_flights(conn)
    assert cids(conn.execute("SELECT cid FROM pilot_online_now").fetchall()) == {BASE + 1}


def test_stale_collector_does_not_hide_flights(conn) -> None:
    conn.execute("INSERT INTO app_setting (key, value) VALUES ('poll_interval_seconds', '30') "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    t = datetime.now(timezone.utc) - timedelta(minutes=30)
    conn.execute("INSERT INTO network_snapshot VALUES (%s,1,1,1,1)", (t,))
    make_session(conn, BASE + 5, session_ending(t, 0))         # last seen 30 minutes ago, collector stopped since
    refresh(conn)
    assert conn.execute("SELECT is_in_progress FROM flight WHERE cid = %s", (BASE + 5,)).fetchone()["is_in_progress"] is False
    assert cids(conn.execute("SELECT cid FROM flight_ext").fetchall()) == {BASE + 5}
    assert conn.execute("SELECT count(*) AS n FROM movements").fetchone()["n"] == 2
