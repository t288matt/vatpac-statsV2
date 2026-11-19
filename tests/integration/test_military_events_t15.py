"""T15: RAAFv cards 142, 143, 161, 184 and Monday event card 156, rebuilt from the owner's current Metabase SQL.

Rows are inserted synthetically (pilot_session plus the flight_summary row the writer would write, which the `flight` view reads). The panel SQL is read from the dashboard JSON, the Grafana time macros are replaced by literal UTC
timestamps, and the query is executed. Rules under test:
  all RAAFv cards  RAAFv = flight-plan remarks contain RAAFVIRTUAL.ORG (any case), whatever the aircraft type.
  card 142         logon time in the window (inclusive edges), distinct rows, newest first, names and CIDs shown.
  card 143         flights ending in the window; per aircraft type: distinct sessions, airborne minutes and hours.
  card 161         flights with logon in the window; one count per session (split rows count once); shares.
  card 184         flights per UTC week of the flight end (weeks start Monday).
  card 156         (issues 324, 407) per UTC month, the distinct finished flights with airborne time connected at some
                   moment in 0700-1200Z on any Monday in that month; one bar per month that has any; no sector logic.
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


def run_panel(conn, dashboard, title_start: str, start: datetime, end: datetime, now: datetime | None = None) -> list[dict]:
    """Run a panel's SQL. ``now`` is a clock seam for panels that read now(): the literal text now() is replaced by that
    instant, so a test does not depend on the day or hour it runs."""
    sql = panel(dashboard, title_start)["targets"][0]["rawSql"]
    sql = sql.replace("$__timeFrom()", f"'{start.isoformat()}'::timestamptz")
    sql = sql.replace("$__timeTo()", f"'{end.isoformat()}'::timestamptz")
    if now is not None:
        assert "now()" in sql
        sql = sql.replace("now()", f"'{now.isoformat()}'::timestamptz")
    return conn.execute(sql.rstrip().rstrip(";")).fetchall()


def add_flight(conn, cid: int, start: datetime, end: datetime, airborne: float = 60.0, dep="YSSY", arr="YMML",
               remarks: str | None = None, aircraft: str = "B738", name: str | None = None,
               segments: int = 1) -> int:
    """One pilot_session with `segments` flight_summary rows (segments > 1 mimics a returning session: one session, several
    flights, which the per-session panels must count once)."""
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival, "
        "aircraft_icao, remarks, name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (cid, f"TST{cid}", start.replace(microsecond=0), start, end, dep, arr, aircraft, remarks, name),
    ).fetchone()["id"]
    for seg in range(1, segments + 1):
        seg_end = end - timedelta(minutes=10 * (segments - seg))
        record_flight(conn, sid, start, seg_end, airborne=airborne, gs=None, alt=None, start_offset_s=seg - 1)
    return sid


def monday_utc(weeks_back: int) -> datetime:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=today.weekday()) - timedelta(weeks=weeks_back)


# ------------------------------------------------------------------ card 142
def test_card_142_lists_raafv_flights_by_remarks_in_logon_window(conn) -> None:
    now = datetime.now(UTC)
    t = now - timedelta(days=2)
    add_flight(conn, 9500001, t, t + timedelta(hours=2), remarks=RAAFV_REMARKS, name="Test Pilot YSSY",
               aircraft="C130")
    add_flight(conn, 9500002, t + timedelta(hours=1), t + timedelta(hours=3), remarks="opr/raafvirtual.org",
               aircraft="F18")
    add_flight(conn, 9500003, t, t + timedelta(hours=2), aircraft="F18")                  # military type, no remarks
    add_flight(conn, 9500004, t, t + timedelta(hours=2), remarks="RMK/OTHER")             # other remarks
    add_flight(conn, 9500005, t, t + timedelta(hours=2), remarks=RAAFV_REMARKS, dep="")   # flight gate: not listed
    old = now - timedelta(days=8)
    add_flight(conn, 9500006, old, old + timedelta(hours=1), remarks=RAAFV_REMARKS)       # outside 7 days
    rows = run_panel(conn, MILITARY, "RAAFv flights (last 7 days)", now - timedelta(days=7), now)
    assert [r["cid"] for r in rows] == [9500002, 9500001]          # newest logon first
    assert rows[1]["name"] == "Test Pilot YSSY"                    # names and CIDs are shown (ADR-009)
    assert rows[1]["aircraft_type"] == "C130"
    assert (rows[1]["departure"], rows[1]["arrival"]) == ("YSSY", "YMML")


def test_card_142_window_edges_and_distinct_rows(conn) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(days=7)
    # logon exactly on the lower edge is inside; a session split in two flight rows is listed once
    add_flight(conn, 9500011, start, start + timedelta(hours=3), remarks=RAAFV_REMARKS, segments=2)
    add_flight(conn, 9500012, start - timedelta(seconds=1), start + timedelta(hours=1), remarks=RAAFV_REMARKS)
    rows = run_panel(conn, MILITARY, "RAAFv flights (last 7 days)", start, now)
    assert [r["cid"] for r in rows] == [9500011]
    assert len(rows) == 1


# ------------------------------------------------------------------ card 143
def test_card_143_airborne_time_by_type_counts_sessions_once(conn) -> None:
    now = datetime.now(UTC)
    t = now - timedelta(days=3)
    # C130: two sessions, 120 and 60 airborne minutes (the 120 minute one is split into two rows of 60)
    add_flight(conn, 9500021, t - timedelta(hours=4), t, 60, aircraft="C130", remarks=RAAFV_REMARKS, segments=2)
    add_flight(conn, 9500022, t - timedelta(hours=2), t, 60, aircraft="c130 ", remarks=RAAFV_REMARKS)
    # F18: one session, 30 minutes
    add_flight(conn, 9500023, t - timedelta(hours=1), t, 30, aircraft="F18", remarks=RAAFV_REMARKS)
    # excluded: no RAAFv remarks; ended outside the 30 days
    add_flight(conn, 9500024, t - timedelta(hours=1), t, 500, aircraft="F18")
    old = now - timedelta(days=40)
    add_flight(conn, 9500025, old - timedelta(hours=1), old, 500, aircraft="C130", remarks=RAAFV_REMARKS)
    rows = run_panel(conn, MILITARY, "RAAFv flights by aircraft type", now - timedelta(days=30), now)
    got = [(r["aircraft_type"], r["total_flights"], float(r["total_airborne_minutes"]),
            float(r["total_airborne_hours"]), float(r["avg_airborne_hours_per_flight"])) for r in rows]
    # C130: 2 sessions, 3 flight rows of 60 min -> 180 min = 3.0 h; average over rows = 1.0 h
    assert got == [("C130", 2, 180.0, 3.0, 1.0), ("F18", 1, 30.0, 0.5, 0.5)]


def test_card_143_window_edge_on_flight_end(conn) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(days=30)
    add_flight(conn, 9500031, start - timedelta(hours=2), start, 60, remarks=RAAFV_REMARKS)                # edge: in
    add_flight(conn, 9500032, start - timedelta(hours=2), start - timedelta(seconds=1), 60,
               remarks=RAAFV_REMARKS)                                                                      # out
    rows = run_panel(conn, MILITARY, "RAAFv flights by aircraft type", start, now)
    assert [(r["aircraft_type"], r["total_flights"]) for r in rows] == [("B738", 1)]


# ------------------------------------------------------------------ card 161
def test_card_161_flights_per_type_one_per_session_with_shares(conn) -> None:
    now = datetime.now(UTC)
    t = now - timedelta(days=2)
    add_flight(conn, 9500041, t - timedelta(hours=3), t, aircraft="C130", remarks=RAAFV_REMARKS, segments=3)
    add_flight(conn, 9500042, t - timedelta(hours=3), t, aircraft="C130", remarks=RAAFV_REMARKS)
    add_flight(conn, 9500043, t - timedelta(hours=3), t, aircraft="F18", remarks=RAAFV_REMARKS)
    add_flight(conn, 9500044, t - timedelta(hours=3), t, aircraft="A332", remarks=RAAFV_REMARKS)
    add_flight(conn, 9500045, t - timedelta(hours=3), t, aircraft="F18")                       # not RAAFv
    old = now - timedelta(days=45)
    add_flight(conn, 9500046, old, old + timedelta(hours=1), aircraft="A332", remarks=RAAFV_REMARKS)  # too old
    rows = run_panel(conn, MILITARY, "Most popular RAAFv aircraft", now - timedelta(days=30), now)
    assert [(r["aircraft"], r["Flights"]) for r in rows] == [("C130", 2), ("A332", 1), ("F18", 1)]
    assert [float(r["% of RAAFv flights"]) for r in rows] == [50.0, 25.0, 25.0]


def test_card_161_window_is_logon_time_and_blank_type_is_labelled(conn) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(days=30)
    add_flight(conn, 9500051, start, start + timedelta(hours=1), aircraft="  ", remarks=RAAFV_REMARKS)  # edge: in
    add_flight(conn, 9500052, start - timedelta(seconds=1), start + timedelta(hours=1), aircraft="C130",
               remarks=RAAFV_REMARKS)                                                                    # out
    rows = run_panel(conn, MILITARY, "Most popular RAAFv aircraft", start, now)
    assert [(r["aircraft"], r["Flights"]) for r in rows] == [("(none)", 1)]


# ------------------------------------------------------------------ card 184
def test_card_184_flights_per_utc_week_of_end(conn) -> None:
    now = datetime.now(UTC)
    w3, w2, w1 = monday_utc(3), monday_utc(2), monday_utc(1)
    add_flight(conn, 9500061, w3 + timedelta(hours=1), w3 + timedelta(hours=2), remarks=RAAFV_REMARKS)
    add_flight(conn, 9500062, w2 + timedelta(hours=1), w2 + timedelta(hours=2), remarks=RAAFV_REMARKS)
    # ends Sunday 23:59 of week w2 and Monday 00:00 of week w1: the edge falls into the new week
    add_flight(conn, 9500063, w1 - timedelta(hours=3), w1 - timedelta(minutes=1), remarks=RAAFV_REMARKS)
    add_flight(conn, 9500064, w1 - timedelta(hours=3), w1, remarks=RAAFV_REMARKS)
    add_flight(conn, 9500065, w1 + timedelta(hours=1), w1 + timedelta(hours=2), aircraft="F18")   # not RAAFv
    old = now - timedelta(days=120)
    add_flight(conn, 9500066, old - timedelta(hours=1), old, remarks=RAAFV_REMARKS)               # outside 98 days
    rows = run_panel(conn, MILITARY, "Flights flown by RAAFv aircraft per week", now - timedelta(days=98), now)
    assert {r["time"]: r["RAAFv flights"] for r in rows} == {w3: 1, w2: 2, w1: 1}
    assert [r["time"] for r in rows] == sorted(r["time"] for r in rows)       # oldest first for the bar chart


# ------------------------------------------------------------------ card 156 (issues 324, 407: one bar per month)
# The panel counts finished flights from the flight summary (`flight`): airborne_minutes > 0 and connected at some moment in
# 0700-1200 UTC on any Monday in a UTC month (start before 12:00, end after 07:00 on that Monday). The time range is the
# dashboard default (14 months), here a fixed pair. MON and NEXT_MON are both Mondays in October 2026 (same UTC month), fixed
# explicit dates (never the relative monday_utc() helper, which is not safe for a monthly assertion).
MON = datetime(2026, 10, 5, tzinfo=UTC)
NEXT_MON = MON + timedelta(days=7)
TITLE = "Unique aircraft airborne during the Monday event window, by month (0700-1200Z)"


def _bars(conn, start: datetime = MON - timedelta(days=1), end: datetime = NEXT_MON + timedelta(days=3)) -> dict[str, int]:
    rows = run_panel(conn, EVENTS, TITLE, start, end)
    assert [r["Month (UTC)"] for r in rows] == sorted((r["Month (UTC)"] for r in rows), key=lambda t: (t[3:7], t[:2]))
    return {r["Month (UTC)"]: r["Aircraft airborne"] for r in rows}


def test_card_156_a_flight_overlapping_the_window_by_one_minute_counts(conn) -> None:
    add_flight(conn, 9500071, MON + timedelta(hours=5), MON + timedelta(hours=7, minutes=1), airborne=30)    # ends 07:01: counts
    add_flight(conn, 9500072, MON + timedelta(hours=11, minutes=59), MON + timedelta(hours=13), airborne=30)  # starts 11:59: counts
    assert _bars(conn) == {"10-2026": 2}


def test_card_156_a_flight_before_or_after_the_window_does_not_count(conn) -> None:
    add_flight(conn, 9500073, MON + timedelta(hours=5), MON + timedelta(hours=6, minutes=59), airborne=30)   # ends 06:59
    add_flight(conn, 9500074, MON + timedelta(hours=5), MON + timedelta(hours=7), airborne=30)               # ends exactly 07:00
    add_flight(conn, 9500075, MON + timedelta(hours=12), MON + timedelta(hours=14), airborne=30)             # starts exactly 12:00
    assert _bars(conn) == {}                                                                                  # a month with no qualifying flights has no bar


def test_card_156_only_mondays_count(conn) -> None:
    tue = MON + timedelta(days=1)
    add_flight(conn, 9500076, tue + timedelta(hours=8), tue + timedelta(hours=10), airborne=60)               # Tuesday 08:00 to 10:00
    sun = MON - timedelta(days=1)
    add_flight(conn, 9500077, sun + timedelta(hours=8), sun + timedelta(hours=10), airborne=60)               # Sunday
    assert _bars(conn) == {}


def test_card_156_a_flight_is_counted_once_however_long_it_spans_the_window(conn) -> None:
    add_flight(conn, 9500078, MON + timedelta(hours=3), MON + timedelta(hours=16), airborne=600)             # spans it all
    add_flight(conn, 9500079, MON + timedelta(hours=8), MON + timedelta(hours=11), airborne=120, segments=2)  # one session, two flight rows
    assert _bars(conn) == {"10-2026": 2}


def test_card_156_a_flight_with_no_airborne_time_does_not_count(conn) -> None:
    add_flight(conn, 9500080, MON + timedelta(hours=8), MON + timedelta(hours=10), airborne=0)               # parked the whole time
    add_flight(conn, 9500081, MON + timedelta(hours=8), MON + timedelta(hours=10), airborne=0.5)             # half a minute of airborne time
    assert _bars(conn) == {"10-2026": 1}


def test_card_156_two_months_give_two_bars_in_date_order(conn) -> None:
    nov_mon = MON + timedelta(days=28)   # 02-11-2026: a Monday in the following UTC month
    add_flight(conn, 9500082, nov_mon + timedelta(hours=9), nov_mon + timedelta(hours=10), airborne=45)
    add_flight(conn, 9500083, nov_mon + timedelta(hours=9), nov_mon + timedelta(hours=11), airborne=45)
    add_flight(conn, 9500084, MON + timedelta(hours=9), MON + timedelta(hours=10), airborne=45)
    assert _bars(conn, end=nov_mon + timedelta(days=3)) == {"10-2026": 1, "11-2026": 2}


def test_card_156_by_month_counts_a_flight_on_two_mondays_once_not_twice(conn) -> None:
    """Issue 407: the monthly panel is not a sum of per-Monday counts. MON (05-10-2026) and NEXT_MON (12-10-2026) are
    both in October 2026. Flight 9500091 is connected at some moment in the window on both Mondays (one long flight
    spanning the week between them); flight 9500092 is connected on the first Monday only. The true distinct count
    for October is 2, not 3."""
    add_flight(conn, 9500091, MON + timedelta(hours=8), NEXT_MON + timedelta(hours=9), airborne=600)   # spans both Mondays
    add_flight(conn, 9500092, MON + timedelta(hours=9), MON + timedelta(hours=10), airborne=45)         # first Monday only
    assert _bars(conn) == {"10-2026": 2}


def test_card_156_respects_the_time_range(conn) -> None:
    add_flight(conn, 9500085, MON + timedelta(hours=9), MON + timedelta(hours=10), airborne=45)
    add_flight(conn, 9500086, NEXT_MON + timedelta(hours=9), NEXT_MON + timedelta(hours=10), airborne=45)
    assert _bars(conn, start=NEXT_MON - timedelta(days=1), end=NEXT_MON + timedelta(days=1)) == {"10-2026": 1}
    assert _bars(conn, start=MON + timedelta(hours=8)) == {"10-2026": 1}           # a range that starts after this Monday's 0700Z leaves it out


def test_card_156_reads_the_flight_summary_not_raw_positions() -> None:
    p = panel(EVENTS, "Unique aircraft airborne during the Monday event window")
    sql = p["targets"][0]["rawSql"].lower()
    for word in ("pilot_sample", "pilot_position", "sector", "frequency", "now()"):
        assert word not in sql
    assert p["type"] == "barchart" and "timeFrom" not in p and p["title"] == TITLE
