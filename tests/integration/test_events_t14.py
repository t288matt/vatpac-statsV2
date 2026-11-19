"""T14: Monday event and RAAFv panels, re-checked against the owner's current Metabase SQL (08-10-2026).

Flights are inserted synthetically (pilot_session plus the flight_summary row the writer would write, which the `flight` view reads), then
the panel SQL is read from the dashboard JSON, the Grafana time macros are replaced by literal UTC
timestamps, and the query is executed. Rules under test:
  card 66   RAAFv = flight-plan remarks contain RAAFVIRTUAL.ORG (any case), whatever the aircraft type;
            hours per UTC week, summed over flights ending in the window.
  card 78   same RAAFv rule; last month; one count per session (v1 de-duplicated on callsign + logon time);
            per-route flights, distinct pilots and shares.
  card 131  Monday window 0700-1200 UTC; a controller session counts if it overlaps the window (starts before 1200,
            ends at or after 0700); one bar per Monday, distinct CIDs (issue 353 replaced the attendance chart).
  card 134  YMML-YSSY (either direction) flights ending Monday 0700-1200 UTC; share of airborne minutes flown
            by pilots whose home airfield is in the home region. (v1 still uses 0800.)
  card 133  stays dropped (frequency matching, ADR-002): no panel mentions it.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO
from helpers import record_flight

pytestmark = pytest.mark.integration

MILITARY = REPO / "grafana" / "dashboards" / "military" / "military.json"
EVENTS = REPO / "grafana" / "dashboards" / "events" / "monday-events.json"

RAAFV_REMARKS = "PBN/A1 OPR/RAAFVIRTUAL.ORG RMK/TEST"


def panel(dashboard, title_start: str) -> dict:
    panels = json.loads(dashboard.read_text(encoding="utf-8"))["panels"]
    return next(p for p in panels if p.get("title", "").startswith(title_start))


def run_panel(conn, dashboard, title_start: str, start: datetime, end: datetime) -> list[dict]:
    sql = panel(dashboard, title_start)["targets"][0]["rawSql"]
    sql = sql.replace("$__timeFrom()", f"'{start.isoformat()}'::timestamptz")
    sql = sql.replace("$__timeTo()", f"'{end.isoformat()}'::timestamptz")
    return conn.execute(sql.rstrip().rstrip(";")).fetchall()


def add_flight(conn, cid: int, start: datetime, end: datetime, airborne: float = 60.0, dep="YSSY", arr="YMML",
               remarks: str | None = None, aircraft: str = "B738", home: str | None = "YSSY",
               segments: int = 1) -> None:
    """One pilot_session with `segments` flight_summary rows (segments > 1 mimics a returning session: one session, several
    flights, which the per-session panels must count once)."""
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival, "
        "aircraft_icao, remarks, home_icao) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (cid, f"TST{cid}", start.replace(microsecond=0), start, end, dep, arr, aircraft, remarks, home),
    ).fetchone()["id"]
    for seg in range(1, segments + 1):
        seg_end = end - timedelta(minutes=10 * (segments - seg))
        record_flight(conn, sid, start, seg_end, airborne=airborne, gs=None, alt=None, start_offset_s=seg - 1)


def add_controller(conn, cid: int, start: datetime, end: datetime) -> None:
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen) "
        "VALUES (%s,%s,%s,%s,%s)", (cid, f"TST{cid}_CTR", start.replace(microsecond=0), start, end))


def monday_utc(weeks_back: int) -> datetime:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=today.weekday()) - timedelta(weeks=weeks_back)


# ------------------------------------------------------------------ card 66
def test_raafv_hours_per_week_uses_remarks_not_aircraft_type(conn) -> None:
    w2, w1 = monday_utc(2), monday_utc(1)
    now = datetime.now(UTC)
    # week of w2: two RAAFv flights (remarks in upper and lower case) = 90 + 150 min = 4.0 h
    add_flight(conn, 9400001, w2 + timedelta(hours=8), w2 + timedelta(hours=10), 90, remarks=RAAFV_REMARKS)
    add_flight(conn, 9400002, w2 + timedelta(days=2, hours=8), w2 + timedelta(days=2, hours=11), 150,
               remarks="rmk/raafvirtual.org", aircraft="C130")
    # a military type with no RAAFv remarks must NOT count (the old repo rule counted it)
    add_flight(conn, 9400003, w2 + timedelta(hours=8), w2 + timedelta(hours=12), 300, aircraft="F18")
    # week of w1: one RAAFv flight of 60 minutes
    add_flight(conn, 9400004, w1 + timedelta(hours=8), w1 + timedelta(hours=9), 60, remarks=RAAFV_REMARKS)
    # older than 98 days: outside the panel window
    old = now - timedelta(days=120)
    add_flight(conn, 9400005, old - timedelta(hours=2), old, 100, remarks=RAAFV_REMARKS)
    rows = run_panel(conn, MILITARY, "Hours flown by RAAFv", now - timedelta(days=98), now)
    got = {r["time"]: float(r["RAAFv hours"]) for r in rows}
    assert got == {w2: 4.0, w1: 1.0}


# ------------------------------------------------------------------ card 78
def test_raafv_routes_last_month_count_each_session_once(conn) -> None:
    now = datetime.now(UTC)
    t = now - timedelta(days=2)
    # a session split into two flight rows still counts once; a second pilot on the same route
    add_flight(conn, 9400011, t - timedelta(hours=3), t, 60, dep="YSSY", arr="YPPH", remarks=RAAFV_REMARKS, segments=2)
    add_flight(conn, 9400012, t - timedelta(hours=3), t, 60, dep="YSSY", arr="YPPH", remarks=RAAFV_REMARKS, aircraft="A332")
    add_flight(conn, 9400013, t - timedelta(hours=2), t, 60, dep="YMML", arr="YBBN", remarks=RAAFV_REMARKS)
    # not RAAFv (military type, no remarks) and RAAFv older than a month: both excluded
    add_flight(conn, 9400014, t - timedelta(hours=2), t, 60, dep="YMML", arr="YBBN", aircraft="F18")
    old = now - timedelta(days=45)
    add_flight(conn, 9400015, old - timedelta(hours=2), old, 60, dep="YPDN", arr="YBCS", remarks=RAAFV_REMARKS)
    rows = run_panel(conn, MILITARY, "Most popular RAAFv routes", now - timedelta(days=30), now)
    assert [(r["route"], r["total_flights"], r["unique_pilots"]) for r in rows] == [
        ("YSSY-YPPH", 2, 2), ("YMML-YBBN", 1, 1)]
    assert [float(r["% of RAAFv flights"]) for r in rows] == [66.67, 33.33]
    assert [float(r["% of RAAFv pilots"]) for r in rows] == [66.67, 33.33]


# ------------------------------------------------------------------ card 131 (controllers per Monday, issue 353)
CONTROLLERS = "Controllers online during the Monday event window"


def test_monday_controllers_chart_counts_distinct_cids_in_the_window_0700_to_1200_utc(conn) -> None:
    mon = monday_utc(2)
    other = mon - timedelta(days=7)

    def h(x: float, day: datetime = mon) -> datetime:
        return day + timedelta(hours=x)

    add_controller(conn, 9400021, h(5), h(7.5))                  # in: overlaps the start
    add_controller(conn, 9400022, h(6), h(7))                    # in: ends exactly 0700 (card 131 rule: ends at or after 0700)
    add_controller(conn, 9400023, h(5), h(6.99))                 # out: ended before 0700
    add_controller(conn, 9400024, h(11.5), h(14))                # in: starts before 1200
    add_controller(conn, 9400025, h(12), h(13))                  # out: starts exactly 1200
    add_controller(conn, 9400026, h(8), h(9))                    # in
    add_controller(conn, 9400026, h(10), h(11))                  # the same CID twice in the window: counted once
    add_controller(conn, 9400027, h(-3), h(30))                  # in: spans the whole day and more, counted once, and only on Mondays
    add_controller(conn, 9400028, h(8, other), h(9, other))      # the Monday before: another bar
    add_controller(conn, 9400029, h(8) + timedelta(days=1), h(9) + timedelta(days=1))   # Tuesday: out
    add_flight(conn, 9400030, h(8), h(9))                        # a pilot: not counted here
    rows = run_panel(conn, EVENTS, CONTROLLERS, other - timedelta(days=1), mon + timedelta(days=2))
    assert [(r["Monday (UTC)"], r["Controllers online"]) for r in rows] == [
        (other.strftime("%d-%m-%Y"), 1), (mon.strftime("%d-%m-%Y"), 5)]   # other: 28; mon: 21, 22, 24, 26, 27


CONTROLLERS_MONTH = "Controllers online during the Monday event window, by month"


def test_monday_controllers_chart_by_month_counts_a_cid_on_two_mondays_once_not_twice(conn) -> None:
    """Issue 407: the monthly panel is not a sum of the weekly bars. 05-10-2026 and 12-10-2026 are both in October 2026.
    CID 9400041 is online in the window on both Mondays (two sessions, same CID); CID 9400040 is online on the first
    Monday only. The weekly bars sum to 3 for the month; the true distinct CID count is 2."""
    mon, next_mon = datetime(2026, 10, 5, tzinfo=UTC), datetime(2026, 10, 12, tzinfo=UTC)

    def h(x: float, day: datetime = mon) -> datetime:
        return day + timedelta(hours=x)

    add_controller(conn, 9400040, h(8), h(9))                        # Monday 1 only
    add_controller(conn, 9400041, h(8), h(9))                        # Monday 1
    add_controller(conn, 9400041, h(8, next_mon), h(9, next_mon))    # same CID again, Monday 2
    window = (mon - timedelta(days=1), next_mon + timedelta(days=2))
    weekly = run_panel(conn, EVENTS, CONTROLLERS, *window)
    assert [(r["Monday (UTC)"], r["Controllers online"]) for r in weekly] == [
        (mon.strftime("%d-%m-%Y"), 2), (next_mon.strftime("%d-%m-%Y"), 1)]    # weekly bars: sum over October = 3
    monthly = run_panel(conn, EVENTS, CONTROLLERS_MONTH, *window)
    assert [(r["Month (UTC)"], r["Controllers online"]) for r in monthly] == [("10-2026", 2)]   # not 3: 9400041 counted once


def test_monday_controllers_chart_has_the_aircraft_chart_format() -> None:
    aircraft = panel(EVENTS, "Aircraft airborne during the Monday event window")
    mine = panel(EVENTS, CONTROLLERS)
    assert mine["type"] == aircraft["type"] == "barchart"
    assert mine["timeFrom"] == aircraft["timeFrom"] == "1825d"
    assert mine["options"] == aircraft["options"] and mine["gridPos"]["w"] == aircraft["gridPos"]["w"]
    assert mine["targets"][0]["format"] == aircraft["targets"][0]["format"] == "table"
    assert "to_char(m.monday, 'DD-MM-YYYY')" in mine["targets"][0]["rawSql"]
    assert all(p.get("title") != "Monday event attendance (0700-1200Z)" for p in json.loads(EVENTS.read_text(encoding="utf-8"))["panels"])


# ------------------------------------------------------------------ card 134
def test_local_share_of_ymml_yssy_airborne_time_monday_window(conn) -> None:
    mon = monday_utc(2)

    def h(x: float) -> datetime:
        return mon + timedelta(hours=x)

    add_flight(conn, 9400031, h(5), h(9), 60, dep="YMML", arr="YSSY", home="YSSY")     # local, 60 min
    add_flight(conn, 9400032, h(5), h(10), 120, dep="YSSY", arr="YMML", home="EGLL")   # not local, 120 min
    add_flight(conn, 9400033, h(5), h(7), 60, dep="YMML", arr="YSSY", home="YMML")     # ends 0700: in, local
    add_flight(conn, 9400034, h(5), h(6.9), 500, dep="YMML", arr="YSSY", home="EGLL")  # ends before 0700: out
    add_flight(conn, 9400035, h(5), h(12), 500, dep="YMML", arr="YSSY", home="EGLL")   # ends 1200: out
    add_flight(conn, 9400036, h(5), h(9), 500, dep="YMML", arr="YBBN", home="EGLL")    # other route: out
    add_flight(conn, 9400037, h(29), h(33), 500, dep="YMML", arr="YSSY", home="EGLL")  # Tuesday: out
    rows = run_panel(conn, EVENTS, "Share of YMML-YSSY", mon - timedelta(days=1), mon + timedelta(days=3))
    assert len(rows) == 1
    assert rows[0]["Monday (UTC)"] == mon.strftime("%d-%m-%Y")
    assert float(rows[0]["Local pilots (%)"]) == 50.0   # (60 + 60) / (60 + 120 + 60)


# ------------------------------------------------------------------ card 133
def test_frequency_matching_chart_stays_dropped() -> None:
    for dash in (EVENTS, MILITARY):
        text = dash.read_text(encoding="utf-8").lower()
        assert "same freq" not in text and "card 133" not in text
