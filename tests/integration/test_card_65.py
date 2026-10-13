"""Card 65 (issue #16, T15): the military aircraft chart, rebuilt from v1's SQL (owner decision 08-10-2026).

v1's rule: distinct pilots (CIDs) per aircraft type, for types in v1's hand-written code list, over flights completed
in the last week; the share of each type is its pilots divided by the SUM of the per-type pilot counts (a pilot who
flew two types counts under both). v3 reads `flight_ext` (finished flights only: rows of flight_summary, issue 89 phase 3) and takes the code list from
`aircraft_type.in_v1_card65_list` (migration 20261009000160), which is deliberately not `is_military`.

The panel SQL is read from the dashboard JSON and run on synthetic flights. The parity test loads the same flights into
a temporary table shaped like v1's `flight_summaries` and runs v1's own SQL (from docs/reference/v1_metabase_cards.json)
against it.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal

import pytest
from conftest import REPO

from helpers import CID, add_flight, make_session, panel, session_ending, set_up_clock

pytestmark = pytest.mark.integration

MILITARY = REPO / "grafana" / "dashboards" / "military" / "military.json"
TITLE = "Most popular military aircraft (unique pilots, last 7 days)"
V1_CARDS = REPO / "docs" / "reference" / "v1_metabase_cards.json"
BASE = 9650000
EXTRAS = {"EA18", "EUFI", "FA18", "HAWK", "K35R", "KC10", "KC30"}   # is_military = true, but not in v1's list


def panel_rows(conn) -> list[dict]:
    """The panel's SQL over the default dashboard range, the last 7 days (168 hours, as the other panel tests do)."""
    sql = (panel(MILITARY, TITLE)["targets"][0]["rawSql"]
           .replace("$__timeFrom()", "(now() - interval '168 hours')")
           .replace("$__timeTo()", "now()"))
    return conn.execute(sql).fetchall()


def by_type(rows) -> dict[str, tuple[int, int]]:
    return {r["aircraft"]: (r["pilots"], r["flights"]) for r in rows}


# ---------------------------------------------------------------- the code list (migration 20261009000160)
def test_card_65_list_is_the_225_exact_codes_of_v1s_sql_and_not_the_is_military_extras(conn) -> None:
    v1_sql = next(c for c in json.loads(V1_CARDS.read_text(encoding="utf-8")) if c["id"] == 65)["sql"]
    in_list = v1_sql[v1_sql.index("IN ("):v1_sql.index("AND fs.completion_time")]
    in_list = re.sub(r"--[^\n]*", "", in_list)
    exact = {c for c in re.findall(r"'([^']*)'", in_list) if "%" not in c and "-" not in c}
    assert len(exact) == 225
    flagged = {r["icao"] for r in conn.execute("SELECT icao FROM aircraft_type WHERE in_v1_card65_list").fetchall()}
    assert flagged == exact
    military = {r["icao"] for r in conn.execute("SELECT icao FROM aircraft_type WHERE is_military").fetchall()}
    assert exact <= military                       # every v1 code is also in the broad flag
    assert military - exact == EXTRAS              # and the broad flag has exactly these seven more


# ---------------------------------------------------------------- the rule
def test_a_military_type_is_listed_and_a_civil_type_is_not(conn) -> None:
    add_flight(conn, BASE + 1, aircraft="F16", days_ago=1)
    add_flight(conn, BASE + 2, aircraft="B738", days_ago=1)
    assert by_type(panel_rows(conn)) == {"F16": (1, 1)}


def test_the_same_pilot_flying_the_same_type_twice_is_one_pilot_and_two_flights(conn) -> None:
    add_flight(conn, BASE + 3, aircraft="C130", days_ago=1)
    add_flight(conn, BASE + 3, aircraft="C130", days_ago=3)
    add_flight(conn, BASE + 4, aircraft="C130", days_ago=2)
    assert by_type(panel_rows(conn)) == {"C130": (2, 3)}


def test_a_pilot_on_two_types_counts_under_both_and_shares_use_the_sum_of_type_counts(conn) -> None:
    add_flight(conn, BASE + 5, aircraft="F16", days_ago=1)
    add_flight(conn, BASE + 5, aircraft="H60", days_ago=2)
    add_flight(conn, BASE + 6, aircraft="H60", days_ago=2)
    rows = panel_rows(conn)
    assert by_type(rows) == {"H60": (2, 2), "F16": (1, 1)}
    assert {r["aircraft"]: r["% of pilots"] for r in rows} == {"H60": Decimal("66.67"), "F16": Decimal("33.33")}


def test_shares_add_up_to_100_percent(conn) -> None:
    for i, t in enumerate(("F16", "F35", "C17", "C17", "H60", "H60", "H60")):
        add_flight(conn, BASE + 10 + i, aircraft=t, days_ago=1)
    rows = panel_rows(conn)
    assert sum(r["pilots"] for r in rows) == 7
    assert abs(sum(r["% of pilots"] for r in rows) - 100) <= Decimal("0.02")   # each share is rounded to 2 places
    assert rows[0]["aircraft"] == "H60"                                       # ordered by pilots, highest first


def test_the_window_is_one_week_by_flight_end(conn) -> None:
    add_flight(conn, BASE + 20, aircraft="F16", days_ago=6.9)      # inside
    add_flight(conn, BASE + 21, aircraft="F16", days_ago=7.1)      # just outside
    add_flight(conn, BASE + 22, aircraft="F35", days_ago=30)       # long outside
    assert by_type(panel_rows(conn)) == {"F16": (1, 1)}


def test_a_flight_not_yet_written_is_not_counted(conn) -> None:
    """Nothing is shown in progress (issue 89 phase 3, not yet deployed): a flight reaches the chart when its pilot has been
    quiet for 8 hours of stored data, measured against the newest snapshot."""
    t = set_up_clock(conn)
    make_session(conn, CID + 650, session_ending(t, 0))                 # still connected
    make_session(conn, CID + 651, session_ending(t, 9 * 3600))          # ended 9 hours before the newest snapshot
    conn.execute("UPDATE pilot_session SET aircraft_icao = 'F16' WHERE cid IN (%s, %s)", (CID + 650, CID + 651))
    assert conn.execute("SELECT write_finished_flights() AS n").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM flight WHERE cid IN (%s, %s)", (CID + 650, CID + 651)).fetchone()["n"] == 1
    assert by_type(panel_rows(conn)) == {"F16": (1, 1)}


def test_is_military_extras_are_not_counted_because_v1_never_listed_them(conn) -> None:
    for i, t in enumerate(sorted(EXTRAS)):
        add_flight(conn, BASE + 30 + i, aircraft=t, days_ago=1)
    add_flight(conn, BASE + 40, aircraft="F18S", days_ago=1)         # in v1's list
    assert by_type(panel_rows(conn)) == {"F18S": (1, 1)}


def test_v1s_wildcard_entries_never_matched_so_prefix_lookalikes_are_not_counted(conn) -> None:
    for i, t in enumerate(("MQ7", "MIG33", "SU99", "UH99")):         # would match 'MQ%', 'MIG%', 'SU-%', 'UH%' if they worked
        add_flight(conn, BASE + 50 + i, aircraft=t, days_ago=1)
    assert panel_rows(conn) == []


def test_an_empty_week_gives_no_rows_and_no_division_error(conn) -> None:
    assert panel_rows(conn) == []
    add_flight(conn, BASE + 60, aircraft="B738", days_ago=1)          # only civil traffic
    add_flight(conn, BASE + 61, aircraft="F16", days_ago=20)          # military, but outside the week
    assert panel_rows(conn) == []


# ---------------------------------------------------------------- parity with v1's own SQL
def test_the_panel_matches_v1s_sql_run_on_the_same_flights(conn) -> None:
    flights = [  # (cid, type, days ago it ended)
        (BASE + 70, "F16", 1), (BASE + 70, "F16", 2), (BASE + 71, "F16", 3), (BASE + 70, "H60", 1),
        (BASE + 72, "C17", 5), (BASE + 73, "C17", 6.5), (BASE + 74, "C17", 6.9), (BASE + 72, "T6", 0.5),
        (BASE + 75, "B738", 1), (BASE + 76, "FA18", 1), (BASE + 77, "MQ7", 2), (BASE + 78, "F35", 7.5),
        (BASE + 79, "MIG29", 4), (BASE + 80, "KC30", 2), (BASE + 81, "A400", 3),
    ]
    conn.execute("CREATE TEMP TABLE flight_summaries (cid bigint, aircraft_type text, completion_time timestamptz)")
    for cid, t, days in flights:
        add_flight(conn, cid, aircraft=t, days_ago=days)
        conn.execute("INSERT INTO flight_summaries VALUES (%s, %s, now() - make_interval(secs => %s))", (cid, t, days * 86400))
    v1_sql = next(c for c in json.loads(V1_CARDS.read_text(encoding="utf-8")) if c["id"] == 65)["sql"]
    v1 = conn.execute(v1_sql.rstrip().rstrip(";")).fetchall()
    assert v1, "v1's SQL returned nothing: the parity data is wrong"
    got = [(r["aircraft"], r["pilots"], r["flights"], r["% of pilots"]) for r in panel_rows(conn)]
    want = [(r["aircraft_type"], r["unique_pilots"], r["total_flights"], r["percentage_of_total_pilots"]) for r in v1]
    assert sorted(got) == sorted(want)
    assert sum(g[3] for g in got) > 99
