"""Issue #153 section C: the documented limits of the flight writer, each pinned by a test of what actually happens today.

(a) a reconnect first stored more than 8 h after its logon becomes a separate flight (v1 would join it);
(b) the collector waits at most 5 s for a session the writer holds; after that one snapshot is lost;
(c) a flight held open by a chain over 8 h long is re-examined on every call and written once, whole, when it goes quiet.
Correctness only; the large-scale timing is the scale test (#162).
"""
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from helpers import apply, at, partition_for
from test_flight_writer import BASE, HOUR, MIN, flown, mk, records, snap, times, write

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration
T1 = datetime(2026, 10, 7, 16, 11, 19, tzinfo=UTC)


# ------------------------------------------------------------------ (a)
def test_a_reconnect_first_stored_over_8_hours_after_its_logon_is_a_separate_flight(conn: Any) -> None:
    """Session 1 ends at E. Session 2 logs on 30 minutes later (inside the 120 minute rule) but its first position is stored 8 h 10 min after
    that logon (the pilot was outside the region, or the collector was down). By then the writer has already written and marked session 1, so
    session 2 has no earlier unprocessed session to join: two flights. A pilot whose session 2 was stored in time gets one flight."""
    first = times(BASE, 20)
    end = first[-1]
    s1 = mk(conn, 7100, first)
    snap(conn, end + 8 * HOUR + MIN)
    assert write(conn) == 1 and flown(conn, s1) is not None            # session 1 is written before session 2 has a stored position
    logon = end + 30 * MIN
    late = times(logon + 8 * HOUR + 10 * MIN, 20)
    s2 = mk(conn, 7100, late, logon=logon)
    snap(conn, late[-1] + 8 * HOUR)
    assert write(conn) == 1
    rows = records(conn, 7100)
    assert [r["session_ids"] for r in rows] == [[s1], [s2]] and rows[0]["end_time"] == end and rows[1]["start_time"] == late[0]

    a = times(BASE, 20)                                                # the control: session 2 stored in time, so the writer sees both
    t1 = mk(conn, 7101, a)
    t2 = mk(conn, 7101, times(a[-1] + 30 * MIN, 20), logon=a[-1] + 30 * MIN)
    snap(conn, a[-1] + 30 * MIN + 20 * MIN + 9 * HOUR)
    assert write(conn) == 1 and [r["session_ids"] for r in records(conn, 7101)] == [[t1, t2]]


# ------------------------------------------------------------------ (b)
def _seed_old_session(db: Any, payload: Any, refdata: Any) -> None:
    partition_for(db, T1)
    partition_for(db, T1 + timedelta(days=1))
    apply(db, at(payload), refdata)
    db.execute("UPDATE pilot_session SET departure = 'YSSY', arrival = 'YMML'")
    snap(db, T1 + 9 * HOUR)                                            # the writer's clock: every stored session is 9 hours old


def _poll(dsn: str, payload: Any, refdata: Any, seconds: int) -> tuple[Any, float]:
    """Apply the snapshot ``seconds`` after the first one as the collector would (its own connection options, so its 5 s lock_timeout).
    Returns (the exception or None, seconds taken)."""
    from vatpac.main import DB_OPTIONS

    c, t0 = psycopg.connect(dsn, options=DB_OPTIONS, autocommit=False), time.monotonic()
    try:
        apply(c, at(payload, seconds), refdata)
        return None, time.monotonic() - t0
    except psycopg.Error as exc:
        return exc, time.monotonic() - t0
    finally:
        c.close()


def _stored(conn: Any) -> dict[str, int]:
    return conn.execute("SELECT (SELECT count(*) FROM pilot_position) AS p, (SELECT count(*) FROM network_snapshot) AS n").fetchone()


def test_a_snapshot_that_waits_over_5_seconds_for_the_writer_is_lost(conn: Any, dsn: str, payload: Any, refdata: Any) -> None:
    """The writer holds its sessions (FOR UPDATE) until its transaction ends; the pilots come back on the same logon 9 hours later, so the
    collector's update of those sessions queues behind it. At the collector's lock_timeout (5 s) the whole snapshot transaction fails and
    nothing of it is stored; the next poll is a new snapshot, so that one is not retried. This is the register's limit, as stated."""
    _seed_old_session(conn, payload, refdata)
    before = _stored(conn)
    writer = psycopg.connect(dsn, autocommit=False)
    try:
        assert writer.execute("SELECT write_finished_flights(500)").fetchone()[0] >= 1      # the transaction stays open: locks held
        err, took = _poll(dsn, payload, refdata, 9 * 3600 + 60)
        assert isinstance(err, psycopg.errors.LockNotAvailable), err
        assert 4.5 < took < 8, took                                    # the 5 s lock_timeout, not the 20 s statement timeout
        assert _stored(conn) == before                                 # nothing of the lost snapshot is stored
    finally:
        writer.commit()
        writer.close()
    err, _ = _poll(dsn, payload, refdata, 9 * 3600 + 90)               # the next poll is fine, and the lost one is not coming back
    assert err is None
    stamps = [r["observed_at"] for r in conn.execute("SELECT observed_at FROM network_snapshot ORDER BY 1").fetchall()]
    assert T1 + timedelta(seconds=9 * 3600 + 60) not in stamps and T1 + timedelta(seconds=9 * 3600 + 90) in stamps


def test_a_snapshot_that_waits_under_5_seconds_for_the_writer_is_kept(conn: Any, dsn: str, payload: Any, refdata: Any) -> None:
    _seed_old_session(conn, payload, refdata)
    writer = psycopg.connect(dsn, autocommit=False)
    writer.execute("SELECT write_finished_flights(500)")
    timer = threading.Timer(1.5, writer.commit)                        # the writer finishes inside the wait
    timer.start()
    try:
        err, took = _poll(dsn, payload, refdata, 9 * 3600 + 60)
    finally:
        timer.join()
        writer.close()
    assert err is None and 1 < took < 4.5, (err, took)
    assert conn.execute("SELECT count(*) AS n FROM network_snapshot WHERE observed_at = %s",
                        (T1 + timedelta(seconds=9 * 3600 + 60),)).fetchone()["n"] == 1


# ------------------------------------------------------------------ (c)
def test_a_chain_longer_than_8_hours_is_re_examined_on_every_call_and_written_whole_once_quiet(conn: Any) -> None:
    """Six sessions of 20 minutes, each logging on 100 minutes after the previous ended: one flight about 10 hours long. While the last session
    is recent the flight is open: every call writes nothing and marks nothing (the early sessions are quiet themselves, but belong to it).
    Once the whole flight has been quiet for 8 hours it is written in one row with every session."""
    sids, t = [], BASE
    for _ in range(6):
        ts = times(t, 20)
        sids.append(mk(conn, 7300, ts, logon=ts[0]))
        t = ts[-1] + 100 * MIN
    last = conn.execute("SELECT max(last_seen) AS t FROM pilot_session").fetchone()["t"]
    assert last - BASE > 8 * HOUR
    snap(conn, last + 60 * MIN)
    for _ in range(3):
        assert write(conn) == 0
        assert [flown(conn, s) for s in sids] == [None] * 6 and records(conn) == []
    snap(conn, last + 8 * HOUR)
    assert write(conn) == 1 and write(conn) == 0
    (row,) = records(conn, 7300)
    assert row["session_ids"] == sids and row["start_time"] == BASE and row["end_time"] == last and row["sample_count"] == 6 * 20
    assert all(flown(conn, s) is not None for s in sids)
