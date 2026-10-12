"""Issue #89 phase 1b (#137): QUIET PER FLIGHT, owner decision 12-10-2026 (migration 20261012000200).

A flight is written once the FLIGHT ITSELF has had no position for flight_quiet_hours (8) of stored data, measured against the newest
snapshot. It does not matter that the same pilot is online again in a later, different flight (the finding on the live deploy: 141 pilots
online again held back all their older finished flights under the old per-pilot rule). Several tests below fail on the old rule.

Flights are the v1 groups: same cid, callsign, departure and arrival, a new connection joins when its logon_time is within 120 minutes of
the latest earlier last position. A flight still being continued by such a reconnect is not quiet.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from test_flight_writer import BASE, HOUR, MIN, flown, mk, records, snap, times, write

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration


def test_an_earlier_finished_flight_is_written_while_a_later_flight_of_the_same_pilot_is_still_active(conn: Any) -> None:
    """The worked example of the owner decision. Fails under the old per-pilot rule (which writes nothing until the pilot is quiet)."""
    leg1 = times(BASE, 120)                                               # 00:00 to 01:59, YSSY-YMML
    leg2 = times(BASE + 9 * HOUR, 120)                                     # 09:00 to 10:59, YMML-YSSY, a different flight
    s1 = mk(conn, 700, leg1, dep="YSSY", arr="YMML")
    s2 = mk(conn, 700, leg2, logon=leg2[0], dep="YMML", arr="YSSY")
    snap(conn, leg2[60])                                                   # leg 2 is in progress; leg 1 has been quiet for 8 h 1 min
    assert write(conn) == 1
    (r,) = records(conn, 700)
    assert (r["departure"], r["session_ids"]) == ("YSSY", [s1])
    assert flown(conn, s1) == leg1[-1] and flown(conn, s2) is None          # only the written flight's session is marked
    first = r
    snap(conn, leg2[-1] + 8 * HOUR)                                        # later the second flight is quiet too
    assert write(conn) == 1
    rows = records(conn, 700)
    assert len(rows) == 2 and rows[0] == first and rows[1]["session_ids"] == [s2] and flown(conn, s2) == leg2[-1]


def test_a_later_session_of_the_same_key_within_120_minutes_keeps_the_flight_open(conn: Any) -> None:
    """S has been quiet for 9 hours (the pilot IS picked) but T (same key) logged on 90 minutes after S's last position and is still
    active: they are ONE flight, which is not quiet, so nothing is written for either. Wrong grouping would write S."""
    s = times(BASE, 20)
    logon = s[-1] + 90 * MIN
    a = mk(conn, 701, s)
    b = mk(conn, 701, [logon + MIN, s[-1] + 9 * HOUR], logon=logon)         # a long session, last seen 9 h after S ended
    snap(conn, s[-1] + 9 * HOUR)
    assert conn.execute("SELECT count(*) AS n FROM pilot_session WHERE last_seen <= %s", (s[-1] + HOUR,)).fetchone()["n"] == 1   # S is old
    assert write(conn) == 0 and flown(conn, a) is None and flown(conn, b) is None and records(conn) == []
    snap(conn, s[-1] + 17 * HOUR + MIN)                                    # the joined flight (T ended 9 h after S) is quiet 8 h later
    assert write(conn) == 1
    (r,) = records(conn, 701)
    assert r["session_ids"] == [a, b]


def test_the_chain_through_an_old_middle_session_keeps_the_whole_flight_open(conn: Any) -> None:
    """S and U are both more than 8 h quiet; T joins U (within 120 minutes of U's end, NOT of S's) and is active. One flight, open."""
    s = times(BASE, 20)
    u_logon = s[-1] + 100 * MIN
    u = [u_logon + MIN, u_logon + 20 * MIN]
    t_logon = u[-1] + 100 * MIN
    ref = u[-1] + 9 * HOUR
    ids = [mk(conn, 702, s), mk(conn, 702, u, logon=u_logon), mk(conn, 702, [t_logon + MIN, ref], logon=t_logon)]
    snap(conn, ref)
    assert ref - u[-1] >= 8 * HOUR and t_logon - s[-1] > 120 * MIN          # U is old; T does not join S directly
    assert write(conn) == 0
    assert all(flown(conn, i) is None for i in ids) and records(conn) == []
    snap(conn, ref + 8 * HOUR)
    assert write(conn) == 1 and records(conn, 702)[0]["session_ids"] == ids


@pytest.mark.parametrize("minutes, written", [(119, 0), (120, 0), (121, 1)], ids=["119-joins-so-open", "120-joins-so-open", "121-separate-so-written"])
def test_the_120_minute_boundary_decides_whether_a_later_active_session_holds_the_flight_open(conn: Any, minutes: int, written: int) -> None:
    s = times(BASE, 20)
    logon = s[-1] + minutes * MIN
    a = mk(conn, 703, s)
    b = mk(conn, 703, [logon + MIN, s[-1] + 9 * HOUR], logon=logon)         # a long session: still active 9 h after S ended
    snap(conn, s[-1] + 9 * HOUR)                                           # T is active, S ended 9 h ago
    assert write(conn) == written
    assert (flown(conn, a) is not None) == bool(written) and flown(conn, b) is None


def test_an_amended_plan_is_a_different_flight_so_the_earlier_one_is_written(conn: Any) -> None:
    s = times(BASE, 20)
    t = times(s[-1] + 30 * MIN, 20)
    a = mk(conn, 704, s, arr="YMML")
    b = mk(conn, 704, t, logon=t[0], arr="YBBN")                           # same callsign, other arrival: v1 keeps them apart
    snap(conn, s[-1] + 8 * HOUR + MIN)                                      # S has been quiet for 8 h 1 min; T ended 7 h 12 min ago
    assert write(conn) == 1
    assert [r["session_ids"] for r in records(conn, 704)] == [[a]] and flown(conn, b) is None


def test_a_session_in_the_middle_that_is_still_active_does_not_hold_back_the_ones_around_it(conn: Any) -> None:
    a = mk(conn, 705, times(BASE, 20), dep="YSSY", arr="YMML")
    mid = mk(conn, 705, times(BASE + 10 * HOUR, 20), logon=BASE + 10 * HOUR, dep="YMML", arr="YBBN")     # active
    c = mk(conn, 705, times(BASE + HOUR, 20), logon=BASE + HOUR, dep="YBBN", arr="YSSY")          # finished
    snap(conn, BASE + 10 * HOUR + 10 * MIN)
    assert write(conn) == 2
    assert {tuple(r["session_ids"]) for r in records(conn, 705)} == {(a,), (c,)}
    assert flown(conn, mid) is None and flown(conn, a) is not None and flown(conn, c) is not None


def test_a_no_plan_session_is_marked_when_it_is_itself_quiet_not_when_its_pilot_is(conn: Any) -> None:
    mk(conn, 706, times(BASE, 20))
    other = mk(conn, 706, times(BASE + 6 * HOUR, 20), logon=BASE + 6 * HOUR, dep=None, arr=None)
    snap(conn, BASE + 9 * HOUR)                                            # first flight quiet; the no-plan session ended 2 h 40 min ago
    assert write(conn) == 1 and flown(conn, other) is None
    snap(conn, BASE + 15 * HOUR)
    assert write(conn) == 0 and flown(conn, other) is not None


def test_the_boundary_is_measured_on_the_flights_newest_last_seen(conn: Any) -> None:
    s = times(BASE, 20)
    t = times(s[-1] + 60 * MIN, 20)
    mk(conn, 707, s)
    mk(conn, 707, t, logon=t[0])
    snap(conn, t[-1] + 8 * HOUR - MIN)
    assert write(conn) == 0
    snap(conn, t[-1] + 8 * HOUR)
    assert write(conn) == 1


def test_a_returning_session_flight_waits_for_its_own_quiet_period(conn: Any) -> None:
    from test_flight_writer import extend
    ts = times(BASE, 20)
    sid = mk(conn, 708, ts)
    snap(conn, ts[-1] + 9 * HOUR)
    assert write(conn) == 1
    back = times(ts[-1] + 10 * HOUR, 10)
    extend(conn, sid, back)
    snap(conn, back[-1] + HOUR)
    assert write(conn) == 0                                                # the returning part is not quiet yet
    snap(conn, back[-1] + 8 * HOUR)
    assert write(conn) == 1 and len(records(conn, 708)) == 2


def test_a_failed_call_leaves_nothing_half_done_and_the_next_call_resumes(conn: Any) -> None:
    mk(conn, 709, times(BASE, 20), dep="YSSY", arr="YMML")
    mk(conn, 709, times(BASE + 3 * HOUR, 20), logon=BASE + 3 * HOUR, dep="YMML", arr="YBBN")
    snap(conn, BASE + 30 * HOUR)
    conn.execute("CREATE FUNCTION boom() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.departure = 'YMML' THEN RAISE EXCEPTION 'boom'; END IF; RETURN NEW; END $$")
    conn.execute("CREATE TRIGGER boom BEFORE INSERT ON flight_record FOR EACH ROW EXECUTE FUNCTION boom()")
    try:
        with pytest.raises(psycopg.errors.Error):
            write(conn)
        assert records(conn) == [] and conn.execute("SELECT count(*) AS n FROM pilot_session WHERE flown_upto IS NOT NULL").fetchone()["n"] == 0
    finally:
        conn.execute("DROP TRIGGER boom ON flight_record")
        conn.execute("DROP FUNCTION boom()")
    assert write(conn) == 2 and write(conn) == 0 and len(records(conn, 709)) == 2


def test_flights_held_open_by_an_active_reconnect_do_not_starve_other_pilots(conn: Any) -> None:
    """50 pilots whose old session is held open by an active joined reconnect come first in the pick (their oldest session is the
    oldest); a normal finished pilot behind them is still written in the same call (pilots that made no progress are skipped)."""
    for i in range(50):
        s_ = times(BASE, 20)
        logon = s_[-1] + 60 * MIN
        mk(conn, 800 + i, s_)
        mk(conn, 800 + i, [logon + MIN, BASE + 10 * HOUR], logon=logon)      # joins the first session, still active at 10:00
    done = times(BASE + HOUR, 20)
    mk(conn, 900, done, logon=done[0])
    snap(conn, BASE + 10 * HOUR)
    assert write(conn, 50) == 1
    assert [r["cid"] for r in records(conn)] == [900]


def test_the_collector_can_update_an_active_session_of_the_same_pilot_while_the_writer_runs(conn: Any, dsn: str) -> None:
    """Only sessions last seen 120+ minutes before the reference time are locked. Hold the writer at its main statement (ACCESS EXCLUSIVE on
    pilot_position) and update the pilot's ACTIVE session from a second connection with a short lock_timeout: it must not be blocked.
    Fails on a writer that locks every unprocessed session of the pilot."""
    import threading
    import time

    done = times(BASE, 20)
    a = mk(conn, 710, done, dep="YSSY", arr="YMML")                         # finished flight: the pilot is picked
    live = BASE + 9 * HOUR
    b = mk(conn, 710, [live, live + MIN], logon=live, dep="YMML", arr="YSSY")   # active, different flight
    snap(conn, live + MIN)
    out: dict[str, int] = {}

    def run() -> None:
        with psycopg.connect(dsn, autocommit=True) as c:
            out["n"] = c.execute("SELECT write_finished_flights()").fetchone()[0]

    holder = psycopg.connect(dsn)
    holder.execute("LOCK TABLE pilot_position IN ACCESS EXCLUSIVE MODE")
    t = threading.Thread(target=run)
    t.start()
    try:
        for _ in range(100):
            if conn.execute("SELECT 1 AS x FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE '%write_finished_flights%' "
                            "AND pid <> pg_backend_pid()").fetchone():
                break
            time.sleep(0.05)
        else:
            pytest.fail("the writer never reached the blocked statement")
        with psycopg.connect(dsn, autocommit=True) as collector:             # the collector's per-poll update of the active session
            collector.execute("SET lock_timeout = '500ms'")
            collector.execute("UPDATE pilot_session SET last_seen = last_seen + interval '30 seconds' WHERE id = %s", (b,))
    finally:
        holder.rollback()
        holder.close()
        t.join(10)
    assert out["n"] == 1 and [r["session_ids"] for r in records(conn, 710)] == [[a]]
    assert flown(conn, a) is not None and flown(conn, b) is None


def test_a_recent_session_is_never_marked_even_when_its_flight_looks_quiet_from_old_data(conn: Any) -> None:
    """A session that is neither locked nor quiet can never be marked: here a very recent session of a different key stays untouched
    while the old one is written."""
    a = mk(conn, 711, times(BASE, 20), dep="YSSY", arr="YMML")
    now_s = BASE + 9 * HOUR
    b = mk(conn, 711, [now_s], logon=now_s, dep="YMML", arr="YSSY")
    snap(conn, now_s)
    assert write(conn) == 1 and flown(conn, a) is not None and flown(conn, b) is None
    assert write(conn) == 0 and flown(conn, b) is None
