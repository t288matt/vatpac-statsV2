"""Helpers for the tests of the raw-position drop gate (issue 89 phase 4, issue 140).

The gate drops a daily partition of pilot_position only when the flight writer has processed its sessions and a written
flight covers every position. Tests that expect a drop therefore have to run the writer first, as production does."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def process_flights(conn: Any, at: datetime | None = None) -> int:
    """Store a snapshot at ``at`` (default: now) so every pilot quiet for 8 hours before it counts as finished, then run
    the flight writer until it has nothing left to write. Returns the number of flights written.

    The writer measures quiet time against the newest stored snapshot, never the clock, and caps that snapshot at now + 5
    minutes, so synthetic sessions in the past are written and a live session (last seen now) is not."""
    t = at or datetime.now(timezone.utc)
    conn.execute("INSERT INTO network_snapshot VALUES (%s, 1, 1, 1, 1) ON CONFLICT DO NOTHING", (t,))
    total = 0
    for _ in range(50):
        n = conn.execute("SELECT write_finished_flights(500) AS n").fetchone()["n"]
        total += n
        if n == 0:
            break
    return total


def unreconciled(conn: Any, p_start: datetime, p_end: datetime) -> list[tuple[int, str]]:
    """What stops a day from being dropped, as (session or flight id, reason) rows."""
    rows = conn.execute("SELECT session_id, reason FROM unreconciled_partition_sessions(%s, %s) ORDER BY 1, 2",
                        (p_start, p_end)).fetchall()
    return [(r["session_id"], r["reason"]) for r in rows]


def drain(conn: Any, retention: int = 90, today: Any = None) -> int:
    """Call the drop function until it drops nothing, as the collector's loop does (one day per call). Returns the total dropped."""
    from datetime import date
    day = today or date(2026, 10, 7)
    total = 0
    for _ in range(700):
        n = conn.execute("SELECT drop_old_pilot_position_partitions(%s, %s) AS n", (retention, day)).fetchone()["n"]
        total += n
        if n == 0:
            break
    return total
