"""Seeded, varied pilot sessions for comparing the flight writer with ops_pending_flight (issue #153).

Not a test file. ``build`` inserts, for a handful of pilots, sessions that sit on the edges of the v1 join rule: a logon exactly 119, 120 or
121 minutes after the previous end, different plans, no plan, no positions, a logon hours before the first stored position, and an
optional second visit to the same session after the writer has marked it (a returning session).
"""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

from test_flight_writer import mk, snap

BASE = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
GAPS_MIN = (5, 60, 119, 120, 121, 200, 700)
PLANS = (("YSSY", "YMML"), ("YSSY", "YMML"), ("YMML", "YSSY"), (None, None), ("YSSY", None))


def build(conn: Any, seed: int, pilots: int = 6, *, positions_always: bool = False) -> datetime:
    """Insert the sessions for ``seed``; returns the end of the last position. No snapshot is stored."""
    rng = random.Random(seed)
    last_end = BASE
    for i in range(pilots):
        cid, t = 9000 + i, BASE + timedelta(minutes=rng.randrange(0, 90))
        for _ in range(rng.randrange(1, 5)):
            n = rng.randrange(1, 40)
            ts = [t + timedelta(seconds=60 * k) for k in range(n)]
            dep, arr = rng.choice(PLANS[:3] if positions_always else PLANS)
            logon = ts[0].replace(microsecond=0) - (timedelta(hours=9) if rng.random() < 0.15 else timedelta(0))
            gs = [rng.choice((0, 0, 250)) for _ in ts]
            if not positions_always and rng.random() < 0.1:                                           # a session with a plan but no stored position
                conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
                             "VALUES (%s, 'QFA1', %s, %s, %s, %s, %s)", (cid, logon, ts[0], ts[-1], dep, arr))
            else:
                mk(conn, cid, ts, logon=logon, dep=dep, arr=arr, gs=gs)
            last_end = max(last_end, ts[-1])
            t = ts[-1] + timedelta(minutes=rng.choice(GAPS_MIN))
    return last_end


def store_snapshot(conn: Any, end: datetime, seed: int) -> None:
    """Store a snapshot ``0..14`` hours after the last position: the writer's reference time."""
    snap(conn, end + timedelta(minutes=random.Random(seed * 7 + 1).randrange(0, 14 * 60)))


def run_writer(conn: Any, rounds: int = 10) -> int:
    """Run the writer until it writes nothing; returns the flights written."""
    total = 0
    for _ in range(rounds):
        n = conn.execute("SELECT write_finished_flights(500) AS n").fetchone()["n"]
        total += n
        if n == 0:
            break
    return total


def settle(conn: Any, end: datetime, seed: int) -> int:
    store_snapshot(conn, end, seed)
    return run_writer(conn)
