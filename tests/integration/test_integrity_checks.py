"""Issue 20 (T16): the scheduled integrity checks must actually FIRE.

Every invariant in ops/audit/invariants.sql gets a deliberately broken database and the test asserts the sweep reports
exactly that invariant (plus any invariant that legitimately follows from the same damage, listed per case). A clean
database must report 0 everywhere. The archive cross-check gets the same treatment: one broken stored row or one
altered archived payload per case. If an invariant is deleted, renamed, or made to always return 0, a test here fails;
the guard test below fails if the pack and the cases ever disagree on the list of invariants.

Runs on the throwaway `_test` database only (the conn fixture refuses anything else). The sweep is run as the
`watchdog_ro` role when it exists (CI and the throwaway-cluster method create it), so the grants are tested too."""
from __future__ import annotations

import gzip
import json
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
for sub in ("ops/watchdog", "ops/audit"):
    if str(REPO / sub) not in sys.path:
        sys.path.insert(0, str(REPO / sub))
import integrity  # noqa: E402
from helpers import apply, at, partition_for  # noqa: E402

SQL = (REPO / "ops" / "audit" / "invariants.sql").read_text(encoding="utf-8")
NAMES = integrity.invariant_names(SQL)
N = 60  # snapshots in the clean world: 30 minutes at 30 s


# ------------------------------------------------------------------ a clean world that satisfies every invariant
class World:
    def __init__(self, conn: Any, newest_age: float = 30.0) -> None:
        self.conn = conn
        now = datetime.now(UTC).replace(microsecond=0)
        self.now = now
        self.ts = [now - timedelta(seconds=newest_age + 30 * i) for i in range(N)]  # ts[0] newest, ts[N-1] oldest
        self.sessions: dict[str, int] = {}
        self.build()

    def build(self) -> None:
        c, ts = self.conn, self.ts
        for d in range(4):
            partition_for(c, self.now + timedelta(days=d))
        for t in ts:
            partition_for(c, t)
        logon = (ts[-1] - timedelta(hours=1)).replace(microsecond=0)
        spans = {"A": (1001, "QFA1", 0, N - 1), "B": (1002, "VOZ2", 0, N - 1), "C": (1003, "JST3", N // 3 * 2, N - 1)}
        # C is a finished flight: it was seen from ts[N-1] to ts[2N/3], i.e. more than 15 minutes ago
        for key, (cid, cs, newest_i, oldest_i) in spans.items():
            self.sessions[key] = c.execute(
                "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
                "VALUES (%s, %s, %s, %s, %s, 'YSSY', 'YMML') RETURNING id",
                (cid, cs, logon, ts[oldest_i], ts[newest_i])).fetchone()["id"]
        for i, t in enumerate(ts):
            members = [k for k, (_, _, ni, oi) in spans.items() if ni <= i <= oi]
            for k in members:
                c.execute("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) "
                          "VALUES (%s, %s, -33.9, 151.2, 5000, 250, 90)", (t, self.sessions[k]))
            c.execute("INSERT INTO network_snapshot (observed_at, pilots_in_region, controllers_in_region, pilots_global, "
                      "controllers_global) VALUES (%s, %s, 1, 100, 10)", (t, len(members)))
            c.execute("INSERT INTO poll_log (polled_at, status, snapshot_ts, pilots_seen, pilots_inside, controllers_seen, "
                      "controllers_kept, polygon_sha, whitelist_sha) VALUES (%s, 'ok', %s, 100, %s, 10, 1, %s, %s)",
                      (t, t, len(members), "a" * 64, "b" * 64))
        for f, sha in (("australian_airspace_polygon.json", "a" * 64), ("controller_callsigns_list.txt", "b" * 64)):
            c.execute("INSERT INTO reference_data_change (changed_at, file, new_sha) VALUES (%s, %s, %s)",
                      (ts[-1] - timedelta(days=1), f, sha))
        c.execute("INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen) VALUES (2001, 'ML_APP', %s, %s, %s)",
                  (logon, ts[-1], ts[0]))
        c.execute("SELECT write_finished_flights(500)")


def sweep(dsn: str, as_role: str | None = None) -> dict[str, int]:
    """Run the pack the way the watchdog does (its own function, a fresh connection, optionally as watchdog_ro)."""
    with psycopg.connect(dsn) as c:
        rows = integrity.run_invariants(c, SQL, timeout_s=60, role=as_role)
    findings, problems = integrity.evaluate(rows, NAMES)
    assert problems == [], problems
    return {n: k for n, k, _ in rows}


@pytest.fixture()
def role(conn: Any) -> str | None:
    return "watchdog_ro" if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'watchdog_ro'").fetchone() else None


def failing(counts: dict[str, int]) -> set[str]:
    return {n for n, k in counts.items() if k > 0}


# ------------------------------------------------------------------ clean data
def test_a_clean_database_reports_zero_for_every_invariant(conn: Any, dsn: str, role: str | None) -> None:
    World(conn)
    counts = sweep(dsn, role)
    assert set(counts) == set(NAMES) and len(counts) == 20
    assert failing(counts) == set(), failing(counts)


def test_an_empty_database_does_not_crash_or_report_missing_rows(conn: Any, dsn: str) -> None:
    counts = sweep(dsn)
    assert set(counts) == set(NAMES)  # every invariant still returns its row on an empty database


def test_the_sweep_is_read_only_and_leaves_the_data_alone(conn: Any, dsn: str) -> None:
    World(conn)
    before = conn.execute("SELECT (SELECT count(*) FROM pilot_position) p, (SELECT count(*) FROM network_snapshot) n").fetchone()
    with psycopg.connect(dsn) as c:
        integrity.run_invariants(c, SQL)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("DELETE FROM network_snapshot")
    assert conn.execute("SELECT (SELECT count(*) FROM pilot_position) p, (SELECT count(*) FROM network_snapshot) n").fetchone() == before


def test_watchdog_ro_can_read_everything_the_sweep_and_the_cross_check_need(conn: Any, dsn: str, role: str | None) -> None:
    if role is None:
        pytest.skip("role watchdog_ro not created (db/roles.sql creates it; see the CI step)")
    World(conn)
    with psycopg.connect(dsn, autocommit=True) as c:  # autocommit, so the role is not rolled back between statements
        c.execute("SET ROLE watchdog_ro")
        for t in ("network_snapshot", "poll_log", "pilot_position", "pilot_session", "controller_session", "flight_summary",
                  "reference_data_change"):
            c.execute(f"SELECT count(*) FROM {t}").fetchone()  # noqa: S608
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("SELECT 1 FROM app_setting")


# ------------------------------------------------------------------ one broken database per invariant
CASES: dict[str, tuple[Callable[[Any, World], Any], set[str]]] = {}


def case(name: str, *also: str) -> Callable[[Callable[[Any, World], Any]], Callable[[Any, World], Any]]:
    """Register a way to break the clean world so that invariant `name` (and the listed knock-on ones) must fire."""
    def reg(fn: Callable[[Any, World], Any]) -> Callable[[Any, World], Any]:
        assert name in NAMES, f"not an invariant in invariants.sql: {name}"
        CASES[name] = (fn, set(also))
        return fn
    return reg


@contextmanager
def constraint_off(conn: Any, table: str, names: list[str]) -> Iterator[None]:
    """Drop constraints so impossible rows can be inserted; afterwards empty the table and put the constraints back."""
    defs = {n: conn.execute("SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint WHERE conrelid = %s::regclass AND conname = %s",
                            (table, n)).fetchone()["d"] for n in names}
    for n in names:
        conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT {n}")
    try:
        yield
    finally:
        conn.execute(f"TRUNCATE {table} CASCADE")
        for n, d in defs.items():
            conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT {n} {d}")


@case("snapshot rows without an ok poll_log row")
def _(conn: Any, w: World) -> None:
    conn.execute("DELETE FROM poll_log WHERE snapshot_ts = %s", (w.ts[5],))


@case("ok poll_log counts differ from network_snapshot")
def _(conn: Any, w: World) -> None:
    conn.execute("UPDATE poll_log SET pilots_seen = pilots_seen + 1 WHERE snapshot_ts = %s", (w.ts[5],))


@case("in-region count above global count", "ok poll_log counts differ from network_snapshot",
      "positions per snapshot differ from pilots_in_region")
def _(conn: Any, w: World) -> None:
    conn.execute("UPDATE network_snapshot SET pilots_in_region = pilots_global + 1 WHERE observed_at = %s", (w.ts[5],))


@case("positions per snapshot differ from pilots_in_region")
def _(conn: Any, w: World) -> None:
    conn.execute("DELETE FROM pilot_position WHERE observed_at = %s AND session_id = %s", (w.ts[5], w.sessions["A"]))


@case("position rows at a timestamp with no network_snapshot")
def _(conn: Any, w: World) -> None:
    conn.execute("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) "
                 "VALUES (%s, %s, -33.9, 151.2, 5000, 250, 90)", (w.ts[5] + timedelta(seconds=7), w.sessions["A"]))


@case("snapshot gaps over 5 minutes (last 3 days)")
def _(conn: Any, w: World) -> None:
    for t in ("poll_log", "pilot_position", "network_snapshot"):  # eleven snapshots (5.5 minutes) are missing
        col = {"poll_log": "snapshot_ts"}.get(t, "observed_at")
        conn.execute(f"DELETE FROM {t} WHERE {col} > %s AND {col} < %s", (w.ts[22], w.ts[10]))  # noqa: S608


@case("newest snapshot in the future or older than 5 minutes")
def _(conn: Any, w: World) -> None:
    conn.execute("DELETE FROM pilot_position WHERE observed_at >= %s", (w.ts[11],))
    conn.execute("DELETE FROM poll_log WHERE snapshot_ts >= %s", (w.ts[11],))
    conn.execute("DELETE FROM network_snapshot WHERE observed_at >= %s", (w.ts[11],))  # newest is now 5.5 minutes old
    conn.execute("UPDATE pilot_session SET last_seen = %s WHERE last_seen > %s", (w.ts[12], w.ts[12]))


@case("pilot session first/last seen differ from first/last stored position")
def _(conn: Any, w: World) -> None:
    conn.execute("UPDATE pilot_session SET last_seen = last_seen + interval '5 seconds' WHERE id = %s", (w.sessions["A"],))


@case("pilot sessions with no position (active in last 3 days)")
def _(conn: Any, w: World) -> None:
    conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) VALUES (1500, 'NOPOS1', %s, %s, %s)",
                 (w.ts[3].replace(microsecond=0), w.ts[3], w.ts[3]))


@case("identity duplicates (cid, callsign, logon_time)",
      "pilot session pairs, same cid+callsign, logon_time within 2 s (identity flap)",
      "pilot sessions with no position (active in last 3 days)")
@contextmanager
def _(conn: Any, w: World) -> Iterator[None]:
    with constraint_off(conn, "pilot_session", ["pilot_session_identity"]):  # the unique constraint normally forbids this
        conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
                     "SELECT cid, callsign, logon_time, %s, %s FROM pilot_session WHERE id = %s", (w.ts[3], w.ts[3], w.sessions["A"]))
        yield


@case("pilot session pairs, same cid+callsign, logon_time within 2 s (identity flap)",
      "pilot sessions with no position (active in last 3 days)")
def _(conn: Any, w: World) -> None:
    conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
                 "SELECT cid, callsign, logon_time + interval '1 second', %s, %s FROM pilot_session WHERE id = %s",
                 (w.ts[3], w.ts[3], w.sessions["A"]))


@case("controller session pairs, same cid+callsign, logon_time within 2 s (identity flap)")
def _(conn: Any, w: World) -> None:
    conn.execute("INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen) "
                 "SELECT cid, callsign, logon_time + interval '1 second', first_seen, last_seen FROM controller_session")


def mass_logon(conn: Any, t: datetime) -> None:
    """Eight sessions created at snapshot t, on top of the pilots already there (at least 8 of at least 10 are new)."""
    for k in range(8):
        sid = conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
                           "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                           (7000 + k, f"MASS{k}", t.replace(microsecond=0) - timedelta(minutes=5), t, t)).fetchone()["id"]
        conn.execute("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) "
                     "VALUES (%s, %s, -33.9, 151.2, 5000, 250, 90)", (t, sid))
    conn.execute("UPDATE network_snapshot SET pilots_in_region = pilots_in_region + 8 WHERE observed_at = %s", (t,))
    conn.execute("UPDATE poll_log SET pilots_inside = pilots_inside + 8 WHERE snapshot_ts = %s", (t,))


@case("snapshots that created sessions for more than 50% of in-region pilots")
def _(conn: Any, w: World) -> None:
    mass_logon(conn, w.ts[5])


def test_the_very_first_snapshot_of_a_database_may_create_every_session(conn: Any, dsn: str) -> None:
    """07-10-2026 21:38 on the live database: go-live. That is not a feed change and must not alert."""
    w = World(conn)
    mass_logon(conn, w.ts[-1])  # the oldest snapshot in the database
    assert sweep(dsn)["snapshots that created sessions for more than 50% of in-region pilots"] == 0
    conn.execute("INSERT INTO network_snapshot (observed_at, pilots_in_region, controllers_in_region, pilots_global, controllers_global) "
                 "VALUES (%s, 0, 1, 100, 10)", (w.ts[-1] - timedelta(seconds=30),))  # an even older snapshot: it is no longer the first
    assert sweep(dsn)["snapshots that created sessions for more than 50% of in-region pilots"] == 1


def test_gaps_of_a_tick_or_two_and_ordinary_restarts_are_not_violations(conn: Any, dsn: str) -> None:
    """45.0002 s (a feed tick missed at 30 s polling) and 90 s (a restart) were reported by the first version of the check."""
    w = World(conn)
    for t in ("poll_log", "pilot_position", "network_snapshot"):
        col = {"poll_log": "snapshot_ts"}.get(t, "observed_at")
        conn.execute(f"DELETE FROM {t} WHERE {col} IN (%s, %s)", (w.ts[10], w.ts[20]))  # noqa: S608  (two 60 s gaps)
        conn.execute(f"DELETE FROM {t} WHERE {col} IN (%s, %s)", (w.ts[30], w.ts[31]))  # noqa: S608  (one 90 s gap)
    assert sweep(dsn)["snapshot gaps over 5 minutes (last 3 days)"] == 0


@case("pilot sessions with no position (active in last 3 days)",
      "flights quiet longer than the quiet period with unprocessed positions")
def _(conn: Any, w: World) -> None:
    old = w.now - timedelta(hours=13)
    conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) VALUES (1600, 'OLD1', %s, %s, %s)",
                 (old, old, old))


@case("flights quiet longer than the quiet period with unprocessed positions",
      "pilot sessions with no position (active in last 3 days)")
def _(conn: Any, w: World) -> None:
    gone = w.ts[0] - timedelta(hours=10)  # ten hours before the newest snapshot, so quiet for longer than 9 h, but not older than 12 h from now
    conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
                 "VALUES (1700, 'GONE1', %s, %s, %s, 'YSSY', 'YMML')", (gone.replace(microsecond=0), gone, gone))


@case("flights written in the last 3 days whose sample_count differs from the stored positions of their sessions")
def _(conn: Any, w: World) -> None:
    sid = w.sessions["C"]
    conn.execute("INSERT INTO flight_summary (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, "
                 "logon_time, sample_count) SELECT id, first_seen, last_seen, cid, callsign, 'YSSY', 'YMML', ARRAY[id], logon_time, "
                 "(SELECT count(*) + 1 FROM pilot_position WHERE session_id = %s) FROM pilot_session WHERE id = %s", (sid, sid))


@case("sessions ended more than 2 days ago with unprocessed positions",
      "flights quiet longer than the quiet period with unprocessed positions",
      "pilot sessions with no position (active in last 3 days)")
def _(conn: Any, w: World) -> None:
    old = w.ts[0] - timedelta(days=2, hours=1)
    conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
                 "VALUES (1701, 'OLD2', %s, %s, %s, 'YSSY', 'YMML')", (old.replace(microsecond=0), old, old))


@case("processed flight-plan sessions in no written flight (ended 2 to 30 days ago)")
def _(conn: Any, w: World) -> None:
    old = w.ts[0] - timedelta(days=5)
    partition_for(conn, old)
    sid = conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival, flown_upto) "
                       "VALUES (1702, 'OLD3', %s, %s, %s, 'YSSY', 'YMML', %s) RETURNING id",
                       (old.replace(microsecond=0), old, old, old)).fetchone()["id"]
    conn.execute("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) VALUES (%s, %s, -33.9, 151.2, 5000, 250, 90)", (old, sid))


def test_a_processed_session_that_is_in_a_written_flight_does_not_fire_the_gate_invariant(conn: Any, dsn: str) -> None:
    w = World(conn)
    old = w.ts[0] - timedelta(days=5)
    partition_for(conn, old)
    sid = conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival, flown_upto) "
                       "VALUES (1703, 'OLD4', %s, %s, %s, 'YSSY', 'YMML', %s) RETURNING id",
                       (old.replace(microsecond=0), old, old, old)).fetchone()["id"]
    conn.execute("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) VALUES (%s, %s, -33.9, 151.2, 5000, 250, 90)", (old, sid))
    name = "processed flight-plan sessions in no written flight (ended 2 to 30 days ago)"
    assert sweep(dsn)[name] == 1
    conn.execute("INSERT INTO flight_summary (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, logon_time, sample_count) "
                 "VALUES (%s, %s, %s, 1703, 'OLD4', 'YSSY', 'YMML', ARRAY[%s], %s, 1)", (sid, old, old, sid, old))
    assert sweep(dsn)[name] == 0
    conn.execute("UPDATE pilot_session SET departure = '' WHERE id = %s", (sid,))     # a session without a plan makes no flight
    conn.execute("DELETE FROM flight_summary")
    assert sweep(dsn)[name] == 0
def test_the_unprocessed_flight_checks_work_per_flight_not_per_pilot(conn: Any, dsn: str) -> None:
    """Owner decision 12-10-2026 (quiet per flight): a pilot with an old finished flight AND a recent different flight is flagged for
    the old one only if it is overdue; a quiet pilot is flagged; a flight kept open by an active reconnect (joined by the v1 rule) is not."""
    n1 = "flights quiet longer than the quiet period with unprocessed positions"
    n3 = "sessions ended more than 2 days ago with unprocessed positions"
    w = World(conn)
    ref = w.ts[0]

    def add(cid: int, cs: str, dep: str, arr: str, logon: Any, last: Any) -> None:
        conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s)", (cid, cs, logon.replace(microsecond=0), logon, last, dep, arr))

    old = ref - timedelta(days=3)
    add(1900, "ACT1", "YSSY", "YMML", old, old + timedelta(hours=1))                      # finished 3 days ago ...
    add(1900, "ACT2", "YMML", "YSSY", ref - timedelta(hours=2), ref - timedelta(hours=1))  # ... a different flight, active recently
    base = sweep(dsn)
    assert base[n1] == 1 and base[n3] == 1                                                 # the old flight is overdue: counted, once
    add(1901, "OPN1", "YSSY", "YMML", old, old + timedelta(hours=1))                      # an old session ...
    add(1901, "OPN1", "YSSY", "YMML", old + timedelta(hours=2), ref - timedelta(minutes=5))   # ... continued by a reconnect (2 h after): ONE open flight
    after = sweep(dsn)
    assert after[n1] == 1 and after[n3] == 1                                               # the open flight adds nothing


@case("raw-position days kept by the deletion check for more than 3 days")
def _(conn: Any, w: World) -> None:
    conn.execute("INSERT INTO drop_gate_refusal (part_name, first_refused, detail) VALUES ('pilot_position_20260101', now() - interval '4 days', 'x')")


def test_a_fresh_refusal_does_not_fire_the_gate_refusal_check(conn: Any, dsn: str) -> None:
    World(conn)
    conn.execute("INSERT INTO drop_gate_refusal (part_name, first_refused, detail) VALUES ('pilot_position_20260101', now() - interval '2 days', 'x')")
    name = "raw-position days kept by the deletion check for more than 3 days"
    assert sweep(dsn)[name] == 0
    conn.execute("UPDATE drop_gate_refusal SET first_refused = now() - interval '4 days'")
    assert sweep(dsn)[name] == 1


@case("daily partitions missing for today..today+3")
def _(conn: Any, w: World) -> None:
    conn.execute(f"DROP TABLE pilot_position_{(w.now + timedelta(days=3)):%Y%m%d}")


@case("reference file hashes in poll_log differ from reference_data_change")
def _(conn: Any, w: World) -> None:
    conn.execute("UPDATE poll_log SET polygon_sha = %s WHERE snapshot_ts = %s", ("z" * 64, w.ts[5]))


def test_the_cases_cover_exactly_the_invariants_in_the_pack() -> None:
    """The guard: delete or rename an invariant, or add one without a case, and this fails."""
    assert set(NAMES) - set(CASES) == set(), "an invariant has no broken-data test"
    assert set(CASES) - set(NAMES) == set()
    assert len(NAMES) == 20 and len(CASES) == len(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_each_invariant_fires_on_broken_data(conn: Any, dsn: str, role: str | None, name: str) -> None:
    fn, also = CASES[name]
    w = World(conn)
    assert failing(sweep(dsn, role)) == set()  # the same world is clean before the damage
    ctx = fn(conn, w)  # plain cases damage the data and return None; constraint cases return a context manager
    if ctx is None:
        counts = sweep(dsn, role)
    else:
        with ctx:
            counts = sweep(dsn, role)
    assert counts[name] >= 1, f"{name!r} stayed at 0 on data that breaks it: the check does not fire"
    assert failing(counts) == {name} | also, f"unexpected knock-on invariants: {failing(counts) - {name} - also}"


def test_the_newest_snapshot_invariant_also_fires_for_a_future_snapshot(conn: Any, dsn: str) -> None:
    World(conn, newest_age=-600)  # newest snapshot is 10 minutes ahead of the clock
    assert sweep(dsn)["newest snapshot in the future or older than 5 minutes"] == 1


def test_the_newest_snapshot_invariant_is_quiet_for_a_fresh_snapshot(conn: Any, dsn: str) -> None:
    World(conn, newest_age=200)
    assert sweep(dsn)["newest snapshot in the future or older than 5 minutes"] == 0


# ------------------------------------------------------------------ the whole watchdog path on a real database
def plan(dsn: str, tmp_path: Path, **kw: Any) -> Any:
    return integrity.Plan(dsn=dsn, sql_path=REPO / "ops" / "audit" / "invariants.sql", archive_dir=tmp_path,
                          config_dir=REPO / "config", **kw)


def test_run_due_reports_ok_on_clean_data_and_a_named_warning_on_broken_data(conn: Any, dsn: str, tmp_path: Path) -> None:
    w = World(conn)
    state: dict[str, Any] = {}
    res, _ = integrity.run_due(plan(dsn, tmp_path, crosscheck_interval_s=0), state, now=1000.0)
    assert res["data_integrity"][0] == "ok" and "all 20 invariants hold" in res["data_integrity"][1]
    conn.execute("DELETE FROM poll_log WHERE snapshot_ts = %s", (w.ts[5],))
    res, _ = integrity.run_due(plan(dsn, tmp_path, crosscheck_interval_s=0), state, now=1000.0 + 72 * 3600)
    level, text = res["data_integrity"]
    assert level == "warn" and "snapshot rows without an ok poll_log row (1)" in text and "1 of 20" in text
    assert state["integrity"]["data_integrity"]["retry_at"] is not None  # rechecked in 6 h, not 3 days


def test_a_pack_that_lost_an_invariant_is_a_warning_not_a_pass(conn: Any, dsn: str, tmp_path: Path) -> None:
    """The watchdog compares what the pack returned with what the pack lists: a row that is gone fails loudly."""
    World(conn)
    rows = [(n, 0, None) for n in NAMES if n != "in-region count above global count"]
    findings, problems = integrity.evaluate(rows, NAMES)
    assert findings == [] and integrity.summarise_invariants(findings, problems, len(NAMES))[0] == "warn"


# ------------------------------------------------------------------ the archive cross-check
FIXTURE = json.loads((REPO / "tests" / "fixtures" / "vatsim-data.sample.json").read_text(encoding="utf-8"))
T0 = datetime(2026, 10, 7, 16, 11, 19, tzinfo=UTC)
INSIDE = {"cid": 8100001, "callsign": "TST1", "logon_time": "2026-10-07T15:00:00.1234567Z", "latitude": -33.9, "longitude": 151.2,
          "groundspeed": 250, "altitude": 5000}


class Archive:
    """The stored rows come from the collector's own ingest of the fixture payload; the archive file from the collector's
    own compact() writer. The tests then damage one side."""

    def __init__(self, conn: Any, refdata: Any, root: Path) -> None:
        from vatpac.archive import compact

        self.conn, self.root = conn, root
        partition_for(conn, T0)
        payload = at(FIXTURE, 0)
        apply(conn, payload, refdata)
        self.compact: dict[str, Any] = compact(payload)
        self.path = root / "compact" / f"{T0:%Y%m%d}" / f"{T0:%H%M%S}.json.gz"
        self.write()

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(gzip.compress(json.dumps(self.compact).encode()))


def cross(conn: Any, dsn: str, arc: Archive, role: str | None = None) -> Any:
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        if role:
            cur.execute(psycopg.sql.SQL("SET ROLE {}").format(psycopg.sql.Identifier(role)))
        return integrity.run_crosscheck(cur, [arc.path], REPO / "config")


@pytest.fixture()
def arc(conn: Any, refdata: Any, tmp_path: Path) -> Archive:
    return Archive(conn, refdata, tmp_path)


def test_cross_check_is_clean_when_stored_rows_match_the_archive(arc: Archive, conn: Any, dsn: str, role: str | None) -> None:
    res = cross(conn, dsn, arc, role)
    assert res.compared == 1 and res.mismatches == [] and res.skipped == 0
    assert integrity.summarise_crosscheck(res)[0] == "ok"


@pytest.mark.parametrize("column,word", [("pilots_global", "pilots_global"), ("pilots_in_region", "pilots_in_region"),
                                         ("controllers_global", "controllers_global"),
                                         ("controllers_in_region", "controllers_in_region")])
def test_cross_check_fires_when_a_stored_count_is_wrong(arc: Archive, conn: Any, dsn: str, column: str, word: str) -> None:
    conn.execute(f"UPDATE network_snapshot SET {column} = {column} + 1")  # noqa: S608
    res = cross(conn, dsn, arc)
    assert any(word in m and "independent" in m for m in res.mismatches), res.mismatches
    assert integrity.summarise_crosscheck(res)[0] == "warn"


def test_cross_check_fires_when_a_stored_pilot_is_missing_even_though_the_counts_agree(arc: Archive, conn: Any, dsn: str) -> None:
    conn.execute("DELETE FROM pilot_position WHERE ctid = (SELECT ctid FROM pilot_position LIMIT 1)")
    res = cross(conn, dsn, arc)
    assert len(res.mismatches) == 1 and "pilot sets differ" in res.mismatches[0]


def test_cross_check_fires_when_the_snapshot_row_is_missing(arc: Archive, conn: Any, dsn: str) -> None:
    conn.execute("TRUNCATE network_snapshot CASCADE")
    assert "no network_snapshot row" in cross(conn, dsn, arc).mismatches[0]


def test_cross_check_fires_when_the_collector_kept_a_pilot_the_independent_rule_rejects(arc: Archive, conn: Any, dsn: str) -> None:
    """Archive says a pilot is far outside the region; the stored rows still hold him as in-region (a polygon regression)."""
    stored = conn.execute("SELECT s.cid FROM pilot_position p JOIN pilot_session s ON s.id = p.session_id LIMIT 1").fetchone()["cid"]
    victim = next(p for p in arc.compact["pilots"] if p["cid"] == stored)
    victim["latitude"], victim["longitude"] = 51.5, -0.1  # London
    arc.write()
    res = cross(conn, dsn, arc)
    assert res.mismatches and any("pilots_in_region" in m or "pilot sets differ" in m for m in res.mismatches)


def test_cross_check_fires_when_an_inside_pilot_was_not_stored(arc: Archive, conn: Any, dsn: str) -> None:
    arc.compact["pilots"].append(dict(INSIDE))
    arc.write()
    msgs = cross(conn, dsn, arc).mismatches
    for word in ("pilots_global", "pilots_in_region", "pilot sets differ"):
        assert any(word in m for m in msgs), (word, msgs)


def test_cross_check_fires_when_a_whitelisted_controller_was_not_stored(arc: Archive, conn: Any, dsn: str) -> None:
    wl = integrity._independent().load_whitelist(REPO / "config" / "controller_callsigns_list.txt")
    stored = {r["callsign"] for r in conn.execute("SELECT callsign FROM controller_session").fetchall()}
    extra = sorted(wl - stored)[0]
    arc.compact["controllers"].append({"cid": 8200001, "callsign": extra, "logon_time": "2026-10-07T15:00:00Z",
                                       "frequency": "123.450", "rating": 5, "facility": 4})
    arc.write()
    msgs = cross(conn, dsn, arc).mismatches
    assert any("controllers_in_region" in m for m in msgs) and any("controllers_global" in m for m in msgs)


def test_cross_check_skips_samples_older_than_a_reference_file_change(arc: Archive, conn: Any, dsn: str) -> None:
    conn.execute("INSERT INTO reference_data_change (changed_at, file, new_sha) VALUES (%s, 'x', %s)", (T0 + timedelta(hours=1), "c" * 64))
    conn.execute("UPDATE network_snapshot SET pilots_in_region = pilots_in_region + 5")  # would be a mismatch, but the rules differed then
    res = cross(conn, dsn, arc)
    assert res.compared == 0 and res.skipped == 1 and res.mismatches == []
    assert integrity.summarise_crosscheck(res)[0] == "warn"  # nothing compared is never reported as a pass


def test_cross_check_reports_a_damaged_archive_file(arc: Archive, conn: Any, dsn: str) -> None:
    arc.path.write_bytes(b"not gzip")
    res = cross(conn, dsn, arc)
    assert res.compared == 0 and "could not be analysed" in res.mismatches[0]


def test_the_scheduled_cross_check_end_to_end(arc: Archive, conn: Any, dsn: str, tmp_path: Path) -> None:
    state: dict[str, Any] = {}
    p = plan(dsn, arc.root, invariants_interval_s=0)
    now = (T0 + timedelta(minutes=10)).timestamp()
    res, _ = integrity.run_due(p, state, now=now)
    assert res["feed_crosscheck"][0] == "ok" and "1 archived snapshots match" in res["feed_crosscheck"][1]
    conn.execute("UPDATE network_snapshot SET controllers_in_region = controllers_in_region + 1")
    assert integrity.run_due(p, state, now=now + 3600)[0]["feed_crosscheck"][0] == "ok"  # not due yet: the stored result stands
    state["integrity"]["feed_crosscheck"]["last_run"] = now - 14 * 86400  # fourteen days later
    res, _ = integrity.run_due(p, state, now=now)
    assert res["feed_crosscheck"][0] == "warn" and "controllers_in_region" in res["feed_crosscheck"][1]


# issue 92: a record the collector rejected makes poll_log.pilots_inside exceed network_snapshot.pilots_in_region by
# design; the two count invariants compare the stored pilots (pilots_kept), so neither fires
def test_a_rejected_pilot_fires_neither_the_poll_log_count_nor_the_positions_invariant(
        conn: Any, dsn: str, payload: dict, refdata: Any, role: str | None) -> None:
    import dataclasses

    from vatpac.filters import inside_polygon
    from vatpac.model import parse_snapshot
    from vatpac.store import apply_snapshot, write_poll_log

    snap = parse_snapshot(at(payload))
    partition_for(conn, snap.observed_at)
    poly, wl = refdata
    p = next(q for q in snap.pilots if inside_polygon(poly, q.latitude, q.longitude))
    bad = dataclasses.replace(p, callsign="REJECTED1", logon_time=p.logon_time.replace(microsecond=500_000))  # breaks a CHECK
    snap = dataclasses.replace(snap, pilots=[*snap.pilots, bad])
    res = apply_snapshot(conn, snap, poly, wl, polygon_sha="a" * 64, whitelist_sha="b" * 64)
    write_poll_log(conn, "ok", snapshot_ts=snap.observed_at, result=res, polygon_sha="a" * 64, whitelist_sha="b" * 64)
    if not conn.autocommit:
        conn.commit()
    row = conn.execute("SELECT p.pilots_inside, p.pilots_kept, n.pilots_in_region FROM poll_log p "
                       "JOIN network_snapshot n ON n.observed_at = p.snapshot_ts").fetchone()
    assert row["pilots_inside"] == row["pilots_kept"] + 1 and row["pilots_in_region"] == row["pilots_kept"]
    counts = sweep(dsn, role)
    assert counts["ok poll_log counts differ from network_snapshot"] == 0
    assert counts["positions per snapshot differ from pilots_in_region"] == 0
