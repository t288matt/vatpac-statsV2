"""Issue #153 (section C, item "the grouping SQL can drift"): the flight writer and the view ops_pending_flight group sessions into flights.

The writer cannot read the view: it groups by the stored POSITION times (first and last position of the unprocessed part of each session)
and breaks a chain at a returning session, which the view (sessions only) cannot know. So the two rules are kept in step by these tests,
on seeded varied data (flight_gen.py: joins at 119, 120 and 121 minutes, other plans, no plan, no positions, a logon 9 h before the first
position, a returning session). The contract the watchdog and the backfill rely on:

  * on fresh data the view's flights ARE the writer's flights (same key, same session count, same newest last_seen);
  * a flight the view calls quiet is written by the writer, and one it does not call quiet is not (same quiet period, same reference time);
  * after the writer has run, the view calls nothing quiet (the watchdog never reports a flight the writer has already dealt with);
  * where they differ (a returning session) the writer splits more finely, never less.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from flight_gen import build, run_writer, settle, store_snapshot
from test_flight_writer import HOUR, extend, records, snap

pytestmark = pytest.mark.integration
SEEDS = range(15)
Key = tuple[Any, ...]


def view_flights(conn: Any, *, quiet_only: bool = False) -> list[Key]:
    """(cid, callsign, departure, arrival, sessions, newest_seen) of the view's rows that have a plan."""
    return sorted(
        (r["cid"], r["callsign"], r["departure"], r["arrival"], r["session_count"], r["newest_seen"])
        for r in conn.execute("SELECT * FROM ops_pending_flight WHERE departure IS NOT NULL AND arrival IS NOT NULL"
                              + (" AND is_quiet" if quiet_only else "")).fetchall())


def written(conn: Any, skip: set[Key] | None = None) -> list[Key]:
    rows = sorted((r["cid"], r["callsign"], r["departure"], r["arrival"], len(r["session_ids"]), r["end_time"]) for r in records(conn))
    return [r for r in rows if r not in (skip or set())]


def set_quiet_hours(conn: Any, hours: int) -> None:
    conn.execute("UPDATE app_setting SET value = %s WHERE key = 'flight_quiet_hours'", (str(hours),))


@pytest.mark.parametrize("seed", SEEDS)
def test_on_fresh_data_the_views_flights_are_the_writers_flights(conn: Any, seed: int) -> None:
    end = build(conn, seed, positions_always=True)
    snap(conn, end + 30 * HOUR)                                       # everything is quiet
    before = view_flights(conn, quiet_only=True)
    assert before == view_flights(conn) and before                    # (and the data does contain flights)
    run_writer(conn)
    assert written(conn) == before


@pytest.mark.parametrize("hours", [3, 8, 13])
@pytest.mark.parametrize("seed", SEEDS)
def test_the_writer_writes_what_the_view_calls_quiet_and_nothing_else(conn: Any, seed: int, hours: int) -> None:
    set_quiet_hours(conn, hours)
    end = build(conn, seed, positions_always=True)
    store_snapshot(conn, end, seed)                                   # 0 to 14 hours after the last position: some flights quiet, some not
    expected = view_flights(conn, quiet_only=True)
    run_writer(conn)
    assert written(conn) == expected
    assert conn.execute("SELECT count(*) AS n FROM ops_pending_flight WHERE is_quiet").fetchone()["n"] == 0


@pytest.mark.parametrize("seed", SEEDS)
def test_sessions_without_positions_make_the_view_a_superset_never_a_miss(conn: Any, seed: int) -> None:
    """A session with a plan but no stored position is in the view's flight and not in the writer's (the writer has no position to place it
    by); the writer marks it once it is quiet. So the view may list more, never less, and nothing quiet is left after the writer ran."""
    end = build(conn, seed)
    store_snapshot(conn, end, seed)
    quiet = view_flights(conn, quiet_only=True)
    run_writer(conn)
    for r in records(conn):
        key = (r["cid"], r["callsign"], r["departure"], r["arrival"])
        mine = [q for q in quiet if q[:4] == key]
        assert mine and len(r["session_ids"]) <= sum(q[4] for q in mine) and r["end_time"] <= max(q[5] for q in mine)
    assert conn.execute("SELECT count(*) AS n FROM ops_pending_flight WHERE is_quiet").fetchone()["n"] == 0


@pytest.mark.parametrize("seed", SEEDS)
def test_with_a_returning_session_the_writer_splits_more_finely_than_the_view_never_less(conn: Any, seed: int) -> None:
    end = build(conn, seed, positions_always=True)
    snap(conn, end + 30 * HOUR)
    settle(conn, end + 30 * HOUR, seed)
    first = set(written(conn))
    for r in conn.execute("SELECT id, last_seen FROM pilot_session WHERE cid % 2 = 0 AND departure IS NOT NULL AND arrival IS NOT NULL "
                          "ORDER BY id").fetchall():                  # the pilots come back on the same logon
        extend(conn, r["id"], [r["last_seen"] + timedelta(minutes=m) for m in (1, 2, 3)])
    snap(conn, end + 80 * HOUR)
    groups = view_flights(conn, quiet_only=True)
    settle(conn, end + 80 * HOUR, seed)
    split = written(conn, first)
    assert conn.execute("SELECT count(*) AS n FROM ops_pending_flight WHERE is_quiet").fetchone()["n"] == 0
    by_key = lambda rows: {k: sum(1 for r in rows if r[:4] == k) for k in {r[:4] for r in rows}}   # noqa: E731
    g, w = by_key(groups), by_key(split)
    assert all(w.get(k, 0) >= n for k, n in g.items()), (g, w)
    assert sum(len(r["session_ids"]) for r in records(conn)) >= sum(r[4] for r in groups)


def test_the_seeded_data_does_contain_a_returning_split_so_the_test_above_can_see_one(conn: Any) -> None:
    """Guards the test above against a generator that never produces the one known difference."""
    seen = False
    for seed in SEEDS:
        conn.execute("TRUNCATE flight_summary, pilot_position, pilot_session, network_snapshot RESTART IDENTITY CASCADE")
        end = build(conn, seed, positions_always=True)
        settle(conn, end + 30 * HOUR, seed)
        first = set(written(conn))
        for r in conn.execute("SELECT id, last_seen FROM pilot_session WHERE departure IS NOT NULL AND arrival IS NOT NULL").fetchall():
            extend(conn, r["id"], [r["last_seen"] + timedelta(minutes=m) for m in (1, 2, 3)])
        snap(conn, end + 80 * HOUR)
        groups = view_flights(conn, quiet_only=True)
        settle(conn, end + 80 * HOUR, seed)
        seen = seen or len(written(conn, first)) > len(groups)
        if seen:
            break
    assert seen
