"""Cards 94 and 95 (when people are online): distinct callsigns per UTC clock hour over the last 30 days.

Sessions are inserted synthetically at fixed hours on the most recent Wednesday and Saturday, then the
panel SQL is read from grafana/dashboards/pilots/pilots-now.json and executed.
Rules (owner decision 08-10-2026: rebuild to match v1; card 94 uses Monday to Friday, not v1's Sun/Mon/Sat):
  94 weekdays: pilots with airborne time, ATC sessions of at least 20 minutes, hours wrap past midnight
  95 weekends: ATC sessions of at least 30 minutes, no wrap (v1 quirk: a flight crossing midnight yields no hours)
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO

pytestmark = pytest.mark.integration

DASHBOARD = REPO / "grafana" / "dashboards" / "pilots" / "pilots-now.json"
WEEKDAYS = "When are people online on weekdays (last 30 days)"
WEEKENDS = "When are people online on weekends (last 30 days)"
_n = {"i": 0}


def run(conn, title: str) -> dict[int, tuple[int, int]]:
    panels = json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"]
    sql = next(p["targets"][0]["rawSql"] for p in panels if p.get("title") == title)
    return {r["hour_utc"]: (r["Pilots (callsigns)"], r["ATC (callsigns)"]) for r in conn.execute(sql).fetchall()}


def last_day(isodow: int) -> datetime:
    """Midnight UTC of the most recent day with this ISO weekday that is at least 2 days ago."""
    day = (datetime.now(UTC) - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    while day.isoweekday() != isodow:
        day -= timedelta(days=1)
    return day


def add_flight(conn, day: datetime, start_hm: tuple[int, int], end: datetime, airborne: float = 60) -> None:
    _n["i"] += 1
    start = day.replace(hour=start_hm[0], minute=start_hm[1])
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, name, first_seen, last_seen, departure, arrival) "
        "VALUES (%s,%s,%s,%s,%s,%s,'YSSY','YMML') RETURNING id",
        (9400000 + _n["i"], f"HR{_n['i']:03d}", start, "Pilot", start, end),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO flight_summary (session_id, gap_segment, span_part, cid, start_time, end_time, sample_count, "
        "airborne_minutes, max_gs_kt, max_alt_ft) VALUES (%s,0,0,%s,%s,%s,10,%s,450,35000)",
        (sid, 9400000 + _n["i"], start, end, airborne),
    )


def add_controller(conn, day: datetime, start_hm: tuple[int, int], minutes: int) -> None:
    _n["i"] += 1
    start = day.replace(hour=start_hm[0], minute=start_hm[1])
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) "
        "VALUES (%s,%s,%s,'Controller',3,%s,%s)",
        (9500000 + _n["i"], f"HRC{_n['i']:03d}_TWR", start, start, start + timedelta(minutes=minutes)),
    )


def test_weekday_counts_hours_touched_by_each_callsign(conn):
    wed = last_day(3)
    add_flight(conn, wed, (10, 30), wed.replace(hour=12, minute=10))     # hours 10, 11, 12
    add_controller(conn, wed, (11, 0), 25)                               # 25 min: counts (needs 20), hour 11
    add_controller(conn, wed, (14, 0), 15)                               # 15 min: below the 20-minute minimum
    got = run(conn, WEEKDAYS)
    assert got == {10: (1, 0), 11: (1, 1), 12: (1, 0)}


def test_weekday_wraps_a_flight_past_midnight(conn):
    wed = last_day(3)
    add_flight(conn, wed, (23, 0), wed + timedelta(days=1, hours=1, minutes=30))   # hours 23, 0, 1
    assert run(conn, WEEKDAYS) == {23: (1, 0), 0: (1, 0), 1: (1, 0)}


def test_weekday_excludes_weekends_and_flights_without_airborne_time(conn):
    sat = last_day(6)
    add_flight(conn, sat, (10, 0), sat.replace(hour=11))                 # Saturday: not a weekday
    wed = last_day(3)
    add_flight(conn, wed, (15, 0), wed.replace(hour=16), airborne=0)     # parked connection: no airborne time
    assert run(conn, WEEKDAYS) == {}


def test_weekend_uses_saturday_and_sunday_and_a_thirty_minute_atc_minimum(conn):
    sat = last_day(6)
    add_flight(conn, sat, (8, 15), sat.replace(hour=9, minute=45))       # hours 8, 9
    add_controller(conn, sat, (9, 0), 35)                                # counts (needs 30), hour 9
    add_controller(conn, sat, (9, 0), 25)                                # below 30: not counted
    assert run(conn, WEEKENDS) == {8: (1, 0), 9: (1, 1)}


def test_weekend_does_not_wrap_a_flight_that_crosses_midnight(conn):
    """Known v1 quirk, kept on purpose."""
    sat = last_day(6)
    add_flight(conn, sat, (23, 0), sat + timedelta(days=1, hours=1))
    assert run(conn, WEEKENDS) == {}
