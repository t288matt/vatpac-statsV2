"""Group A of the database-questions catalogue (issue #267, 04-11-2026): the dashboard "Pilots: records and totals".

New queries under test: A2 (most flights), A4 (longest flights), A5 (flights per week), A10 (average flight length).
Panels copied from existing dashboards (A1, A3, A6 to A9) are checked to be identical to their source panel; their rules
are asserted by the tests of the original cards (test_pilots_people_t15.py, test_audit_t30.py, test_online_hours.py ...).

Expected numbers are worked out by hand from synthetic flights, never by the SQL under test. Flights are written as
flight_summary rows (record_flight), which is what the `flight` and `flight_ext` views read.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO
from helpers import add_flight, record_flight

pytestmark = pytest.mark.integration

DASH = REPO / "grafana" / "dashboards"
RECORDS = DASH / "pilots" / "pilots-records.json"
MOST_FLIGHTS = "Pilots with the most flights (last 30 days)"
LONGEST = "Longest flights (last 30 days)"
WEEKLY = "Flights finished per week"
AVERAGE = "Average flight length (7 days, 30 days, since go-live)"
_n = {"i": 0}


def panels(path=RECORDS) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["panels"]


def sql_of(title: str, frm: datetime | None = None) -> str:
    now = datetime.now(UTC)
    frm, to = frm or now - timedelta(days=365), now + timedelta(days=1)
    p = next(p for p in panels() if p.get("title") == title)
    return p["targets"][0]["rawSql"].replace("$__timeFrom()", f"'{frm.isoformat()}'").replace("$__timeTo()", f"'{to.isoformat()}'")


def monday(weeks_ago: int) -> datetime:
    """00:00 UTC on the Monday `weeks_ago` weeks before the current week."""
    now = datetime.now(UTC)
    return datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=now.weekday()) - timedelta(weeks=weeks_ago)


def flight_ending(conn, cid: int, end: datetime, airborne: float | None = 60, name: str | None = None) -> None:
    """One flight that finished at ``end`` (a 90 minute connection)."""
    _n["i"] += 1
    start = end - timedelta(minutes=90)
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, name, first_seen, last_seen, departure, arrival) "
        "VALUES (%s,%s,%s,%s,%s,%s,'YSSY','YMML') RETURNING id",
        (cid, f"PR{_n['i']:05d}", start.replace(microsecond=0), name or f"Pilot {cid}", start, end),
    ).fetchone()["id"]
    record_flight(conn, sid, start, end, airborne=airborne)


# ------------------------------------------------------------------ the dashboard itself
def test_dashboard_has_the_catalogue_questions_and_no_inputs():
    d = json.loads(RECORDS.read_text(encoding="utf-8"))
    assert d["uid"] == "vatpac-pilots-records" and d["title"] == "Pilots: records and totals"
    assert [v["name"] for v in d["templating"]["list"]] == ["go_live"]       # no inputs: only the hidden constant
    questions = sorted({p["description"].split()[1] for p in d["panels"] if p.get("description")})
    assert questions == sorted(f"A{i}" for i in range(1, 11))                 # every question A1 to A10 has a panel
    assert all("Definition" in p["description"] for p in d["panels"] if p.get("description"))


RESTYLED = {("pilots/pilots-now.json", "When are people online on weekdays (last 30 days)"),
            ("pilots/pilots-now.json", "When are people online on weekends (last 30 days)")}


@pytest.mark.parametrize("title,src", [
    ("Which member spent the most time online? (last 7 days)", "pilots/pilots-traffic.json"),      # A1, card 155
    ("Pilots with the most diverse routes (last 14 days)", "pilots/pilots-traffic.json"),           # A3, card 46
    ("Active pilots and controllers (last 90 days, log scale)", "demographics/pilot-origins.json"),  # A6, card 93
    ("Busiest days by flights (last 6 months)", "pilots/pilots-traffic.json"),                       # A7, card 128
    ("Busiest days by pilots online (last 6 months)", "pilots/pilots-traffic.json"),                # A8, card 129
    ("When are people online on weekdays (last 30 days)", "pilots/pilots-now.json"),               # A9, card 94
    ("When are people online on weekends (last 30 days)", "pilots/pilots-now.json"),               # A9, card 95
])
def test_copied_panels_run_the_same_sql_and_window_as_their_source(title, src):
    mine = next(p for p in panels() if p.get("title") == title)
    theirs = next(p for p in panels(DASH / src) if p.get("title") == title)
    assert mine["targets"][0]["rawSql"] == theirs["targets"][0]["rawSql"]
    assert mine.get("timeFrom") == theirs.get("timeFrom")
    if (src, title) in RESTYLED:
        return          # issue 280: restyled on pilots-now (bar chart); the query and the window must stay the same
    assert mine["type"] == theirs["type"]


# ------------------------------------------------------------------ A2: most flights
def test_a2_ranks_pilots_by_flights_in_thirty_days_and_breaks_ties_by_cid(conn):
    for _ in range(3):
        add_flight(conn, 9400002, days_ago=2, name="Busy Pilot")           # 3 flights
    for cid in (9400004, 9400003):                                          # two pilots with 2: lower CID first
        for _ in range(2):
            add_flight(conn, cid, days_ago=5, name=f"Pilot {cid}")
    add_flight(conn, 9400003, days_ago=29.9, name="Pilot 9400003")          # inside the window: makes 3 flights for 9400003
    for _ in range(5):
        add_flight(conn, 9400005, days_ago=30.1, name="Too Old")            # outside the window: must not count
    add_flight(conn, 9400006, days_ago=1, airborne=0, name="Parked Pilot")      # parked flights are not counted: this pilot is absent
    add_flight(conn, 9400006, days_ago=1, airborne=None, name="Parked Pilot")
    add_flight(conn, 9400004, days_ago=1, airborne=0, name="Pilot 9400004")     # parked flights add nothing to a flying pilot (2, not 4)
    add_flight(conn, 9400004, days_ago=1, airborne=None, name="Pilot 9400004")
    rows = conn.execute(sql_of(MOST_FLIGHTS)).fetchall()
    assert [(r["Pilot"], r["Flights"]) for r in rows] == [
        ("Busy Pilot", 3), ("Pilot 9400003", 3), ("Pilot 9400004", 2)]


def test_a2_shows_ten_rows_at_most_and_the_latest_name(conn):
    for i in range(12):
        add_flight(conn, 9401000 + i, days_ago=3)
    add_flight(conn, 9401000, days_ago=4, name="Old Name")
    add_flight(conn, 9401000, days_ago=1, name="New Name")                  # cid 9401000 has 3 flights: first, latest name
    rows = conn.execute(sql_of(MOST_FLIGHTS)).fetchall()
    assert len(rows) == 10
    assert (rows[0]["Pilot"], rows[0]["Flights"]) == ("New Name", 3)


# ------------------------------------------------------------------ A4: longest flight
def test_a4_orders_by_airborne_hours_and_skips_old_and_parked_flights(conn):
    add_flight(conn, 9402001, days_ago=2, airborne=300, name="Five Hours", callsign="QFA1", dep="YSSY", arr="EGLL")
    add_flight(conn, 9402002, days_ago=3, airborne=612, name="Ten Hours Plus", callsign="QFA2", dep="YPPH", arr="EGLL")
    add_flight(conn, 9402003, days_ago=4, airborne=None, name="Unmeasured")       # no airborne figure (parked): left out
    add_flight(conn, 9402004, days_ago=31, airborne=900, name="Too Old")           # finished 31 days ago: left out
    add_flight(conn, 9402005, days_ago=1, airborne=0, name="Parked")               # 0.0 hours: left out
    rows = conn.execute(sql_of(LONGEST)).fetchall()
    assert [r["Pilot"] for r in rows] == ["Ten Hours Plus", "Five Hours"]
    assert [float(r["Airborne hours"]) for r in rows] == [10.2, 5.0]            # 612 / 60 = 10.2
    assert (rows[0]["Callsign"], rows[0]["Route"]) == ("QFA2", "YPPH-EGLL")
    end = conn.execute("SELECT end_time FROM flight WHERE cid = 9402002").fetchone()["end_time"]
    assert rows[0]["Finished (UTC)"] == end.astimezone(UTC).strftime("%d-%m-%Y %H:%M")


def test_a4_shows_ten_rows_at_most_and_breaks_ties_by_latest_finish(conn):
    for i in range(11):
        add_flight(conn, 9403000 + i, days_ago=2 + i / 10, airborne=120, name=f"Pilot {i}")  # all 2.0 hours
    rows = conn.execute(sql_of(LONGEST)).fetchall()
    assert [r["Pilot"] for r in rows] == [f"Pilot {i}" for i in range(10)]            # later finish (smaller days_ago) first


# ------------------------------------------------------------------ A5: flights per week
def weekly(conn, frm: datetime | None = None) -> dict[datetime, int]:
    return {r["time"]: r["flights"] for r in conn.execute(sql_of(WEEKLY, frm)).fetchall()}


def test_a5_counts_flights_by_the_week_they_finished_and_shows_empty_weeks_as_zero(conn):
    w2, w3, w4 = monday(4), monday(3), monday(2)
    flight_ending(conn, 9410001, w2 + timedelta(hours=1))                                   # Monday 01:00
    flight_ending(conn, 9410002, w2 + timedelta(days=3))
    flight_ending(conn, 9410003, w2 + timedelta(days=7) - timedelta(minutes=1))             # Sunday 23:59, still the week of w2
    flight_ending(conn, 9410004, w4 + timedelta(days=1), airborne=0)                        # parked flights are not counted
    flight_ending(conn, 9410006, w4 + timedelta(days=2), airborne=None)
    flight_ending(conn, 9410005, w4 + timedelta(days=7))                                    # Monday 00:00 of the next week: not w4
    got = weekly(conn, frm=w2)
    assert got[w2] == 3 and got[w3] == 0 and got[w4] == 0 and got[monday(1)] == 1
    assert sum(got.values()) == 4


def test_a5_the_first_week_is_whole_when_the_range_starts_mid_week(conn):
    w = monday(3)
    flight_ending(conn, 9411001, w + timedelta(hours=2))                                    # before the range start, same week
    flight_ending(conn, 9411002, w + timedelta(days=4))
    assert weekly(conn, frm=w + timedelta(days=3))[w] == 2


def test_a5_weeks_follow_utc_even_when_the_session_time_zone_is_sydney(conn):
    w = monday(3)
    flight_ending(conn, 9412001, w + timedelta(hours=1))        # Monday 01:00 UTC
    flight_ending(conn, 9412002, w - timedelta(hours=1))        # Sunday 23:00 UTC is Monday 09:00 Sydney: must stay in the earlier week
    conn.execute("SET TIME ZONE 'Australia/Sydney'")
    try:
        got = weekly(conn, frm=w - timedelta(days=7))
    finally:
        conn.execute("RESET TIME ZONE")
    assert got[w] == 1 and got[w - timedelta(days=7)] == 1


# ------------------------------------------------------------------ A10: average flight length
def test_a10_averages_airborne_minutes_per_window_and_leaves_out_flights_with_none(conn):
    add_flight(conn, 9420001, days_ago=2, airborne=60)       # in all three windows
    add_flight(conn, 9420002, days_ago=10, airborne=120)     # 30 days and since go-live
    add_flight(conn, 9420003, days_ago=20, airborne=180)     # 30 days and since go-live
    add_flight(conn, 9420004, days_ago=100, airborne=240)    # since go-live only
    add_flight(conn, 9420005, days_ago=1, airborne=0)        # no airborne time: left out
    add_flight(conn, 9420006, days_ago=1, airborne=None)     # no figure: left out
    rows = {r["Period"]: r for r in conn.execute(sql_of(AVERAGE)).fetchall()}
    assert list(rows) == ["Last 7 days", "Last 30 days", "Since go-live"]
    assert [(float(rows[k]["Average airborne minutes"]), rows[k]["Flights with airborne time"]) for k in rows] == [
        (60.0, 1), (120.0, 3), (150.0, 4)]
    assert float(rows["Since go-live"]["Average airborne hours"]) == 2.5


def test_a10_with_no_flights_shows_three_rows_with_zero_flights_and_no_average(conn):
    rows = conn.execute(sql_of(AVERAGE)).fetchall()
    assert [(r["Period"], r["Flights with airborne time"], r["Average airborne minutes"]) for r in rows] == [
        ("Last 7 days", 0, None), ("Last 30 days", 0, None), ("Since go-live", 0, None)]


# ------------------------------------------------------------------ every new query as the read-only role
def test_new_queries_run_as_grafana_ro_within_the_statement_limit(conn):
    conn.execute("SET ROLE grafana_ro")
    conn.execute("SET statement_timeout = '30s'")        # SET ROLE does not apply the role's own settings; state the limit
    try:
        for title in (MOST_FLIGHTS, LONGEST, WEEKLY, AVERAGE):
            assert isinstance(conn.execute(sql_of(title)).fetchall(), list)
    finally:
        conn.execute("RESET statement_timeout")
        conn.execute("RESET ROLE")


# ------------------------------------------------------------------ review fixes: window edges and tie-breaks
def test_a10_window_edges_6_9_and_7_1_days_and_29_9_and_30_1_days(conn):
    add_flight(conn, 9430001, days_ago=6.9, airborne=60)     # inside 7 days
    add_flight(conn, 9430002, days_ago=7.1, airborne=120)    # outside 7 days, inside 30
    add_flight(conn, 9430003, days_ago=29.9, airborne=180)   # inside 30 days
    add_flight(conn, 9430004, days_ago=30.1, airborne=240)   # outside 30 days
    rows = {r["Period"]: r for r in conn.execute(sql_of(AVERAGE)).fetchall()}
    assert [(float(r["Average airborne minutes"]), r["Flights with airborne time"]) for r in rows.values()] == [
        (60.0, 1), (120.0, 3), (150.0, 4)]


def test_a2_ties_are_broken_by_the_ten_lowest_cids_whatever_the_insert_order(conn):
    for cid in range(9440040, 9440000, -1):          # 40 pilots, one flight each, the highest cid inserted first
        add_flight(conn, cid, days_ago=3, name=f"Pilot {cid}")
    rows = conn.execute(sql_of(MOST_FLIGHTS)).fetchall()
    assert [r["Pilot"] for r in rows] == [f"Pilot {c}" for c in range(9440001, 9440011)]


def test_a4_ties_on_hours_and_finish_are_broken_by_session_id(conn):
    end = datetime.now(UTC).replace(microsecond=0) - timedelta(days=2)
    for cid in range(9450001, 9450012):               # 11 flights, same airborne time and same finish time
        flight_ending(conn, cid, end, airborne=120, name=f"Pilot {cid}")
    for sid in conn.execute("SELECT session_id FROM flight_summary ORDER BY session_id DESC").fetchall():
        conn.execute("UPDATE flight_summary SET written_at = now() WHERE session_id = %s", (sid["session_id"],))
    rows = conn.execute(sql_of(LONGEST)).fetchall()   # heap order is now descending; the query must still give ascending ids
    assert [r["Pilot"] for r in rows] == [f"Pilot {c}" for c in range(9450001, 9450011)]
