"""Issue #98: dropping a partition must not hold the lock on pilot_position while later partitions are finalised, and a
partition that cannot be locked is kept for the next run instead of failing the whole call.

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


def build(conn, retention: int) -> None:
    """Partitions 300, 250, 200, 181, 179 and 120 days old (retention 180 or 90 decides which are due), a session that
    straddles the cutoff, a session still open, one session over two old days and an ordinary old one."""
    add_session(conn, 9810001, day(300), day(300) + timedelta(hours=2), [day(300), day(300) + timedelta(minutes=1)])
    add_session(conn, 9810002, day(250), day(249), [day(250), day(249)])                    # two days, both old
    add_session(conn, 9810003, day(200), day(200) + timedelta(hours=1), [day(200)])
    add_session(conn, 9810004, day(retention + 1), day(retention - 1), [day(retention + 1), day(retention - 1)])  # straddles: pins
    add_session(conn, 9810005, day(120), day(0), [day(120)])                                  # open now: pins day 120
    add_session(conn, 9810006, day(retention - 5), day(retention - 5) + timedelta(hours=1), [day(retention - 5)])


@pytest.mark.parametrize("retention", [90, 180])
def test_same_partitions_dropped_pinned_and_finalised_as_the_previous_function(conn, v1, retention) -> None:
    build(conn, retention)
    conn.execute("SELECT refresh_flight_summary(NULL)")           # as live: non-final summaries already exist
    before = state(conn)
    conn.execute("BEGIN")
    old_n = conn.execute("SELECT drop_old_pilot_position_partitions_v1(%s, %s) AS n", (retention, TODAY)).fetchone()["n"]
    old_state = state(conn)
    conn.execute("ROLLBACK")
    assert state(conn) == before                                     # the rollback restored it, so the new run starts equal
    new_n = conn.execute("SELECT drop_old_pilot_position_partitions(%s, %s) AS n", (retention, TODAY)).fetchone()["n"]
    assert (new_n, state(conn)) == (old_n, old_state)
    assert new_n >= 2                                                # something really was dropped
    assert any(p.endswith(f"{day(retention + 1):%Y%m%d}") for p in state(conn)[0])   # the straddling session's day is pinned


def test_a_busy_partition_is_kept_without_an_error_and_dropped_next_time(conn, v1, dsn) -> None:
    sid = add_session(conn, 9810010, day(200), day(200) + timedelta(hours=1), [day(200)])
    add_session(conn, 9810011, day(199), day(199) + timedelta(hours=1), [day(199)])
    part = f"pilot_position_{day(200):%Y%m%d}"
    other = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    try:
        other.execute(f"SELECT count(*) FROM {part}")                # an open reader holds ACCESS SHARE on the partition
        conn.execute("SET lock_timeout = '5s'")
        t0 = time.time()
        n = conn.execute("SELECT drop_old_pilot_position_partitions(90, %s) AS n", (TODAY,)).fetchone()["n"]
        assert n == 1                                                # the free partition (199 days) was dropped
        assert 1.5 < time.time() - t0 < 8                            # three attempts of one second, not the 5 second session value
        parts, fin = state(conn)
        assert part in parts and f"pilot_position_{day(199):%Y%m%d}" not in parts
        assert all(f for s, f in fin if s == sid)                    # the finalisation was kept, not rolled back
        # the previous function fails the whole call on the same lock and rolls everything back
        conn.execute("SET lock_timeout = '1s'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            conn.execute("SELECT drop_old_pilot_position_partitions_v1(90, %s)", (TODAY,))
    finally:
        other.rollback()
        other.close()
    conn.execute("SET lock_timeout = 0")
    assert conn.execute("SELECT drop_old_pilot_position_partitions(90, %s) AS n", (TODAY,)).fetchone()["n"] == 1
    assert part not in state(conn)[0]


@pytest.mark.parametrize("fn, parent_locked", [("drop_old_pilot_position_partitions", False),
                                                ("drop_old_pilot_position_partitions_v1", True)])
def test_the_parent_is_not_locked_while_later_partitions_are_finalised(conn, v1, dsn, fn, parent_locked) -> None:
    """Partition 1 is finalised, then the finalisation of partition 2 is held up by a row lock on its summary. The previous
    function had already dropped partition 1 by then (parent locked, so a collector insert fails on its lock timeout); the
    new one has dropped nothing yet, so the insert goes through. The v1 case proves the test can see the difference."""
    add_session(conn, 9810020, day(200), day(200) + timedelta(hours=1), [day(200)])
    second = add_session(conn, 9810021, day(199), day(199) + timedelta(hours=1), [day(199)])
    live = add_session(conn, 9810022, day(0), day(0), [day(0)])
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
            insert = ("INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES (%s,%s,5000,250)",
                      (day(0) + timedelta(seconds=30), live))
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
