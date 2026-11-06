"""Groups B and C of the database-questions catalogue (issue #267, 04-11-2026): the dashboards "Routes and airports" and
"Aircraft and airlines".

New queries under test: B5 (departures), B6 (arrivals), B8 (international arrivals), B10 (busiest airport, rolling 24 hours and
7 days), C3 (aircraft types), C5 (airline airborne hours). B7 was dropped by the owner. Panels copied from existing dashboards are
checked to be identical to their source panel (SQL, window, type, options); their rules are asserted by the tests of the original
cards. Expected numbers are worked out by hand from synthetic flights, never by the SQL under test.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import REPO
from helpers import add_flight

pytestmark = pytest.mark.integration

DASH = REPO / "grafana" / "dashboards"
ROUTES = DASH / "pilots" / "pilots-routes-airports.json"
AIRCRAFT = DASH / "pilots" / "pilots-aircraft-airlines.json"
B5, B6 = "Airports with the most departures (last 30 days, top 10)", "Airports with the most arrivals (last 30 days, top 10)"
B8 = "Australian airports with the most international arrivals (last 30 days, top 10)"
B10_DAY = "Busiest airports, departures plus arrivals (rolling last 24 hours, top 10)"
B10_WEEK = "Busiest airports, departures plus arrivals (rolling last 7 days, top 10)"
C3 = "Most popular aircraft types (last 30 days, top 10)"
C5 = "Airlines with the most airborne hours (last 30 days, top 10)"
HOUR = 1 / 24


def dashboard(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def panel(path: Path, title: str) -> dict:
    return next(p for p in dashboard(path)["panels"] if p.get("title") == title)


def rows(conn, path: Path, title: str) -> list[dict]:
    return conn.execute(panel(path, title)["targets"][0]["rawSql"]).fetchall()


def window(path: Path, p: dict) -> str:
    """The time range a panel really uses: its own override or the dashboard default."""
    return f"now-{p['timeFrom']}" if p.get("timeFrom") else dashboard(path)["time"]["from"]


# ------------------------------------------------------------------ the dashboards themselves
def test_routes_dashboard_has_the_catalogue_questions_and_no_inputs():
    d = dashboard(ROUTES)
    assert d["uid"] == "vatpac-routes-airports" and d["title"] == "Routes and airports"
    assert [v["name"] for v in d["templating"]["list"]] == ["go_live"]              # no inputs: only the hidden constant
    questions = sorted({p["description"].split()[1] for p in d["panels"] if p.get("description")})
    assert questions == ["B1", "B10", "B2", "B3", "B4", "B5", "B6", "B8", "B9"]        # B7 was dropped by the owner
    assert [p["type"] != "text" for p in d["panels"]].count(True) == 12
    assert all("Definition" in p["description"] for p in d["panels"] if p.get("description"))


def test_aircraft_dashboard_has_the_catalogue_questions_and_no_inputs():
    d = dashboard(AIRCRAFT)
    assert d["uid"] == "vatpac-aircraft-airlines" and d["title"] == "Aircraft and airlines"
    assert [v["name"] for v in d["templating"]["list"]] == ["go_live"]
    questions = sorted({p["description"].split()[1] for p in d["panels"] if p.get("description")})
    assert questions == sorted(f"C{i}" for i in range(1, 11))
    assert [p["type"] != "text" for p in d["panels"]].count(True) == 11
    assert all("Definition" in p["description"] for p in d["panels"] if p.get("description"))


COPIES = [
    (ROUTES, "Most popular domestic routes (last 30 days)", "pilots/pilots-traffic.json"),                  # B1, card 97
    (ROUTES, "Most popular international routes (last 30 days)", "pilots/pilots-traffic.json"),             # B2, card 45
    (ROUTES, "Most popular VFR routes (last 30 days)", "pilots/pilots-traffic.json"),                       # B3, card 59
    (ROUTES, "Busiest home-region airports (last 30 days)", "pilots/pilots-traffic.json"),                  # B4, card 55
    (ROUTES, "Where pilots currently online come from", "demographics/pilot-origins.json"),                # B9, card 99
    (ROUTES, "Who flies most in Australian airspace: flights by home region (last 30 days)", "demographics/pilot-origins.json"),     # card 100
    (ROUTES, "Who spends most time in Australian airspace: airborne hours by home region (last 30 days)", "demographics/pilot-origins.json"),  # 101
    (AIRCRAFT, "Most popular aircraft by flights (last 6 months)", "pilots/pilots-traffic.json"),           # C1, card 90
    (AIRCRAFT, "Most popular aircraft by pilots (last 6 months)", "pilots/pilots-traffic.json"),            # C2, card 135
    (AIRCRAFT, "Most popular airlines (last 30 days)", "pilots/pilots-traffic.json"),                       # C4, card 58
    (AIRCRAFT, "Most popular callsigns (last 30 days)", "pilots/pilots-traffic.json"),                      # C6, card 57
    (AIRCRAFT, "Most popular military aircraft (unique pilots, last 7 days)", "military/military.json"),    # C7, card 65
    (AIRCRAFT, "Hours flown by RAAFv aircraft per week (last 14 weeks)", "military/military.json"),        # C8, card 66
    (AIRCRAFT, "Flights flown by RAAFv aircraft per week (last 14 weeks)", "military/military.json"),      # C8, card 184
    (AIRCRAFT, "Most popular RAAFv routes (last month)", "military/military.json"),                         # C9, card 78
    (AIRCRAFT, "Highest groundspeeds (last 7 days)", "pilots/pilots-traffic.json"),                         # C10, card 61
]


# Source panels not yet restyled (issue 280); only type, options and fieldConfig of the copy may differ. Empty since batch 4, when the last source
# pages (Pilot origins, Military) were restyled; add a (source, title) pair here only while a copy is restyled ahead of its source.
RESTYLED: set[tuple[str, str]] = set()


@pytest.mark.parametrize("path,title,src", COPIES)
def test_copied_panels_run_the_same_sql_window_and_display_as_their_source(path, title, src):
    mine, theirs = panel(path, title), panel(DASH / src, title)
    assert [t["rawSql"] for t in mine["targets"]] == [t["rawSql"] for t in theirs["targets"]]
    assert [t.get("format") for t in mine["targets"]] == [t.get("format") for t in theirs["targets"]]
    assert window(path, mine) == window(DASH / src, theirs)          # the source dashboard's default range, kept
    if (src, title) in RESTYLED:
        return          # issue 280, batch 1: restyled ahead of its source; query, format and window were compared above
    assert mine["type"] == theirs["type"] and mine["options"] == theirs["options"] and mine["fieldConfig"] == theirs["fieldConfig"]


def test_every_restyled_exemption_still_differs_from_its_copy():
    """Issue 280: an entry in RESTYLED is only allowed while a copy really differs from its source; remove it once they match again."""
    for src, title in sorted(RESTYLED):
        differs = [path for path, t, s in COPIES if (s, t) == (src, title)
                   and any(panel(path, title).get(k) != panel(DASH / src, title).get(k) for k in ("type", "options", "fieldConfig"))]
        assert differs, f"stale RESTYLED entry: {src}: {title}"

# ------------------------------------------------------------------ B5, B6: departures and arrivals
def b5_b6_flights(conn) -> None:
    for _ in range(3):
        add_flight(conn, 9500001, dep="YSSY", arr="YMML", days_ago=2)
    add_flight(conn, 9500002, dep="YMML", arr="YSSY", days_ago=2)
    for _ in range(2):
        add_flight(conn, 9500003, dep="YBBN", arr="YSSY", days_ago=2)
    for _ in range(2):
        add_flight(conn, 9500004, dep="YSCB", arr="YSSY", days_ago=2)
    add_flight(conn, 9500005, dep="YSSY", arr="YSSY", days_ago=2)                          # a circuit: one departure and one arrival
    add_flight(conn, 9500006, dep="EGLL", arr="YSSY", days_ago=2)                          # arrival counts; EGLL is not an Australian airport
    add_flight(conn, 9500007, dep="YSSY", arr="ZZZZ", days_ago=2)                          # ZZZZ: left out
    add_flight(conn, 9500007, dep="ZZZZ", arr="YSSY", days_ago=2)
    add_flight(conn, 9500008, dep="YSSY", arr="YMML", days_ago=2, airborne=0)              # parked: left out
    add_flight(conn, 9500008, dep="YSSY", arr="YMML", days_ago=2, airborne=None)
    add_flight(conn, 9500009, dep="YPPH", arr="YSSY", days_ago=29.95)                      # started 30.03 days ago: arrival counts, departure does not
    add_flight(conn, 9500010, dep="YSSY", arr="YMML", days_ago=30.2)                       # outside the window altogether


def test_b5_counts_departures_by_start_time_at_australian_airports_with_airborne_time_and_not_zzzz(conn):
    b5_b6_flights(conn)
    got = [(r["Airport"], r["Departures"]) for r in rows(conn, ROUTES, B5)]
    # YSSY 3 + circuit 1 = 4; YBBN 2 and YSCB 2 tie, lower code first; YMML 1. No EGLL, no YPPH (started outside), no ZZZZ.
    assert got == [("YSSY", 4), ("YBBN", 2), ("YSCB", 2), ("YMML", 1)]


def test_b6_counts_arrivals_by_end_time_at_australian_airports_with_airborne_time_and_not_zzzz(conn):
    b5_b6_flights(conn)
    got = [(r["Airport"], r["Arrivals"]) for r in rows(conn, ROUTES, B6)]
    # YSSY: 1 + 2 + 2 + circuit 1 + EGLL 1 + YPPH 1 = 8; YMML: 3. The ZZZZ, parked and 30.2-day flights add nothing.
    assert got == [("YSSY", 8), ("YMML", 3)]


@pytest.mark.parametrize("title,column", [(B5, "Departures"), (B6, "Arrivals")])
def test_b5_b6_show_ten_rows_at_most_and_break_ties_by_airport_code(conn, title, column):
    for i in range(12, 0, -1):                                           # 12 airports with one movement each, highest code first
        add_flight(conn, 9501000 + i, dep=f"Y{i:03d}", arr=f"Y{i:03d}", days_ago=3)
    got = rows(conn, ROUTES, title)
    assert [(r["Airport"], r[column]) for r in got] == [(f"Y{i:03d}", 1) for i in range(1, 11)]


# ------------------------------------------------------------------ B8: international arrivals
def test_b8_counts_arrivals_at_australian_airports_from_outside_the_region(conn):
    add_flight(conn, 9510001, dep="EGLL", arr="YSSY", days_ago=2)
    add_flight(conn, 9510001, dep="NZAA", arr="YSSY", days_ago=3)                          # same pilot: 2 flights, 1 pilot so far
    add_flight(conn, 9510002, dep="NZAA", arr="YSSY", days_ago=4)                          # second pilot: YSSY 3 flights, 2 pilots
    add_flight(conn, 9510003, dep="NZAA", arr="YMML", days_ago=2)
    add_flight(conn, 9510004, dep="WSSS", arr="YMML", days_ago=2)                          # YMML 2 flights, 2 pilots
    add_flight(conn, 9510005, dep="NZAA", arr="YPPH", days_ago=29.9)                       # inside the window: YPPH 1
    add_flight(conn, 9510006, dep="YSSY", arr="NZAA", days_ago=2)                          # leaving the region: not an arrival at an Australian airport
    add_flight(conn, 9510006, dep="YSSY", arr="YMML", days_ago=2)                          # domestic: not international
    add_flight(conn, 9510007, dep="NZAA", arr="YPPH", days_ago=2, airborne=0)              # parked
    add_flight(conn, 9510007, dep="NZAA", arr="YPPH", days_ago=2, airborne=None)
    add_flight(conn, 9510008, dep="NZAA", arr="ZZZZ", days_ago=2)                          # ZZZZ either end
    add_flight(conn, 9510008, dep="ZZZZ", arr="YSSY", days_ago=2)
    add_flight(conn, 9510009, dep="EGLL", arr="YSSY", days_ago=30.2)                       # too old
    add_flight(conn, 9510010, dep="NZAA", arr="YBBN", days_ago=29.95)                      # started 30.03 days ago, ended inside: counted (by end time)
    add_flight(conn, 9510011, dep=None, arr="YSSY", days_ago=2)                            # no filed departure: never an international arrival
    got = [(r["Airport"], r["International arrivals"], r["Pilots"]) for r in rows(conn, ROUTES, B8)]
    assert got == [("YSSY", 3, 2), ("YMML", 2, 2), ("YBBN", 1, 1), ("YPPH", 1, 1)]


def test_b8_shows_ten_rows_at_most_and_breaks_ties_by_airport_code(conn):
    for i in range(12, 0, -1):
        add_flight(conn, 9511000 + i, dep="NZAA", arr=f"Y{i:03d}", days_ago=3)
    assert [r["Airport"] for r in rows(conn, ROUTES, B8)] == [f"Y{i:03d}" for i in range(1, 11)]


# ------------------------------------------------------------------ B10: busiest airport, rolling 24 hours and 7 days
def b10_flights(conn) -> None:
    """Each flight ends `h` hours ago and starts two hours before that (add_flight's default 120 minutes)."""
    add_flight(conn, 9520001, dep="YSSY", arr="YMML", days_ago=3 * HOUR)                  # F1
    add_flight(conn, 9520002, dep="YMML", arr="YSSY", days_ago=10 * HOUR)                 # F2
    add_flight(conn, 9520003, dep="YSSY", arr="YBBN", days_ago=23 * HOUR)                 # F3: started 25 h ago, so its departure is outside 24 h
    add_flight(conn, 9520004, dep="YBBN", arr="YSSY", days_ago=25 * HOUR)                 # F4: outside 24 h, inside 7 days
    add_flight(conn, 9520005, dep="YSSY", arr="YMML", days_ago=2 * HOUR, airborne=0)      # F5: parked
    add_flight(conn, 9520006, dep="YSSY", arr="ZZZZ", days_ago=2 * HOUR)                  # F6: ZZZZ
    add_flight(conn, 9520007, dep="YPPH", arr="YSSY", days_ago=5)                         # F7: 7 days only
    add_flight(conn, 9520008, dep="YSSY", arr="YMML", days_ago=7.1)                       # F8: outside both
    add_flight(conn, 9520009, dep="YMML", arr="YBBN", days_ago=6.95)                      # F9: started 7.03 days ago, arrival inside 7 days
    add_flight(conn, 9520010, dep="EGLL", arr="YSSY", days_ago=1 * HOUR)                  # F10: only the arrival is at an Australian airport


def test_b10_rolling_24_hours_counts_departures_at_start_and_arrivals_at_end(conn):
    b10_flights(conn)
    got = [(r["Airport"], r["Movements"], r["Departures"], r["Arrivals"]) for r in rows(conn, ROUTES, B10_DAY)]
    # YSSY: dep F1 + arr F2, F10 = 3; YMML: dep F2 + arr F1 = 2; YBBN: arr F3 = 1 (F3's departure and all of F4 are older than 24 h).
    assert got == [("YSSY", 3, 1, 2), ("YMML", 2, 1, 1), ("YBBN", 1, 0, 1)]


def test_b10_rolling_7_days_counts_departures_at_start_and_arrivals_at_end(conn):
    b10_flights(conn)
    got = [(r["Airport"], r["Movements"], r["Departures"], r["Arrivals"]) for r in rows(conn, ROUTES, B10_WEEK)]
    # YSSY: dep F1, F3; arr F2, F4, F7, F10 = 6. YBBN: dep F4; arr F3, F9 = 3. YMML: dep F2; arr F1 = 2. YPPH: dep F7 = 1.
    # F8 is outside; F9's departure at YMML started 7.03 days ago so it is not counted.
    assert got == [("YSSY", 6, 2, 4), ("YBBN", 3, 1, 2), ("YMML", 2, 1, 1), ("YPPH", 1, 1, 0)]


@pytest.mark.parametrize("title", [B10_DAY, B10_WEEK])
def test_b10_shows_ten_rows_at_most_and_breaks_ties_by_airport_code(conn, title):
    for i in range(12, 0, -1):
        add_flight(conn, 9521000 + i, dep=f"Y{i:03d}", arr=f"Y{i:03d}", days_ago=6 * HOUR)    # 2 movements each
    assert [r["Airport"] for r in rows(conn, ROUTES, title)] == [f"Y{i:03d}" for i in range(1, 11)]


def test_b10_with_no_flights_is_an_empty_table_not_an_error(conn):
    assert rows(conn, ROUTES, B10_DAY) == [] and rows(conn, ROUTES, B10_WEEK) == []


# ------------------------------------------------------------------ C3: aircraft types
def test_c3_counts_flights_and_pilots_per_type_normalised_with_airborne_time_and_not_zzzz(conn):
    for cid in (9530001, 9530001, 9530002):
        add_flight(conn, cid, aircraft="B738", days_ago=2)                                  # B738: 3 flights, 2 pilots
    add_flight(conn, 9530003, aircraft="A388", days_ago=2)
    add_flight(conn, 9530004, aircraft="A388", days_ago=3)
    add_flight(conn, 9530006, aircraft=" a388 ", days_ago=4)                                # trimmed and upper-cased: A388 3 flights, 3 pilots
    for cid in (9530005, 9530005):
        add_flight(conn, cid, aircraft="A320", days_ago=2)                                  # A320: 2 flights, 1 pilot
    for _ in range(5):
        add_flight(conn, 9530007, aircraft="ZZZZ", days_ago=2)                              # the placeholder type is left out
    add_flight(conn, 9530008, aircraft=None, days_ago=2)                                    # no type filed
    add_flight(conn, 9530008, aircraft="", days_ago=2)
    for _ in range(4):
        add_flight(conn, 9530009, aircraft="B738", days_ago=2, airborne=0)                  # parked
    add_flight(conn, 9530009, aircraft="B738", days_ago=2, airborne=None)
    for _ in range(5):
        add_flight(conn, 9530010, aircraft="B738", days_ago=30.1)                           # outside the window
    add_flight(conn, 9530011, aircraft="C172", days_ago=29.95)                              # started 30.03 days ago, ended inside: counted (by end time)
    got = [(r["Aircraft type"], r["Flights"], r["Pilots"]) for r in rows(conn, AIRCRAFT, C3)]
    assert got == [("A388", 3, 3), ("B738", 3, 2), ("A320", 2, 1), ("C172", 1, 1)]                          # the A388/B738 tie goes to the lower code


def test_c3_shows_ten_rows_at_most_and_breaks_ties_by_type_code(conn):
    for i in range(12, 0, -1):
        add_flight(conn, 9531000 + i, aircraft=f"T{i:03d}", days_ago=3)
    assert [r["Aircraft type"] for r in rows(conn, AIRCRAFT, C3)] == [f"T{i:03d}" for i in range(1, 11)]


# ------------------------------------------------------------------ C5: airline airborne hours
def test_c5_ranks_airlines_by_airborne_hours_not_flights_and_ignores_parked_old_and_private_callsigns(conn):
    add_flight(conn, 9540001, callsign="QFA1", airborne=300, days_ago=2)                    # Qantas: 300 + 150 = 450 min = 7.5 h, 2 flights
    add_flight(conn, 9540002, callsign="QFA2", airborne=150, days_ago=3)
    add_flight(conn, 9540003, callsign="QFA3", airborne=0, days_ago=2)                      # parked: adds neither hours nor a flight
    add_flight(conn, 9540003, callsign="QFA4", airborne=None, days_ago=2)
    add_flight(conn, 9540004, callsign="QFA5", airborne=1000, days_ago=30.1)                # too old
    add_flight(conn, 9540005, callsign="JST1", airborne=200, days_ago=2)                    # Jetstar: 400 min = 6.666.. h -> 6.7, 2 flights
    add_flight(conn, 9540006, callsign="JST2", airborne=200, days_ago=2)
    add_flight(conn, 9540007, callsign="VOZ1", airborne=600, days_ago=2)                    # Virgin Australia: 10.0 h from one flight: first
    for n in range(3):
        add_flight(conn, 9540008, callsign=f"XYZ{n + 1}", airborne=90, days_ago=2)          # not in the airline list: shown by prefix, 4.5 h, 3 flights
    add_flight(conn, 9540009, callsign="VHABC", airborne=1000, days_ago=2)                  # a private registration is not an airline
    add_flight(conn, 9540010, callsign="ABC1", airborne=30, days_ago=29.95)                 # started 30.03 days ago, ended inside: counted (by end time)
    got = [(r["Airline"], float(r["Airborne hours"]), r["Flights"]) for r in rows(conn, AIRCRAFT, C5)]
    assert got == [("Virgin Australia", 10.0, 1), ("Qantas", 7.5, 2), ("Jetstar", 6.7, 2), ("XYZ", 4.5, 3), ("ABC", 0.5, 1)]


def test_c5_shows_ten_rows_at_most_and_breaks_ties_by_airline_prefix(conn):
    for i in range(11, -1, -1):
        add_flight(conn, 9541000 + i, callsign=f"ZA{chr(65 + i)}1", airborne=60, days_ago=3)   # 12 airlines with 1.0 h each
    assert [r["Airline"] for r in rows(conn, AIRCRAFT, C5)] == [f"ZA{chr(65 + i)}" for i in range(10)]


# ------------------------------------------------------------------ every new query as the read-only role
def test_new_queries_run_as_grafana_ro_within_the_statement_limit(conn):
    conn.execute("SET ROLE grafana_ro")
    conn.execute("SET statement_timeout = '30s'")        # SET ROLE does not apply the role's own settings; state the limit
    try:
        for path, titles in ((ROUTES, (B5, B6, B8, B10_DAY, B10_WEEK)), (AIRCRAFT, (C3, C5))):
            for title in titles:
                assert isinstance(rows(conn, path, title), list)
    finally:
        conn.execute("RESET statement_timeout")
        conn.execute("RESET ROLE")
