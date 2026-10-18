"""T30 test audit: tests added to kill mutants that survived the existing suite (see docs/TEST_AUDIT.md).

Each test names the mutant it kills. Style follows the existing integration tests: synthetic rows, rules taken from
SPEC and docs/CHART_DEFINITIONS.md, expected values written by hand (not derived from the SQL under test).
"""
from __future__ import annotations

import calendar
import copy
import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import REPO

import test_atc_sessions_t15 as atc15
import test_atc_t14 as atc14
import test_controller_retention as ret
import test_events_t14 as ev14
import test_online_hours as hours
import test_pilots_t14 as p14
from drop_gate_helpers import process_flights
from helpers import apply, at, partition_for
from helpers import T0, flights, make_session, run, set_poll_interval

pytestmark = pytest.mark.integration

T1 = datetime(2026, 10, 7, 16, 11, 19, tzinfo=timezone.utc)


@pytest.fixture()
def db(conn):
    partition_for(conn, T1)
    return conn


def test_controller_whitelist_is_an_exact_case_sensitive_match(db, payload, expected, refdata) -> None:
    """Mutants C08 (prefix match) and C09 (case-insensitive match): a callsign that merely starts with, or differs
    only in case from, a whitelisted one must not be stored."""
    p = at(payload)
    base = next(c for c in p["controllers"] if c["callsign"] == "ML_APP")
    for i, cs in enumerate(("ML_APP2", "ML_APP_X", "ml_app", "Ml_App")):
        extra = copy.deepcopy(base)
        extra.update(callsign=cs, cid=9990000 + i)
        p["controllers"].append(extra)
    apply(db, p, refdata)
    stored = sorted(r["callsign"] for r in db.execute("SELECT callsign FROM controller_session").fetchall())
    assert stored == expected["controllers_kept_callsigns"]


def test_pilots_with_plan_needs_both_departure_and_arrival(db, payload, expected, refdata) -> None:
    """Mutant C11: pilots_with_plan counted a pilot with only one end filed. The fixture has pilots inside the
    polygon with a departure but an empty arrival (TESTNOARR1/2); they must not be counted."""
    res = apply(db, at(payload), refdata)
    assert res.pilots_with_plan == len(expected["pilots_passing_flight_gate_inside"])


# ------------------------------------------------------------------ flight boundaries

def test_groundspeed_of_exactly_60_kt_is_airborne(conn) -> None:
    """Mutant S01 (`gs_kt > 60`): SPEC section 5 says airborne = groundspeed of 60 kt or more at the earlier
    sample. The existing tests use 59 and 250 but never 60 itself."""
    make_session(conn, 9700001, run(5, gs=60))
    row = flights(conn, 9700001)[0]
    assert float(row["airborne_minutes"]) == pytest.approx(4 * 15 / 60, abs=0.01)  # 1.0


def test_samples_exactly_120_minutes_apart_still_count_as_airborne_time(conn) -> None:
    """Mutant S03 (`<` instead of `<=` on the 120 minute pair rule): two samples exactly 120 minutes apart are in the
    same gap segment, so the earlier one contributes its capped gap. The cap is what the collector publishes at the
    default 30 s poll interval, 2.5 x 30 = 75 s (ADR-024); the schema seed of 40 s is only a fallback."""
    conn.execute("UPDATE app_setting SET value = '75' WHERE key = 'airborne_gap_cap_seconds'")
    make_session(conn, 9700002, [(T0, 250), (T0 + timedelta(minutes=120), 250)])
    rows = flights(conn, 9700002)
    assert len(rows) == 1
    assert float(rows[0]["airborne_minutes"]) == pytest.approx(75 / 60, abs=0.01)


def test_samples_121_minutes_apart_add_no_airborne_time(conn) -> None:
    """A pair more than 120 minutes apart counts zero. Since issue 89 phase 3 a silence inside one connection no longer cuts
    the flight in two (v1 never cut a connection), so this is ONE flight with no airborne time."""
    make_session(conn, 9700003, [(T0, 250), (T0 + timedelta(minutes=121), 250)])
    rows = flights(conn, 9700003)
    assert len(rows) == 1
    assert [float(r["airborne_minutes"]) for r in rows] == [0.0]


def test_airborne_minutes_keep_two_decimals(conn) -> None:
    """Mutant S08 (round to 0 places): 7 gaps of 15 s = 105 s = 1.75 minutes."""
    make_session(conn, 9700004, run(8, gs=250))
    assert float(flights(conn, 9700004)[0]["airborne_minutes"]) == 1.75


# ------------------------------------------------------------------ partition retention boundaries
SESSION_SQL = ("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
               "VALUES (%s,%s,%s,%s,%s) RETURNING id")


def _partitions(conn) -> set[str]:
    return {r["p"] for r in conn.execute(
        "SELECT c.relname AS p FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "WHERE i.inhparent = 'pilot_position'::regclass").fetchall()}


def _session_with_position(conn, cid: int, day: datetime, last_seen: datetime) -> None:
    partition_for(conn, day)
    sid = conn.execute(SESSION_SQL, (cid, f"RET{cid}", day, day, max(day, last_seen))).fetchone()["id"]
    conn.execute("INSERT INTO pilot_position (observed_at, session_id, gs_kt) VALUES (%s,%s,250)", (day, sid))


def test_retention_drops_exactly_the_days_older_than_ninety(conn) -> None:
    """Mutants S20 (`>=`) and S22 (retention counted in 12 hour days). Today 07-10-2026, retention 90 days:
    the cut-off is 09-07-2026 00:00 UTC. The partition for 08-07-2026 ends exactly at the cut-off and goes; the
    one for 09-07-2026 stays; so does one 60 days old (a halved retention would drop it)."""
    cases = {9710001: date(2026, 7, 8), 9710002: date(2026, 7, 9), 9710003: date(2026, 8, 8)}
    for cid, d in cases.items():
        day = datetime(d.year, d.month, d.day, 10, tzinfo=timezone.utc)
        _session_with_position(conn, cid, day, day + timedelta(minutes=5))
    process_flights(conn)  # issue 89 phase 4: the gate drops only what the flight writer has processed
    dropped = conn.execute("SELECT drop_old_pilot_position_partitions(90, DATE '2026-10-07') AS n").fetchone()["n"]
    assert dropped == 1
    assert _partitions(conn) == {"pilot_position_20260709", "pilot_position_20260808"}


def test_session_last_seen_exactly_at_the_cutoff_pins_its_partition(conn) -> None:
    """Mutant S21 (`>` instead of `>=` in the pinning rule)."""
    old = datetime(2026, 6, 1, 10, tzinfo=timezone.utc)
    cutoff = datetime(2026, 7, 9, tzinfo=timezone.utc)
    _session_with_position(conn, 9710010, old, cutoff)
    process_flights(conn)  # processed, so only the pinning rule can keep the partition (kills the `>` mutant)
    assert conn.execute("SELECT drop_old_pilot_position_partitions(90, DATE '2026-10-07') AS n").fetchone()["n"] == 0
    assert "pilot_position_20260601" in _partitions(conn)


def test_ensure_partitions_creates_yesterday_and_seven_days_ahead(conn) -> None:
    """Mutants S24 and S25: the docstring promises yesterday..today+7 (yesterday covers a snapshot that straddles
    midnight). The existing test only checks today..today+7."""
    from vatpac.maintenance import ensure_partitions

    ensure_partitions(conn, date(2026, 10, 7), 7)
    got = {p for p in _partitions(conn) if p.startswith("pilot_position_2026")}
    assert got == {f"pilot_position_202610{d:02d}" for d in range(6, 15)}


def test_daily_maintenance_creates_seven_partitions_ahead_by_default(conn) -> None:
    """Mutant C27 (PARTITIONS_AHEAD = 1): `run_maintenance` relies on the default, which no test exercised."""
    from vatpac.maintenance import run_maintenance
    from vatpac.settings import Settings

    out = run_maintenance(conn, Settings())
    assert out["failed"] == 0
    today = datetime.now(timezone.utc).date()
    assert f"pilot_position_{today + timedelta(days=7):%Y%m%d}" in _partitions(conn)


# ------------------------------------------------------------------ controller reconnect merge
def _ctrl_session(conn, first: datetime, last: datetime, cid: int = 9720001, callsign: str = "TEST_TWR") -> None:
    conn.execute("INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen, rating) "
                 "VALUES (%s,%s,%s,%s,%s,3)", (cid, callsign, first.replace(microsecond=0), first, last))


def _merged(conn, cid: int = 9720001) -> list[dict]:
    return conn.execute("SELECT start_time, end_time, session_count FROM controller_session_merged "
                        "WHERE cid = %s ORDER BY start_time", (cid,)).fetchall()


RECONNECT_START = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def _reconnect(conn, cid: int, gap: timedelta) -> list[dict]:
    """One controller session of 30 minutes, then a second one starting ``gap`` after the first one's last sighting."""
    a = RECONNECT_START
    _ctrl_session(conn, a, a + timedelta(minutes=30), cid)
    _ctrl_session(conn, a + timedelta(minutes=30) + gap, a + timedelta(minutes=60) + gap, cid)
    return _merged(conn, cid)


def test_reconnect_after_exactly_five_minutes_is_one_session(conn) -> None:
    """Mutant V02 (`>=` instead of `>`). SPEC: reconnects within 5 minutes of the previous last sighting are merged;
    5:00 exactly is within."""
    one = _reconnect(conn, 9720001, timedelta(minutes=5))
    assert len(one) == 1 and one[0]["session_count"] == 2
    assert one[0]["start_time"] == RECONNECT_START and one[0]["end_time"] == RECONNECT_START + timedelta(minutes=65)


def test_reconnect_after_five_minutes_and_a_second_is_two_sessions(conn) -> None:
    """Mutants V01 (window 10 minutes) and a window of 5:01 or more: one second over the limit is a new session (issue #49)."""
    two = _reconnect(conn, 9720002, timedelta(minutes=5, seconds=1))
    assert [r["session_count"] for r in two] == [1, 1]
    assert two[1]["start_time"] == RECONNECT_START + timedelta(minutes=35, seconds=1)


def test_reconnect_after_seven_minutes_is_two_sessions(conn) -> None:
    """Mutants V01 (window 10 minutes) and a window of 7 minutes (issue #49): a 7 minute gap is a new session."""
    two = _reconnect(conn, 9720003, timedelta(minutes=7))
    assert [r["session_count"] for r in two] == [1, 1]


def test_reconnect_gap_is_measured_from_the_latest_sighting_of_all_earlier_sessions(conn) -> None:
    """Mutant: gap measured from the immediately preceding session instead of the latest last_seen so far. B lies inside
    A and ends early (10:20); C starts at 10:34, 14 minutes after B but 4 minutes after A (10:30), so all three merge."""
    a = RECONNECT_START
    _ctrl_session(conn, a, a + timedelta(minutes=30), 9720004)
    _ctrl_session(conn, a + timedelta(minutes=10), a + timedelta(minutes=20), 9720004)
    _ctrl_session(conn, a + timedelta(minutes=34), a + timedelta(minutes=50), 9720004)
    merged = _merged(conn, 9720004)
    assert len(merged) == 1 and merged[0]["session_count"] == 3 and merged[0]["end_time"] == a + timedelta(minutes=50)


def test_reconnects_of_other_callsigns_or_other_controllers_are_never_merged(conn) -> None:
    a = RECONNECT_START
    _ctrl_session(conn, a, a + timedelta(minutes=30), 9720005, callsign="ML_APP")
    _ctrl_session(conn, a + timedelta(minutes=31), a + timedelta(minutes=60), 9720005, callsign="ML_TWR")
    _ctrl_session(conn, a + timedelta(minutes=31), a + timedelta(minutes=60), 9720006, callsign="ML_APP")
    assert len(_merged(conn, 9720005)) == 2 and len(_merged(conn, 9720006)) == 1


# ------------------------------------------------------------------ online_now windows
def _snap(conn, ts: datetime) -> None:
    conn.execute("INSERT INTO network_snapshot VALUES (%s,1,1,1,1)", (ts,))


def test_online_now_means_seen_in_the_last_two_snapshots(conn) -> None:
    """Mutants V06 and V08 (window widened to 30 min): online = seen within 2 x the poll interval of the newest
    snapshot (ADR-024; app_setting poll_interval_seconds, published by the collector, 30 s so a 60 s window).
    Pilots and controllers 10 s and 45 s old are online, 90 s old are not."""
    conn.execute("INSERT INTO app_setting (key, value) VALUES ('poll_interval_seconds', '30') "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
    now = datetime.now(timezone.utc)
    _snap(conn, now - timedelta(seconds=5))
    for cid, age in ((9730001, 10), (9730002, 45), (9730003, 90)):
        t = now - timedelta(seconds=age)
        conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
                     "VALUES (%s,%s,%s,%s,%s)", (cid, f"ON{cid}", (t - timedelta(hours=1)).replace(microsecond=0), t - timedelta(hours=1), t))
        conn.execute("INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen) "
                     "VALUES (%s,%s,%s,%s,%s)", (cid, f"ON{cid}_TWR", (t - timedelta(hours=1)).replace(microsecond=0), t - timedelta(hours=1), t))
    rows = conn.execute("SELECT kind, cid FROM online_now ORDER BY kind, cid").fetchall()
    assert [(r["kind"], r["cid"]) for r in rows] == [("controller", 9730001), ("controller", 9730002),
                                                       ("pilot", 9730001), ("pilot", 9730002)]


def test_online_lists_exclude_sessions_when_the_collector_has_been_silent(conn) -> None:
    """Mutants V09 and V10 (5 minute wall-clock guard widened to 5 hours) on controller_online_now and
    pilot_online_now: the newest snapshot and the sessions are 20 minutes old, so nobody is online."""
    old = datetime.now(timezone.utc) - timedelta(minutes=20)
    _snap(conn, old - timedelta(seconds=15))
    _snap(conn, old)
    partition_for(conn, old)
    sid = conn.execute(SESSION_SQL, (9730010, "SILENT", (old - timedelta(hours=1)).replace(microsecond=0),
                                     old - timedelta(hours=1), old)).fetchone()["id"]
    conn.execute("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt) "
                 "VALUES (%s,%s,-33.9,151.2,5000,250)", (old, sid))
    conn.execute("INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen) "
                 "VALUES (9730011,'SILENT_TWR',%s,%s,%s)", ((old - timedelta(hours=1)).replace(microsecond=0), old - timedelta(hours=1), old))
    assert conn.execute("SELECT count(*) AS n FROM pilot_online_now").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM controller_online_now").fetchone()["n"] == 0


# ------------------------------------------------------------------ maintenance housekeeping
def test_trim_poll_log_removes_only_rows_older_than_400_days(conn) -> None:
    """Mutants C28 (4 days) and C31 (deletes recent rows): poll_log retention is 400 days (SPEC section 6)."""
    from vatpac.maintenance import trim_poll_log

    for days in (401, 399, 1):
        conn.execute("INSERT INTO poll_log (status, polled_at) VALUES ('ok', now() - make_interval(days => %s))", (days,))
    assert trim_poll_log(conn) == 1
    ages = sorted(round(r["d"]) for r in conn.execute(
        "SELECT extract(epoch FROM now() - polled_at) / 86400 AS d FROM poll_log").fetchall())
    assert ages == [1, 399]


def test_flight_refresh_writes_finished_flights_not_flights_still_open(conn) -> None:
    """Issue 89 phase 5 replaces mutant C29 (the 12 h window of the pieces refresh, which no longer exists): refresh_flights() calls the
    writer, which writes a flight 8 hours after its last position (against the newest snapshot) and not before."""
    from vatpac.maintenance import refresh_flights

    now = datetime.now(timezone.utc).replace(microsecond=0)
    make_session(conn, 9740001, run(10, start=now - timedelta(hours=11)))
    make_session(conn, 9740002, run(10, start=now - timedelta(hours=5)))
    conn.execute("INSERT INTO network_snapshot VALUES (%s,1,1,1,1)", (now,))
    assert refresh_flights(conn) == 1
    got = [r["cid"] for r in conn.execute("SELECT cid FROM flight_summary WHERE cid IN (9740001, 9740002)").fetchall()]
    assert got == [9740001]


def test_sync_settings_publishes_the_values_the_views_read(conn) -> None:
    """The flight view reads app_setting (home prefixes, airborne gap cap); sync_settings must write the configured
    values, not defaults. Custom values are used so a hard-coded default would fail."""
    from vatpac.maintenance import sync_settings
    from vatpac.settings import Settings

    try:
        sync_settings(conn, Settings(home_icao_prefixes="y,nz", airborne_gap_cap_seconds=55))
        rows = {r["key"]: r["value"] for r in conn.execute(
            "SELECT key, value FROM app_setting WHERE key IN ('home_icao_prefixes', 'airborne_gap_cap_seconds')").fetchall()}
        assert rows == {"home_icao_prefixes": "Y,NZ", "airborne_gap_cap_seconds": "55"}
    finally:
        conn.execute("DELETE FROM app_setting WHERE key IN ('home_icao_prefixes', 'airborne_gap_cap_seconds')")


def test_a_zzzz_arrival_is_flagged_as_zzzz(conn) -> None:
    """Mutant S13 (`is_zzzz` looked only at the departure): the existing flag test only has ZZZZ as departure."""
    make_session(conn, 9700010, run(5), dep="YSSY", arr="ZZZZ")
    row = flights(conn, 9700010)[0]
    assert row["is_zzzz"] is True and row["is_circuit"] is False


def test_run_maintenance_uses_the_configured_retention(conn) -> None:
    """Mutant C30 (retention hard-coded to 1 day): a 40 day old partition must survive a 60 day retention."""
    from vatpac.maintenance import run_maintenance
    from vatpac.settings import Settings

    old = datetime.now(timezone.utc) - timedelta(days=40)
    name = partition_for(conn, old)
    out = run_maintenance(conn, Settings(position_retention_days=60))
    assert out["failed"] == 0
    assert name in _partitions(conn)


# ------------------------------------------------------------------ Monday attendance, controller open flag, views
def _monday_snapshot(conn, mon: datetime) -> None:
    conn.execute("INSERT INTO network_snapshot VALUES (%s,0,0,0,0)", (mon,))


def test_monday_attendance_edges_for_controllers_and_unplanned_pilots(conn) -> None:
    """Mutants S17 (controller ending exactly 0700 dropped) and S18 (pilot gate ignores an empty arrival)."""
    mon = ev14.monday_utc(2)
    _monday_snapshot(conn, mon)

    def h(x: float) -> datetime:
        return mon + timedelta(hours=x)

    ev14.add_controller(conn, 9810001, h(5), h(7))              # ends exactly 0700: present
    ev14.add_flight(conn, 9810002, h(8), h(9), arr="")          # no arrival filed: not a flight, not counted
    ev14.add_flight(conn, 9810003, h(8), h(9))                  # counted
    rows = ev14.run_panel(conn, ev14.EVENTS, "Monday event attendance", mon - timedelta(days=1), mon + timedelta(days=1))
    assert [(r["Pilots"], r["Controllers"]) for r in rows] == [(1, 1)]


def test_monday_attendance_includes_the_current_weeks_monday(conn) -> None:
    """Mutant S19 (series stops a week early)."""
    now = datetime.now(timezone.utc)
    _monday_snapshot(conn, now - timedelta(days=21))
    this_monday = now.date() - timedelta(days=now.weekday())
    got = conn.execute("SELECT max(monday_date) AS d FROM monday_attendance").fetchone()["d"]
    assert got == this_monday


def test_controller_is_open_only_when_seen_in_the_newest_snapshot(conn) -> None:
    """Mutant V05 (is_open window widened to 30 min): the window is 2 x the poll interval (ADR-024); the session seen 5 s
    ago is open, the one last seen 10 minutes ago is not, at any interval up to 5 minutes."""
    set_poll_interval(conn)
    now = datetime.now(timezone.utc)
    _snap(conn, now)
    _ctrl_session(conn, now - timedelta(hours=1), now - timedelta(seconds=5), 9820001, "OPEN_TWR")
    _ctrl_session(conn, now - timedelta(hours=2), now - timedelta(minutes=10), 9820002, "SHUT_TWR")
    got = {r["cid"]: r["is_open"] for r in conn.execute("SELECT cid, is_open FROM controller_session_merged").fetchall()}
    assert got == {9820001: True, 9820002: False}


def test_rating_groups_s3_and_c3_have_their_own_columns(conn) -> None:
    """Mutants V11 and V12 (rank_group_of mislabels ratings 7 and 4): the existing test only asserts S3 and C3 are 0."""
    atc14.add_session(conn, 9830001, atc14.M1, 90, rating=4)
    atc14.add_session(conn, 9830002, atc14.M1, 90, rating=7)
    row = atc14.by_month(conn, atc14.RATING_TITLE)[atc14.M1]
    assert {k: v for k, v in row.items() if k != "time"} == {"S1": 0, "S2": 0, "S3": 1, "C1": 0, "C3": 1, "I1+": 0}


def test_movements_ignore_zzzz_and_cohorts_ignore_cids_below_100000(conn) -> None:
    """Mutants V16 (ZZZZ departures counted as movements) and V17 (cohort 0 included)."""
    p14.add_flight(conn, 9840001, dep="YSSY", arr="ZZZZ")
    assert conn.execute("SELECT count(*) AS n FROM movements").fetchone()["n"] == 0
    p14.add_flight(conn, 50000, dep="YSSY", arr="YMML")
    p14.add_flight(conn, 150000, dep="YSSY", arr="YMML")
    assert [r["cohort"] for r in conn.execute("SELECT DISTINCT cohort FROM cohorts WHERE cid IN (50000, 150000)").fetchall()] == ["100000-199999"]


def test_data_freshness_counts_only_the_last_hour(conn) -> None:
    """Mutant V18 (hour -> day)."""
    conn.execute("INSERT INTO poll_log (status, polled_at) VALUES ('ok', now() - interval '2 hours')")
    conn.execute("INSERT INTO poll_log (status, polled_at) VALUES ('ok', now())")
    assert conn.execute("SELECT ok_last_hour FROM data_freshness").fetchone()["ok_last_hour"] == 1


def test_pilot_latest_ignores_sessions_older_than_three_hours(conn) -> None:
    """Mutant V23 (3 h -> 30 h)."""
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    partition_for(conn, old)
    sid = conn.execute(SESSION_SQL, (9850001, "OLD1", old.replace(microsecond=0), old, old)).fetchone()["id"]
    conn.execute("INSERT INTO pilot_position (observed_at, session_id, gs_kt) VALUES (%s,%s,250)", (old, sid))
    assert conn.execute("SELECT count(*) AS n FROM pilot_latest").fetchone()["n"] == 0


# ------------------------------------------------------------------ pilot panels
def _panel(path, title: str) -> dict:
    return next(p for p in json.loads(path.read_text(encoding="utf-8"))["panels"] if p.get("title") == title)


def _days(spec: str | None) -> float:
    if not spec:
        return 30
    n, unit = int(spec[:-1]), spec[-1]
    return {"d": n, "h": n / 24, "M": n * 30}[unit]


def _run(conn, path, title: str) -> list[dict]:
    """Panel SQL with Grafana's macros expanded from the panel's own timeFrom (default 30 days)."""
    p = _panel(path, title)
    days = _days(p.get("timeFrom"))
    sql = (p["targets"][0]["rawSql"].replace("$__timeFrom()", f"(now() - interval '{days} days')")
           .replace("$__timeTo()", "now()"))
    return conn.execute(sql).fetchall()


def test_card_61_speed_and_altitude_boundaries(conn) -> None:
    """Mutants P19 (cap 750 -> 850 kt) and P20 (altitude `>=`): peak above 100 kt and not above 750 kt, highest
    altitude above 1000 ft."""
    p14.add_flight(conn, 9860001, callsign="OK1", aircraft="A388", gs=101, alt=1001)
    p14.add_flight(conn, 9860002, callsign="GS100", aircraft="A388", gs=100, alt=35000)
    p14.add_flight(conn, 9860003, callsign="ALT1000", aircraft="A388", gs=500, alt=1000)
    p14.add_flight(conn, 9860004, callsign="TOOFAST", aircraft="A388", gs=800, alt=35000)
    assert [r["callsign"] for r in _run(conn, p14.TRAFFIC, p14.SPEED)] == ["OK1"]


def test_card_93_a_pilot_with_exactly_30_airborne_minutes_counts(conn) -> None:
    """Mutant P30 (`>= 31`)."""
    p14.add_flight(conn, 9860010, airborne=30, days_ago=10)
    p14.add_flight(conn, 9860011, airborne=29.9, days_ago=10)
    assert p14.people(conn)["Pilots"] == 1


def test_international_and_domestic_routes_are_separated(conn) -> None:
    """Mutant P23 (domestic routes leak into the international panel)."""
    p14.add_flight(conn, 9860020, dep="YSSY", arr="YMML")
    p14.add_flight(conn, 9860021, dep="YSSY", arr="EGLL")
    p14.add_flight(conn, 9860022, dep="EGLL", arr="KJFK")
    intl = {r["route"] for r in _run(conn, p14.TRAFFIC, "Most popular international routes (last 30 days)")}
    dom = {r["route"] for r in _run(conn, p14.TRAFFIC, "Most popular domestic routes (last 30 days)")}
    assert intl == {"YSSY-EGLL", "EGLL-KJFK"} and dom == {"YSSY-YMML"}


def test_vfr_routes_need_three_distinct_pilots(conn) -> None:
    """Mutant P22 (minimum 3 pilots -> 2)."""
    for cid, arr in ((9860030, "YBBN"), (9860031, "YBBN"), (9860032, "YBBN"), (9860033, "YMML"), (9860034, "YMML")):
        p14.add_flight(conn, cid, dep="YSSY", arr=arr, flight_rules="V")
    rows = _run(conn, p14.TRAFFIC, "Most popular VFR routes (last 30 days)")
    assert [(r["route"], r["pilots"]) for r in rows] == [("YSSY-YBBN", 3)]


def test_popular_callsigns_need_two_distinct_pilots(conn) -> None:
    """Mutant P24 (minimum 2 pilots -> 1)."""
    p14.add_flight(conn, 9860040, callsign="QFA1")
    p14.add_flight(conn, 9860041, callsign="QFA1")
    p14.add_flight(conn, 9860042, callsign="QFA2")
    rows = _run(conn, p14.TRAFFIC, "Most popular callsigns (last 30 days)")
    assert [(r["callsign"], r["unique_pilots"]) for r in rows] == [("QFA1", 2)]


def test_airlines_need_three_letters_and_a_digit(conn) -> None:
    """Mutants P26 and V14: a private registration such as VHABC must not appear as an airline (card 58)."""
    p14.add_flight(conn, 9860050, callsign="QFA123")
    p14.add_flight(conn, 9860051, callsign="VHABC")
    rows = _run(conn, p14.TRAFFIC, "Most popular airlines (last 30 days)")
    assert [r["flights"] for r in rows] == [1]


def test_selcal_percentage_uses_flights_with_remarks_and_the_sel_slash_marker(conn) -> None:
    """Mutants P27 (denominator includes flights without remarks) and V15 (`SEL` instead of `SEL/`)."""
    for cid, remarks in ((9860060, "RMK/ABC SEL/ABCD"), (9860061, "RMK/SELF TEST"), (9860062, "RMK/OK"), (9860063, None)):
        p14.add_flight(conn, cid, remarks=remarks)
    rows = _run(conn, p14.TRAFFIC, "Flights with SELCAL in the remarks")
    assert float(rows[0]["SELCAL (%)"]) == 33.33


def test_diverse_routes_only_count_flights_over_sixty_minutes(conn) -> None:
    """Mutant P28 (60 -> 6 minutes)."""
    for i, arr in enumerate(("YMML", "YBBN", "YPPH")):
        p14.add_flight(conn, 9860070, dep="YSSY", arr=arr, minutes=30, days_ago=1 + i)     # short hops: ignored
    for i, arr in enumerate(("YMML", "YBBN")):
        p14.add_flight(conn, 9860071, dep="YSSY", arr=arr, minutes=90, days_ago=1 + i)
    rows = _run(conn, p14.TRAFFIC, "Pilots with the most diverse routes (last 14 days)")
    assert [(r["pilot"], r["unique_routes"]) for r in rows] == [("Pilot 9860071", 2)]


def test_busiest_days_shows_five_rows(conn) -> None:
    """Mutant P25 (top 5 -> top 50)."""
    for i in range(7):
        p14.add_flight(conn, 9860080 + i, days_ago=1 + i)
    assert len(_run(conn, p14.TRAFFIC, "Busiest days by flights (last 6 months)")) == 5


def test_servers_panel_counts_home_region_pilots_only(conn) -> None:
    """Mutant P31 (filter on pilot_is_local removed)."""
    for cid, home, server in ((9860090, "YSSY", "SYDNEY"), (9860091, "EGLL", "LONDON")):
        p14.add_flight(conn, cid, days_ago=0.2, home_icao=home, server=server)
    rows = _run(conn, p14.ORIGINS, "Servers used by home-region pilots (last 24 hours)")
    assert [(r["server"], r["pilots"]) for r in rows] == [("SYDNEY", 1)]


def test_military_aircraft_panel_lists_only_military_types(conn) -> None:
    """Mutant P37 (military flag inverted)."""
    p14.add_flight(conn, 9860100, aircraft="F16", days_ago=1)
    p14.add_flight(conn, 9860101, aircraft="B738", days_ago=1)
    rows = _run(conn, ev14.MILITARY, "Most popular military aircraft (unique pilots, last 7 days)")
    assert [r["aircraft"] for r in rows] == ["F16"]


def test_raafv_routes_panel_shows_at_most_25_routes(conn) -> None:
    """Mutant P38 (limit 25 -> 2)."""
    now = datetime.now(timezone.utc)
    t = now - timedelta(days=2)
    for i in range(26):
        ev14.add_flight(conn, 9860200 + i, t - timedelta(hours=2), t, 60, dep=f"Y{i:03d}", arr="YMML",
                        remarks=ev14.RAAFV_REMARKS)
    rows = ev14.run_panel(conn, ev14.MILITARY, "Most popular RAAFv routes", now - timedelta(days=30), now)
    assert len(rows) == 25


def test_departure_board_airborne_starts_at_60_kt(conn) -> None:
    """Mutant P17 (`> 60`): the board labels 60 kt AIRBORNE and 59 kt TAXIING."""
    now = datetime.now(timezone.utc)
    _snap(conn, now - timedelta(seconds=15))
    _snap(conn, now)
    partition_for(conn, now)
    for cid, cs, gs in ((9870001, "BRD60", 60), (9870002, "BRD59", 59)):
        sid = conn.execute(
            "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
            "VALUES (%s,%s,%s,%s,%s,'YSSY','YMML') RETURNING id",
            (cid, cs, (now - timedelta(hours=1)).replace(microsecond=0), now - timedelta(hours=1), now)).fetchone()["id"]
        conn.execute("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt) "
                     "VALUES (%s,%s,-33.9,151.2,100,%s)", (now, sid, gs))
    sql = _panel(p14.TRAFFIC.parent / "pilots-now.json", "Departure board")["targets"][0]["rawSql"].replace(
        "${airport:sqlstring}", "'YSSY'")
    got = {r["Flight"]: r["Status"] for r in conn.execute(sql).fetchall()}
    assert got == {"BRD60": "AIRBORNE", "BRD59": "TAXIING"}


def test_pilots_online_now_shows_the_newest_snapshot(conn) -> None:
    """Mutant P18 (ORDER BY ... ASC): the 'now' tile must read the newest snapshot, not the oldest."""
    now = datetime.now(timezone.utc)
    conn.execute("INSERT INTO network_snapshot VALUES (%s,3,0,30,0)", (now - timedelta(hours=1),))
    conn.execute("INSERT INTO network_snapshot VALUES (%s,7,0,70,0)", (now - timedelta(seconds=10),))
    pn = p14.TRAFFIC.parent / "pilots-now.json"
    assert _run(conn, pn, "Pilots online now (in region)")[0]["Pilots"] == 7
    assert _run(conn, pn, "Pilots online now (network-wide)")[0]["Pilots"] == 70


# ------------------------------------------------------------------ ATC panels
def test_controllers_logged_on_needs_more_than_sixty_minutes(conn) -> None:
    """Mutant P05 (60 -> 6 minutes) on card 115."""
    atc15.add(conn, 9880001, atc15.days_ago(3), 45, rating=2)
    atc15.add(conn, 9880002, atc15.days_ago(3), 90, rating=2)
    rows = {r["rank_group"]: r["controllers"] for r in atc15.run(conn, "Controllers logged on (last 30 days)")}
    assert rows["S1"] == 1


def test_top_50_providers_includes_short_sessions_and_stops_at_fifty(conn) -> None:
    """Mutants P07 (zero-length filter becomes 600 minutes) and P06 (limit 50 -> 5)."""
    title = "Top 50 service providers (last 30 days)"
    atc15.add(conn, 9880100, atc15.days_ago(3), 5)
    assert len(atc15.run(conn, title)) == 1
    for i in range(51):
        atc15.add(conn, 9881000 + i, atc15.days_ago(3), 10 + i)
    assert len(atc15.run(conn, title)) == 50


def test_card_63_exactly_sixty_minutes_does_not_count(conn) -> None:
    """Mutant P02 (`>= 60`)."""
    atc14.add_session(conn, 9890001, atc14.M1, 60, callsign="TEST_CTR")
    assert atc14.by_month(conn, atc14.CTR_TITLE)[atc14.M1]["Controllers"] == 0


def test_card_63_window_is_30_days_to_month_end(conn) -> None:
    """Mutant P03 (window 30 -> 25 days): a session 28 days before the end of the month is inside."""
    length = calendar.monthrange(atc14.M1.year, atc14.M1.month)[1]
    atc14.add_session(conn, 9890002, atc14.M1, 90, callsign="TEST_CTR", day_offset=length - 28)
    assert atc14.by_month(conn, atc14.CTR_TITLE)[atc14.M1]["Controllers"] == 1


def test_card_116_exactly_sixty_minutes_does_not_count(conn) -> None:
    """Mutant P04 (`>= 60`)."""
    atc14.add_session(conn, 9890003, atc14.M1, 60, rating=3)
    assert atc14.by_month(conn, atc14.RATING_TITLE)[atc14.M1]["S2"] == 0


def test_retention_active_window_is_thirty_days(conn) -> None:
    """Mutant P08 (30 -> 31 days)."""
    ret.add_session(conn, 9000001, 30.5, 10)
    assert ret.scalar(conn, "Active controllers") == 0


def test_retention_rate_needs_sessions_over_sixty_minutes(conn) -> None:
    """Mutant P10 (`>= 60`): a controller whose only recent session is exactly 60 minutes is not in the numerator."""
    start = (datetime.now(timezone.utc) - timedelta(days=5)).replace(microsecond=0)   # whole seconds: exact 60.0 minutes
    conn.execute("INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) "
                 "VALUES (9000001,'TEST9000001_TWR',%s,'Controller 9000001',3,%s,%s)",
                 (start, start, start + timedelta(minutes=60)))
    ret.add_session(conn, 9000002, 45, 30)
    assert float(ret.scalar(conn, "Controller retention rate")) == 0.0


def test_help_list_threshold_is_under_18_minutes(conn) -> None:
    """Mutant P11 (18 -> 25 minutes): a 20 minute session is not 'short'."""
    ret.add_session(conn, 9000003, 120, 30)    # a real earlier session: without one nobody is reactivated (issue 93, ADR-028)
    ret.add_session(conn, 9000003, 5, 20)
    assert ret.cids(conn, "Who might need help reactivating?") == set()
    assert ret.cids(conn, "Who reactivated after being away for more than 60 days?") == {9000003}


def test_online_hours_atc_minimums_are_inclusive(conn) -> None:
    """Mutants P14 and P16 (20 -> 21 and 30 -> 31 minutes): exactly the minimum counts (v1 uses >=)."""
    wed, sat = hours.last_day(3), hours.last_day(6)
    hours.add_controller(conn, wed, (11, 0), 20)
    hours.add_controller(conn, sat, (9, 0), 30)
    assert hours.run(conn, hours.WEEKDAYS) == {11: (0, 1)}
    assert hours.run(conn, hours.WEEKENDS) == {9: (0, 1)}


def test_online_hours_ignore_flights_older_than_thirty_days(conn) -> None:
    """Mutant P15 (window 30 -> 90 days)."""
    old = hours.last_day(3) - timedelta(days=42)
    hours.add_flight(conn, old, (10, 0), old.replace(hour=11))
    assert hours.run(conn, hours.WEEKDAYS) == {}


def test_panel_windows_match_their_titles() -> None:
    """Mutant P39 (the RAAFv 7 day panel overridden to 70 days in JSON): no existing test reads the panel's
    timeFrom except card 61. For every panel titled '(last N days|hours)', the window actually applied (panel
    timeFrom, else the dashboard range, else an interval literal in the SQL) must be N."""
    unit_hours = {"day": 24, "days": 24, "hour": 1, "hours": 1}
    bad = []
    for f in sorted((REPO / "grafana" / "dashboards").glob("*/*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        dash_from = d.get("time", {}).get("from", "")
        for p in d["panels"]:
            m = re.search(r"\(last (\d+) (days?|hours?)\b", p.get("title", ""))
            if not m or not p.get("targets"):
                continue
            want = int(m.group(1)) * unit_hours[m.group(2)]
            spec = p.get("timeFrom") or dash_from.replace("now-", "")
            sql = " ".join(t.get("rawSql", "") for t in p["targets"])
            lits = [int(n) * 24 for n in re.findall(r"interval '(\d+) days'", sql, re.I)]
            lits += [int(n) for n in re.findall(r"interval '(\d+) hours?'", sql, re.I)]
            got = {"d": 24, "h": 1}.get(spec[-1:], 0) * int(spec[:-1]) if spec[:-1].isdigit() else 0
            if want not in ([got] + lits):
                bad.append(f"{f.parent.name}/{f.name}: {p['title']} (title {want} h, window {got} h, sql {lits})")
    assert not bad, "\n".join(bad)


def _expand_macros(sql: str) -> str:
    """Grafana macros as the PostgreSQL data source expands them, for a 30 day range ending now."""
    sql = sql.replace("$__timeFrom()", "(now() - interval '30 days')").replace("$__timeTo()", "now()")
    sql = re.sub(r"\$__timeFilter\((\w+)\)", r"\1 BETWEEN (now() - interval '30 days') AND now()", sql)
    sql = re.sub(r"\$__timeGroupAlias\((\w+), \$__interval\)", r"date_trunc('minute', \1) AS time", sql)
    return sql.replace("${airport:sqlstring}", "'YSSY'")


def test_every_panel_query_runs_as_grafana_ro(conn) -> None:
    """Gap: 40 of the 84 query panels were never executed by any test (only parsed by validate_dashboards.py), and the
    ones that were ran as the superuser. Run all of them as the real read-only role, so a missing grant, a base-table
    reference or a runtime SQL error fails here instead of on the live dashboards. Empty data is fine (smoke test)."""
    conn.execute("SET ROLE grafana_ro")
    failures = []
    try:
        for f in sorted((REPO / "grafana" / "dashboards").glob("*/*.json")):
            for p in json.loads(f.read_text(encoding="utf-8"))["panels"]:
                for t in p.get("targets", []):
                    if "rawSql" not in t:
                        continue
                    try:
                        conn.execute(_expand_macros(t["rawSql"])).fetchall()
                    except Exception as exc:  # noqa: BLE001 - report every broken panel, not just the first
                        failures.append(f"{f.parent.name}/{f.name}: {p['title']}: {str(exc).splitlines()[0]}")
    finally:
        conn.execute("RESET ROLE")
    assert not failures, "\n".join(failures)
