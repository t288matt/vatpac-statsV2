"""Issue #51: the SQL must read the clock in UTC whatever time zone the database session uses.

Production is safe because the Grafana role is pinned to UTC (`db/roles.sql`, tested in test_grafana_ro.py). These tests
protect the SQL itself: each one moves the session to a zone where a day or an hour differs from UTC (Sydney is UTC+10 or
+11, Los Angeles is UTC-7 or -8), builds data whose UTC reading is known, and asserts the UTC answer. Removing any
`AT TIME ZONE 'UTC'` from the three places below changes an answer in at least one zone (mutants U03, U05, U07 and the
variants in the commit message). CI also runs the whole integration suite a second time in a Sydney session.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO
from helpers import record_flight

pytestmark = pytest.mark.integration

EVENTS = REPO / "grafana" / "dashboards" / "events" / "monday-events.json"
NOW_PANELS = REPO / "grafana" / "dashboards" / "atc" / "atc-positions.json"
ZONES = ["Australia/Sydney", "America/Los_Angeles"]
_n = {"i": 0}


@pytest.fixture(params=ZONES)
def zone(conn, request) -> str:
    conn.execute("SELECT set_config('TimeZone', %s, false)", (request.param,))
    assert conn.execute("SHOW TimeZone").fetchone()["TimeZone"] == request.param
    return request.param


def panel_sql(path, title: str) -> str:
    panels = json.loads(path.read_text(encoding="utf-8"))["panels"]
    return next(p["targets"][0]["rawSql"] for p in panels if p.get("title", "").startswith(title))


def add_flight(conn, start: datetime, end: datetime, *, airborne: float = 60.0, dep="YSSY", arr="YMML",
               home: str | None = "YSSY") -> None:
    _n["i"] += 1
    cid = 9930000 + _n["i"]
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, name, first_seen, last_seen, departure, arrival, home_icao) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (cid, f"TZ{_n['i']:04d}", start.replace(microsecond=0), "Pilot", start, end, dep, arr, home),
    ).fetchone()["id"]
    record_flight(conn, sid, start, end, airborne=airborne)


def add_controller(conn, start: datetime, minutes: int) -> None:
    _n["i"] += 1
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) "
        "VALUES (%s,%s,%s,'Controller',3,%s,%s)",
        (9940000 + _n["i"], f"TZC{_n['i']:03d}_TWR", start.replace(microsecond=0), start, start + timedelta(minutes=minutes)),
    )


def monday_before(days_back: int) -> datetime:
    """Midnight UTC on the Monday of the week that contained the day ``days_back`` days ago."""
    day = (datetime.now(UTC) - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def test_the_session_time_zone_is_what_the_run_asked_for(conn) -> None:
    """CI's second pass sets VATPAC_TEST_EXPECT_TZ together with the DSN option; if the option were silently ignored
    the pass would prove nothing, so fail here. Without the variable there is nothing to check."""
    expected = os.environ.get("VATPAC_TEST_EXPECT_TZ")
    if expected:
        assert conn.execute("SHOW TimeZone").fetchone()["TimeZone"] == expected


# ------------------------------------------------------------------ card 134 (U03)
def test_card_134_monday_window_and_week_are_read_in_utc(conn, zone) -> None:
    """Flights ending Monday 07:00 to 12:00 UTC count, whatever the session zone. January, so Los Angeles is UTC-8:
    07:30 UTC is Sunday 23:30 there (a wrong weekday test drops it) and 08:00 UTC is Monday 00:00 (a wrong time of day
    drops it). In Sydney (UTC+11) 11:30 UTC is 22:30 (a wrong time of day drops it) and Sunday 20:00 UTC is Monday
    07:00 (a wrong weekday and time of day let a flight in that must stay out)."""
    mon = datetime(2026, 1, 12, tzinfo=UTC)

    def h(x: float) -> datetime:
        return mon + timedelta(hours=x)

    add_flight(conn, h(5), h(8), home="YSSY")                     # in, local
    add_flight(conn, h(5), h(11.5), home="EGLL")                  # in, not local
    add_flight(conn, h(5), h(7.5), home="YSSY")                   # in, local (Sunday 23:30 in Los Angeles)
    add_flight(conn, h(-6), h(-4), airborne=500, home="EGLL")                  # Sunday 20:00 UTC: out (Monday 07:00 in Sydney)
    add_flight(conn, h(5), h(12.5), airborne=500, home="EGLL")                 # Monday 12:30 UTC: out
    sql = (panel_sql(EVENTS, "Share of YMML-YSSY")
           .replace("$__timeFrom()", f"'{(mon - timedelta(days=2)).isoformat()}'::timestamptz")
           .replace("$__timeTo()", f"'{(mon + timedelta(days=3)).isoformat()}'::timestamptz"))
    rows = conn.execute(sql).fetchall()
    assert [(r["time"], float(r["Local pilots (%)"])) for r in rows] == [(mon, 66.7)]   # (60 + 60) / (60 + 60 + 60)


# ------------------------------------------------------------------ cards 94 and 95 (U07)
def run_hours(conn, title: str) -> dict[int, tuple[int, int]]:
    rows = conn.execute(panel_sql(NOW_PANELS, title)).fetchall()
    return {r["hour_utc"]: (r["Pilots (callsigns)"], r["ATC (callsigns)"]) for r in rows}


def test_cards_94_and_95_pick_weekdays_and_hours_in_utc(conn, zone) -> None:
    """A week that ended at least four days ago (inside the 30 days of the panels). In Sydney Friday 20:30 UTC is already
    Saturday and Sunday 20:00 UTC is already Monday; in Los Angeles Monday 05:00 UTC and Saturday 05:00 UTC are still the
    day before. A wrong weekday or hour test moves a callsign between the two panels or into another hour."""
    mon = monday_before(10)
    add_flight(conn, mon + timedelta(days=4, hours=20, minutes=30), mon + timedelta(days=4, hours=22))   # Friday 20 to 22 UTC
    add_flight(conn, mon + timedelta(hours=5), mon + timedelta(hours=6, minutes=30))                     # Monday 5 to 6 UTC
    add_flight(conn, mon + timedelta(days=5, hours=5), mon + timedelta(days=5, hours=6))                 # Saturday 5 to 6 UTC
    add_flight(conn, mon + timedelta(days=6, hours=20), mon + timedelta(days=6, hours=21))               # Sunday 20 to 21 UTC
    add_controller(conn, mon + timedelta(hours=5), 30)                                                   # Monday 05:00 UTC
    add_controller(conn, mon + timedelta(days=6, hours=20), 30)                                          # Sunday 20:00 UTC
    assert run_hours(conn, "When are people online on weekdays") == {
        5: (1, 1), 6: (1, 0), 20: (1, 0), 21: (1, 0), 22: (1, 0)}
    assert run_hours(conn, "When are people online on weekends") == {5: (1, 0), 6: (1, 0), 20: (1, 1), 21: (1, 0)}


# ------------------------------------------------------------------ monday_attendance (U05)
def test_monday_attendance_starts_at_the_utc_week_of_the_first_snapshot(conn, zone) -> None:
    """The first snapshot is Sunday 20:00 UTC, which is Monday morning in Sydney. The series must still start at the
    Monday before it (UTC week), so that Monday's attendance is listed."""
    mon = monday_before(21)
    conn.execute("INSERT INTO network_snapshot VALUES (%s,0,0,0,0)", (mon + timedelta(days=6, hours=20),))
    add_flight(conn, mon + timedelta(hours=8), mon + timedelta(hours=9))
    rows = conn.execute("SELECT monday_date, pilot_cids FROM monday_attendance ORDER BY monday_date").fetchall()
    assert rows[0]["monday_date"] == mon.date() and rows[0]["pilot_cids"] == 1
    assert rows[-1]["monday_date"] == monday_before(0).date()
