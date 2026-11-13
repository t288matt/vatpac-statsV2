"""Issue #153 (section H): a pilot whose writer batch hits the statement timeout used to be retried first on every call, for ever.

A timeout rolls the whole call back, the writer picks the oldest pilots again, and the same batch times out again (the daily maintenance with
its 15 minute timeout was the only way out). Now maintenance.refresh_flights halves the batch, and a pilot that times out ALONE is left out of
the 5-minute job for FLIGHT_SKIP_S while the other pilots are written (migration 20261113000200: the skip setting and the pick notice).
A slow pilot is simulated with a trigger that sleeps when that pilot's flight is inserted; the statement timeout is 700 ms.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest

from test_flight_writer import BASE, HOUR, MIN, mk, records, snap, times, write

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration
SLOW = 5001


@pytest.fixture()
def slow_pilot(conn: Any) -> Iterator[None]:
    conn.execute("CREATE FUNCTION slow_insert() RETURNS trigger LANGUAGE plpgsql AS "
                 f"$$ BEGIN IF NEW.cid = {SLOW} THEN PERFORM pg_sleep(2); END IF; RETURN NEW; END $$")
    conn.execute("CREATE TRIGGER slow_flight_summary BEFORE INSERT ON flight_summary FOR EACH ROW EXECUTE FUNCTION slow_insert()")
    try:
        yield
    finally:
        conn.execute("RESET statement_timeout")
        conn.execute("DROP TRIGGER IF EXISTS slow_flight_summary ON flight_summary")
        conn.execute("DROP FUNCTION IF EXISTS slow_insert()")


def seed_pilots(conn: Any) -> None:
    """The slow pilot is the OLDEST, so every batch picks it first; three ordinary pilots follow."""
    mk(conn, SLOW, times(BASE, 20))
    for i, cid in enumerate((5002, 5003, 5004)):
        mk(conn, cid, times(BASE + (i + 1) * 10 * MIN, 20))
    snap(conn, BASE + 12 * HOUR)


def cids(conn: Any) -> list[int]:
    return sorted(r["cid"] for r in records(conn))


def test_a_pilot_that_times_out_alone_is_skipped_so_the_others_are_written(conn: Any, slow_pilot: None, caplog: pytest.LogCaptureFixture) -> None:
    """Fails on the old job: every call picks the slow pilot with the others, times out, rolls back and writes nothing."""
    from vatpac.maintenance import DEMOTED, refresh_flights

    seed_pilots(conn)
    conn.execute("SET statement_timeout = '700ms'")
    with caplog.at_level("WARNING"):
        n = refresh_flights(conn, batch=4)
    conn.execute("RESET statement_timeout")
    assert n == 3 and cids(conn) == [5002, 5003, 5004]
    assert list(DEMOTED) == [SLOW] and DEMOTED[SLOW] > time.monotonic()
    assert any(getattr(r, "cid", None) == SLOW for r in caplog.records)
    assert conn.execute("SELECT flown_upto FROM pilot_session WHERE cid = %s", (SLOW,)).fetchone()["flown_upto"] is None   # untouched, not lost


def test_the_next_calls_leave_the_pilot_out_without_timing_out_again(conn: Any, slow_pilot: None) -> None:
    from vatpac.maintenance import refresh_flights

    seed_pilots(conn)
    conn.execute("SET statement_timeout = '700ms'")
    refresh_flights(conn, batch=4)
    t0 = time.monotonic()
    assert refresh_flights(conn, batch=4) == 0                       # nothing left but the skipped pilot: no timeout, no retry
    assert time.monotonic() - t0 < 0.5
    conn.execute("RESET statement_timeout")
    assert cids(conn) == [5002, 5003, 5004]


def test_the_batch_is_halved_when_the_timeout_names_several_pilots(conn: Any, slow_pilot: None) -> None:
    """A call over 4 pilots that times out is retried over 2 and then 1, so the pilots ahead of the slow one are written in the same call."""
    from vatpac.maintenance import refresh_flights

    mk(conn, 5000, times(BASE, 20))
    seed_pilots(conn)                                                # 5000 is older than the slow pilot's session? same start: cid order decides
    conn.execute("SET statement_timeout = '700ms'")
    refresh_flights(conn, batch=8)
    conn.execute("RESET statement_timeout")
    assert 5000 in cids(conn) and SLOW not in cids(conn)


def test_a_skipped_pilot_comes_back_after_the_skip_runs_out_and_when_the_daily_maintenance_runs(conn: Any, slow_pilot: None) -> None:
    from vatpac.maintenance import DEMOTED, refresh_flights

    seed_pilots(conn)
    conn.execute("SET statement_timeout = '700ms'")
    refresh_flights(conn, batch=4)
    DEMOTED[SLOW] = time.monotonic() - 1                             # the skip has run out: the pilot is tried (and times out) again
    t0 = time.monotonic()
    assert refresh_flights(conn, batch=4) == 0 and time.monotonic() - t0 > 0.6 and SLOW in DEMOTED
    conn.execute("RESET statement_timeout")
    conn.execute("DROP TRIGGER slow_flight_summary ON flight_summary")   # the problem has gone; the daily maintenance ignores the skip list
    assert refresh_flights(conn, batch=4, guarded=False) == 1 and cids(conn) == [SLOW, 5002, 5003, 5004] and not DEMOTED


def test_the_skip_setting_leaves_a_pilot_out_and_is_gone_after_the_statement(conn: Any) -> None:
    seed_pilots(conn)
    conn.execute("SELECT set_config('vatpac.flight_writer_skip', %s, false)", (f"{{{SLOW}}}",))
    assert write(conn) == 3 and cids(conn) == [5002, 5003, 5004]
    conn.execute("SELECT set_config('vatpac.flight_writer_skip', '', false)")
    assert write(conn) == 1 and cids(conn) == [SLOW, 5002, 5003, 5004]   # empty means nobody is skipped: today's behaviour


def test_the_collectors_call_does_not_leave_the_setting_on_the_connection(conn: Any) -> None:
    from vatpac.maintenance import DEMOTED, refresh_flights

    seed_pilots(conn)
    DEMOTED[SLOW] = time.monotonic() + 600
    assert refresh_flights(conn, batch=4) == 3
    assert conn.execute("SELECT current_setting('vatpac.flight_writer_skip', true) AS v").fetchone()["v"] in (None, "")
    assert refresh_flights(conn, batch=4, guarded=False) == 1 and cids(conn) == [SLOW, 5002, 5003, 5004]


def test_a_timeout_in_a_later_round_does_not_demote_a_pilot_who_already_finished(conn: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The last pick notice can name a pilot of an earlier round (the cancel hit a later pick). With a batch over 1 that is never blamed:
    the batch is narrowed to one first. Fails when a single named pilot is demoted at any batch size."""
    from vatpac import maintenance

    calls: list[int] = []

    def fake(_conn: Any, batch: int, skip: set[int], picked: list[list[int]]) -> int:
        calls.append(batch)
        if batch > 1:
            picked.append([7])
            raise psycopg.errors.QueryCanceled("timeout")
        return 3

    monkeypatch.setattr(maintenance, "_write_flights", fake)
    assert maintenance.refresh_flights(conn, batch=50) == 3
    assert calls == [50, 1] and 7 not in maintenance.DEMOTED
