"""Issues #200 and #238: the airborne gap cap and the flight quiet hours each have ONE source, the app_setting row.

Against the throwaway `_test` database: setting_num raises on a missing or bad row; the flight writer stops (and writes nothing) when
either row is missing; the shared pending-flight view, the invariant pack, the alert wording and ops/backfill_flights.sh all follow a changed
flight_quiet_hours; the watchdog role reads the two ops views and nothing more than before, and grafana_ro cannot read them.

How the rows are set up: the migrations seed flight_quiet_hours = 8 and remove the stale airborne_gap_cap_seconds seed (40); the collector
publishes the cap at every start. The `conn` fixture (conftest) plays the collector's part with TEST_GAP_CAP before each test, and every test here
that needs a row missing, bad or different deletes or updates it itself (the fixture restores the rows afterwards).
"""
from __future__ import annotations

import re
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from test_flight_writer import BASE, HOUR, flown, mk, records, snap, times, write

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
for sub in ("ops/watchdog", "ops/audit"):
    if str(REPO / sub) not in sys.path:
        sys.path.insert(0, str(REPO / sub))
import integrity  # noqa: E402
import messages  # noqa: E402
from test_integrity_checks import SQL, World  # noqa: E402

QUIET = "flights quiet longer than the quiet period with unprocessed positions"
MIGRATION = (REPO / "db" / "migrations" / "20261112000010_settings_single_source.sql").read_text(encoding="utf-8")


def set_setting(conn: Any, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_setting (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))


def num(conn: Any, key: str) -> Decimal:
    return conn.execute("SELECT setting_num(%s) AS v", (key,)).fetchone()["v"]


# ------------------------------------------------------------------ setting_num
def test_setting_num_returns_the_row_as_a_number(conn: Any) -> None:
    set_setting(conn, "flight_quiet_hours", "8")
    set_setting(conn, "airborne_gap_cap_seconds", "75")
    assert num(conn, "flight_quiet_hours") == Decimal(8) and num(conn, "airborne_gap_cap_seconds") == Decimal(75)
    set_setting(conn, "flight_quiet_hours", "2.5")
    assert num(conn, "flight_quiet_hours") == Decimal("2.5")


def test_setting_num_raises_when_the_row_is_missing_and_names_the_key(conn: Any) -> None:
    conn.execute("DELETE FROM app_setting WHERE key = 'airborne_gap_cap_seconds'")
    with pytest.raises(psycopg.errors.NoDataFound, match="airborne_gap_cap_seconds is missing"):
        num(conn, "airborne_gap_cap_seconds")
    with pytest.raises(psycopg.errors.NoDataFound, match="no_such_key is missing"):
        num(conn, "no_such_key")


@pytest.mark.parametrize("bad", ["soon", "", "7 hours", "NaN", "Infinity", "-Infinity"])
def test_setting_num_raises_on_a_value_that_is_not_a_finite_number(conn: Any, bad: str) -> None:
    set_setting(conn, "flight_quiet_hours", bad)
    with pytest.raises(psycopg.errors.InvalidTextRepresentation, match="flight_quiet_hours is not"):
        num(conn, "flight_quiet_hours")


def test_the_migration_removes_only_the_stale_seed_of_the_gap_cap(conn: Any) -> None:
    """A fresh database (seed 40) has no gap cap until the collector starts; production (75, rewritten by the collector) is not touched."""
    stmt = re.search(r"^DELETE FROM app_setting WHERE key = 'airborne_gap_cap_seconds' AND value = '40';$", MIGRATION, re.M)
    assert stmt
    set_setting(conn, "airborne_gap_cap_seconds", "75")
    conn.execute(stmt.group(0))
    assert conn.execute("SELECT value FROM app_setting WHERE key = 'airborne_gap_cap_seconds'").fetchone()["value"] == "75"
    set_setting(conn, "airborne_gap_cap_seconds", "40")
    conn.execute(stmt.group(0))
    assert conn.execute("SELECT 1 FROM app_setting WHERE key = 'airborne_gap_cap_seconds'").fetchone() is None
    assert conn.execute("SELECT value FROM app_setting WHERE key = 'flight_quiet_hours'").fetchone()["value"] == "8"   # the quiet-hours seed stays


# ------------------------------------------------------------------ the writer stops when a setting is missing
def _one_due_flight(conn: Any, cid: int) -> tuple[int, list[datetime]]:
    ts = times(BASE, 20)
    sid = mk(conn, cid, ts)
    snap(conn, ts[-1] + 9 * HOUR)
    return sid, ts


@pytest.mark.parametrize("key", ["airborne_gap_cap_seconds", "flight_quiet_hours"])
def test_a_missing_setting_row_stops_the_writer_with_a_clear_error_and_writes_nothing(conn: Any, key: str) -> None:
    sid, _ = _one_due_flight(conn, 4001)
    conn.execute("DELETE FROM app_setting WHERE key = %s", (key,))
    with pytest.raises(psycopg.errors.NoDataFound, match=f"setting {key} is missing from app_setting"):
        write(conn)
    assert records(conn) == [] and flown(conn, sid) is None
    set_setting(conn, key, "8" if key == "flight_quiet_hours" else "75")      # the row is back: the same flight is now written
    assert write(conn) == 1 and len(records(conn, 4001)) == 1


def test_a_missing_flight_quiet_hours_row_stops_the_writer_even_with_nothing_to_write(conn: Any) -> None:
    conn.execute("DELETE FROM app_setting WHERE key = 'flight_quiet_hours'")
    with pytest.raises(psycopg.errors.NoDataFound):
        write(conn)


@pytest.mark.parametrize("key", ["airborne_gap_cap_seconds", "flight_quiet_hours"])
def test_a_non_numeric_setting_still_stops_the_writer(conn: Any, key: str) -> None:
    sid, _ = _one_due_flight(conn, 4002)
    set_setting(conn, key, "soon")
    with pytest.raises(psycopg.errors.InvalidTextRepresentation):
        write(conn)
    assert records(conn) == [] and flown(conn, sid) is None


def test_a_quiet_period_below_2_5_hours_still_raises_the_existing_error(conn: Any) -> None:
    _one_due_flight(conn, 4003)
    set_setting(conn, "flight_quiet_hours", "2.4")
    with pytest.raises(psycopg.errors.RaiseException, match="must be at least 2.5"):
        write(conn)


def test_with_the_rows_the_collector_and_the_migration_leave_the_writer_uses_them(conn: Any) -> None:
    """Production state: 75 and 8. A 100 s gap is capped at 75 s and the flight waits 8 hours."""
    set_setting(conn, "airborne_gap_cap_seconds", "75")
    set_setting(conn, "flight_quiet_hours", "8")
    ts = [BASE, BASE + timedelta(seconds=100)]
    mk(conn, 4004, ts, gs=100)
    snap(conn, ts[-1] + 8 * HOUR - timedelta(minutes=1))
    assert write(conn) == 0
    snap(conn, ts[-1] + 8 * HOUR)
    assert write(conn) == 1
    (r,) = records(conn, 4004)
    assert float(r["airborne_minutes"]) == pytest.approx(75 / 60, abs=0.01)


def test_the_live_writer_reads_the_settings_only_through_setting_num(conn: Any) -> None:
    src = conn.execute("SELECT prosrc FROM pg_proc WHERE oid = 'public.write_finished_flights(integer)'::regprocedure").fetchone()["prosrc"]
    assert "quiet_h := setting_num('flight_quiet_hours');" in src and "gap_cap := setting_num('airborne_gap_cap_seconds');" in src
    assert "app_setting" not in src and "COALESCE((SELECT value" not in src


def test_the_grafana_and_other_roles_cannot_run_the_readers(conn: Any) -> None:
    for role in ("grafana_ro", "collector", "backup_ro"):
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            continue
        for fn in ("setting_num('flight_quiet_hours')", "ops_setting_num('flight_quiet_hours')"):
            assert conn.execute("SELECT has_function_privilege(%s, %s, 'EXECUTE') AS ok",
                                (role, "public." + fn.split("(")[0] + "(text)")).fetchone()["ok"] is False, (role, fn)


# ------------------------------------------------------------------ the shared views
def _pending(conn: Any) -> list[dict]:
    return conn.execute("SELECT * FROM ops_pending_flight ORDER BY cid").fetchall()


def test_ops_setting_shows_exactly_the_two_settings_and_follows_them(conn: Any) -> None:
    set_setting(conn, "flight_quiet_hours", "6")
    set_setting(conn, "airborne_gap_cap_seconds", "55")
    got = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM ops_setting").fetchall()}
    assert got == {"flight_quiet_hours": Decimal(6), "airborne_gap_cap_seconds": Decimal(55), "flight_quiet_check_grace_minutes": Decimal(30)}
    conn.execute("DELETE FROM app_setting WHERE key = 'airborne_gap_cap_seconds'")
    with pytest.raises(psycopg.errors.NoDataFound):
        conn.execute("SELECT key, value FROM ops_setting").fetchall()


def test_ops_pending_flight_is_quiet_by_the_setting_and_follows_a_change(conn: Any) -> None:
    ts = times(BASE, 20)
    mk(conn, 4101, ts)                                       # the newest position is ts[-1]
    snap(conn, ts[-1] + 7 * HOUR)                            # quiet for 7 hours of stored data
    (row,) = _pending(conn)
    assert row["cid"] == 4101 and row["session_count"] == 1 and row["newest_seen"] == ts[-1] and row["is_quiet"] is False   # 8 hours
    set_setting(conn, "flight_quiet_hours", "6")
    assert [r["is_quiet"] for r in _pending(conn)] == [True]
    set_setting(conn, "flight_quiet_hours", "7")             # the boundary is inclusive, as in the writer
    assert [r["is_quiet"] for r in _pending(conn)] == [True]
    set_setting(conn, "flight_quiet_hours", "12")
    assert [r["is_quiet"] for r in _pending(conn)] == [False]


def test_ops_pending_flight_joins_reconnects_by_the_v1_rule_and_is_agreed_with_the_writer(conn: Any) -> None:
    """One flight of two sessions (logon 90 minutes after the first ends) is one row with 2 sessions; a session without a plan is its own row.
    Whatever the view calls quiet, the writer then writes (one flight written for one quiet row; none for the plan-less session)."""
    a = times(BASE, 30)
    mk(conn, 4102, a)
    b = times(a[-1] + timedelta(minutes=90), 30)
    mk(conn, 4102, b, logon=a[-1] + timedelta(minutes=90))
    mk(conn, 4103, times(BASE, 10), dep=None, arr=None)
    snap(conn, b[-1] + 9 * HOUR)
    rows = _pending(conn)
    assert [(r["cid"], r["session_count"], r["is_quiet"]) for r in rows] == [(4102, 2, True), (4103, 1, True)]
    assert write(conn) == 1 and len(records(conn, 4102)) == 1
    assert _pending(conn) == []                              # processed sessions are no longer pending


def test_ops_pending_flight_is_empty_and_not_quiet_without_a_snapshot(conn: Any) -> None:
    mk(conn, 4104, times(BASE, 5))
    assert [r["is_quiet"] for r in _pending(conn)] == [None]  # no snapshot, no reference time: nothing is quiet (the writer's cold start)
    assert conn.execute("SELECT count(*) AS n FROM ops_pending_flight WHERE is_quiet").fetchone()["n"] == 0


# ------------------------------------------------------------------ the invariant and the alert wording follow the setting
def _pack(dsn: str, role: str | None) -> dict[str, tuple[int, str | None]]:
    with psycopg.connect(dsn) as c:
        rows = integrity.run_invariants(c, SQL, timeout_s=60, role=role)
    findings, problems = integrity.evaluate(rows, integrity.invariant_names(SQL))
    assert problems == [], problems
    return {n: (k, d) for n, k, d in rows}


def _role(conn: Any) -> str | None:
    return "watchdog_ro" if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'watchdog_ro'").fetchone() else None


def _alert(dsn: str, role: str | None) -> str:
    with psycopg.connect(dsn) as c:
        rows = integrity.run_invariants(c, SQL, timeout_s=60, role=role)
    findings, problems = integrity.evaluate(rows, integrity.invariant_names(SQL))
    res = integrity.summarise_invariants([f for f in findings if f.name == QUIET], problems, len(integrity.invariant_names(SQL)))
    return messages.humanise("data_integrity", "warn", res[1], now=datetime(2026, 11, 12, 10, 0, tzinfo=UTC), facts=integrity.facts_of(res))


def tok(hours: str, grace: str) -> str:
    return f"flight_quiet_hours={hours} flight_quiet_check_grace_minutes={grace}"


def test_the_invariant_and_the_alert_wording_follow_both_settings(conn: Any, dsn: str) -> None:
    role = _role(conn)
    w = World(conn)
    gone = w.ts[0] - timedelta(hours=7)                      # an unprocessed flight whose last position is 7 hours before the newest snapshot
    conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
                 "VALUES (4201, 'GONE7', %s, %s, %s, 'YSSY', 'YMML')", (gone.replace(microsecond=0), gone, gone))
    grace = lambda v: set_setting(conn, "flight_quiet_check_grace_minutes", v)  # noqa: E731
    assert conn.execute("SELECT value FROM app_setting WHERE key = 'flight_quiet_check_grace_minutes'").fetchone()["value"] == "30"   # the seed
    set_setting(conn, "flight_quiet_hours", "8")
    assert _pack(dsn, role)[QUIET] == (0, tok("8", "30"))                  # 7 hours is below 8 h 30 min: not overdue
    set_setting(conn, "flight_quiet_hours", "6.5")
    assert _pack(dsn, role)[QUIET] == (1, tok("6.5", "30"))                # 6 h 30 + 30 min = 7 h: the boundary is inclusive, it fires
    set_setting(conn, "flight_quiet_hours", "6")
    assert _pack(dsn, role)[QUIET] == (1, tok("6", "30"))
    assert "some flights ended over 6 hours 30 minutes ago" in _alert(dsn, role).splitlines()[0]
    grace("90")                                                            # a larger grace moves the check: 6 h + 90 min = 7 h 30 min > 7 h
    assert _pack(dsn, role)[QUIET] == (0, tok("6", "90"))
    grace("0")
    assert _pack(dsn, role)[QUIET] == (1, tok("6", "0"))
    assert "some flights ended over 6 hours ago" in _alert(dsn, role).splitlines()[0]
    grace("30")
    set_setting(conn, "flight_quiet_hours", "12")
    older = w.ts[0] - timedelta(hours=13)
    conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
                 "VALUES (4202, 'GONE13', %s, %s, %s, 'YSSY', 'YMML')", (older.replace(microsecond=0), older, older))
    assert _pack(dsn, role)[QUIET] == (1, tok("12", "30"))                 # 13 h is past 12 h 30 min
    assert "some flights ended over 12 hours 30 minutes ago" in _alert(dsn, role).splitlines()[0]


def test_a_flight_quiet_for_less_than_quiet_hours_plus_grace_does_not_trip_the_invariant(conn: Any, dsn: str) -> None:
    role = _role(conn)
    w = World(conn)
    for cid, mins in ((4211, 8 * 60 + 5), (4212, 8 * 60 + 29)):            # quiet past 8 h (the writer would write it) but inside the 30 minute grace
        t = w.ts[0] - timedelta(minutes=mins)
        conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
                     "VALUES (%s, %s, %s, %s, %s, 'YSSY', 'YMML')", (cid, f"G{cid}", t.replace(microsecond=0), t, t))
    assert _pack(dsn, role)[QUIET][0] == 0
    assert conn.execute("SELECT count(*) AS n FROM ops_pending_flight WHERE is_quiet AND cid IN (4211, 4212)").fetchone()["n"] == 2
    t = w.ts[0] - timedelta(minutes=8 * 60 + 31)
    conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
                 "VALUES (4213, 'G4213', %s, %s, %s, 'YSSY', 'YMML')", (t.replace(microsecond=0), t, t))
    assert _pack(dsn, role)[QUIET][0] == 1


def test_a_missing_grace_row_makes_the_sweep_fail(conn: Any, dsn: str) -> None:
    World(conn)
    conn.execute("DELETE FROM app_setting WHERE key = 'flight_quiet_check_grace_minutes'")
    with psycopg.connect(dsn) as c, pytest.raises(psycopg.errors.NoDataFound, match="flight_quiet_check_grace_minutes"):
        integrity.run_invariants(c, SQL, timeout_s=60, role=_role(conn))


def test_the_second_pending_flight_invariant_still_counts_sessions_of_flights_ended_over_two_days_ago(conn: Any, dsn: str) -> None:
    name = "sessions ended more than 2 days ago with unprocessed positions"
    w = World(conn)
    old = w.ts[0] - timedelta(days=3)
    for logon in (old, old + timedelta(hours=2)):            # two sessions of one callsign more than 120 minutes apart: two flights, two sessions
        conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
                     "VALUES (4203, 'OLDX', %s, %s, %s, 'YSSY', 'YMML')", (logon.replace(microsecond=0), logon, logon + timedelta(minutes=30)))
    assert _pack(dsn, _role(conn))[name][0] == 2


def test_a_missing_setting_makes_the_sweep_fail_instead_of_passing_silently(conn: Any, dsn: str) -> None:
    World(conn)
    conn.execute("DELETE FROM app_setting WHERE key = 'flight_quiet_hours'")
    with psycopg.connect(dsn) as c, pytest.raises(psycopg.errors.NoDataFound):
        integrity.run_invariants(c, SQL, timeout_s=60, role=_role(conn))


# ------------------------------------------------------------------ grants: the watchdog role reads the views and nothing more than before
@pytest.fixture()
def as_watchdog(conn: Any, dsn: str) -> Any:
    if _role(conn) is None:
        pytest.skip("role watchdog_ro not created (db/roles.sql creates it; see the CI step)")
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET ROLE watchdog_ro")
        yield c


def test_watchdog_ro_reads_the_two_views(conn: Any, as_watchdog: Any) -> None:
    set_setting(conn, "flight_quiet_hours", "8")
    set_setting(conn, "airborne_gap_cap_seconds", "75")
    got = {r[0]: r[1] for r in as_watchdog.execute("SELECT key, value FROM ops_setting").fetchall()}
    assert got == {"flight_quiet_hours": 8, "airborne_gap_cap_seconds": 75, "flight_quiet_check_grace_minutes": 30}
    as_watchdog.execute("SELECT count(*) FROM ops_pending_flight").fetchone()
    assert as_watchdog.execute("SELECT ops_setting_num('flight_quiet_hours')").fetchone()[0] == 8   # the wrapper answers for the two settings


@pytest.mark.parametrize("sql", [
    "SELECT 1 FROM app_setting",
    "SELECT setting_num('flight_quiet_hours')",
    "SELECT ops_setting_num('home_icao_prefixes')",
    "SELECT ops_setting_num('poll_interval_seconds')",
    "SELECT write_finished_flights(1)",
    "SELECT 1 FROM controller_rating_event",
    "INSERT INTO app_setting (key, value) VALUES ('x', '1')",
    "UPDATE app_setting SET value = '1'",
    "DELETE FROM app_setting",
    "UPDATE ops_setting SET value = 1",
    "DROP VIEW ops_setting",
])
def test_watchdog_ro_gets_nothing_more_than_before(as_watchdog: Any, sql: str) -> None:
    with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.WrongObjectType,
                        psycopg.errors.FeatureNotSupported, psycopg.errors.ObjectNotInPrerequisiteState)):
        as_watchdog.execute(sql)
    as_watchdog.execute("SET default_transaction_read_only = off")
    with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.WrongObjectType, psycopg.errors.FeatureNotSupported,
                        psycopg.errors.ObjectNotInPrerequisiteState)):
        as_watchdog.execute(sql)


def test_the_watchdog_role_has_select_on_exactly_the_tables_it_had_plus_the_two_views(conn: Any) -> None:
    if _role(conn) is None:
        pytest.skip("role watchdog_ro not created")
    got = {r["table_name"] for r in conn.execute(
        "SELECT table_name FROM information_schema.role_table_grants WHERE grantee = 'watchdog_ro' AND privilege_type = 'SELECT' "
        "AND table_schema = 'public'").fetchall()}
    assert got == {"poll_log", "network_snapshot", "controller_unlisted_callsign", "controller_unlisted_sighting", "pilot_session", "controller_session", "pilot_position",
                   "flight_summary", "drop_gate_refusal", "reference_data_change", "ops_setting", "ops_pending_flight"}
    others = conn.execute(
        "SELECT count(*) AS n FROM information_schema.role_table_grants WHERE grantee = 'watchdog_ro' AND table_schema = 'public' "
        "AND privilege_type <> 'SELECT'").fetchone()["n"]
    assert others == 0


# ------------------------------------------------------------------ ops/backfill_flights.sh uses the shared definition
def _backfill_sql() -> str:
    sh = (REPO / "ops" / "backfill_flights.sh").read_text(encoding="utf-8")
    m = re.search(r"REMAINING_SQL=\"\$\(cat <<'SQL'\n(.*?)\nSQL\n\)\"", sh, re.S)
    assert m, "REMAINING_SQL block not found in ops/backfill_flights.sh"
    return m.group(1)


def test_backfill_flights_counts_what_the_shared_view_says_is_due_and_follows_the_setting(conn: Any) -> None:
    sql = _backfill_sql()
    assert "ops_pending_flight" in sql and "COALESCE" not in sql and "app_setting" not in sql
    ts = times(BASE, 20)
    mk(conn, 4301, ts)
    snap(conn, ts[-1] + 7 * HOUR)
    left = lambda: conn.execute(sql).fetchone()["count"]  # noqa: E731
    assert left() == 0                                       # 8 hours: not yet
    set_setting(conn, "flight_quiet_hours", "6")
    assert left() == 1
    assert write(conn) == 1
    assert left() == 0
