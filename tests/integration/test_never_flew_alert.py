"""Issue 331: the Telegram alert for pilots who log on, file a plan and never get airborne, tested from the approved design and the
owner's decisions on the issue (not from the code).

One shared database setup per module (the scenario rows are loaded once, see `db`); one test per rule group with many cases in it.
Tests that need data the alert view can see from another connection (the watchdog cycle) come last and empty the tables first.
Cases that change data run in a transaction that is rolled back. Everything is UTC on a throwaway database (name ends _test).

1. The panel 'Pilots who logged on but never flew (90 days)' lists exactly the candidates it listed before issue 406 (the query as it
   stood on main, before the recency sort, is tests/fixtures/never_flew_panel_before.sql): same rows and figures, ignoring order,
   with the new last_qualifying_flight column and the recency sort itself as the one intended difference.
2. Who is a candidate and who is not; logons, reconnects and two sessions at once.
3. The grants of the watchdog login. 4. The watchdog cycle: every logon alerts once, the 24 hour memory, first run, dry-run.
"""
from __future__ import annotations

import copy
import json
import logging
import re
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from helpers import add_flight, apply, panel, partition_for, run_panel

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ops" / "watchdog"))
import messages  # noqa: E402
import never_flew  # noqa: E402

DASH = REPO / "grafana" / "dashboards" / "pilots" / "pilot-onboarding.json"
TITLE = "Pilots who logged on but never flew (90 days)"
OLD_PANEL_SQL = (REPO / "tests" / "fixtures" / "never_flew_panel_before.sql").read_text(encoding="utf-8")
YNAME = "Test Pilot YSSY"          # the fourth character from the end is a Y: the panel's rule
NONY = "Test Pilot XSSY"
TABLES = ("pilot_position, pilot_session, controller_rating_event, controller_frequency_event, controller_session, network_snapshot, "
          "poll_log, controller_unlisted_callsign, reference_data_change, flight_summary, drop_gate_refusal, system_sample")


# ---------------------------------------------------------------------------------------------------------- data builders
def snapshots(c) -> None:
    """Two fresh network snapshots, so 'online now' (seen in the last two snapshots and within 5 minutes) has something to compare to."""
    for ago in (40, 25):
        c.execute("INSERT INTO network_snapshot VALUES (now() - make_interval(secs => %s),1,1,1,1) ON CONFLICT DO NOTHING", (ago,))


def online(c, cid: int, name: str | None = YNAME, *, logon_ago: timedelta = timedelta(minutes=10), dep: str | None = "YSSY",
           arr: str | None = "YMML", max_gs: int | None = 0, callsign: str | None = None, seen_ago: timedelta = timedelta(seconds=10),
           route: str | None = "DCT") -> int:
    """A session that is online now (unless seen_ago is large), on the ground by default, with a plan filed by default."""
    now = datetime.now(UTC)
    logon = (now - logon_ago).replace(microsecond=0)
    seen = now - seen_ago
    partition_for(c, seen)
    sid = c.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, name, first_seen, last_seen, departure, arrival, route, max_gs_kt) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (cid, callsign or f"QA{cid}", logon, name, logon, seen, dep, arr, route, max_gs)).fetchone()["id"]
    c.execute("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) VALUES (%s,%s,-33.94,151.17,20,%s,90)",
              (seen, sid, max_gs or 0))
    return sid


def finished(c, cid: int, *, days_ago: float, airborne: float | None, name: str = YNAME) -> None:
    add_flight(c, cid, days_ago=days_ago, airborne=airborne, name=name)


def candidates(c) -> dict[int, dict]:
    return {r["cid"]: r for r in c.execute("SELECT * FROM ops_never_flew_alert ORDER BY cid, logon_time").fetchall()}


def world(c) -> None:
    snapshots(c)
    # candidates
    finished(c, 11, days_ago=10, airborne=0); finished(c, 11, days_ago=20, airborne=0); online(c, 11)           # repeat, Y name
    online(c, 12)                                                                                                # first-timer, Y name
    online(c, 21, max_gs=59)                                                                                     # first-timer, under 60 kt
    finished(c, 24, days_ago=3, airborne=0); online(c, 24, max_gs=None)                                          # repeat, no groundspeed yet
    online(c, 26, "Yabc")                                                                                        # shortest name that counts
    # not candidates
    online(c, 13, NONY)                                                                                          # first-timer, not a Y name
    finished(c, 14, days_ago=5, airborne=0, name=NONY); online(c, 14, NONY)                                      # repeat, not a Y name
    finished(c, 15, days_ago=120, airborne=0); online(c, 15)                                                     # lapsed: zero flight 120 days ago
    finished(c, 16, days_ago=120, airborne=80); online(c, 16)                                                    # lapsed: flew long ago
    online(c, 17, dep=None, arr=None)                                                                            # no plan
    online(c, 18, dep="", arr="")                                                                                # empty plan
    online(c, 29, arr=None)                                                                                      # half a plan
    online(c, 19, max_gs=250)                                                                                    # airborne
    online(c, 20, max_gs=60)                                                                                     # at the writer's 60 kt line
    finished(c, 22, days_ago=3, airborne=0); finished(c, 22, days_ago=4, airborne=30); online(c, 22)             # airborne time in 90 days
    online(c, 23, seen_ago=timedelta(minutes=10))                                                                # not online now
    online(c, 27, "Yab")                                                                                         # name too short
    finished(c, 28, days_ago=5, airborne=None); online(c, 28)                                                    # only a NULL-airborne flight
    online(c, 25, "")                                                                                            # empty name: no Y


@pytest.fixture(scope="module")
def db(dsn: str):
    """One connection and one scenario for the whole module (the per-test `conn` fixture would empty the tables before every test)."""
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as c:
        name = c.execute("SELECT current_database() AS d").fetchone()["d"]
        assert name.endswith("_test"), f"refusing to TRUNCATE {name!r}"
        for r in c.execute("SELECT inhrelid::regclass::text AS part FROM pg_inherits WHERE inhparent = 'pilot_position'::regclass").fetchall():
            c.execute(f"DROP TABLE IF EXISTS {r['part']}")                                                         # noqa: S608
        c.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")                                                     # noqa: S608
        world(c)
        yield c
        c.execute("RESET ROLE")
        c.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")                                                     # noqa: S608


@contextmanager
def rolled_back(c):
    """Changes made inside are undone afterwards, so the shared scenario stays as loaded."""
    with c.transaction(force_rollback=True):
        yield


# Issue 406 changed the panel on purpose: it now sorts by last_qualifying_flight (recency) instead of total_flights, and the
# view grew that one new column. OLD_PANEL_SQL is the query as it stood before issue 406 (no recency column, counted-first order).
OLD_COLUMNS = ("cid", "name", "total_flights", "flights_with_zero_enroute_time", "zero_enroute_percentage",
               "avg_time_online_minutes", "total_time_online_minutes")


def same_candidates(old_rows: list[dict], new_rows: list[dict]) -> bool:
    """True when both row sets agree on every column the view had before issue 406, ignoring order (the allowed difference)."""
    key = lambda r: tuple(r[c] for c in OLD_COLUMNS)                                                              # noqa: E731
    return sorted(map(key, old_rows)) == sorted(map(key, new_rows))


def sorted_by_recency(rows: list[dict]) -> bool:
    vals = [r["last_qualifying_flight"] for r in rows]
    return vals == sorted(vals, reverse=True)


def strip_limit(sql: str) -> str:
    return re.sub(r"\n\s*LIMIT 200", "", sql)


# ------------------------------------------------------------------------------------------------ 1. panel unchanged (except the fix)
def test_panel_returns_identical_rows_before_and_after_on_awkward_data(db) -> None:
    """Old query (before issue 406) and new query (this branch) on the same data, same transaction (same now()): same candidates and
    same per-row figures (every pre-406 column), with order and the new last_qualifying_flight column as the only differences."""
    sql = panel(DASH, TITLE)["targets"][0]["rawSql"]
    assert "pilot_never_flew_90d" in sql and "flight_summaries" not in sql and "SUBSTRING" not in sql.upper()    # one rule, not a copy
    assert "last_qualifying_flight" in sql and "ORDER BY last_qualifying_flight DESC" in sql                     # the intended change
    old0, new0 = db.execute(OLD_PANEL_SQL).fetchall(), run_panel(db, DASH, TITLE)
    assert same_candidates(old0, new0) and sorted_by_recency(new0)                                                # the shared scenario alone
    with rolled_back(db):
        finished(db, 101, days_ago=5, airborne=0, name="Old Name XXXX")
        finished(db, 101, days_ago=2, airborne=0, name="New Name YMML")             # renamed: the newer name has the Y
        finished(db, 102, days_ago=5, airborne=0, name="Was Listed YSSY")
        finished(db, 102, days_ago=2, airborne=0, name="Now Plain XSSY")            # renamed the other way: not listed
        finished(db, 103, days_ago=3, airborne=0, name=""); finished(db, 103, days_ago=4, airborne=0, name="")     # empty name
        finished(db, 104, days_ago=3, airborne=0, name="Kept Name YSSY"); finished(db, 104, days_ago=1, airborne=0, name="")
        finished(db, 105, days_ago=3, airborne=0, name="Null Mix YSSY"); finished(db, 105, days_ago=2, airborne=None, name="Null Mix YSSY")
        finished(db, 106, days_ago=2, airborne=None, name="Only Null YSSY")          # only NULL: not on the panel
        online(db, 107, "Still Flying YSSY")                                         # in progress, no finished flight
        finished(db, 108, days_ago=4, airborne=0, name="In Progress YSSY"); online(db, 108, "In Progress YSSY")
        finished(db, 109, days_ago=1, airborne=0, name="Edge Inside YSSY")
        finished(db, 110, days_ago=1, airborne=0, name="Edge Outside YSSY")
        db.execute("UPDATE flight_summary SET end_time = now() - interval '90 days', start_time = now() - interval '90 days 2 hours' WHERE cid = 109")
        db.execute("UPDATE flight_summary SET end_time = now() - interval '90 days 1 second', start_time = now() - interval '90 days 2 hours' WHERE cid = 110")
        for cid, nm in ((111, "Yab"), (112, "Yabc"), (113, "Y")):                    # too short, just long enough, one letter
            finished(db, cid, days_ago=2, airborne=0, name=nm)
        finished(db, 114, days_ago=2, airborne=0, name="Flew Once YSSY"); finished(db, 114, days_ago=3, airborne=45, name="Flew Once YSSY")
        finished(db, 115, days_ago=2, airborne=0, name="Flew Long Ago YSSY"); finished(db, 115, days_ago=200, airborne=90, name="Flew Long Ago YSSY")
        finished(db, 116, days_ago=120, airborne=0, name="Lapsed YSSY")
        old, new = db.execute(OLD_PANEL_SQL).fetchall(), run_panel(db, DASH, TITLE)
        assert same_candidates(old, new) and sorted_by_recency(new)
        assert {r["cid"] for r in new} - {11, 24} == {101, 104, 105, 108, 109, 112, 115}
        assert [r["name"] for r in new if r["cid"] == 101] == ["New Name YMML"] and [r["total_flights"] for r in new if r["cid"] == 105] == [1]
    with rolled_back(db):              # the LIMIT now keeps the 200 most recently qualifying, which issue 406 means can differ from
                                        # the 200 with most flights: recency (i, fine-grained) and flight count (i % 3) vary independently
        for i in range(205):
            finished(db, 5000 + i, days_ago=1 + i * 0.3, airborne=0, name=f"Bulk {i:03d} YSSY")   # the pilot's newest zero flight
            for k in range(i % 3):
                finished(db, 5000 + i, days_ago=40 + i * 0.3 + k, airborne=0, name=f"Bulk {i:03d} YSSY")  # older zero flights, same cid
        old_all, new_all = db.execute(strip_limit(OLD_PANEL_SQL)).fetchall(), db.execute(strip_limit(sql)).fetchall()
        assert same_candidates(old_all, new_all)                                  # same 205 candidates either way, LIMIT aside
        old, new = db.execute(OLD_PANEL_SQL).fetchall(), run_panel(db, DASH, TITLE)
        assert len(old) == len(new) == 200
        assert sorted_by_recency(new)
        expected_cids = {r["cid"] for r in sorted(new_all, key=lambda r: r["last_qualifying_flight"], reverse=True)[:200]}
        assert {r["cid"] for r in new} == expected_cids                           # the 200 kept are the 200 most recent of the 205
        assert {r["cid"] for r in new} != {r["cid"] for r in old}                 # proving the fix changes who is shown, not just the order
    with rolled_back(db):                                                                                        # an empty database
        db.execute(f"TRUNCATE {TABLES} CASCADE")                                                                 # noqa: S608
        assert db.execute(OLD_PANEL_SQL).fetchall() == run_panel(db, DASH, TITLE) == []


def test_panel_last_airborne_flight_column_and_plain_titles(db) -> None:
    """Issue 415: the panel ends with last_airborne_flight (the end of the pilot's latest flight with airborne time, at any age, NULL
    when there is none, shown as 'Never'); every column has a plain title; the other columns and the rows are unchanged."""
    p = panel(DASH, TITLE)
    sql = p["targets"][0]["rawSql"]
    assert "pilot_never_flew_90d" in sql and "flight_summaries" not in sql and "SUBSTRING" not in sql.upper()
    base = run_panel(db, DASH, TITLE)
    assert list(base[0])[-1] == "last_airborne_flight"                           # placed last
    assert all(r["last_airborne_flight"] is None for r in base)                  # nobody in the shared scenario flew in old history
    with rolled_back(db):
        finished(db, 120, days_ago=3, airborne=0, name="Old Flyer YSSY")
        finished(db, 120, days_ago=200, airborne=90, name="Old Flyer YSSY")
        finished(db, 120, days_ago=150, airborne=45, name="Old Flyer YSSY")      # a later (more recent) old airborne flight wins
        finished(db, 121, days_ago=2, airborne=0, name="Never Flown YSSY")
        finished(db, 121, days_ago=300, airborne=None, name="Never Flown YSSY")  # a NULL-airborne flight is not airborne time
        finished(db, 121, days_ago=250, airborne=0, name="Never Flown YSSY")     # nor is a zero one
        got = {r["cid"]: r for r in run_panel(db, DASH, TITLE)}
        expected = db.execute("SELECT max(end_time) AS e FROM flight WHERE cid = 120 AND airborne_minutes > 0").fetchone()["e"]
        assert expected is not None and got[120]["last_airborne_flight"] == expected
        assert expected < datetime.now(UTC) - timedelta(days=140)                 # it is the 150-day-old flight, outside the 90 days
        assert 121 in got and got[121]["last_airborne_flight"] is None
        old = db.execute(OLD_PANEL_SQL).fetchall()
        assert same_candidates(old, list(got.values())) and sorted_by_recency(list(got.values()))
        for r in got.values():                                                    # the other columns are the view's, unchanged
            v = db.execute("SELECT * FROM pilot_never_flew_90d WHERE cid = %s", (r["cid"],)).fetchone()
            assert (r["name"], r["total_flights"], r["last_qualifying_flight"]) == (v["name"], v["total_flights"], v["last_qualifying_flight"])
    # the titles: one override per column of the query, none for a column that is not there, and the new column maps NULL to Never
    cols = list(base[0])
    assert len(cols) == 9 and "min_time_online_minutes" not in cols and "max_time_online_minutes" not in cols      # issue 415: nine columns
    names = [o["matcher"]["options"] for o in p["fieldConfig"]["overrides"] if o["matcher"]["id"] == "byName"]
    assert sorted(names) == sorted(cols) and len(names) == len(set(names))
    titles = {o["matcher"]["options"]: next(x["value"] for x in o["properties"] if x["id"] == "displayName")
              for o in p["fieldConfig"]["overrides"]}
    assert titles == {"cid": "VATSIM ID", "name": "Pilot", "total_flights": "Flights (90 days)",
                      "flights_with_zero_enroute_time": "Flights never airborne", "zero_enroute_percentage": "% never airborne",
                      "avg_time_online_minutes": "Average time online (min)", "total_time_online_minutes": "Total time online (min)",
                      "last_qualifying_flight": "Last logged flight", "last_airborne_flight": "Last airborne flight"}
    last = next(o for o in p["fieldConfig"]["overrides"] if o["matcher"]["options"] == "last_airborne_flight")
    maps = next(x["value"] for x in last["properties"] if x["id"] == "mappings")
    assert maps == [{"type": "special", "options": {"match": "null", "result": {"text": "Never"}}}]


def test_panel_computes_last_airborne_flight_only_for_the_kept_rows() -> None:
    """The extra lookup sits outside the inner LIMIT 200, so it runs for at most 200 pilots (checked on the text of the query)."""
    sql = panel(DASH, TITLE)["targets"][0]["rawSql"]
    inner, outer = sql[sql.index("FROM (") : sql.rindex(") q")], sql[: sql.index("FROM (")] + sql[sql.rindex(") q") :]
    assert "LIMIT 200" in inner and "airborne_minutes" not in inner and "LIMIT" not in outer and "airborne_minutes > 0" in outer


# ------------------------------------------------------------------------------------------------- 2. who is a candidate
def test_candidates_are_exactly_the_ones_the_design_names(db) -> None:
    got = candidates(db)
    assert set(got) == {11, 12, 21, 24, 26}, sorted(got)
    assert {c: got[c]["kind"] for c in got} == {11: "repeat", 24: "repeat", 12: "first_timer", 21: "first_timer", 26: "first_timer"}
    # the alert and the panel never disagree about who counts
    panel_cids = {r["cid"]: r["name"] for r in run_panel(db, DASH, TITLE)}
    unflown_online = {r["cid"] for r in db.execute(
        "SELECT cid FROM pilot_session WHERE last_seen > now() - interval '5 minutes' AND departure <> '' AND arrival <> '' "
        "AND COALESCE(max_gs_kt, 0) < 60").fetchall()}
    assert {c for c, r in got.items() if r["kind"] == "repeat"} == set(panel_cids) & unflown_online
    assert all(got[c]["name"] == panel_cids[c] for c in set(panel_cids) & set(got))
    # what the message needs
    r = got[12]
    assert (r["name"], r["departure"], r["arrival"], r["route"], r["callsign"]) == (YNAME, "YSSY", "YMML", "DCT", "QA12")
    assert (round(r["lat"], 2), round(r["lon"], 2)) == (-33.94, 151.17) and r["logon_number"] == 1 and r["logon_time"].tzinfo is not None
    assert got[11]["flights_90d"] == 2
    with rolled_back(db):                                                    # a finished (lapsed) flight makes a first-timer neither kind
        finished(db, 12, days_ago=200, airborne=0)
        assert 12 not in candidates(db)
    with rolled_back(db):                                                    # no position row yet: the view still lists the logon
        db.execute("DELETE FROM pilot_position WHERE session_id IN (SELECT id FROM pilot_session WHERE cid = 12)")
        r = candidates(db)[12]
        assert r["lat"] is None and r["lon"] is None
    with rolled_back(db):                                                    # a dead collector: nobody is online
        db.execute("UPDATE pilot_session SET first_seen = now() - interval '40 minutes', last_seen = now() - interval '20 minutes'")
        db.execute("DELETE FROM network_snapshot")
        for ago in (1800, 1200):
            db.execute("INSERT INTO network_snapshot VALUES (now() - make_interval(secs => %s),1,1,1,1)", (ago,))
        online(db, 60, logon_ago=timedelta(minutes=40), seen_ago=timedelta(minutes=20))
        assert candidates(db) == {}


def test_every_logon_is_its_own_row_reconnects_and_two_sessions_at_once(db, payload, refdata) -> None:
    with rolled_back(db):                                                    # two sessions of one cid and callsign, two logon times
        online(db, 40, callsign="QFA9", logon_ago=timedelta(minutes=12), seen_ago=timedelta(seconds=35))
        online(db, 40, callsign="QFA9", logon_ago=timedelta(minutes=1))
        rows = db.execute("SELECT * FROM ops_never_flew_alert WHERE cid = 40 ORDER BY logon_time").fetchall()
        assert len(rows) == 2 and rows[0]["logon_time"] < rows[1]["logon_time"] and [r["logon_number"] for r in rows] == [1, 2]
    with rolled_back(db):                                                    # two callsigns of one cid online together
        online(db, 41, callsign="AAA1", logon_ago=timedelta(minutes=5))
        online(db, 41, callsign="BBB2", logon_ago=timedelta(minutes=3))
        rows = db.execute("SELECT callsign, logon_number FROM ops_never_flew_alert WHERE cid = 41 ORDER BY logon_time").fetchall()
        assert [(r["callsign"], r["logon_number"]) for r in rows] == [("AAA1", 1), ("BBB2", 2)]
    with rolled_back(db):                                                    # the collector's own write path gives a new logon time
        def feed(ts: datetime, logon: datetime) -> dict:
            p = copy.deepcopy(payload)
            pilot = copy.deepcopy(p["pilots"][0])
            pilot.update(cid=9000001, name="Test Pilot YSSY", callsign="TEST1", latitude=-33.94, longitude=151.17, altitude=20, groundspeed=0)
            pilot["flight_plan"].update(departure="YSSY", arrival="YMML", route="DCT")
            pilot["logon_time"] = logon.strftime("%Y-%m-%dT%H:%M:%SZ")
            p["pilots"], p["controllers"], p["atis"] = [pilot], [], []
            p["general"]["update_timestamp"] = ts.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
            return p

        now = datetime.now(UTC).replace(microsecond=0)
        partition_for(db, now); partition_for(db, now - timedelta(minutes=10))
        logon1, logon2 = now - timedelta(minutes=12), now - timedelta(seconds=50)
        apply(db, feed(now - timedelta(minutes=7), logon1), refdata)         # first connection, last seen 7 minutes ago
        assert 9000001 not in candidates(db)
        apply(db, feed(now - timedelta(seconds=30), logon2), refdata)        # back, new logon time
        apply(db, feed(now - timedelta(seconds=2), logon2), refdata)
        sessions = db.execute("SELECT logon_time FROM pilot_session WHERE cid = 9000001 ORDER BY logon_time").fetchall()
        assert [s["logon_time"] for s in sessions] == [logon1, logon2]
        got = candidates(db)[9000001]
        assert got["logon_time"] == logon2 and got["logon_number"] == 2


def test_first_timer_who_flew_in_a_session_not_yet_written_as_a_flight_is_not_a_first_timer(db) -> None:
    """Risk 9(b). A flight is written some hours after the session goes quiet (flight_quiet_hours, 8). A new pilot who flew, dropped
    and logged on again within that time has a session over 60 kt on record but no flight row yet. The design says first-timer means
    'has never flown'. Expected: no alert for the second logon."""
    with rolled_back(db):
        online(db, 50, callsign="FLEW1", logon_ago=timedelta(hours=2), seen_ago=timedelta(minutes=40), max_gs=420)    # flew, then dropped
        online(db, 50, callsign="FLEW1", logon_ago=timedelta(minutes=2), max_gs=0)                                    # back on the ground
        assert 50 not in candidates(db)


# ----------------------------------------------------------------------------------------------- 3. the watchdog login
def test_watchdog_ro_reads_the_alert_view_and_gets_nothing_new_beyond_it(db) -> None:
    if db.execute("SELECT 1 FROM pg_roles WHERE rolname = 'watchdog_ro'").fetchone() is None:
        pytest.skip("role watchdog_ro not created")
    cfg = db.execute("SELECT rolconfig FROM pg_roles WHERE rolname = 'watchdog_ro'").fetchone()["rolconfig"]
    assert "statement_timeout=10s" in cfg and "default_transaction_read_only=on" in cfg
    # privilege matrix for the two new views, for every role
    expect = {("grafana_ro", "pilot_never_flew_90d"): True, ("grafana_ro", "ops_never_flew_alert"): False,
              ("watchdog_ro", "pilot_never_flew_90d"): False, ("watchdog_ro", "ops_never_flew_alert"): True,
              ("collector", "pilot_never_flew_90d"): False, ("collector", "ops_never_flew_alert"): False,
              ("backup_ro", "pilot_never_flew_90d"): True, ("backup_ro", "ops_never_flew_alert"): True}
    for (role, view), want in expect.items():
        if db.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None:
            pytest.skip(f"role {role} not created")
        assert db.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS s", (role, view)).fetchone()["s"] is want, (role, view)
        for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not db.execute("SELECT has_table_privilege(%s, %s, %s) AS s", (role, view, priv)).fetchone()["s"], (role, view, priv)
    db.execute("SET ROLE watchdog_ro")                       # SET ROLE does not apply role settings: set the login's timeout by hand
    try:
        db.execute("SET statement_timeout = '10s'")
        t0 = time.monotonic()
        assert [r["cid"] for r in db.execute("SELECT cid FROM ops_never_flew_alert ORDER BY cid").fetchall()] == [11, 12, 21, 24, 26]
        assert time.monotonic() - t0 < 10
        for obj in ("pilot_never_flew_90d", "flight", "pilot_latest", "app_setting", "controller_frequency_event", "controller_rating_event"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute(f"SELECT * FROM {obj} LIMIT 1")                                                       # noqa: S608
        for stmt in ("DELETE FROM ops_never_flew_alert", "CREATE OR REPLACE VIEW ops_never_flew_alert AS SELECT 1 AS cid"):
            with pytest.raises(psycopg.Error):
                db.execute(stmt)
    finally:
        db.execute("RESET statement_timeout")
        db.execute("RESET ROLE")
    if db.execute("SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro'").fetchone() is not None:
        db.execute("SET ROLE grafana_ro")
        try:
            assert len(run_panel(db, DASH, TITLE)) >= 1                                                          # the panel still works
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                db.execute("SELECT * FROM ops_never_flew_alert")
        finally:
            db.execute("RESET ROLE")


# ----------------------------------------------------------------------------------------------- 4. the watchdog cycle
class Sent:
    """Stands in for Alerter.notify and keeps what would go to Telegram."""
    def __init__(self) -> None:
        self.items: list[tuple[str, tuple, dict]] = []

    def __call__(self, name, result) -> None:
        self.items.append((name, tuple(result), messages.facts_of(result)))

    @property
    def cids(self) -> list[int]:
        return [f["cid"] for _, _, f in self.items]

    @property
    def logons(self) -> list[tuple[int, str]]:
        return [(f["cid"], f["logon"]) for _, _, f in self.items]


T = 1_800_000_000.0


def cycle(dsn, state, sent, *, dry=False, now=T, remember=24):
    return never_flew.run(dsn, state, remember, dry, sent, now=now)


def fresh(db) -> None:
    """These tests read through the watchdog's own connection, so their rows are committed; start from empty tables."""
    db.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")                                                       # noqa: S608
    snapshots(db)


def test_every_logon_alerts_once_first_run_silent_and_24_hour_memory(db, dsn) -> None:
    fresh(db)
    s12, s21 = online(db, 12), online(db, 21)
    state: dict = {}
    sent = Sent()
    cycle(dsn, state, sent)                                              # first run after deploy: records, sends nothing
    cycle(dsn, state, sent, now=T + 60)
    assert sent.items == []
    online(db, 31, logon_ago=timedelta(minutes=2)); online(db, 30, logon_ago=timedelta(minutes=9))
    cycle(dsn, state, sent, now=T + 120)
    assert sent.cids == [30, 31]                                         # one message per logon, oldest logon first
    assert sent.items[0][0] == never_flew.NAME == "never-flew" and sent.items[0][1][0] == "warn"
    for i in range(3, 6):
        cycle(dsn, state, sent, now=T + 60 * i)
    assert sent.cids == [30, 31]                                         # none repeated on the next cycles
    # a drop and a reconnect 5 minutes later, with a new logon time, alerts a second time
    db.execute("UPDATE pilot_session SET last_seen = now() - interval '6 minutes' WHERE id = %s", (s12,))
    cycle(dsn, state, sent, now=T + 400)
    assert sent.cids == [30, 31]
    online(db, 12, callsign="QFA9", logon_ago=timedelta(seconds=30))
    cycle(dsn, state, sent, now=T + 460)
    cycle(dsn, state, sent, now=T + 520)
    assert sent.cids == [30, 31, 12]
    # remembered 24 hours, then forgotten
    db.execute("UPDATE pilot_session SET last_seen = now() - interval '6 minutes' WHERE id = %s", (s21,))
    s30 = db.execute("SELECT id FROM pilot_session WHERE cid = 30").fetchone()["id"]
    db.execute("UPDATE pilot_session SET last_seen = now() - interval '6 minutes' WHERE id = %s", (s30,))
    cycle(dsn, state, sent, now=T + 23 * 3600)                           # 23 hours on, the logon has left the view
    db.execute("UPDATE pilot_session SET last_seen = now() - interval '10 seconds' WHERE id = %s", (s30,))      # same logon returns
    cycle(dsn, state, sent, now=T + 23 * 3600 + 60)
    assert sent.cids == [30, 31, 12], "a logon alerted 23 hours ago must still be remembered"
    db.execute("UPDATE pilot_session SET last_seen = now() - interval '6 minutes' WHERE id = %s", (s30,))
    cycle(dsn, state, sent, now=T + 25 * 3600)                           # over 24 hours after its alert
    db.execute("UPDATE pilot_session SET last_seen = now() - interval '10 seconds' WHERE id = %s", (s30,))
    cycle(dsn, state, sent, now=T + 25 * 3600 + 60)
    assert sent.cids == [30, 31, 12, 30], "after 24 hours the logon is forgotten, so it alerts again if it ever returns"
    # the memory survives a restart (the status file is JSON)
    again = Sent()
    cycle(dsn, json.loads(json.dumps(state)), again, now=T + 25 * 3600 + 120)
    assert again.items == []


def test_dry_run_logs_and_sends_nothing_records_the_logon_and_a_failed_read_records_nothing(db, dsn, caplog) -> None:
    fresh(db)
    online(db, 12)
    sent = Sent()
    cycle(dsn, {}, sent, dry=True)                                       # first run in dry-run
    assert sent.items == []
    state: dict = {}
    cycle(dsn, state, sent, dry=True)
    online(db, 113, logon_ago=timedelta(minutes=1))
    with caplog.at_level(logging.INFO):
        cycle(dsn, state, sent, dry=True, now=T + 60)
    assert sent.items == []
    assert any("113" in r.getMessage() and r.levelno >= logging.INFO for r in caplog.records), "dry-run must say what it would have sent"
    cycle(dsn, state, sent, dry=False, now=T + 120)                      # the switch is turned off later: that logon is not sent
    assert sent.items == []
    online(db, 114, logon_ago=timedelta(seconds=30))                     # a genuinely new logon after the switch is sent
    cycle(dsn, state, sent, dry=False, now=T + 180)
    assert sent.cids == [114]
    broken: dict = {}
    with pytest.raises(psycopg.Error):
        never_flew.run("postgresql://nobody:x@127.0.0.1:1/nothing_test?connect_timeout=2", broken, 24, False, Sent(), now=T)
    assert not broken.get(never_flew.STATE_KEY)
