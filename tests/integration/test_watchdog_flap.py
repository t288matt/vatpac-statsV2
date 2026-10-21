"""Issue 20: the watchdog's identity-flap queries against a real database, and the least-privilege role they run as.

The queries run as the owner (they need no more than SELECT); the role tests run as watchdog_ro, which migrate.sh
creates (CI creates it from the same SQL block), and skip when it does not exist."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
import psycopg.rows  # noqa: E402
pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("watchdog_under_test_db", REPO / "ops" / "watchdog" / "watchdog.py")
assert spec and spec.loader
wd = importlib.util.module_from_spec(spec)
sys.modules["watchdog_under_test_db"] = wd
spec.loader.exec_module(wd)

PILOT = ("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
         "VALUES (%s, %s, %s::timestamptz, now() - make_interval(hours => %s), now() - make_interval(hours => %s))")
CTRL = PILOT.replace("pilot_session", "controller_session")


def run(conn) -> tuple[str, str]:
    with conn.cursor(row_factory=psycopg.rows.tuple_row) as cur:  # the watchdog's own connection returns tuples
        return wd.check_identity_flap(cur)


def test_clean_database_is_ok(conn) -> None:
    assert run(conn)[0] == wd.OK


def test_pilot_pair_one_second_apart_is_found(conn) -> None:
    conn.execute(PILOT, (1001, "QFA1", "2026-10-08 04:06:12+00", 2, 1))
    conn.execute(PILOT, (1001, "QFA1", "2026-10-08 04:06:13+00", 1, 0))
    level, detail = run(conn)
    assert level == wd.WARN and detail.startswith("1 pilot and 0 controller")


def test_controller_pair_is_found(conn) -> None:
    conn.execute(CTRL, (2001, "ML_APP", "2026-10-08 04:06:12+00", 2, 1))
    conn.execute(CTRL, (2001, "ML_APP", "2026-10-08 04:06:14+00", 1, 0))  # 2 s apart is not "within 2 s": quiet
    assert run(conn)[0] == wd.OK
    conn.execute(CTRL, (2001, "ML_APP", "2026-10-08 04:06:13+00", 1, 0))
    assert "2 controller" in run(conn)[1]  # :13 pairs with :12 and with :14, as in ops/check_identity_flap.sql


def test_same_callsign_other_cid_or_far_logon_is_not_a_pair(conn) -> None:
    conn.execute(PILOT, (1001, "QFA1", "2026-10-08 04:06:12+00", 2, 1))
    conn.execute(PILOT, (1002, "QFA1", "2026-10-08 04:06:13+00", 2, 1))  # another person
    conn.execute(PILOT, (1001, "QFA2", "2026-10-08 04:06:13+00", 2, 1))  # another callsign
    conn.execute(PILOT, (1001, "QFA1", "2026-10-08 09:00:00+00", 2, 1))  # a genuine later logon
    assert run(conn)[0] == wd.OK


def test_old_pairs_age_out_of_the_window_so_the_alert_recovers(conn) -> None:
    conn.execute(PILOT, (1001, "QFA1", "2026-10-01 04:06:12+00", 200, 100))
    conn.execute(PILOT, (1001, "QFA1", "2026-10-01 04:06:13+00", 100, 90))
    assert run(conn)[0] == wd.OK


@pytest.fixture()
def as_watchdog(conn):
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'watchdog_ro'").fetchone() is None:
        pytest.skip("role watchdog_ro not created (migrate.sh creates it; see the CI step)")
    conn.execute("SET ROLE watchdog_ro")
    try:
        yield conn
    finally:
        conn.execute("RESET ROLE")


def test_watchdog_ro_runs_the_flap_check_and_stays_read_only(as_watchdog) -> None:
    c = as_watchdog
    assert run(c)[0] == wd.OK  # SELECT on both session tables works
    for sql in ("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
                "VALUES (1, 'X', now(), now(), now())", "DELETE FROM controller_session", "SELECT 1 FROM app_setting",
                "DELETE FROM pilot_position",
                "INSERT INTO flight_summary (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, "
                "logon_time, sample_count) VALUES (1, now(), now(), 1, 'X', 'YSSY', 'YMML', ARRAY[1], now(), 1)",
                "DELETE FROM flight_summary"):
        with pytest.raises(psycopg.errors.Error):
            c.execute(sql)
