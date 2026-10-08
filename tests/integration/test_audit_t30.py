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
from helpers import apply, at, partition_for
from test_flight_view import T0, flights, make_session, run

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



def test_replaying_a_snapshot_does_not_count_a_near_miss_poll_twice(db, payload, refdata) -> None:
    """Mutant C17 (`>=` instead of `>` on the polls_seen guard): replaying the same feed timestamp must not
    increase polls_seen, so a re-delivered snapshot cannot inflate the near-miss counters."""
    snap = at(payload)
    apply(db, snap, refdata)
    apply(db, snap, refdata)
    rows = db.execute("SELECT callsign, polls_seen FROM controller_unlisted_sighting ORDER BY callsign").fetchall()
    assert [(r["callsign"], r["polls_seen"]) for r in rows] == [("AD_XYZ_TWR", 1), ("ML_XYZ_CTR", 1)]



# ------------------------------------------------------------------ flight_summary boundaries

def test_groundspeed_of_exactly_60_kt_is_airborne(conn) -> None:
    """Mutant S01 (`gs_kt > 60`): SPEC section 5 says airborne = groundspeed of 60 kt or more at the earlier
    sample. The existing tests use 59 and 250 but never 60 itself."""
    make_session(conn, 9700001, run(5, gs=60))
    row = flights(conn, 9700001)[0]
    assert float(row["airborne_minutes"]) == pytest.approx(4 * 15 / 60, abs=0.01)  # 1.0



def test_samples_exactly_120_minutes_apart_still_count_as_airborne_time(conn) -> None:
    """Mutant S03 (`<` instead of `<=` on the 120 minute pair rule): two samples exactly 120 minutes apart are in the
    same gap segment, so the earlier one contributes its capped gap (40 s)."""
    make_session(conn, 9700002, [(T0, 250), (T0 + timedelta(minutes=120), 250)])
    rows = flights(conn, 9700002)
    assert len(rows) == 1
    assert float(rows[0]["airborne_minutes"]) == pytest.approx(40 / 60, abs=0.01)



def test_samples_121_minutes_apart_add_no_airborne_time(conn) -> None:
    make_session(conn, 9700003, [(T0, 250), (T0 + timedelta(minutes=121), 250)])
    rows = flights(conn, 9700003)
    assert len(rows) == 2
    assert [float(r["airborne_minutes"]) for r in rows] == [0.0, 0.0]



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
    dropped = conn.execute("SELECT drop_old_pilot_position_partitions(90, DATE '2026-10-07') AS n").fetchone()["n"]
    assert dropped == 1
    assert _partitions(conn) == {"pilot_position_20260709", "pilot_position_20260808"}



def test_session_last_seen_exactly_at_the_cutoff_pins_its_partition(conn) -> None:
    """Mutant S21 (`>` instead of `>=` in the pinning rule)."""
    old = datetime(2026, 6, 1, 10, tzinfo=timezone.utc)
    cutoff = datetime(2026, 7, 9, tzinfo=timezone.utc)
    _session_with_position(conn, 9710010, old, cutoff)
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
                 "VALUES (%s,%s,%s,%s,%s,3)", (cid, callsign, first, first, last))


def _merged(conn, cid: int = 9720001) -> list[dict]:
    return conn.execute("SELECT start_time, end_time, session_count FROM controller_session_merged "
                        "WHERE cid = %s ORDER BY start_time", (cid,)).fetchall()


def test_reconnect_after_exactly_five_minutes_is_one_session(conn) -> None:
    """Mutants V01 (window 10 min) and V02 (`>=`). SPEC: reconnects within 5 minutes of the previous last sighting
    are merged. 5:00 exactly merges; 5:01 does not; 7 minutes does not."""
    a = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
    _ctrl_session(conn, a, a + timedelta(minutes=30), 9720001)
    _ctrl_session(conn, a + timedelta(minutes=35), a + timedelta(minutes=60), 9720001)        # exactly 5:00
    _ctrl_session(conn, a, a + timedelta(minutes=30), 9720002)
    _ctrl_session(conn, a + timedelta(minutes=35, seconds=1), a + timedelta(minutes=60), 9720002)  # 5:01
    _ctrl_session(conn, a, a + timedelta(minutes=30), 9720003)
    _ctrl_session(conn, a + timedelta(minutes=37), a + timedelta(minutes=60), 9720003)        # 7:00
    one = _merged(conn, 9720001)
    assert len(one) == 1 and one[0]["session_count"] == 2
    assert one[0]["start_time"] == a and one[0]["end_time"] == a + timedelta(minutes=60)
    assert len(_merged(conn, 9720002)) == 2
    assert len(_merged(conn, 9720003)) == 2



# ------------------------------------------------------------------ online_now windows
def _snap(conn, ts: datetime) -> None:
    conn.execute("INSERT INTO network_snapshot VALUES (%s,1,1,1,1)", (ts,))


def test_online_now_means_seen_in_the_last_two_snapshots(conn) -> None:
    """Mutants V06 and V08 (30 s window widened to 30 min): SPEC section 5, online = seen within the last 2
    snapshots (30 s before the newest). Pilots and controllers 10 s old are online, 45 s old are not."""
    now = datetime.now(timezone.utc)
    _snap(conn, now - timedelta(seconds=5))
    for cid, age in ((9730001, 10), (9730002, 45)):
        t = now - timedelta(seconds=age)
        conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
                     "VALUES (%s,%s,%s,%s,%s)", (cid, f"ON{cid}", t - timedelta(hours=1), t - timedelta(hours=1), t))
        conn.execute("INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen) "
                     "VALUES (%s,%s,%s,%s,%s)", (cid, f"ON{cid}_TWR", t - timedelta(hours=1), t - timedelta(hours=1), t))
    rows = conn.execute("SELECT kind, cid FROM online_now ORDER BY kind, cid").fetchall()
    assert [(r["kind"], r["cid"]) for r in rows] == [("controller", 9730001), ("pilot", 9730001)]



def test_online_lists_exclude_sessions_when_the_collector_has_been_silent(conn) -> None:
    """Mutants V09 and V10 (5 minute wall-clock guard widened to 5 hours) on controller_online_now and
    pilot_online_now: the newest snapshot and the sessions are 20 minutes old, so nobody is online."""
    old = datetime.now(timezone.utc) - timedelta(minutes=20)
    _snap(conn, old - timedelta(seconds=15))
    _snap(conn, old)
    partition_for(conn, old)
    sid = conn.execute(SESSION_SQL, (9730010, "SILENT", old - timedelta(hours=1), old - timedelta(hours=1), old)).fetchone()["id"]
    conn.execute("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt) "
                 "VALUES (%s,%s,-33.9,151.2,5000,250)", (old, sid))
    conn.execute("INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen) "
                 "VALUES (9730011,'SILENT_TWR',%s,%s,%s)", (old - timedelta(hours=1), old - timedelta(hours=1), old))
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



def test_flight_refresh_covers_sessions_seen_in_the_last_twelve_hours(conn) -> None:
    """Mutant C29 (window 12 h -> 1 minute): refresh_flights() with its default window refreshes a session last seen
    11 hours ago but not one last seen 13 hours ago."""
    from vatpac.maintenance import refresh_flights

    now = datetime.now(timezone.utc).replace(microsecond=0)
    make_session(conn, 9740001, run(10, start=now - timedelta(hours=11)))
    make_session(conn, 9740002, run(10, start=now - timedelta(hours=13)))
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
    """Mutant V05 (is_open window 30 s -> 30 min)."""
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
    sid = conn.execute(SESSION_SQL, (9850001, "OLD1", old, old, old)).fetchone()["id"]
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
        sid = p14.add_flight(conn, cid, dep="YSSY", arr=arr)
        conn.execute("UPDATE pilot_session SET flight_rules = 'V' WHERE id = %s", (sid,))
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
        sid = p14.add_flight(conn, cid)
        conn.execute("UPDATE pilot_session SET remarks = %s WHERE id = %s", (remarks, sid))
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
