"""Issue #207: one rule for an ICAO text code, the database function norm_code().

Strong rule (owner decision 12-11-2026): strip spaces, tabs, CR and LF from both ends, upper-case, empty becomes NULL. The expected values
below come from a plain Python statement of the rule, never from the SQL under test. Covered: the function (value, volatility, who may call
it), the two indexes (built on it, valid, used by a query written either way, the old ones gone), every view and function that used to hold
a copy (no copy left in the catalogue, and the output follows the rule on awkward rows), the two military panels (same rows as the old text
on data the collector can store), and the flight writer and the pending-flight view (which already used this rule: no change).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from helpers import CID, T0, add_flight, make_session, run, run_panel, session_ending, set_up_clock, write_all

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
MILITARY = REPO / "grafana" / "dashboards" / "military" / "military.json"
BASE = CID + 20000


def rule(value: str | None) -> str | None:
    """The strong rule, stated once in Python (the oracle)."""
    if value is None:
        return None
    return value.strip(" \t\r\n").upper() or None


AWKWARD = [None, "", " ", "\t", "\r\n", " \t\r\n ", "ymml", " YMML ", "\tymml\r\n", "Y M", "ym\tml", "YMML\n", "\nYMML", "b738", " Fa18 ", "ÿmml"]


def one(conn: Any, sql: str, *params: Any) -> Any:
    return conn.execute(sql, params).fetchone()["v"]


# ------------------------------------------------------------------ the function
@pytest.mark.parametrize("value", AWKWARD)
def test_norm_code_follows_the_rule(conn: Any, value: str | None) -> None:
    assert one(conn, "SELECT norm_code(%s) AS v", value) == rule(value)


def test_norm_code_is_immutable_parallel_safe_and_in_public(conn: Any) -> None:
    row = conn.execute("SELECT provolatile, proparallel, prolang, pronamespace::regnamespace::text AS ns FROM pg_proc "
                       "WHERE oid = 'norm_code(text)'::regprocedure").fetchone()
    assert (row["provolatile"], row["proparallel"], row["ns"]) == ("i", "s", "public")


@pytest.mark.parametrize("role", ["grafana_ro", "collector", "watchdog_ro", "backup_ro"])
def test_every_role_can_execute_norm_code(conn: Any, role: str) -> None:
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
        pytest.skip(f"role {role} not created (migrate.sh and roles.sql create it; see the CI step)")
    assert one(conn, "SELECT has_function_privilege(%s, 'norm_code(text)', 'EXECUTE') AS v", role) is True


# ------------------------------------------------------------------ the indexes
def test_the_two_indexes_use_the_function_and_are_valid(conn: Any) -> None:
    rows = {r["indexname"]: r["indexdef"] for r in conn.execute(
        "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'pilot_session' AND indexname LIKE '%_code_idx'").fetchall()}
    assert set(rows) == {"pilot_session_departure_code_idx", "pilot_session_arrival_code_idx"}
    assert "norm_code(departure)" in rows["pilot_session_departure_code_idx"]
    assert "norm_code(arrival)" in rows["pilot_session_arrival_code_idx"]
    bad = conn.execute("SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                       "WHERE i.indrelid = 'pilot_session'::regclass AND NOT (i.indisvalid AND i.indisready)").fetchall()
    assert bad == []                                          # an interrupted CONCURRENTLY build would leave an INVALID index
    old = conn.execute("SELECT indexname FROM pg_indexes WHERE indexname IN ('pilot_session_departure_norm_idx', 'pilot_session_arrival_norm_idx')").fetchall()
    assert old == []


@pytest.mark.parametrize("column", ["departure", "arrival"])
@pytest.mark.parametrize("spelling", ["norm_code({c})", "NULLIF(upper(btrim({c}, E' \\t\\r\\n')), '')"])
def test_the_planner_uses_the_index_for_either_spelling(conn: Any, column: str, spelling: str) -> None:
    """The function is inlined, so a query written with the function and one written with the long expression both reach the index."""
    for i in range(30):
        make_session(conn, BASE + i, run(3, start=T0 + timedelta(minutes=i)), dep="YSSY" if i % 2 else "YMML", arr="YMML")
    conn.execute("ANALYZE pilot_session")
    conn.execute("SET enable_seqscan = off")
    try:
        plan = "\n".join(next(iter(r.values())) for r in conn.execute(
            f"EXPLAIN SELECT count(*) FROM pilot_session WHERE {spelling.format(c=column)} = 'YMML'").fetchall())  # noqa: S608
    finally:
        conn.execute("RESET enable_seqscan")
    assert f"pilot_session_{column}_code_idx" in plan, plan


# ------------------------------------------------------------------ no copy of the rule is left in the catalogue
def test_no_view_or_function_holds_a_copy_of_the_rule(conn: Any) -> None:
    views = ["home_prefix", "flight", "flight_ext", "pilot_latest", "pilot_online_now", "ops_pending_flight"]
    for v in views:
        text = one(conn, "SELECT pg_get_viewdef(%s::regclass) AS v", v)
        assert "upper(" not in text.lower(), (v, text)
    for f in ["icao_is_home", "icao_region", "write_finished_flights", "unreconciled_partition_sessions"]:
        text = one(conn, "SELECT string_agg(prosrc, ' ') AS v FROM pg_proc WHERE proname = %s AND pronamespace = 'public'::regnamespace", f)
        assert "upper(" not in text.lower() and "btrim" not in text.lower(), f
        assert "norm_code" in text, f


# ------------------------------------------------------------------ views and helpers on awkward rows
def test_pilot_latest_follows_the_rule(conn: Any) -> None:
    t = set_up_clock(conn)
    specs = [(" ymml\t", "ysSy ", " b738 "), ("YMML", "YSSY", "B738"), ("", "YSSY", ""), ("   ", " ", "  "), ("\tYBBN\n", "YSCB", "\tA320\r"),
             ("YPPH", "YMML", None), (" yssy", "", "FA18")]
    ids = {}
    for i, (dep, arr, ac) in enumerate(specs):
        sid = make_session(conn, BASE + 100 + i, session_ending(t, 5 + i, n=20), dep=dep, arr=arr)
        conn.execute("UPDATE pilot_session SET aircraft_icao = %s WHERE id = %s", (ac, sid))
        ids[sid] = (dep, arr, ac)
    rows = {r["session_id"]: r for r in conn.execute("SELECT * FROM pilot_latest").fetchall()}
    assert set(rows) == set(ids)
    for sid, (dep, arr, ac) in ids.items():
        assert (rows[sid]["departure"], rows[sid]["arrival"], rows[sid]["aircraft_icao"]) == (rule(dep), rule(arr), rule(ac))


def test_flight_ext_aircraft_follows_the_rule_and_finds_its_category(conn: Any) -> None:
    sids = {add_flight(conn, BASE + 200 + i, aircraft=ac): ac for i, ac in enumerate(["B738", " fa18 ", "\tHAWK\r\n", "", "   ", None, "zzzz"])}
    rows = {r["session_id"]: r for r in conn.execute("SELECT * FROM flight_ext").fetchall()}
    for sid, ac in sids.items():
        assert rows[sid]["aircraft_icao"] == rule(ac)
    fa18 = next(s for s, ac in sids.items() if ac == " fa18 ")
    hawk = next(s for s, ac in sids.items() if ac == "\tHAWK\r\n")
    assert rows[fa18]["is_military"] is True and rows[hawk]["is_military"] is True      # the aircraft_type join uses the same rule
    assert rows[next(s for s, ac in sids.items() if ac == "zzzz")]["is_military"] is False


def test_home_prefix_and_the_helpers_follow_the_rule(conn: Any) -> None:
    conn.execute("INSERT INTO app_setting (key, value) VALUES ('home_icao_prefixes', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                 ("Y,\t ya \r\n, ,\tv",))
    assert {r["prefix"] for r in conn.execute("SELECT prefix FROM home_prefix").fetchall()} == {"Y", "YA", "V"}
    assert one(conn, "SELECT icao_is_home(%s) AS v", "\tyssy\r\n") is True
    assert one(conn, "SELECT icao_is_home(%s) AS v", " egll ") is False
    assert one(conn, "SELECT icao_is_home(%s) AS v", "") is False
    assert one(conn, "SELECT icao_is_home(NULL) AS v") is False
    assert one(conn, "SELECT icao_region(%s) AS v", "\tymml\n") == "Australia"
    assert one(conn, "SELECT icao_region(%s) AS v", "  ") == "Unknown"
    assert one(conn, "SELECT icao_region(NULL) AS v") == "Unknown"


def test_the_flight_view_reads_the_same_home_prefixes_as_home_prefix(conn: Any) -> None:
    conn.execute("INSERT INTO app_setting (key, value) VALUES ('home_icao_prefixes', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                 ("\tYM\r\n, nz ",))
    sid_home = add_flight(conn, BASE + 300, dep="YMML", arr="KJFK")
    sid_other = add_flight(conn, BASE + 301, dep="NZAA", arr="KJFK")
    sid_none = add_flight(conn, BASE + 302, dep="EGLL", arr="KJFK")
    got = {r["session_id"]: r["touches_home_region"] for r in conn.execute("SELECT session_id, touches_home_region FROM flight").fetchall()}
    assert got == {sid_home: True, sid_other: True, sid_none: False}
    assert {r["prefix"] for r in conn.execute("SELECT prefix FROM home_prefix").fetchall()} == {"YM", "NZ"}


# ------------------------------------------------------------------ the flight writer and the pending-flight view are unchanged
def test_the_writer_gates_and_keys_with_the_rule(conn: Any) -> None:
    variants = [("\tymml\r\n", " ysSy "), ("YMML", "YSSY"), ("", "YSSY"), ("   ", "   "), ("YBBN", "\t")]
    for i, (dep, arr) in enumerate(variants):
        make_session(conn, BASE + 400 + i, run(40, start=T0 + timedelta(hours=i)), dep=dep, arr=arr)
    write_all(conn)
    got = {r["cid"]: (r["departure"], r["arrival"]) for r in conn.execute("SELECT cid, departure, arrival FROM flight_summary").fetchall()}
    assert got == {BASE + 400: ("YMML", "YSSY"), BASE + 401: ("YMML", "YSSY")}


def test_a_reconnect_spelt_two_ways_is_one_flight(conn: Any) -> None:
    make_session(conn, BASE + 450, run(40, start=T0), dep=" YMML", arr="YSSY\t")
    make_session(conn, BASE + 450, run(40, start=T0 + timedelta(minutes=40)), dep="ymml", arr=" YSSY")
    conn.execute("UPDATE pilot_session SET callsign = 'SAME450' WHERE cid = %s", (BASE + 450,))
    write_all(conn)
    assert conn.execute("SELECT count(*) AS n FROM flight_summary WHERE cid = %s", (BASE + 450,)).fetchone()["n"] == 1


def test_the_pending_flight_view_groups_with_the_rule_for_the_watchdog_role(conn: Any, dsn: str) -> None:
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'watchdog_ro'").fetchone():
        pytest.skip("role watchdog_ro not created (migrate.sh creates it; see the CI step)")
    now = datetime.now(UTC)
    conn.execute("INSERT INTO network_snapshot VALUES (%s,1,1,1,1)", (now,))
    make_session(conn, BASE + 500, run(10, start=now - timedelta(days=2)), dep="\tymml\r\n", arr=" ysSy ")
    make_session(conn, BASE + 501, run(10, start=now - timedelta(days=2)), dep="  ", arr="YSSY")
    with psycopg.connect(dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as c:
        c.execute("SET ROLE watchdog_ro")
        rows = {r["cid"]: (r["departure"], r["arrival"]) for r in c.execute("SELECT cid, departure, arrival FROM ops_pending_flight").fetchall()}
    assert rows[BASE + 500] == ("YMML", "YSSY")
    assert rows[BASE + 501] == (None, "YSSY")


# ------------------------------------------------------------------ the two military panels: the same rows as the old text on storable data
OLD_PANEL_FLIGHTS = """
SELECT DISTINCT f.callsign, f.cid, f.name,
       upper(btrim(f.aircraft_icao)) AS aircraft_type,
       f.departure, f.arrival, f.remarks, f.logon_time
FROM flight f
WHERE f.remarks ILIKE '%RAAFVIRTUAL.ORG%'
  AND f.logon_time BETWEEN (now() - interval '168 hours') AND now()
ORDER BY f.logon_time DESC, f.callsign
LIMIT 500
"""
OLD_PANEL_POPULAR = """
SELECT t.aircraft AS aircraft,
       count(*) AS "Flights",
       round(100.0 * count(*) / sum(count(*)) OVER (), 2) AS "% of RAAFv flights"
FROM (
  SELECT COALESCE(NULLIF(upper(btrim(f.aircraft_icao)), ''), '(none)') AS aircraft,
         row_number() OVER (PARTITION BY f.session_id ORDER BY f.end_time DESC) AS rn
  FROM flight f
  WHERE f.remarks ILIKE '%RAAFVIRTUAL.ORG%'
    AND f.logon_time BETWEEN (now() - interval '720 hours') AND now()
) t
WHERE t.rn = 1
GROUP BY t.aircraft
ORDER BY count(*) DESC, t.aircraft
LIMIT 25
"""
FLIGHTS_TITLE = "RAAFv flights (last 7 days)"
POPULAR_TITLE = "Most popular RAAFv aircraft last month (flights)"


def _rows(rows: list[dict]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True, default=str) for r in rows)


def test_the_military_panels_return_the_same_rows_as_before_on_storable_data(conn: Any) -> None:
    """Data as the collector can store it: spaces around a code, lower case, no value at all. (It never stores a tab, CR, LF or a blank.)"""
    for i, ac in enumerate(["B738", " b738 ", "FA18", " fa18", "hawk ", "  K35R", None, "C130", "E190 "]):
        add_flight(conn, BASE + 600 + i, aircraft=ac, remarks="RAAFVIRTUAL.ORG", days_ago=1 + i * 0.3)
    assert _rows(run_panel(conn, MILITARY, FLIGHTS_TITLE)) == _rows(conn.execute(OLD_PANEL_FLIGHTS).fetchall())
    assert _rows(run_panel(conn, MILITARY, POPULAR_TITLE)) == _rows(conn.execute(OLD_PANEL_POPULAR).fetchall())
    assert len(run_panel(conn, MILITARY, FLIGHTS_TITLE)) == 9


def test_the_military_panels_follow_the_strong_rule_on_awkward_data(conn: Any) -> None:
    add_flight(conn, BASE + 700, aircraft="\tHAWK\r\n", remarks="RAAFVIRTUAL.ORG")
    add_flight(conn, BASE + 701, aircraft="  ", remarks="RAAFVIRTUAL.ORG")
    flights = {r["cid"]: r["aircraft_type"] for r in run_panel(conn, MILITARY, FLIGHTS_TITLE)}
    assert flights == {BASE + 700: "HAWK", BASE + 701: None}
    popular = {r["aircraft"]: r["Flights"] for r in run_panel(conn, MILITARY, POPULAR_TITLE)}
    assert popular == {"HAWK": 1, "(none)": 1}
