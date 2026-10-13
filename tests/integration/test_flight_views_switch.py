"""Issue #89 phase 3 (#139, not yet deployed): `flight`, `flight_ext`, `movements`, `cohorts` and `monday_attendance` read flight_record.

Design: docs/DESIGN_89_FINAL.md sections 4, 7.2 (phase 3), 7.4 and 7.5; the requirement-to-test table is
docs/CONFORMANCE_89_PHASE3.md. The question every test here answers: do the panels show the same numbers after the switch?

  * Old against new, on the SAME data. Real sessions and positions are built, the pieces are refreshed (the old path:
    flight_summary, read through the renamed view `flight_legacy`) and the writer runs (the new path: flight_record, read through
    the new `flight`). Each affected view is compared with its pre-phase-3 definition, copied below as a temporary view
    (`old_*`), by EXCEPT ALL in both directions. The data avoids the cases design section 5.2 lists as EXPECTED differences
    (a session longer than 8 hours, a silence inside a connection, a join decided by logon_time); those have their own test.
  * The new views are thin: no panel view reaches the pieces, the legacy view, the raw positions or pilot_session.
  * grafana_ro can read the views and nothing else, and every panel runs as grafana_ro on data.
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

# ---------------------------------------------------------------------------------------------------------------------
# The definitions as they were BEFORE phase 3 (migrations 20261009000140 for flight_ext, 20261007000010 for movements and
# cohorts, 20261007000020 for monday_attendance), re-pointed from `flight` to `flight_legacy`. Copied, not derived, so the
# comparison cannot drift with the migration under test.
# ---------------------------------------------------------------------------------------------------------------------
OLD_VIEWS = [
    """
    CREATE TEMP VIEW old_flight_ext AS
    SELECT f.session_id, f.cid, f.callsign, f.name, f.home_icao, f.server,
           f.departure, f.arrival,
           f.departure || '-' || f.arrival AS route_label,
           upper(btrim(f.aircraft_icao)) AS aircraft_icao,
           f.flight_rules, f.remarks,
           f.start_time, f.end_time,
           extract(epoch FROM (f.end_time - f.start_time)) / 60.0 AS duration_minutes,
           f.airborne_minutes, f.max_gs_kt, f.max_alt_ft,
           f.is_circuit, f.is_zzzz,
           icao_is_home(f.departure) AS dep_is_home,
           icao_is_home(f.arrival) AS arr_is_home,
           icao_is_home(f.home_icao) AS pilot_is_local,
           icao_region(f.home_icao) AS pilot_region,
           (f.cid / 100000)::int AS cid_group,
           CASE WHEN f.callsign ~ '^[A-Z]{3}[0-9]' THEN left(f.callsign, 3) END AS airline_prefix,
           a.name AS airline_name,
           t.category AS aircraft_category,
           COALESCE(t.is_military, false) AS is_military,
           (f.remarks ~* 'SEL/') AS has_selcal,
           (f.remarks IS NOT NULL AND btrim(f.remarks) <> '') AS has_remarks
    FROM flight_legacy f
    LEFT JOIN airline a
           ON a.prefix = CASE WHEN f.callsign ~ '^[A-Z]{3}[0-9]' THEN left(f.callsign, 3) END
    LEFT JOIN aircraft_type t
           ON t.icao = upper(btrim(f.aircraft_icao))
    WHERE NOT f.is_in_progress
    """,
    """
    CREATE TEMP VIEW old_movements AS
    SELECT departure AS airport, 'departure'::text AS movement_type, start_time AS movement_time,
           session_id, cid, callsign
    FROM old_flight_ext
    WHERE dep_is_home AND NOT is_zzzz
    UNION ALL
    SELECT arrival, 'arrival', end_time, session_id, cid, callsign
    FROM old_flight_ext
    WHERE arr_is_home AND NOT is_zzzz
    """,
    """
    CREATE TEMP VIEW old_cohorts AS
    SELECT session_id, cid, cid_group,
           (cid_group * 100000)::text || '-' || (cid_group * 100000 + 99999)::text AS cohort,
           start_time, end_time
    FROM old_flight_ext
    WHERE cid_group > 0
    """,
    """
    CREATE TEMP VIEW old_monday_attendance AS
    WITH mondays AS (
      SELECT d::date AS monday_date
      FROM generate_series(
             date_trunc('week', (SELECT go_live FROM data_since) AT TIME ZONE 'UTC'),
             date_trunc('week', now() AT TIME ZONE 'UTC'),
             interval '1 week') AS d
    ), win AS (
      SELECT monday_date,
             (monday_date + time '07:00') AT TIME ZONE 'UTC' AS win_start,
             (monday_date + time '12:00') AT TIME ZONE 'UTC' AS win_end
      FROM mondays
    ), pil AS (
      SELECT w.monday_date, count(DISTINCT fs.cid) AS n
      FROM win w
      JOIN flight_summary fs ON fs.start_time < w.win_end AND fs.end_time >= w.win_start
      JOIN pilot_session ps ON ps.id = fs.session_id
      WHERE btrim(ps.departure, E' \\t\\r\\n') <> '' AND btrim(ps.arrival, E' \\t\\r\\n') <> ''
      GROUP BY w.monday_date
    ), ctl AS (
      SELECT w.monday_date, count(DISTINCT c.cid) AS n
      FROM win w
      JOIN controller_session_merged c ON c.start_time < w.win_end AND c.end_time >= w.win_start
      GROUP BY w.monday_date
    )
    SELECT w.monday_date, w.win_start, w.win_end,
           COALESCE(p.n, 0) AS pilot_cids,
           COALESCE(c.n, 0) AS controller_cids
    FROM win w
    LEFT JOIN pil p ON p.monday_date = w.monday_date
    LEFT JOIN ctl c ON c.monday_date = w.monday_date
    """,
]


def build(conn: Any) -> None:
    """Eleven pilots (ten flights) on and around Monday 05-10-2026, run through BOTH paths. Steps of 15 s keep every airborne sum a multiple of
    0.25 minutes, so the old per-piece rounding and the new single rounding cannot differ."""
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
    conn.execute("SELECT refresh_flight_summary(NULL)")          # the old path (this also runs the writer for up to 50 pilots)
    while write(conn):                                           # the new path, for any pilots left
        pass
    for ddl in OLD_VIEWS:
        conn.execute(ddl)


def only_in(conn: Any, left: str, right: str) -> list[dict]:
    return conn.execute(f"SELECT * FROM {left} EXCEPT ALL SELECT * FROM {right}").fetchall()  # noqa: S608


def same(conn: Any, old: str, new: str) -> int:
    """Both directions empty: returns the number of rows compared."""
    assert only_in(conn, old, new) == [], f"rows only in {old}"
    assert only_in(conn, new, old) == [], f"rows only in {new}"
    return conn.execute(f"SELECT count(*) AS n FROM {new}").fetchone()["n"]  # noqa: S608


def cols(conn: Any, rel: str) -> list[tuple[str, str]]:
    return [(r["attname"], r["t"]) for r in conn.execute(
        "SELECT attname, format_type(atttypid, atttypmod) AS t FROM pg_attribute WHERE attrelid = %s::regclass AND attnum > 0 "
        "AND NOT attisdropped ORDER BY attnum", (rel,)).fetchall()]


# ------------------------------------------------------------------ old against new, view by view
def test_flight_returns_the_same_rows_as_the_old_view_for_finished_flights(conn: Any) -> None:
    build(conn)
    conn.execute(f"CREATE TEMP VIEW old_flight AS SELECT {FLIGHT_COLS} FROM flight_legacy WHERE NOT is_in_progress")  # noqa: S608
    conn.execute(f"CREATE TEMP VIEW new_flight AS SELECT {FLIGHT_COLS} FROM flight")  # noqa: S608
    assert same(conn, "old_flight", "new_flight") == 10          # 11 pilots, one without a plan; the reconnect pair is one flight
    assert conn.execute("SELECT count(*) AS n FROM flight_legacy WHERE cid = 1000403").fetchone()["n"] == 1


def test_flight_ext_returns_the_same_rows_as_the_old_view(conn: Any) -> None:
    build(conn)
    assert same(conn, "old_flight_ext", "flight_ext") == 10
    # flight_ext keeps its 31 columns, names, order and types (CREATE OR REPLACE would have refused a change)
    assert cols(conn, "flight_ext") == cols(conn, "old_flight_ext") and len(cols(conn, "flight_ext")) == 31


def test_movements_return_the_same_rows_as_the_old_view(conn: Any) -> None:
    build(conn)
    n = same(conn, "old_movements", "movements")
    assert n > 0 and cols(conn, "movements") == cols(conn, "old_movements")
    # ZZZZ (1000404) makes no movement; the circuit (1000405) makes a departure and an arrival; a flight outside Y* none
    assert conn.execute("SELECT count(*) AS n FROM movements WHERE cid IN (1000404, 1000406)").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM movements WHERE cid = 1000405").fetchone()["n"] == 2


def test_cohorts_return_the_same_rows_as_the_old_view(conn: Any) -> None:
    build(conn)
    assert same(conn, "old_cohorts", "cohorts") == 10
    assert cols(conn, "cohorts") == cols(conn, "old_cohorts")
    assert conn.execute("SELECT cohort FROM cohorts WHERE cid = 1100409").fetchone()["cohort"] == "1100000-1199999"


def test_monday_attendance_returns_the_same_rows_as_the_old_view(conn: Any) -> None:
    build(conn)
    assert same(conn, "old_monday_attendance", "monday_attendance") >= 1
    assert cols(conn, "monday_attendance") == cols(conn, "old_monday_attendance")
    row = conn.execute("SELECT pilot_cids FROM monday_attendance WHERE monday_date = %s", (MON.date(),)).fetchone()
    # present between 07:00 and 12:00Z: 1000402 (08:00), 1000403 (starts 11:50), 1000410 (ends exactly 07:00). Not 1000411
    # (starts exactly 12:00), not 1000401 (ends 06:40), not the no-plan pilot, not the Tuesday pilots.
    assert row["pilot_cids"] == 3


def test_touches_home_region_follows_the_home_prefix_setting_in_both_views(conn: Any) -> None:
    build(conn)
    conn.execute("INSERT INTO app_setting (key, value) VALUES ('home_icao_prefixes', 'E') ON CONFLICT (key) DO UPDATE SET value = 'E'")
    conn.execute(f"CREATE TEMP VIEW old_flight AS SELECT {FLIGHT_COLS} FROM flight_legacy WHERE NOT is_in_progress")  # noqa: S608
    conn.execute(f"CREATE TEMP VIEW new_flight AS SELECT {FLIGHT_COLS} FROM flight")  # noqa: S608
    same(conn, "old_flight", "new_flight")
    assert [r["cid"] for r in conn.execute("SELECT cid FROM flight WHERE touches_home_region").fetchall()] == [1000406]


# ------------------------------------------------------------------ the expected differences (design 5.2) stay what they are
def test_a_session_longer_than_8_hours_is_two_old_rows_and_one_new_flight_and_every_view_follows(conn: Any) -> None:
    set_poll_interval(conn, 30)
    snap(conn, MON)
    ts = times(MON + 8 * HOUR, 61, 600)                          # 10 hours, one connection
    mk(conn, 1000420, ts, callsign="LNG420", gs=250)
    snap(conn, ts[-1] + 9 * HOUR)
    conn.execute("SELECT refresh_flight_summary(NULL)")
    while write(conn):
        pass
    for ddl in OLD_VIEWS:
        conn.execute(ddl)
    assert conn.execute("SELECT count(*) AS n FROM flight_legacy WHERE cid = 1000420").fetchone()["n"] == 2
    assert conn.execute("SELECT count(*) AS n FROM flight WHERE cid = 1000420").fetchone()["n"] == 1
    assert conn.execute("SELECT sum(sample_count) AS n FROM flight_legacy WHERE cid = 1000420").fetchone()["n"] == \
        conn.execute("SELECT sample_count AS n FROM flight WHERE cid = 1000420").fetchone()["n"] == 61
    # the views built on flight follow: one flight, one departure and one arrival (old: two flights, four movements)
    assert conn.execute("SELECT count(*) AS n FROM old_movements WHERE cid = 1000420").fetchone()["n"] == 4
    assert conn.execute("SELECT count(*) AS n FROM movements WHERE cid = 1000420").fetchone()["n"] == 2
    assert conn.execute("SELECT count(*) AS n FROM cohorts WHERE cid = 1000420").fetchone()["n"] == 1
    # Monday attendance counts the pilot once in both
    assert same(conn, "old_monday_attendance", "monday_attendance") >= 1


# ------------------------------------------------------------------ thin views: no pieces, no positions, no pilot_session
PANEL_VIEWS = ["flight", "flight_ext", "movements", "cohorts", "monday_attendance"]
UNREACHABLE = {"pilot_position", "pilot_session", "flight_summary", "flight_part", "flight_legacy", "online_now"}


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


def test_no_panel_view_reaches_the_pieces_the_legacy_view_the_positions_or_pilot_session(conn: Any) -> None:
    got = reach(conn, PANEL_VIEWS)
    assert "flight_record" in got                                # they do read the new table ...
    assert got & UNREACHABLE == set(), got & UNREACHABLE         # ... and none of the old machinery
    for v in ("flight", "flight_ext", "movements", "cohorts"):
        assert reach(conn, [v]) & (UNREACHABLE | {"controller_session"}) == set(), v


def test_the_old_view_is_still_there_under_its_new_name_with_its_old_columns(conn: Any) -> None:
    legacy = [c for c, _ in cols(conn, "flight_legacy")]
    assert {"gap_segment", "span_part", "is_in_progress"} <= set(legacy)
    new = cols(conn, "flight")
    old = cols(conn, "flight_legacy")
    assert new == [c for c in old if c[0] not in {"gap_segment", "span_part", "is_in_progress"}]     # same names, order, types
    assert [c for c, _ in new][:3] == ["session_id", "cid", "callsign"]
    assert "flight_part" in reach(conn, ["flight_legacy"]) and "flight_summary" in reach(conn, ["flight_legacy"])
    readers = {r["relname"] for r in conn.execute(
        "SELECT c.relname FROM pg_depend d JOIN pg_rewrite rw ON rw.oid = d.objid JOIN pg_class c ON c.oid = rw.ev_class "
        "WHERE d.refobjid = 'flight_legacy'::regclass AND c.relname <> 'flight_legacy'").fetchall()}
    assert readers == set()                                      # no other view reads it any more


# ------------------------------------------------------------------ grants: grafana_ro reads the views and nothing else
def test_grafana_ro_reads_every_panel_view_and_not_the_tables_behind_them(conn: Any) -> None:
    build(conn)
    conn.execute("SET ROLE grafana_ro")
    try:
        for v in PANEL_VIEWS:
            conn.execute(f"SELECT * FROM {v} LIMIT 1").fetchall()  # noqa: S608
        assert conn.execute("SELECT count(*) AS n FROM flight").fetchone()["n"] == 10
        for table in ("flight_record", "flight_summary", "pilot_position", "pilot_session"):
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
    rows = conn.execute(panel_sql("pilots/pilots-traffic.json", "Busiest days by flights (last 6 months)")).fetchall()
    assert sum(r["total_flights"] for r in rows) == 1


def test_count_panel_over_movements_counts_only_written_flights(conn: Any) -> None:
    one_connected_one_gone(conn)
    rows = conn.execute(panel_sql("pilots/pilots-traffic.json", "Busiest home-region airports (last 30 days)")).fetchall()
    assert {r["airport"]: (r["departures"], r["arrivals"]) for r in rows} == {"YSSY": (1, 0), "YMML": (0, 1)}


def test_count_panel_reading_flight_directly_counts_only_written_flights(conn: Any) -> None:
    one_connected_one_gone(conn)
    rows = conn.execute(panel_sql("pilots/pilots-traffic.json", "Busiest days by pilots online (last 6 months)")).fetchall()
    assert sum(r["unique_pilots"] for r in rows) == 1


def test_a_written_flight_is_counted_by_the_panel_that_reads_flight(conn: Any) -> None:
    t = set_up_clock(conn)
    make_session(conn, CID + 803, session_ending(t, 9 * 3600))
    assert write(conn) == 1
    rows = conn.execute(panel_sql("pilots/pilots-traffic.json", "Busiest days by pilots online (last 6 months)")).fetchall()
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


# ------------------------------------------------------------------ the owner's comparison script (ops/compare_views_89_phase3.sql)
SCRIPT = REPO / "ops" / "compare_views_89_phase3.sql"


def _metrics(conn: Any) -> tuple[dict[str, int], list[str]]:
    rows = conn.execute(SCRIPT.read_text(encoding="utf-8")).fetchall()
    return {r["metric"]: int(r["value"]) for r in rows if r["ord"] < 100}, [r["metric"] for r in rows if r["ord"] >= 100]


def test_the_phase_3_comparison_script_is_one_read_only_statement(conn: Any) -> None:
    body = "\n".join(line for line in SCRIPT.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("--"))
    assert body.count(";") == 1 and not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE)\b", body, re.I)


def test_the_phase_3_comparison_script_reports_no_difference_on_ordinary_data(conn: Any) -> None:
    build(conn)
    m, details = _metrics(conn)
    assert m["pilots fully processed (scope)"] == 11
    assert m["flight: old rows (flight_legacy, finished)"] == m["flight: new rows (flight)"] == 10
    for key, value in m.items():
        if "only in" in key:
            assert value == 0, key
    assert m["monday_attendance: Mondays whose pilot or controller count differs (expect 0, or the latest Monday while pilots are unwritten)"] == 0
    assert details == []


def test_the_phase_3_comparison_script_shows_the_expected_difference_for_a_session_over_8_hours(conn: Any) -> None:
    set_poll_interval(conn, 30)
    snap(conn, MON)
    ts = times(MON + 8 * HOUR, 61, 600)
    mk(conn, 1000421, ts, callsign="LNG421")
    snap(conn, ts[-1] + 9 * HOUR)
    conn.execute("SELECT refresh_flight_summary(NULL)")
    while write(conn):
        pass
    m, details = _metrics(conn)
    assert m["flight: old rows (flight_legacy, finished)"] == 2 and m["flight: new rows (flight)"] == 1
    assert m["flight: rows only in old (EXCEPT ALL, 29 shared columns)"] == 2 and m["flight: rows only in new"] == 1
    assert m["movements: old rows"] == 4 and m["movements: new rows"] == 2
    assert len([d for d in details if d.startswith("flight only in old")]) == 2
