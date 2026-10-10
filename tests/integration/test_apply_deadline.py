"""Issue 88 against Postgres: a blocked apply ends by its deadline, returns its connection and never overlaps."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import psycopg
import pytest

from helpers import at, partition_for
from test_hardening_ingest import _collector

pytestmark = pytest.mark.integration

T1 = datetime(2026, 10, 7, 16, 11, 19, tzinfo=timezone.utc)


@pytest.fixture()
def db(conn):
    partition_for(conn, T1)
    return conn


def test_a_blocked_apply_ends_by_its_deadline_frees_the_pool_and_the_next_poll_succeeds(
        db, dsn, payload, refdata, monkeypatch) -> None:
    import vatpac.main as main

    monkeypatch.setattr(main, "APPLY_DEADLINE_S", 1.0)
    monkeypatch.setattr(main, "APPLY_TIMEOUT_S", 8.0)
    col, pool = _collector(dsn, at(payload), refdata)
    blocker = psycopg.connect(dsn, autocommit=False)
    try:
        blocker.execute("LOCK TABLE pilot_session IN ACCESS EXCLUSIVE MODE")  # every apply now waits on this lock

        async def scenario():
            first = asyncio.create_task(col.poll_once())
            await asyncio.sleep(0.4)  # the worker thread is inside the transaction, waiting for the lock
            assert col._apply_lock.locked()
            with pytest.raises(main.ApplyBusyError):  # a second apply never overlaps the first
                col._apply(main.parse_snapshot(col.feed.payload), col.ref)
            t0 = time.monotonic()
            outcome = await first
            return outcome, t0

        t_start = time.monotonic()
        outcome, _ = asyncio.run(scenario())
        elapsed = time.monotonic() - t_start
        assert outcome.status == "error" and 0.9 < elapsed < 4.0, elapsed  # the deadline (1 s), not the 8 s wait
        assert not col._apply_lock.locked()
        waiting = db.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database() "
                             "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()").fetchone()["n"]
        assert waiting == 0  # no backend is left waiting for the lock
        assert pool.get_stats()["pool_available"] >= 1  # the connection went back to the pool
        blocker.rollback()  # release the lock
        assert db.execute("SELECT count(*) AS n FROM pilot_session").fetchone()["n"] == 0  # nothing half-written
        assert asyncio.run(col.poll_once()).status == "ok"
        assert db.execute("SELECT count(*) AS n FROM pilot_session").fetchone()["n"] > 0
        assert [r["status"] for r in db.execute("SELECT status FROM poll_log ORDER BY id").fetchall()] == ["error", "ok"]
    finally:
        blocker.close()
        pool.close()
