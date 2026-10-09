"""T14: pilot and demographic cards re-checked against the owner's current v1 Metabase SQL (08-10-2026).

Flights and controller sessions are inserted synthetically (flight_summary / controller_session rows relative
to now()), then the panel SQL is read from the dashboard JSON and executed, so the tests cover the SQL that
Grafana runs. Grafana's $__timeFrom() / $__timeTo() macros are replaced with now() minus the panel's
`timeFrom` (or 30 days, the dashboard default) and now().

Rules under test:
  card 61  highest groundspeed, 7 days, one row per callsign; peak above 100 kt, highest altitude above 1000 ft,
           destination filed; military, supersonic, business jet and turboprop types out, plus the explicit v1
           codes (Embraer E-series); other regional jets stay in (v1 does not exclude them)
  card 93  active pilots (30+ airborne minutes) and controllers (30+ minutes, session started OR ended in
           90 days) with one bar per rating, v1 names (11 = Administrator, anything else Unknown)
  card 129 ten dates with the most distinct pilots by LOGON date (UTC), 6 months, weekday name and rank
  card 135 distinct pilots per aircraft category over 6 months, flights with airborne time and an aircraft type
  card 55  movements stay as v1 (ADR-018): parked connections still count; checked on the existing panel
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO

pytestmark = pytest.mark.integration

TRAFFIC = REPO / "grafana" / "dashboards" / "pilots" / "pilots-traffic.json"
ORIGINS = REPO / "grafana" / "dashboards" / "demographics" / "pilot-origins.json"

SPEED = "Highest groundspeeds (last 7 days)"
PILOTS_BY_AIRCRAFT = "Most popular aircraft by pilots (last 6 months)"
DAYS = "Busiest days by pilots online (last 6 months)"
ACTIVE = "Active pilots and controllers (last 90 days, log scale)"
AIRPORTS = "Busiest home-region airports (last 30 days)"


def panel(path, title: str) -> dict:
    dash = json.loads(path.read_text(encoding="utf-8"))
    return next(p for p in dash["panels"] if p.get("title") == title)


def run_panel(conn, path, title: str) -> list[dict]:
    p = panel(path, title)
    days = int(str(p.get("timeFrom", "30d")).rstrip("d"))
    sql = (p["targets"][0]["rawSql"]
           .replace("$__timeFrom()", f"(now() - interval '{days * 24} hours')")  # hours: day intervals shift by an hour across DST in a non-UTC session
           .replace("$__timeTo()", "now()"))
    return conn.execute(sql).fetchall()


_serial = {"n": 0}


def add_flight(conn, cid: int, *, callsign: str | None = None, dep="YSSY", arr="YMML", aircraft="B738",
               days_ago: float = 1.0, minutes: int = 120, airborne: float | None = 100, gs: int | None = 450,
               alt: int | None = 35000, logon_days_ago: float | None = None) -> int:
    """One pilot session plus its flight_summary row. The flight ends ``days_ago`` days ago."""
    _serial["n"] += 1
    end = datetime.now(UTC) - timedelta(days=days_ago)
    start = end - timedelta(minutes=minutes)
    logon = datetime.now(UTC) - timedelta(days=logon_days_ago) if logon_days_ago is not None else start
    logon = min(logon, start)
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, name, first_seen, last_seen, departure, arrival, aircraft_icao) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (cid, callsign or f"TST{_serial['n']:04d}", logon.replace(microsecond=0), f"Pilot {cid}", logon, end, dep, arr, aircraft),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO flight_summary (session_id, gap_segment, span_part, cid, start_time, end_time, sample_count, "
        "airborne_minutes, max_gs_kt, max_alt_ft) VALUES (%s,0,0,%s,%s,%s,10,%s,%s,%s)",
        (sid, cid, start, end, airborne, gs, alt),
    )
    return sid


def add_controller(conn, cid: int, rating: int, *, start_days_ago: float, minutes: int) -> None:
    start = datetime.now(UTC) - timedelta(days=start_days_ago)
    end = start + timedelta(minutes=minutes)
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (cid, f"TST{cid}_TWR", start.replace(microsecond=0), f"Controller {cid}", rating, start, end),
    )


def people(conn) -> dict[str, int]:
    return {r["label"]: r["people"] for r in run_panel(conn, ORIGINS, ACTIVE)}


def controllers_only(conn) -> dict[str, int]:
    got = people(conn)
    got.pop("Pilots")
    return got


def utc_date_days_ago(days: int):
    return (datetime.now(UTC) - timedelta(days=days)).date()


# ---------------------------------------------------------------- card 61

def test_card_61_filters_follow_current_v1(conn):
    add_flight(conn, 9100001, callsign="FAST1", aircraft="B748", gs=650)                  # kept, fastest
    add_flight(conn, 9100002, callsign="MID1", aircraft="CRJ9", gs=480)                   # regional jet: kept (v1)
    add_flight(conn, 9100003, callsign="BIZ1", aircraft="C550", gs=690)                   # business jet: out
    add_flight(conn, 9100004, callsign="EMB1X", aircraft="E190", gs=600)                  # v1 explicit E-series: out
    add_flight(conn, 9100005, callsign="MIL1", aircraft="F16", gs=680)                    # military: out
    add_flight(conn, 9100006, callsign="TURBO1", aircraft="DH8Q", gs=620)                 # explicit v1 code: out
    add_flight(conn, 9100007, callsign="LOWALT", aircraft="A388", gs=640, alt=800)        # never above 1000 ft: out
    add_flight(conn, 9100008, callsign="SLOW1", aircraft="A388", gs=90)                   # peak not above 100 kt: out
    add_flight(conn, 9100009, callsign="OLD1", aircraft="A388", gs=700, days_ago=9)       # older than 7 days: out
    add_flight(conn, 9100010, callsign="NOARR", aircraft="A388", gs=660, arr="")          # no destination: out
    add_flight(conn, 9100011, callsign="MIGX", aircraft="MIG29X", gs=670)                 # MIG% pattern: out
    add_flight(conn, 9100012, callsign="NOTYPE", aircraft=None, gs=660)                   # NULL type: out (v1 NOT IN)
    rows = run_panel(conn, TRAFFIC, SPEED)
    assert [r["callsign"] for r in rows] == ["FAST1", "MID1"]
    assert rows[0]["Peak groundspeed (kt)"] == 650


def test_card_61_one_row_per_callsign_with_the_highest_speed(conn):
    add_flight(conn, 9100001, callsign="DUP1", aircraft="B77W", gs=500, days_ago=2)
    add_flight(conn, 9100001, callsign="DUP1", aircraft="B77W", gs=580, days_ago=1)
    rows = run_panel(conn, TRAFFIC, SPEED)
    assert len(rows) == 1 and rows[0]["Peak groundspeed (kt)"] == 580


def test_card_61_limited_to_twenty(conn):
    for i in range(22):
        add_flight(conn, 9100100 + i, callsign=f"TOP{i:02d}", aircraft="B77W", gs=400 + i)
    rows = run_panel(conn, TRAFFIC, SPEED)
    assert len(rows) == 20 and rows[0]["Peak groundspeed (kt)"] == 421


# ---------------------------------------------------------------- card 135

def test_card_135_six_month_window_and_filters(conn):
    add_flight(conn, 9200001, aircraft="B738", days_ago=100)                 # inside 6 months (was outside 30 days)
    add_flight(conn, 9200001, aircraft="B738", days_ago=5)                   # same pilot again: counted once
    add_flight(conn, 9200002, aircraft="B738", days_ago=3)
    add_flight(conn, 9200003, aircraft="A359", days_ago=3)
    add_flight(conn, 9200004, aircraft="B738", days_ago=250)                 # older than 6 months: out
    add_flight(conn, 9200005, aircraft="B738", days_ago=3, airborne=0)       # never airborne: out (v1 enroute > 0)
    add_flight(conn, 9200006, aircraft="", days_ago=3)                       # no aircraft type: out
    add_flight(conn, 9200007, aircraft="ZZ99", days_ago=3)                   # not in the lookup: Other
    got = {r["category"]: r["pilots"] for r in run_panel(conn, TRAFFIC, PILOTS_BY_AIRCRAFT)}
    assert got["Boeing 737 family"] == 2
    assert got["Airbus A350 family"] == 1
    assert got["Other / uncategorised"] == 1
    assert sum(got.values()) == 4


def test_card_135_ordered_by_pilots(conn):
    for cid in (9200001, 9200002):
        add_flight(conn, cid, aircraft="B738")
    add_flight(conn, 9200003, aircraft="A359")
    rows = run_panel(conn, TRAFFIC, PILOTS_BY_AIRCRAFT)
    assert [r["pilots"] for r in rows] == sorted((r["pilots"] for r in rows), reverse=True)


# ---------------------------------------------------------------- card 129

def test_card_129_counts_by_logon_date_with_rank(conn):
    # Three pilots log on 10 days ago, two on 20 days ago (one of them twice), one on 30 days ago.
    for cid in (9300001, 9300002, 9300003):
        add_flight(conn, cid, logon_days_ago=10, days_ago=9.9)
    add_flight(conn, 9300004, logon_days_ago=20, days_ago=19.9)
    add_flight(conn, 9300004, logon_days_ago=20, days_ago=19.8)
    add_flight(conn, 9300005, logon_days_ago=20, days_ago=19.9)
    add_flight(conn, 9300006, logon_days_ago=30, days_ago=29.9)
    rows = run_panel(conn, TRAFFIC, DAYS)
    assert [r["unique_pilots"] for r in rows] == [3, 2, 1]
    assert [r["rank"] for r in rows] == [1, 2, 3]
    assert rows[0]["member_date"] == utc_date_days_ago(10)
    assert rows[0]["day_name"] == utc_date_days_ago(10).strftime("%A")


def test_card_129_is_dated_by_logon_not_flight_start(conn):
    # Logged on 15 days ago, flight ended 13.9 days ago: the logon day is the one counted.
    add_flight(conn, 9300001, logon_days_ago=15, days_ago=13.9, minutes=120)
    rows = run_panel(conn, TRAFFIC, DAYS)
    assert [r["member_date"] for r in rows] == [utc_date_days_ago(15)]


def test_card_129_top_ten_and_six_month_window(conn):
    for i in range(12):
        add_flight(conn, 9300100 + i, logon_days_ago=5 + i, days_ago=4.9 + i)
    add_flight(conn, 9300200, logon_days_ago=200, days_ago=199.9)        # outside 180 days
    rows = run_panel(conn, TRAFFIC, DAYS)
    assert len(rows) == 10
    assert utc_date_days_ago(200) not in {r["member_date"] for r in rows}


# ---------------------------------------------------------------- card 93

def test_card_93_pilots_need_thirty_airborne_minutes(conn):
    add_flight(conn, 9400001, airborne=40, days_ago=10)
    add_flight(conn, 9400001, airborne=45, days_ago=11)                  # same pilot: once
    add_flight(conn, 9400002, airborne=20, days_ago=10)                  # too short
    add_flight(conn, 9400003, airborne=60, days_ago=120)                 # outside 90 days
    assert people(conn)["Pilots"] == 1


def test_card_93_controllers_by_v1_rating_names(conn):
    add_controller(conn, 9410001, 3, start_days_ago=10, minutes=40)      # S2
    add_controller(conn, 9410002, 8, start_days_ago=10, minutes=40)      # I1 (no longer folded into I1+)
    add_controller(conn, 9410003, 8, start_days_ago=11, minutes=40)      # second I1
    add_controller(conn, 9410004, 11, start_days_ago=10, minutes=40)     # feed name for 11: SUP
    add_controller(conn, 9410005, 12, start_days_ago=10, minutes=40)     # feed name for 12: ADM
    add_controller(conn, 9410008, 13, start_days_ago=10, minutes=40)     # not a known rating: Unknown
    add_controller(conn, 9410006, 3, start_days_ago=10, minutes=20)      # under 30 minutes: out
    add_controller(conn, 9410007, 3, start_days_ago=120, minutes=40)     # ended long before 90 days: out
    assert controllers_only(conn) == {"S2": 1, "I1": 2, "Supervisor": 1, "Administrator": 1, "Unknown": 1}


def test_card_93_session_ending_inside_the_window_counts(conn):
    # Started 90 days and 10 minutes ago, 60 minutes long: it ended inside the window (v1: start OR end).
    add_controller(conn, 9410010, 5, start_days_ago=90 + 10 / 1440, minutes=60)
    assert controllers_only(conn) == {"C1": 1}


# ---------------------------------------------------------------- card 55 (unchanged, ADR-018)

def test_card_55_parked_connections_still_count(conn):
    # No airborne time and no altitude: still one departure and one arrival (ADR-018).
    add_flight(conn, 9500001, dep="YSSY", arr="YMML", airborne=0, gs=0, alt=0, days_ago=2)
    add_flight(conn, 9500002, dep="YSSY", arr="YSSY", airborne=5, days_ago=2)
    got = {r["airport"]: (r["departures"], r["arrivals"]) for r in run_panel(conn, TRAFFIC, AIRPORTS)}
    assert got["YSSY"] == (2, 1)
    assert got["YMML"] == (0, 1)


def test_every_changed_panel_query_runs(conn):
    for path, title in [(TRAFFIC, SPEED), (TRAFFIC, PILOTS_BY_AIRCRAFT), (TRAFFIC, DAYS), (ORIGINS, ACTIVE)]:
        run_panel(conn, path, title)
