"""T14: Monday event and RAAFv panels, re-checked against the owner's current Metabase SQL (08-10-2026).

Flights are inserted synthetically (pilot_session plus the flight_summary row the writer would write, which the `flight` view reads), then
the panel SQL is read from the dashboard JSON, the Grafana time macros are replaced by literal UTC
timestamps, and the query is executed. Rules under test:
  card 66   RAAFv = flight-plan remarks contain RAAFVIRTUAL.ORG (any case), whatever the aircraft type;
            hours per UTC week, summed over flights ending in the window.
  card 78   same RAAFv rule; last month; one count per session (v1 de-duplicated on callsign + logon time);
            per-route flights, distinct pilots and shares.
  card 131  Monday window 0700-1200 UTC; a session counts if it overlaps the window (starts before 1200,
            ends at or after 0700).
  card 134  YMML-YSSY (either direction) flights ending Monday 0700-1200 UTC; share of airborne minutes flown
            by pilots whose home airfield is in the home region. (v1 still uses 0800: see docs/t14_notes/events.md.)
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


# ------------------------------------------------------------------ card 131
def test_monday_attendance_window_0700_to_1200_utc(conn) -> None:
    mon = monday_utc(2)
    conn.execute("INSERT INTO network_snapshot (observed_at, pilots_in_region, controllers_in_region, "
                 "pilots_global, controllers_global) VALUES (%s,0,0,0,0)", (mon,))

    def h(x: float) -> datetime:
        return mon + timedelta(hours=x)

    add_flight(conn, 9400021, h(5), h(7.5))             # in: overlaps the start
    add_flight(conn, 9400022, h(6), h(7))               # in: ends exactly 0700
    add_flight(conn, 9400023, h(5), h(6.99))            # out: ended before 0700
    add_flight(conn, 9400024, h(11.5), h(14))           # in: starts before 1200
    add_flight(conn, 9400025, h(12), h(14))             # out: starts exactly 1200
    add_controller(conn, 9400026, h(6), h(7.5))         # in
    add_controller(conn, 9400027, h(12), h(13))         # out
    rows = run_panel(conn, EVENTS, "Monday event attendance", mon - timedelta(days=1), mon + timedelta(days=1))
    assert [(r["Pilots"], r["Controllers"]) for r in rows] == [(3, 1)]
    assert rows[0]["time"] == h(7)


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
    assert rows[0]["time"] == mon
    assert float(rows[0]["Local pilots (%)"]) == 50.0   # (60 + 60) / (60 + 120 + 60)


# ------------------------------------------------------------------ card 133
def test_frequency_matching_chart_stays_dropped() -> None:
    for dash in (EVENTS, MILITARY):
        text = dash.read_text(encoding="utf-8").lower()
        assert "same freq" not in text and "card 133" not in text
