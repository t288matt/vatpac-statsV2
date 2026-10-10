"""Issue #98: dropping partitions must not stall the collector's inserts, must not drop a partition that changed since it
was finalised, and a partition that cannot be locked is kept for the next run instead of failing the whole call.

The previous function (migration 20261007000021) is read from its file and created as drop_old_pilot_position_partitions_v1
so the two can be run on identical data: same partitions dropped, same pinned, same finalised summaries.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from helpers import REPO, partition_for

pytestmark = pytest.mark.integration

OLD_MIGRATION = REPO / "db" / "migrations" / "20261007000021_partition_functions_secure.sql"
TODAY = date(2026, 10, 7)
FN = "drop_old_pilot_position_partitions"
SESSION_SQL = ("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
               "VALUES (%s,%s,%s,%s,%s,'YSSY','YMML') RETURNING id")


@pytest.fixture()
def v1(conn):
    text = OLD_MIGRATION.read_text(encoding="utf-8")
    m = re.search(r"CREATE OR REPLACE FUNCTION drop_old_pilot_position_partitions\(.*?\n\$fn\$;", text, re.S)
    assert m, "old function not found"
    conn.execute(m.group(0).replace("drop_old_pilot_position_partitions(", "drop_old_pilot_position_partitions_v1(", 1))
    try:
        yield
    finally:
        conn.execute("DROP FUNCTION IF EXISTS drop_old_pilot_position_partitions_v1(int, date)")


def day(offset: int) -> datetime:
    return datetime(2026, 10, 7, 12, tzinfo=timezone.utc) - timedelta(days=offset)


def pname(offset: int) -> str:
    return f"pilot_position_{day(offset):%Y%m%d}"


def add_session(conn, cid: int, first: datetime, last: datetime, positions: list[datetime]) -> int:
    for t in positions:
        partition_for(conn, t)
    sid = conn.execute(SESSION_SQL, (cid, f"T{cid}", first.replace(microsecond=0), first, last)).fetchone()["id"]
    for t in positions:
        conn.execute("INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES (%s,%s,5000,250)", (t, sid))
    return sid


def state(conn) -> tuple[list[str], list[tuple[int, bool]]]:
    parts = sorted(r["p"] for r in conn.execute(
        "SELECT c.relname AS p FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'pilot_position'::regclass").fetchall())
    fin = [(r["session_id"], r["final"]) for r in conn.execute(
        "SELECT session_id, final FROM flight_summary ORDER BY session_id, gap_segment, span_part").fetchall()]
    return parts, fin


def notices(conn) -> list[str]:
    seen: list[str] = []
    conn.add_notice_handler(lambda d: seen.append(d.message_primary))
    return seen


def call(conn, retention: int = 90, fn: str = FN) -> int:
    return conn.execute(f"SELECT {fn}(%s, %s) AS n", (retention, TODAY)).fetchone()["n"]


def old_partitions(conn, offsets: list[int]) -> None:
    for i, off in enumerate(offsets):
        add_session(conn, 9820000 + i, day(off), day(off) + timedelta(hours=1), [day(off)])


class Writer:
    """The collector's insert pattern into today's partition every 50 ms, with its 5 second lock timeout."""

    def __init__(self, dsn: str, sid: int) -> None:
        self.dsn, self.sid = dsn, sid
        self.stop = threading.Event()
        self.worst = 0.0
        self.errors: list[str] = []
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self) -> None:
        with psycopg.connect(self.dsn, autocommit=True, options="-c lock_timeout=5000") as c:
            while not self.stop.is_set():
                t0 = time.time()
                try:
                    c.execute("INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) "
                              "VALUES (clock_timestamp(), %s, 5000, 250)", (self.sid,))
                except psycopg.Error as e:
                    self.errors.append(type(e).__name__)
                self.worst = max(self.worst, time.time() - t0)
                self.stop.wait(0.05)

    def __enter__(self) -> "Writer":
        self.thread.start()
        return self

    def __exit__(self, *a) -> None:
        self.stop.set()
        self.thread.join(10)


def live_session(conn) -> int:
    now = datetime.now(timezone.utc)
    return add_session(conn, 9829999, now, now, [now])


def build(conn, retention: int) -> None:
    """Partitions 300, 250, 200 and 120 days old (retention 180 or 90 decides which are due), a session that
    straddles the cutoff, a session still open, one session over two old days and an ordinary old one."""
    add_session(conn, 9810001, day(300), day(300) + timedelta(hours=2), [day(300), day(300) + timedelta(minutes=1)])
    add_session(conn, 9810002, day(250), day(249), [day(250), day(249)])                    # two days, both old
    add_session(conn, 9810003, day(200), day(200) + timedelta(hours=1), [day(200)])
    add_session(conn, 9810004, day(retention + 1), day(retention - 1), [day(retention + 1), day(retention - 1)])  # straddles: pins
    add_session(conn, 9810005, day(120), day(0), [day(120)])                                  # open now: pins day 120
    add_session(conn, 9810006, day(retention - 5), day(retention - 5) + timedelta(hours=1), [day(retention - 5)])


# ------------------------------------------------------------------ same results as the previous function
@pytest.mark.parametrize("retention", [90, 180])
def test_same_partitions_dropped_pinned_and_finalised_as_the_previous_function(conn, v1, retention) -> None:
    build(conn, retention)
    conn.execute("SELECT refresh_flight_summary(NULL)")           # as live: non-final summaries already exist
    before = state(conn)
    conn.execute("BEGIN")
    old_n = call(conn, retention, FN + "_v1")
    old_state = state(conn)
    conn.execute("ROLLBACK")
    assert state(conn) == before                                     # the rollback restored it, so the new run starts equal
    new_n = call(conn, retention)
    assert (new_n, state(conn)) == (old_n, old_state)
    assert new_n >= 2                                                # something really was dropped
    assert any(p.endswith(f"{day(retention + 1):%Y%m%d}") for p in state(conn)[0])   # the straddling session's day is pinned


def test_at_most_ten_per_call_and_the_collector_loop_reaches_the_same_end_state(conn, v1) -> None:
    from vatpac.maintenance import drop_old_partitions
    old_partitions(conn, list(range(300, 275, -1)))                  # 25 partitions due
    conn.execute("SELECT refresh_flight_summary(NULL)")
    before = state(conn)
    conn.execute("BEGIN")
    assert call(conn, 90, FN + "_v1") == 25
    old_state = state(conn)
    conn.execute("ROLLBACK")
    assert state(conn) == before
    assert call(conn) == 10                                          # one call: a short transaction, ten partitions
    assert len(state(conn)[0]) == len(before[0]) - 10
    assert drop_old_partitions(conn, TODAY, 90) == 15               # the collector's loop does the rest
    assert state(conn) == old_state
    assert drop_old_partitions(conn, TODAY, 90) == 0


def test_lock_timeout_is_restored(conn) -> None:
    old_partitions(conn, [200, 199])
    conn.execute("SET lock_timeout = '7s'")
    assert call(conn) == 2
    assert conn.execute("SHOW lock_timeout").fetchone()["lock_timeout"] == "7s"


# ------------------------------------------------------------------ busy partitions
@pytest.mark.parametrize("busy", [0, 1, 2, 3])
def test_busy_partitions_are_kept_without_an_error_or_a_stall(conn, dsn, busy) -> None:
    """Four partitions due; the oldest `busy` of them have a reader on them (worst case: the retries happen before the
    first drop). No exception, the free ones are dropped, the writer never waits and never fails."""
    offsets = [203, 202, 201, 200]
    old_partitions(conn, offsets)
    live = live_session(conn)
    readers = []
    seen = notices(conn)
    try:
        for off in offsets[:busy]:
            c = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
            c.execute(f"SELECT count(*) FROM {pname(off)}")
            readers.append(c)
        with Writer(dsn, live) as w:
            t0 = time.time()
            n = call(conn)
            elapsed = time.time() - t0
        assert n == 4 - busy
        assert w.errors == [] and w.worst < 0.5, (w.errors, w.worst)
        assert [p for p in state(conn)[0] if p in {pname(o) for o in offsets}] == sorted(pname(o) for o in offsets[:busy])
        assert sum("could not lock it" in s for s in seen) == busy
        if busy:
            assert elapsed >= 0.9 * busy                              # three attempts half a second apart for each
    finally:
        for c in readers:
            c.rollback()
            c.close()


def test_a_reader_that_leaves_during_the_retries_lets_the_drop_through(conn, dsn) -> None:
    old_partitions(conn, [200])
    reader = psycopg.connect(dsn, autocommit=False)
    reader.execute(f"SELECT count(*) FROM {pname(200)}")
    threading.Timer(0.7, reader.rollback).start()
    assert call(conn) == 1                                            # the third attempt succeeded
    assert pname(200) not in state(conn)[0]
    reader.close()


def test_an_open_writer_transaction_on_the_parent_does_not_hold_the_drop_for_the_session_lock_timeout(conn, dsn) -> None:
    old_partitions(conn, [200])
    live = live_session(conn)
    writer = psycopg.connect(dsn, autocommit=False)
    writer.execute("INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES (clock_timestamp(), %s, 1, 1)", (live,))
    try:
        conn.execute("SET lock_timeout = '5s'")
        t0 = time.time()
        assert call(conn) == 0                                        # the DROP needs the parent: waits 1 s per attempt, 3 attempts
        assert 2.5 < time.time() - t0 < 6                             # not 3 x the session's 5 seconds
        assert pname(200) in state(conn)[0]
    finally:
        writer.rollback()
        writer.close()
    assert call(conn) == 1


def test_a_busy_partition_after_the_first_drop_is_skipped_without_waiting(conn, dsn) -> None:
    """The parent is locked after the first drop, so a busy partition must be skipped at once (no sleeping, no waiting)."""
    old_partitions(conn, [203, 202, 201])
    reader = psycopg.connect(dsn, autocommit=False)
    reader.execute(f"SELECT count(*) FROM {pname(202)}")
    try:
        t0 = time.time()
        assert call(conn) == 2
        assert time.time() - t0 < 1.0
        assert [p for p in state(conn)[0] if p in {pname(203), pname(202), pname(201)}] == [pname(202)]
    finally:
        reader.rollback()
        reader.close()


def test_a_pinned_partition_and_a_busy_one_in_the_same_run(conn, dsn) -> None:
    add_session(conn, 9810101, day(250), day(0), [day(250)])           # open now: pins day 250
    old_partitions(conn, [240, 239])
    reader = psycopg.connect(dsn, autocommit=False)
    reader.execute(f"SELECT count(*) FROM {pname(240)}")
    try:
        assert call(conn) == 1                                        # 239 dropped, 240 busy, 250 pinned
        assert {pname(250), pname(240)} <= set(state(conn)[0]) and pname(239) not in state(conn)[0]
    finally:
        reader.rollback()
        reader.close()
    assert call(conn) == 1 and pname(240) not in state(conn)[0] and pname(250) in state(conn)[0]


# ------------------------------------------------------------------ the parent is not locked while later partitions are finalised
@pytest.mark.parametrize("fn, parent_locked", [(FN, False), (FN + "_v1", True)])
def test_the_parent_is_not_locked_while_later_partitions_are_finalised(conn, v1, dsn, fn, parent_locked) -> None:
    """Partition 1 is finalised, then the finalisation of partition 2 is held up by a row lock on its summary. The previous
    function had already dropped partition 1 by then (parent locked, so a collector insert fails on its lock timeout); the
    new one has dropped nothing yet, so the insert goes through. The v1 case proves the test can see the difference."""
    add_session(conn, 9810020, day(200), day(200) + timedelta(hours=1), [day(200)])
    second = add_session(conn, 9810021, day(199), day(199) + timedelta(hours=1), [day(199)])
    live = live_session(conn)
    conn.execute("SELECT refresh_flight_summary(NULL)")             # non-final summaries exist, as live
    holder = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    result: dict = {}

    def run() -> None:
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as c:
            result["n"] = c.execute(f"SELECT {fn}(90, %s) AS n", (TODAY,)).fetchone()["n"]

    try:
        holder.execute("UPDATE flight_summary SET computed_at = now() WHERE session_id = %s", (second,))
        th = threading.Thread(target=run)
        th.start()
        time.sleep(2)                                                # the function is waiting for that row
        assert th.is_alive()
        with psycopg.connect(dsn, autocommit=True, options="-c lock_timeout=1000") as w:
            insert = ("INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES (clock_timestamp(),%s,5000,250)", (live,))
            if parent_locked:
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    w.execute(*insert)
            else:
                w.execute(*insert)                                   # must not time out on a lock
        holder.rollback()
        th.join(30)
    finally:
        holder.close()
    assert result["n"] == 2


# ------------------------------------------------------------------ a partition that changed after it was finalised
def _race(conn, dsn, change) -> tuple[int, list[str]]:
    """Partition A (200 days old, session X) is finalised first; the finalisation of B (199 days old, session Y) is held up
    by a row lock; while it waits, `change(x)` is applied to X; then the function continues."""
    x = add_session(conn, 9830001, day(200), day(200) + timedelta(hours=1), [day(200), day(200) + timedelta(minutes=5)])
    y = add_session(conn, 9830002, day(199), day(199) + timedelta(hours=1), [day(199)])
    conn.execute("SELECT refresh_flight_summary(NULL)")
    holder = psycopg.connect(dsn, autocommit=False)
    out: dict = {}
    seen: list[str] = []

    def run() -> None:
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as c:
            c.add_notice_handler(lambda d: seen.append(d.message_primary))
            out["n"] = c.execute(f"SELECT {FN}(90, %s) AS n", (TODAY,)).fetchone()["n"]

    try:
        holder.execute("UPDATE flight_summary SET computed_at = now() WHERE session_id = %s", (y,))
        th = threading.Thread(target=run)
        th.start()
        time.sleep(2)
        assert th.is_alive()
        change(x)
        holder.rollback()
        th.join(30)
    finally:
        holder.close()
    return out["n"], seen


def test_a_session_reactivated_after_its_partition_was_finalised_keeps_the_partition(conn, dsn) -> None:
    def reactivate(x: int) -> None:
        conn.execute("UPDATE pilot_session SET last_seen = now() WHERE id = %s", (x,))
        conn.execute("INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES (%s,%s,5000,250)",
                     (day(200) + timedelta(minutes=10), x))
    n, seen = _race(conn, dsn, reactivate)
    assert n == 1                                                     # B dropped, A kept
    assert pname(200) in state(conn)[0] and pname(199) not in state(conn)[0]
    assert any("seen again" in s for s in seen)


def test_a_late_position_for_a_finalised_session_keeps_the_partition(conn, dsn) -> None:
    def late(x: int) -> None:                                         # the session itself stays old; only a position arrives
        conn.execute("INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES (%s,%s,5000,250)",
                     (day(200) + timedelta(minutes=30), x))
    n, seen = _race(conn, dsn, late)
    assert n == 1
    assert pname(200) in state(conn)[0] and any("newer than the final summary" in s for s in seen)


def test_a_session_that_lost_its_final_summary_keeps_the_partition(conn, dsn) -> None:
    def unfinalise(x: int) -> None:
        conn.execute("UPDATE flight_summary SET final = false WHERE session_id = %s", (x,))
    n, seen = _race(conn, dsn, unfinalise)
    assert n == 1 and pname(200) in state(conn)[0]
    assert any("no longer has a final summary" in s for s in seen)


def test_a_partition_with_an_unsummarisable_session_is_kept(conn) -> None:
    """The finalise step is stubbed to leave every session non-final: nothing may be dropped (finalise rule)."""
    old_partitions(conn, [200])
    seen = notices(conn)
    sig = "refresh_flight_summary_sessions(bigint[], boolean)"
    conn.execute(f"ALTER FUNCTION {sig} RENAME TO refresh_flight_summary_sessions_real")
    conn.execute("CREATE FUNCTION refresh_flight_summary_sessions(bigint[], boolean) RETURNS int LANGUAGE sql AS 'SELECT 0'")
    try:
        assert call(conn) == 0
        assert pname(200) in state(conn)[0]
        assert any("could not be summarised" in s for s in seen)
    finally:
        conn.execute(f"DROP FUNCTION {sig}")
        conn.execute("ALTER FUNCTION refresh_flight_summary_sessions_real(bigint[], boolean) RENAME TO refresh_flight_summary_sessions")
    assert call(conn) == 1                                            # with the real finalise it is dropped
