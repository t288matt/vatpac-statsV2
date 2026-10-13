"""Issue #98: dropping partitions must not stall the collector's inserts, must not drop a partition that changed since it
was checked, and a partition that cannot be locked is kept for the next run instead of failing the whole call.

Issue 89 phase 4 (#140): the gate no longer finalises the pieces table; a partition is droppable once the flight writer has
processed its sessions and a written flight covers every position (db/migrations/20261013000210). The lock behaviour is the one
of migration 20261009000170 and is unchanged, so these tests keep their shape; the reconciliation itself is tested in
test_drop_gate.py. Every builder here runs the writer (process_flights) before the first call, as production does.
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

from drop_gate_helpers import drain, process_flights
from helpers import partition_for

pytestmark = pytest.mark.integration

TODAY = date(2026, 10, 7)
FN = "drop_old_pilot_position_partitions"
SESSION_SQL = ("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
               "VALUES (%s,%s,%s,%s,%s,'YSSY','YMML') RETURNING id")


@pytest.fixture(autouse=True)
def _no_endless_waits(conn):
    """A drop that waits for a lock must fail the test after 20 seconds, not hang the suite (a mutated function would)."""
    conn.execute("SET lock_timeout = '20s'")


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


def state(conn) -> tuple[list[str], list[int]]:
    parts = sorted(r["p"] for r in conn.execute(
        "SELECT c.relname AS p FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid WHERE i.inhparent = 'pilot_position'::regclass").fetchall())
    flights = [r["session_id"] for r in conn.execute("SELECT session_id FROM flight_summary ORDER BY session_id").fetchall()]
    return parts, flights


def notices(conn) -> list[str]:
    seen: list[str] = []
    conn.add_notice_handler(lambda d: seen.append(d.message_primary))
    return seen


def call(conn, retention: int = 90) -> int:
    return conn.execute(f"SELECT {FN}(%s, %s) AS n", (retention, TODAY)).fetchone()["n"]


def old_partitions(conn, offsets: list[int]) -> None:
    for i, off in enumerate(offsets):
        add_session(conn, 9820000 + i, day(off), day(off) + timedelta(hours=1), [day(off)])
    process_flights(conn)


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
    process_flights(conn)


# ------------------------------------------------------------------ what is dropped
@pytest.mark.parametrize("retention", [90, 180])
def test_due_unpinned_processed_partitions_are_dropped_and_the_rest_kept(conn, retention) -> None:
    build(conn, retention)
    before = state(conn)
    n = drain(conn, retention)
    after = state(conn)
    assert n >= 2                                                    # something really was dropped
    assert any(p.endswith(f"{day(retention + 1):%Y%m%d}") for p in after[0])   # the straddling session's day is pinned
    assert pname(120) in after[0]                                    # a session open now pins its day
    assert after[1] == before[1]                                     # written flights are never touched by a drop


def test_one_day_per_call_and_the_collector_loop_reaches_the_end_state(conn, monkeypatch) -> None:
    """After a DROP the parent table stays locked until the call ends, so a call drops exactly one day; the collector's loop (with
    a pause between calls, so the lock is released and the polls can write) drains the backlog."""
    import vatpac.maintenance as m
    monkeypatch.setattr(m, "DROP_PAUSE_S", 0)
    old_partitions(conn, list(range(300, 275, -1)))                  # 25 partitions due
    before = state(conn)
    assert call(conn) == 1                                           # one call: one day
    assert len(state(conn)[0]) == len(before[0]) - 1
    assert call(conn) == 1                                           # and the next call, the next day
    assert m.drop_old_partitions(conn, TODAY, 90) == 23              # the collector's loop does the rest
    assert not [p for p in state(conn)[0] if p in {pname(o) for o in range(300, 275, -1)}]
    assert state(conn)[1] == before[1]
    assert m.drop_old_partitions(conn, TODAY, 90) == 0


def test_lock_timeout_is_restored(conn) -> None:
    """Inside the caller's own transaction the setting made for the drops must not leak into the rest of it."""
    old_partitions(conn, [200, 199])
    conn.execute("BEGIN")
    conn.execute("SET LOCAL lock_timeout = '7s'")
    assert drain(conn) == 2
    assert conn.execute("SHOW lock_timeout").fetchone()["lock_timeout"] == "7s"
    conn.execute("ROLLBACK")


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
        assert n == (1 if busy < 3 else 0)                         # three candidates are tried; at most one is dropped
        assert w.errors == [] and w.worst < 0.5, (w.errors, w.worst)
        dropped = [offsets[busy]] if busy < 3 else []
        assert [p for p in state(conn)[0] if p in {pname(o) for o in offsets}] == sorted(pname(o) for o in offsets if o not in dropped)
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


def test_a_busy_first_day_does_not_starve_the_next_and_only_one_is_dropped(conn, dsn) -> None:
    old_partitions(conn, [203, 202, 201])
    reader = psycopg.connect(dsn, autocommit=False)
    reader.execute(f"SELECT count(*) FROM {pname(203)}")
    try:
        assert call(conn) == 1                                       # 203 busy (three tries), 202 dropped, 201 left for the next call
        assert [p for p in state(conn)[0] if p in {pname(203), pname(202), pname(201)}] == sorted([pname(203), pname(201)])
    finally:
        reader.rollback()
        reader.close()
    assert drain(conn) == 2


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


# ------------------------------------------------------------------ the parent is not locked while the partitions are checked
def test_the_parent_is_not_locked_while_the_partitions_are_checked(conn, dsn) -> None:
    """The checks read flight_summary. Another session holds that table exclusively, so the function waits inside its checks,
    before any drop. A collector insert into pilot_position must go through meanwhile: nothing is dropped yet, so the parent is
    not locked (the earlier design finalised and dropped partition by partition and held the parent lock across the later checks)."""
    add_session(conn, 9810020, day(200), day(200) + timedelta(hours=1), [day(200)])
    add_session(conn, 9810021, day(199), day(199) + timedelta(hours=1), [day(199)])
    live = live_session(conn)
    process_flights(conn)
    holder = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    result: dict = {}

    def run() -> None:
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as c:
            result["n"] = c.execute(f"SELECT {FN}(90, %s) AS n", (TODAY,)).fetchone()["n"]

    try:
        holder.execute("LOCK TABLE flight_summary IN ACCESS EXCLUSIVE MODE")
        th = threading.Thread(target=run)
        th.start()
        time.sleep(2)                                                # the function is waiting for flight_summary
        assert th.is_alive()
        with psycopg.connect(dsn, autocommit=True, options="-c lock_timeout=1000") as w:
            w.execute("INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES (clock_timestamp(),%s,5000,250)", (live,))
        holder.rollback()
        th.join(30)
    finally:
        holder.close()
    assert result["n"] == 1


# ------------------------------------------------------------------ a partition that changed after it was checked
@pytest.fixture()
def hook(conn):
    """Lets a test change the data at a chosen moment INSIDE the drop function. The real reconciliation function is wrapped: on its
    n-th call (step 1 is the first call of a partition, step 2 the second) `action` runs first, in the same session."""
    conn.execute("ALTER FUNCTION unreconciled_partition_sessions(timestamptz, timestamptz) RENAME TO unreconciled_partition_sessions_real")
    conn.execute("CREATE TABLE hook_calls (n int)")
    state_ = {"action": None, "on_call": 0}

    def install(on_call: int, action_sql: str) -> None:
        state_.update(action=action_sql, on_call=on_call)
        conn.execute(f"""
            CREATE OR REPLACE FUNCTION unreconciled_partition_sessions(p_start timestamptz, p_end timestamptz)
            RETURNS TABLE (session_id bigint, reason text) LANGUAGE plpgsql AS $f$
            DECLARE c int;
            BEGIN
                INSERT INTO hook_calls VALUES (1);
                SELECT count(*) INTO c FROM hook_calls;
                IF c = {on_call} THEN
                    {action_sql};
                END IF;
                RETURN QUERY SELECT * FROM unreconciled_partition_sessions_real(p_start, p_end);
            END $f$""")

    try:
        yield install
    finally:
        conn.execute("DROP FUNCTION IF EXISTS unreconciled_partition_sessions(timestamptz, timestamptz)")
        conn.execute("ALTER FUNCTION unreconciled_partition_sessions_real(timestamptz, timestamptz) RENAME TO unreconciled_partition_sessions")
        conn.execute("DROP TABLE IF EXISTS hook_calls")


def test_a_late_position_between_the_check_and_the_recheck_keeps_the_partition(conn, hook) -> None:
    """Two partitions pass step 1 (calls 1 and 2). Before step 2 re-checks the first one (call 3) a position arrives in it that no
    flight covers. That one is kept, the other is dropped."""
    x = add_session(conn, 9830001, day(200), day(200) + timedelta(hours=1), [day(200), day(200) + timedelta(minutes=5)])
    add_session(conn, 9830002, day(199), day(199) + timedelta(hours=1), [day(199)])
    process_flights(conn)
    hook(3, f"INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES ('{day(200) + timedelta(minutes=30):%Y-%m-%d %H:%M:%S+00}', {x}, 5000, 250)")
    seen = notices(conn)
    assert call(conn) == 1
    assert pname(200) in state(conn)[0] and pname(199) not in state(conn)[0]
    assert any("DROP REFUSED" in s and pname(200) in s for s in seen)


def test_a_session_seen_again_between_the_recheck_and_the_lock_keeps_the_partition(conn, hook) -> None:
    """The last chance is inside the lock (the second call of the reconciliation for the day is step 2, after its pinning test). A session
    of the day comes back meanwhile; the in-lock pinning test keeps the day."""
    x = add_session(conn, 9830011, day(200), day(200) + timedelta(hours=1), [day(200)])
    process_flights(conn)
    hook(2, f"UPDATE pilot_session SET last_seen = now() WHERE id = {x}")
    seen = notices(conn)
    assert call(conn) == 0
    assert pname(200) in state(conn)[0]
    assert any("DROP REFUSED" in s and pname(200) in s for s in seen)


def test_a_position_added_by_a_session_not_in_the_earlier_list_between_check_and_lock_keeps_the_day(conn, hook) -> None:
    """The in-lock check is rebuilt from the partition, not from a list of sessions made before the lock. Session z has positions
    only in day 150 when day 200 is checked (calls 1 and 3 for day 200, 2 and 4 for day 150); at the in-lock call for day 200
    (call 5) z receives a position in day 200 that no written flight covers. Day 200 is kept, day 150 is dropped."""
    add_session(conn, 9830021, day(200), day(200) + timedelta(hours=1), [day(200)])
    z = add_session(conn, 9830022, day(150), day(150) + timedelta(hours=1), [day(150)])
    process_flights(conn)
    late = day(200) - timedelta(hours=6)
    hook(5, f"INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES ('{late:%Y-%m-%d %H:%M:%S+00}', {z}, 5000, 250)")
    seen = notices(conn)
    assert call(conn) == 1
    assert pname(200) in state(conn)[0] and pname(150) not in state(conn)[0]
    assert any("DROP REFUSED" in s and pname(200) in s for s in seen)
