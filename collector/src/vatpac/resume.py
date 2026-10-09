"""Resume point after a restart: the newest stored snapshot time."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("vatpac")


def db_last_ts(pool: ConnectionPool, max_future_s: int) -> datetime | None:
    sql = "SELECT max(observed_at) FROM network_snapshot WHERE observed_at <= now() + make_interval(secs => %s)"
    with pool.connection() as conn, conn.cursor(row_factory=tuple_row) as cur:  # ignores a future-dated row
        row = cur.execute(sql, (max_future_s,)).fetchone()
    return row[0] if row else None


async def load_last_ts(pool: ConnectionPool, max_future_s: int, current: datetime | None) -> datetime | None:
    """Resume from the newest stored snapshot, so a replay after a restart is logged as skipped, not ok.

    On a read failure the current value is kept (and the error logged)."""
    try:
        return await asyncio.to_thread(db_last_ts, pool, max_future_s)
    except Exception:
        log.exception("could not read the newest stored snapshot time")
        return current
