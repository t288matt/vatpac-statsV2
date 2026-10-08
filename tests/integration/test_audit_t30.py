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
