"""The flight views read the finished-flights table `flight_summary` (issue 89; built as flight_summary in phase 1, switched in phase 3 and
renamed in phase 5; all three phases are deployed).

The old-against-new equivalence tests of phase 3 compared the new views with the old merging view
(a view of the old pieces), which phase 5 removes; the equivalence ACROSS THE RENAME is tested in test_phase5_remove_old.py.

  * The thin views: no panel view reaches the positions or pilot_session.
  * grafana_ro can read the views and nothing else, and every panel runs as grafana_ro on data.
  * Panels count written flights only (a flight not yet written is in no view).
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import REPO

from helpers import CID, make_session, run, session_ending, set_poll_interval, set_up_clock
from test_flight_writer import HOUR, MIN, mk, snap, times, write

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration

MON = datetime(2026, 10, 5, tzinfo=UTC)                      # a Monday
DAY = timedelta(days=1)
DASH = REPO / "grafana" / "dashboards"

FLIGHT_COLS = ("session_id, cid, callsign, logon_time, name, home_icao, server, departure, arrival, aircraft_icao, aircraft_faa, "
               "flight_rules, route, remarks, planned_alt, deptime, pilot_rating, military_rating, start_time, end_time, duration, "
               "duration_minutes, airborne_minutes, max_gs_kt, max_alt_ft, sample_count, is_circuit, is_zzzz, touches_home_region")

def build(conn: Any) -> None:
    """Eleven pilots (ten flights) on and around Monday 05-10-2026, written by the writer."""
    assert MON.weekday() == 0
    set_poll_interval(conn, 30)
    snap(conn, MON)                                              # go-live: the Monday series starts here
    gs = [0] * 5 + [250] * 30 + [10] * 5
    pat = {"name": "Pat Yoda", "home_icao": "YSSY", "server": "SYD1", "aircraft_icao": "B738", "route": "DCT", "deptime": "0900",
           "planned_alt": "F350", "flight_rules": "I", "pilot_rating": 1, "military_rating": 0}
    mk(conn, 1000401, times(MON + 6 * HOUR + 30 * MIN, 40, 15), callsign="QFA101", gs=gs, alt=7000, remarks="RMK/ABC SEL/ABCD", **pat)
    mk(conn, 1000402, times(MON + 8 * HOUR, 60, 15), callsign="VOZ202", dep="YBBN", arr="YPPH",
       **{**pat, "name": "Bea Yarra", "home_icao": "YBBN", "aircraft_icao": "A320"})
    a = times(MON + 11 * HOUR + 50 * MIN, 20, 15)                # a reconnect 20 minutes after the first part: ONE flight
    b = times(a[-1] + 20 * MIN, 20, 15)
    reconnect = {**pat, "name": "Cee Yass", "aircraft_icao": "A321"}
    mk(conn, 1000403, a, callsign="JST303", dep="YMML", arr="YSCB", **reconnect)
    mk(conn, 1000403, b, callsign="JST303", dep="YMML", arr="YSCB", **reconnect)
    mk(conn, 1000404, times(MON + 2 * HOUR, 30, 15), callsign="ZZZ404", arr="ZZZZ", **pat)
    mk(conn, 1000405, times(MON + 3 * HOUR, 30, 15), callsign="CIR405", dep="YSSY", arr="YSSY", **pat)
    mk(conn, 1000406, times(MON + 4 * HOUR, 30, 15), callsign="BAW406", dep="EGLL", arr="KJFK",
       **{**pat, "name": "Dee Yeo", "home_icao": "EGLL", "server": "LON1", "aircraft_icao": "B77W"})
    mk(conn, 1000407, times(MON + 5 * HOUR, 30, 15), callsign="NOP407", dep=None, arr=None)           # no plan: no flight
    mk(conn, 1000408, times(MON + DAY, 40, 15), callsign="RAAF08", dep="YSRI", arr="YSRI",
       **{**pat, "aircraft_icao": "F16", "remarks": "OPR/RAAFVIRTUAL.ORG"})
    mk(conn, 1100409, times(MON + DAY + 2 * HOUR, 40, 15), callsign="XYZ409", **pat)                  # joining cohort 11
    mk(conn, 1000410, times(MON + 6 * HOUR + 50 * MIN + timedelta(seconds=15), 40, 15),
       callsign="EDGE10", **pat)                                 # 40 samples 15 s apart: ends exactly 07:00:00, inside the Monday window
    mk(conn, 1000411, times(MON + 12 * HOUR, 20, 15), callsign="EDGE11", **pat)                      # starts exactly 12:00:00: outside
    snap(conn, MON + 4 * DAY)                                    # the data ends days later: every pilot is quiet
    while write(conn):                                           # the writer, until nothing is left
        pass


# ------------------------------------------------------------------ thin views: no pieces, no positions, no pilot_session
PANEL_VIEWS = ["flight", "flight_ext", "movements", "cohorts", "monday_attendance"]
UNREACHABLE = {"pilot_position", "pilot_session", "online_now"}


def reach(conn: Any, views: list[str]) -> set[str]:
    return {r["relname"] for r in conn.execute(
        """
        WITH RECURSIVE reach(oid) AS (
            SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'public' AND c.relname = ANY(%s)
            UNION
            SELECT d.refobjid
              FROM reach r
              JOIN pg_rewrite rw ON rw.ev_class = r.oid
              JOIN pg_depend d ON d.classid = 'pg_rewrite'::regclass AND d.objid = rw.oid
                              AND d.refclassid = 'pg_class'::regclass AND d.refobjid <> r.oid
        )
        SELECT DISTINCT c.relname FROM reach JOIN pg_class c ON c.oid = reach.oid
        """, (views,)).fetchall()}


def test_no_panel_view_reaches_the_positions_or_pilot_session(conn: Any) -> None:
    got = reach(conn, PANEL_VIEWS)
    assert "flight_summary" in got                               # they do read the finished-flights table ...
    assert got & UNREACHABLE == set(), got & UNREACHABLE         # ... and none of the raw data
    for v in ("flight", "flight_ext", "movements", "cohorts"):
        assert reach(conn, [v]) & (UNREACHABLE | {"controller_session"}) == set(), v


# ------------------------------------------------------------------ grants: grafana_ro reads the views and nothing else
def test_grafana_ro_reads_every_panel_view_and_not_the_tables_behind_them(conn: Any) -> None:
    build(conn)
    conn.execute("SET ROLE grafana_ro")
    try:
        for v in PANEL_VIEWS:
            conn.execute(f"SELECT * FROM {v} LIMIT 1").fetchall()  # noqa: S608
        assert conn.execute("SELECT count(*) AS n FROM flight").fetchone()["n"] == 10
        for table in ("flight_summary", "pilot_position", "pilot_session"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608
    finally:
        conn.execute("RESET ROLE")


def _expand(sql: str) -> str:
    """Grafana macros as the PostgreSQL data source expands them, for a 30 day range ending now."""
    sql = sql.replace("$__timeFrom()", "(now() - interval '30 days')").replace("$__timeTo()", "now()")
    sql = re.sub(r"\$__timeFilter\((\w+)\)", r"\1 BETWEEN (now() - interval '30 days') AND now()", sql)
    sql = re.sub(r"\$__timeGroupAlias\((\w+), \$__interval\)", r"date_trunc('minute', \1) AS time", sql)
    return sql.replace("${airport:sqlstring}", "'YSSY'")


def test_every_panel_runs_as_grafana_ro_on_data_with_the_new_views(conn: Any) -> None:
    """The existing smoke test (test_audit_t30) runs every panel on an empty database; this runs them with flights in the table, so
    a column the panels read but the thin view no longer has, or a type change, fails here."""
    build(conn)
    conn.execute("SET ROLE grafana_ro")
    failures: list[str] = []
    try:
        for f in sorted(DASH.glob("*/*.json")):
            for p in json.loads(f.read_text(encoding="utf-8"))["panels"]:
                for t in p.get("targets", []):
                    if "rawSql" not in t:
                        continue
                    try:
                        conn.execute(_expand(t["rawSql"])).fetchall()
                    except Exception as exc:  # noqa: BLE001 - report every broken panel
                        failures.append(f"{f.parent.name}/{f.name}: {p['title']}: {str(exc).splitlines()[0]}")
    finally:
        conn.execute("RESET ROLE")
    assert not failures, "\n".join(failures)


# ------------------------------------------------------------------ panels count written flights only (ADR-025's job, now done by construction)
def panel_sql(path: str, title: str) -> str:
    dash = json.loads((DASH / path).read_text(encoding="utf-8"))
    p = next(p for p in dash["panels"] if p.get("title") == title)
    days = int(str(p.get("timeFrom", "30d")).rstrip("d"))
    return (p["targets"][0]["rawSql"]
            .replace("$__timeFrom()", f"(now() - interval '{days} days')")
            .replace("$__timeTo()", "now()")
            .replace("${airport:sqlstring}", "'YSSY'"))


def one_connected_one_gone(conn: Any) -> datetime:
    """A pilot still connected and a pilot who left 9 hours before the newest snapshot, both YSSY-YMML."""
    t = set_up_clock(conn)                                       # 30 s polling, newest snapshot 5 s ago
    make_session(conn, CID + 801, session_ending(t, 0))          # still connected: not quiet, not written
    make_session(conn, CID + 802, session_ending(t, 9 * 3600))   # quiet for 9 hours: written
    assert conn.execute("SELECT write_finished_flights() AS n").fetchone()["n"] == 1
    return t


def test_a_flight_not_yet_written_is_in_no_flight_view(conn: Any) -> None:
    one_connected_one_gone(conn)
    for view in ("flight", "flight_ext", "cohorts"):
        got = {r["cid"] for r in conn.execute(f"SELECT cid FROM {view}").fetchall()}  # noqa: S608
        assert got == {CID + 802}, view
    assert [(r["cid"], r["movement_type"]) for r in conn.execute("SELECT cid, movement_type FROM movements ORDER BY 1, 2").fetchall()] == \
        [(CID + 802, "arrival"), (CID + 802, "departure")]


def test_count_panel_over_flight_ext_counts_only_written_flights(conn: Any) -> None:
    one_connected_one_gone(conn)
    rows = conn.execute(panel_sql("pilots/pilots-traffic.json", "Busiest days by flights (last 5 years)")).fetchall()
    assert sum(r["total_flights"] for r in rows) == 1


def test_count_panel_over_movements_counts_only_written_flights(conn: Any) -> None:
    one_connected_one_gone(conn)
    rows = conn.execute(panel_sql("pilots/pilots-traffic.json", "Busiest home-region airports (last 30 days)")).fetchall()
    assert {r["airport"]: (r["departures"], r["arrivals"]) for r in rows} == {"YSSY": (1, 0), "YMML": (0, 1)}


def test_count_panel_reading_flight_directly_counts_only_written_flights(conn: Any) -> None:
    one_connected_one_gone(conn)
    rows = conn.execute(panel_sql("pilots/pilots-traffic.json", "Busiest days by pilots online (last 5 years)")).fetchall()
    assert sum(r["unique_pilots"] for r in rows) == 1


def test_a_written_flight_is_counted_by_the_panel_that_reads_flight(conn: Any) -> None:
    t = set_up_clock(conn)
    make_session(conn, CID + 803, session_ending(t, 9 * 3600))
    assert write(conn) == 1
    rows = conn.execute(panel_sql("pilots/pilots-traffic.json", "Busiest days by pilots online (last 5 years)")).fetchall()
    assert sum(r["unique_pilots"] for r in rows) == 1


def test_a_reconnecting_pilot_is_written_whole_or_not_at_all(conn: Any) -> None:
    """The newest session is still connected, so the pilot is not quiet: neither the earlier session's flight nor the new one is
    written (per-pilot quiet period, design 1.3). Nothing half-visible."""
    t = set_up_clock(conn)
    make_session(conn, CID + 804, session_ending(t, 600))
    make_session(conn, CID + 804, run(20, start=t - timedelta(seconds=15 * 19 + 5)))
    assert write(conn) == 0
    assert conn.execute("SELECT count(*) AS n FROM flight WHERE cid = %s", (CID + 804,)).fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM movements WHERE cid = %s", (CID + 804,)).fetchone()["n"] == 0


def test_a_stopped_collector_never_makes_a_flight_appear_early(conn: Any) -> None:
    """Quiet is measured against the newest STORED snapshot, so a collector stopped 30 minutes ago has not made anyone quiet."""
    set_poll_interval(conn, 30)
    t = datetime.now(UTC) - timedelta(minutes=30)
    snap(conn, t)
    make_session(conn, CID + 805, session_ending(t, 0))
    assert write(conn) == 0
    assert conn.execute("SELECT count(*) AS n FROM flight_ext").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM movements").fetchone()["n"] == 0


def test_departure_board_still_shows_a_connected_pilot_with_no_flight_row(conn: Any) -> None:
    """Live panels read pilot_latest, not flight: a connected pilot is on the board although the flight is not written yet."""
    one_connected_one_gone(conn)
    rows = conn.execute(panel_sql("pilots/pilots-now.json", "Departure board")).fetchall()
    assert {r["Flight"]: r["Status"] for r in rows}[f"TEST{CID + 801}"] == "AIRBORNE"
    assert {r["cid"] for r in conn.execute("SELECT cid FROM pilot_online_now").fetchall()} == {CID + 801}


def test_monday_attendance_counts_a_pilot_only_once_the_flight_is_written(conn: Any) -> None:
    """Pilots become part of the head count about 8 hours after they disconnect (design section 8)."""
    set_poll_interval(conn, 30)
    snap(conn, MON)
    mk(conn, 1000430, times(MON + 8 * HOUR, 40, 15), callsign="MON430")
    snap(conn, MON + 8 * HOUR + 30 * MIN)                        # 20 minutes after the pilot's last position
    assert write(conn) == 0
    assert conn.execute("SELECT pilot_cids FROM monday_attendance WHERE monday_date = %s", (MON.date(),)).fetchone()["pilot_cids"] == 0
    snap(conn, MON + 17 * HOUR)                                  # more than 8 hours of stored data later
    assert write(conn) == 1
    assert conn.execute("SELECT pilot_cids FROM monday_attendance WHERE monday_date = %s", (MON.date(),)).fetchone()["pilot_cids"] == 1
