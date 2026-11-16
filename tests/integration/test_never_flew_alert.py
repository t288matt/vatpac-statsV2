"""Issue 331: the Telegram alert for pilots who log on, file a plan and never get airborne, tested from the approved design and the
owner's decisions on the issue (not from the code).

1. The panel 'Pilots who logged on but never flew (90 days)' returns exactly the rows it returned before the change. The panel query
   as it stood on main is kept in tests/fixtures/never_flew_panel_before.sql; both queries run on the same data in one transaction.
2. Who is a candidate (the alert view) and who is not.
3. Every logon alerts; a logon never alerts twice; 24 hours of memory; nothing on the first run; dry-run logs and sends nothing.
4. The watchdog login reads the alert view and nothing else that is new.

The watchdog cycle is driven through ops/watchdog/never_flew.py run(); the alert view is read as the owner here and as watchdog_ro in
the privilege tests. Everything is UTC and runs on a throwaway database (name ends _test).
"""
from __future__ import annotations

import copy
import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from helpers import add_flight, apply, panel, partition_for, run_panel

psycopg = pytest.importorskip("psycopg")
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


# ---------------------------------------------------------------------------------------------------------- data builders
def snapshots(conn) -> None:
    """Two fresh network snapshots, so 'online now' (seen in the last two snapshots and within 5 minutes) has something to compare to."""
    for ago in (40, 25):
        conn.execute("INSERT INTO network_snapshot VALUES (now() - make_interval(secs => %s),1,1,1,1) ON CONFLICT DO NOTHING", (ago,))


def online(conn, cid: int, name: str | None = YNAME, *, logon_ago: timedelta = timedelta(minutes=10), dep: str | None = "YSSY",
           arr: str | None = "YMML", max_gs: int | None = 0, callsign: str | None = None, seen_ago: timedelta = timedelta(seconds=10),
           route: str | None = "DCT") -> int:
    """A session that is online now (unless seen_ago is large), on the ground by default, with a plan filed by default."""
    now = datetime.now(UTC)
    logon = (now - logon_ago).replace(microsecond=0)
    seen = now - seen_ago
    partition_for(conn, seen)
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, name, first_seen, last_seen, departure, arrival, route, max_gs_kt) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (cid, callsign or f"QA{cid}", logon, name, logon, seen, dep, arr, route, max_gs)).fetchone()["id"]
    conn.execute("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) VALUES (%s,%s,-33.94,151.17,20,%s,90)",
                 (seen, sid, max_gs or 0))
    return sid


def finished(conn, cid: int, *, days_ago: float, airborne: float | None, name: str = YNAME) -> None:
    add_flight(conn, cid, days_ago=days_ago, airborne=airborne, name=name)


def candidates(conn) -> dict[int, dict]:
    return {r["cid"]: r for r in conn.execute("SELECT * FROM ops_never_flew_alert ORDER BY cid, logon_time").fetchall()}


# ------------------------------------------------------------------------------------------------ 1. panel unchanged
def panel_rows_old_and_new(conn) -> tuple[list[dict], list[dict]]:
    old = conn.execute(OLD_PANEL_SQL).fetchall()
    new = run_panel(conn, DASH, TITLE)
    return old, new


def test_panel_is_unchanged_on_an_empty_database(conn) -> None:
    old, new = panel_rows_old_and_new(conn)
    assert old == new == []


def test_panel_returns_identical_rows_on_awkward_data(conn) -> None:
    """Same data, old query from main and new query from the branch, same transaction (same now()): identical rows, same order."""
    with conn.transaction():
        # listed: two zero-airborne flights; a renamed pilot (newest name wins); a name that is only on an older flight
        finished(conn, 101, days_ago=5, airborne=0, name="Old Name XXXX")
        finished(conn, 101, days_ago=2, airborne=0, name="New Name YMML")             # renamed: the newer name has the Y
        finished(conn, 102, days_ago=5, airborne=0, name="Was Listed YSSY")
        finished(conn, 102, days_ago=2, airborne=0, name="Now Plain XSSY")            # renamed the other way: not listed
        finished(conn, 103, days_ago=3, airborne=0, name="")                           # empty name: the CID text is used, no Y
        finished(conn, 103, days_ago=4, airborne=0, name="")
        finished(conn, 104, days_ago=3, airborne=0, name="Kept Name YSSY")
        finished(conn, 104, days_ago=1, airborne=0, name="")                           # newest name empty: the older one is used
        # a flight with NULL airborne time is left out of the panel's flights; the pilot still has the zero-airborne one
        finished(conn, 105, days_ago=3, airborne=0, name="Null Mix YSSY")
        finished(conn, 105, days_ago=2, airborne=None, name="Null Mix YSSY")
        finished(conn, 106, days_ago=2, airborne=None, name="Only Null YSSY")          # only NULL: not on the panel at all
        # an in-progress flight (a session online now, no finished flight) is not on the panel
        snapshots(conn)
        online(conn, 107, "Still Flying YSSY")
        finished(conn, 108, days_ago=4, airborne=0, name="In Progress YSSY")           # one finished zero flight plus one in progress
        online(conn, 108, "In Progress YSSY")
        # exactly 90 days old is inside (>=), one second older is outside
        finished(conn, 109, days_ago=1, airborne=0, name="Edge Inside YSSY")
        finished(conn, 110, days_ago=1, airborne=0, name="Edge Outside YSSY")
        conn.execute("UPDATE flight_summary SET end_time = now() - interval '90 days', start_time = now() - interval '90 days 2 hours' WHERE cid = 109")
        conn.execute("UPDATE flight_summary SET end_time = now() - interval '90 days 1 second', start_time = now() - interval '90 days 2 hours' WHERE cid = 110")
        # names that are too short, and the shortest that counts (four characters, Y first)
        finished(conn, 111, days_ago=2, airborne=0, name="Yab")
        finished(conn, 112, days_ago=2, airborne=0, name="Yabc")
        finished(conn, 113, days_ago=2, airborne=0, name="Y")
        # a pilot with any airborne time in 90 days is not listed; airborne earlier than 90 days does not rescue a zero in 90 days
        finished(conn, 114, days_ago=2, airborne=0, name="Flew Once YSSY")
        finished(conn, 114, days_ago=3, airborne=45, name="Flew Once YSSY")
        finished(conn, 115, days_ago=2, airborne=0, name="Flew Long Ago YSSY")
        finished(conn, 115, days_ago=200, airborne=90, name="Flew Long Ago YSSY")
        finished(conn, 116, days_ago=120, airborne=0, name="Lapsed YSSY")              # nothing in 90 days: not listed
        old, new = panel_rows_old_and_new(conn)
    assert old == new
    listed = {r["cid"] for r in new}
    assert listed == {101, 104, 105, 108, 109, 112, 115}, listed             # 102 is renamed away, 103 has no Y
    assert [r["name"] for r in new if r["cid"] == 101] == ["New Name YMML"]
    assert [r["total_flights"] for r in new if r["cid"] == 105] == [1]                 # the NULL-airborne flight is not counted


def test_panel_limit_and_order_are_unchanged(conn) -> None:
    """205 listed pilots: the LIMIT 200 and the order (most flights first, then name) are the panel's own and stay."""
    with conn.transaction():
        for i in range(205):
            for k in range(1 + i % 3):
                finished(conn, 5000 + i, days_ago=1 + k, airborne=0, name=f"Bulk {i:03d} YSSY")
        old, new = panel_rows_old_and_new(conn)
    assert len(old) == len(new) == 200
    assert old == new


def test_panel_columns_are_unchanged(conn) -> None:
    finished(conn, 101, days_ago=2, airborne=0)
    old, new = panel_rows_old_and_new(conn)
    assert list(old[0]) == list(new[0])


# ------------------------------------------------------------------------------------------------- 2. who is a candidate
def world(conn) -> None:
    snapshots(conn)
    # candidates
    finished(conn, 11, days_ago=10, airborne=0); finished(conn, 11, days_ago=20, airborne=0); online(conn, 11)    # repeat, Y name
    online(conn, 12)                                                                                                # first-timer, Y name
    online(conn, 21, max_gs=59)                                                                                     # first-timer, still under 60 kt
    finished(conn, 24, days_ago=3, airborne=0); online(conn, 24, max_gs=None)                                       # repeat, no groundspeed at all yet
    online(conn, 26, "Yabc")                                                                                        # shortest name that counts
    # not candidates
    online(conn, 13, NONY)                                                                                          # first-timer, not a Y name
    finished(conn, 14, days_ago=5, airborne=0, name=NONY); online(conn, 14, NONY)                                   # repeat, not a Y name
    finished(conn, 15, days_ago=120, airborne=0); online(conn, 15)                                                  # lapsed: zero flight, but 120 days ago
    finished(conn, 16, days_ago=120, airborne=80); online(conn, 16)                                                 # lapsed: flew, long ago
    online(conn, 17, dep=None, arr=None)                                                                            # no plan
    online(conn, 18, dep="", arr="")                                                                                # empty plan
    online(conn, 29, arr=None)                                                                                      # half a plan
    online(conn, 19, max_gs=250)                                                                                    # airborne
    online(conn, 20, max_gs=60)                                                                                     # at the writer's 60 kt airborne line
    finished(conn, 22, days_ago=3, airborne=0); finished(conn, 22, days_ago=4, airborne=30); online(conn, 22)      # airborne time in 90 days
    online(conn, 23, seen_ago=timedelta(minutes=10))                                                                # not online now
    online(conn, 27, "Yab")                                                                                         # name too short
    finished(conn, 28, days_ago=5, airborne=None); online(conn, 28)                                                 # only a NULL-airborne flight
    online(conn, 25, "")                                                                                            # empty name: the CID text has no Y


def test_candidates_are_exactly_the_ones_the_design_names(conn) -> None:
    world(conn)
    got = candidates(conn)
    assert set(got) == {11, 12, 21, 24, 26}, sorted(got)
    assert {c: got[c]["kind"] for c in got} == {11: "repeat", 24: "repeat", 12: "first_timer", 21: "first_timer", 26: "first_timer"}


def test_everyone_the_panel_lists_who_is_online_unflown_with_a_plan_is_a_candidate(conn) -> None:
    world(conn)
    panel_cids = {r["cid"]: r["name"] for r in run_panel(conn, DASH, TITLE)}
    unflown_online = {r["cid"] for r in conn.execute(
        "SELECT cid FROM pilot_session WHERE last_seen > now() - interval '5 minutes' AND departure <> '' AND arrival <> '' "
        "AND COALESCE(max_gs_kt, 0) < 60").fetchall()}
    got = candidates(conn)
    for cid in panel_cids.keys() & unflown_online:
        assert cid in got and got[cid]["kind"] == "repeat" and got[cid]["name"] == panel_cids[cid]
    assert {c for c, r in got.items() if r["kind"] == "repeat"} == set(panel_cids) & unflown_online


def test_a_first_timer_is_not_one_once_a_finished_flight_exists(conn) -> None:
    snapshots(conn)
    online(conn, 12)
    assert 12 in candidates(conn)
    finished(conn, 12, days_ago=200, airborne=0)                                   # a finished flight, but lapsed: now neither kind
    assert 12 not in candidates(conn)


def test_the_view_carries_what_the_message_needs(conn) -> None:
    snapshots(conn)
    online(conn, 12, route="DCT WOL DCT")
    r = candidates(conn)[12]
    assert (r["name"], r["departure"], r["arrival"], r["route"], r["callsign"]) == (YNAME, "YSSY", "YMML", "DCT WOL DCT", "QA12")
    assert (round(r["lat"], 2), round(r["lon"], 2)) == (-33.94, 151.17)
    assert r["logon_number"] == 1 and r["logon_time"].tzinfo is not None


def test_view_works_with_no_position_row_yet(conn) -> None:
    snapshots(conn)
    sid = online(conn, 12)
    conn.execute("DELETE FROM pilot_position WHERE session_id = %s", (sid,))
    r = candidates(conn)[12]
    assert r["lat"] is None and r["lon"] is None


def test_offline_pilot_and_stale_data(conn) -> None:
    """Nobody is online when the newest snapshot is old (a dead collector must not alert on the last people seen)."""
    snapshots(conn)
    online(conn, 12, seen_ago=timedelta(minutes=7))
    assert candidates(conn) == {}


def test_a_dead_collector_shows_nobody_online(conn) -> None:
    """The newest snapshots are 20 and 30 minutes old and the pilot was last seen in the newest one: not online now."""
    for ago in (1800, 1200):
        conn.execute("INSERT INTO network_snapshot VALUES (now() - make_interval(secs => %s),1,1,1,1)", (ago,))
    online(conn, 12, logon_ago=timedelta(minutes=40), seen_ago=timedelta(minutes=20))
    assert candidates(conn) == {}


# ------------------------------------------------------------------------------- logons, reconnects, two sessions at once
def test_a_reconnect_is_its_own_row_with_its_own_logon_time(conn) -> None:
    """Two sessions of one CID and callsign with different logon times (a drop and a reconnect) are two rows when both
    are in the online window, with different logon times, so they are two keys."""
    snapshots(conn)
    online(conn, 12, callsign="QFA9", logon_ago=timedelta(minutes=12), seen_ago=timedelta(seconds=35))
    online(conn, 12, callsign="QFA9", logon_ago=timedelta(minutes=1))
    rows = [r for r in conn.execute("SELECT * FROM ops_never_flew_alert WHERE cid = 12 ORDER BY logon_time").fetchall()]
    assert len(rows) == 2 and rows[0]["logon_time"] < rows[1]["logon_time"]
    assert [r["logon_number"] for r in rows] == [1, 2]


def test_a_reconnect_through_the_real_ingest_gets_a_new_logon_time_and_a_separate_row(conn, payload, refdata) -> None:
    """The collector's own write path: the feed gives a reconnected pilot a new logon_time, so pilot_session gets a second row."""
    def feed(ts: datetime, logon: datetime | None, gs: int = 0) -> dict:
        p = copy.deepcopy(payload)
        pilot = copy.deepcopy(p["pilots"][0])
        pilot.update(cid=9000001, name="Test Pilot YSSY", callsign="TEST1", latitude=-33.94, longitude=151.17, altitude=20,
                     groundspeed=gs)
        pilot["flight_plan"].update(departure="YSSY", arrival="YMML", route="DCT")
        pilot["logon_time"] = logon.strftime("%Y-%m-%dT%H:%M:%SZ") if logon else pilot["logon_time"]
        p["pilots"], p["controllers"], p["atis"] = [pilot], [], []
        p["general"]["update_timestamp"] = ts.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        return p

    now = datetime.now(UTC).replace(microsecond=0)
    partition_for(conn, now); partition_for(conn, now - timedelta(minutes=10))
    logon1, logon2 = now - timedelta(minutes=12), now - timedelta(seconds=50)
    apply(conn, feed(now - timedelta(minutes=7), logon1), refdata)                  # first connection, last seen 7 minutes ago
    assert candidates(conn) == {}                                                    # 7 minutes ago is not online now
    apply(conn, feed(now - timedelta(seconds=30), logon2), refdata)                  # back, new logon time
    apply(conn, feed(now - timedelta(seconds=2), logon2), refdata)
    sessions = conn.execute("SELECT logon_time FROM pilot_session WHERE cid = 9000001 ORDER BY logon_time").fetchall()
    assert [s["logon_time"] for s in sessions] == [logon1, logon2]
    got = candidates(conn)
    assert list(got) == [9000001] and got[9000001]["logon_time"] == logon2 and got[9000001]["logon_number"] == 2


def test_two_sessions_of_one_cid_online_together_give_two_rows_not_a_crash(conn) -> None:
    snapshots(conn)
    online(conn, 12, callsign="AAA1", logon_ago=timedelta(minutes=5))
    online(conn, 12, callsign="BBB2", logon_ago=timedelta(minutes=3))
    rows = conn.execute("SELECT callsign, logon_number FROM ops_never_flew_alert WHERE cid = 12 ORDER BY logon_time").fetchall()
    assert [(r["callsign"], r["logon_number"]) for r in rows] == [("AAA1", 1), ("BBB2", 2)]


def test_first_timer_who_flew_in_a_session_not_yet_written_as_a_flight_is_not_a_first_timer(conn) -> None:
    """Risk 9(b). A flight is written some hours after the session goes quiet (flight_quiet_hours, 8). A new pilot who flew, dropped
    and logged on again within that time has a session with groundspeed over 60 kt on record but no flight row yet. The design says
    first-timer means 'has never flown'. Expected: no alert for the second logon."""
    snapshots(conn)
    online(conn, 12, callsign="FLEW1", logon_ago=timedelta(hours=2), seen_ago=timedelta(minutes=40), max_gs=420)   # flew, then dropped
    online(conn, 12, callsign="FLEW1", logon_ago=timedelta(minutes=2), max_gs=0)                                    # reconnects on the ground
    assert 12 not in candidates(conn)


# ----------------------------------------------------------------------------- 3. the watchdog cycle (memory, first run, dry run)
class Sent:
    """Stands in for Alerter.notify and keeps what would go to Telegram."""
    def __init__(self) -> None:
        self.items: list[tuple[str, tuple, dict]] = []

    def __call__(self, name, result) -> None:
        self.items.append((name, tuple(result), messages.facts_of(result)))

    @property
    def logons(self) -> list[tuple[int, str]]:
        return [(f["cid"], f["logon"]) for _, _, f in self.items]


T = 1_800_000_000.0


def cycle(dsn, state, sent, *, dry=False, now=T, remember=24):
    return never_flew.run(dsn, state, remember, dry, sent, now=now)


def test_first_run_after_deploy_records_the_current_rows_and_sends_nothing(conn, dsn) -> None:
    snapshots(conn)
    online(conn, 12); online(conn, 21)
    state: dict = {}
    sent = Sent()
    cycle(dsn, state, sent)
    assert sent.items == []
    cycle(dsn, state, sent, now=T + 60)                                  # the same two rows again: still nothing
    assert sent.items == []


def test_one_alert_per_logon_and_none_repeated_on_the_next_cycles(conn, dsn) -> None:
    snapshots(conn)
    state: dict = {}
    sent = Sent()
    cycle(dsn, state, sent)                                              # first run on an empty view
    online(conn, 12)
    cycle(dsn, state, sent, now=T + 60)
    assert [c for c, _ in sent.logons] == [12]
    for i in range(2, 6):
        cycle(dsn, state, sent, now=T + 60 * i)
    assert [c for c, _ in sent.logons] == [12]                           # still one
    assert sent.items[0][0] == never_flew.NAME == "never-flew"
    assert sent.items[0][1][0] == "warn"


def test_a_drop_and_a_reconnect_with_a_new_logon_time_alerts_a_second_time(conn, dsn) -> None:
    snapshots(conn)
    state: dict = {}
    sent = Sent()
    cycle(dsn, state, sent)
    s1 = online(conn, 12, callsign="QFA9", logon_ago=timedelta(minutes=6))
    cycle(dsn, state, sent, now=T + 60)
    assert len(sent.items) == 1
    conn.execute("UPDATE pilot_session SET last_seen = now() - interval '6 minutes' WHERE id = %s", (s1,))      # dropped
    cycle(dsn, state, sent, now=T + 120)
    assert len(sent.items) == 1
    online(conn, 12, callsign="QFA9", logon_ago=timedelta(seconds=30))                                          # back 5 minutes later
    cycle(dsn, state, sent, now=T + 180)
    assert [c for c, _ in sent.logons] == [12, 12] and sent.logons[0][1] != sent.logons[1][1]
    cycle(dsn, state, sent, now=T + 240)
    assert len(sent.items) == 2


def test_two_pilots_in_one_cycle_give_two_messages_oldest_logon_first(conn, dsn) -> None:
    snapshots(conn)
    state: dict = {}
    sent = Sent()
    cycle(dsn, state, sent)
    online(conn, 31, logon_ago=timedelta(minutes=2))
    online(conn, 30, logon_ago=timedelta(minutes=9))
    cycle(dsn, state, sent, now=T + 60)
    assert [c for c, _ in sent.logons] == [30, 31]


def test_a_logon_is_remembered_for_24_hours_and_then_forgotten(conn, dsn) -> None:
    snapshots(conn)
    state: dict = {}
    sent = Sent()
    cycle(dsn, state, sent)
    sid = online(conn, 12)
    cycle(dsn, state, sent, now=T + 60)
    assert len(sent.items) == 1
    conn.execute("UPDATE pilot_session SET last_seen = now() - interval '6 minutes' WHERE id = %s", (sid,))       # leaves the view
    cycle(dsn, state, sent, now=T + 23 * 3600)                                                                  # 23 hours on: still remembered
    conn.execute("UPDATE pilot_session SET last_seen = now() - interval '10 seconds' WHERE id = %s", (sid,))   # same logon returns
    cycle(dsn, state, sent, now=T + 23 * 3600 + 60)
    assert len(sent.items) == 1, "a logon alerted 23 hours ago must still be remembered"
    conn.execute("UPDATE pilot_session SET last_seen = now() - interval '6 minutes' WHERE id = %s", (sid,))
    cycle(dsn, state, sent, now=T + 60 + 25 * 3600)                                                             # over 24 hours after its alert
    conn.execute("UPDATE pilot_session SET last_seen = now() - interval '10 seconds' WHERE id = %s", (sid,))
    cycle(dsn, state, sent, now=T + 60 + 25 * 3600 + 60)
    assert len(sent.items) == 2, "after 24 hours the logon is forgotten, so it alerts again if it ever returns"


def test_the_memory_survives_a_restart_through_the_status_file(conn, dsn) -> None:
    snapshots(conn)
    state: dict = {}
    sent = Sent()
    cycle(dsn, state, sent)
    online(conn, 12)
    cycle(dsn, state, sent, now=T + 60)
    restored = json.loads(json.dumps(state))                              # the watchdog keeps its state in a JSON status file
    again = Sent()
    cycle(dsn, restored, again, now=T + 120)
    assert len(sent.items) == 1 and again.items == []


def test_dry_run_logs_and_sends_nothing_and_records_the_logon_as_seen(conn, dsn, caplog) -> None:
    snapshots(conn)
    state: dict = {}
    sent = Sent()
    cycle(dsn, state, sent, dry=True)                                      # first run
    online(conn, 12)
    with caplog.at_level(logging.INFO):
        cycle(dsn, state, sent, dry=True, now=T + 60)
    assert sent.items == []
    assert any("12" in r.getMessage() and r.levelno >= logging.INFO for r in caplog.records), "dry-run must say what it would have sent"
    cycle(dsn, state, sent, dry=False, now=T + 120)                        # the switch is turned off later: that logon is not sent
    assert sent.items == []
    online(conn, 13 + 100, logon_ago=timedelta(minutes=1))                 # a genuinely new logon after the switch is sent
    cycle(dsn, state, sent, dry=False, now=T + 180)
    assert [c for c, _ in sent.logons] == [113]


def test_dry_run_first_run_sends_nothing(conn, dsn) -> None:
    snapshots(conn)
    online(conn, 12)
    sent = Sent()
    cycle(dsn, {}, sent, dry=True)
    assert sent.items == []


def test_a_failed_read_raises_before_anything_is_recorded(dsn) -> None:
    state: dict = {}
    with pytest.raises(psycopg.Error):
        never_flew.run("postgresql://nobody:x@127.0.0.1:1/nothing_test?connect_timeout=2", state, 24, False, Sent(), now=T)
    assert not state.get(never_flew.STATE_KEY)


# ----------------------------------------------------------------------------------------------- 4. the watchdog login
@pytest.fixture()
def as_watchdog(conn):
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'watchdog_ro'").fetchone() is None:
        pytest.skip("role watchdog_ro not created (db/roles.sql creates it; see the CI step)")
    conn.execute("SET ROLE watchdog_ro")
    try:
        yield conn
    finally:
        conn.execute("RESET ROLE")


def test_watchdog_ro_reads_the_alert_view_within_its_timeout(conn) -> None:
    world(conn)
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'watchdog_ro'").fetchone() is None:
        pytest.skip("role watchdog_ro not created")
    cfg = conn.execute("SELECT rolconfig FROM pg_roles WHERE rolname = 'watchdog_ro'").fetchone()["rolconfig"]
    assert "statement_timeout=10s" in cfg and "default_transaction_read_only=on" in cfg
    conn.execute("SET ROLE watchdog_ro")                       # SET ROLE does not apply role settings: set the login's timeout by hand
    try:
        conn.execute("SET statement_timeout = '10s'")
        t0 = time.monotonic()
        rows = conn.execute("SELECT cid, kind FROM ops_never_flew_alert ORDER BY cid").fetchall()
        assert [r["cid"] for r in rows] == [11, 12, 21, 24, 26]
        assert time.monotonic() - t0 < 10
    finally:
        conn.execute("RESET statement_timeout")
        conn.execute("RESET ROLE")


@pytest.mark.parametrize("obj", ["pilot_never_flew_90d", "flight", "pilot_latest", "app_setting",
                                 "controller_frequency_event", "controller_rating_event"])
def test_watchdog_ro_gets_nothing_new_beyond_the_alert_view(conn, as_watchdog, obj: str) -> None:
    """The login reads the alert view only: not the shared panel view and not the tables and views under it that it could not read
    before (flight_summary and pilot_session were already readable by it and stay so)."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(f"SELECT * FROM {obj} LIMIT 1")      # noqa: S608


def test_watchdog_ro_cannot_write_through_or_change_the_view(conn, as_watchdog) -> None:
    for sql in ("DELETE FROM ops_never_flew_alert", "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
                "VALUES (1,'X',now(),now(),now())", "CREATE OR REPLACE VIEW ops_never_flew_alert AS SELECT 1 AS cid"):
        with pytest.raises(psycopg.Error):
            conn.execute(sql)


def test_privileges_of_both_roles_on_the_two_new_views(conn) -> None:
    """Row-level matrix for the two new views, whichever way the roles are applied: grafana_ro reads the shared panel view only,
    watchdog_ro reads the alert view only, nobody else has either, and nobody can write to them."""
    expect = {("grafana_ro", "pilot_never_flew_90d"): True, ("grafana_ro", "ops_never_flew_alert"): False,
              ("watchdog_ro", "pilot_never_flew_90d"): False, ("watchdog_ro", "ops_never_flew_alert"): True,
              ("collector", "pilot_never_flew_90d"): False, ("collector", "ops_never_flew_alert"): False,
              ("backup_ro", "pilot_never_flew_90d"): True, ("backup_ro", "ops_never_flew_alert"): True}
    for (role, view), want in expect.items():
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None:
            pytest.skip(f"role {role} not created")
        got = conn.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS s", (role, view)).fetchone()["s"]
        assert got is want, (role, view)
        for priv in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            assert not conn.execute("SELECT has_table_privilege(%s, %s, %s) AS s", (role, view, priv)).fetchone()["s"], (role, view, priv)


def test_grafana_ro_still_reads_the_panel_and_cannot_read_the_alert_view(conn) -> None:
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro'").fetchone() is None:
        pytest.skip("role grafana_ro not created")
    finished(conn, 101, days_ago=2, airborne=0)
    conn.execute("SET ROLE grafana_ro")
    try:
        assert len(run_panel(conn, DASH, TITLE)) == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM ops_never_flew_alert")
    finally:
        conn.execute("RESET ROLE")


def test_panel_query_in_the_dashboard_has_no_sql_of_its_own_rule(conn) -> None:
    """The panel and the alert read one rule: the panel query reads the shared view and does not carry a second copy."""
    sql = panel(DASH, TITLE)["targets"][0]["rawSql"]
    assert "pilot_never_flew_90d" in sql and "flight_summaries" not in sql and "SUBSTRING" not in sql.upper()
