"""Everything that reads or writes Postgres. One autocommit connection as the stats_loader role (time zone UTC)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import psycopg

from .api import COUNTS

_COLS = ("cid", "callsign", "rating", "session_start", "session_end", "server", *COUNTS)
_UPSERT = f"""
INSERT INTO atc_stats_session AS t (connection_id, {", ".join(_COLS)})
VALUES (%(connection_id)s, {", ".join(f"%({c})s" for c in _COLS)})
ON CONFLICT (connection_id) DO UPDATE SET {", ".join(f"{c} = EXCLUDED.{c}" for c in _COLS)}, updated_at = now()
WHERE ({", ".join(f"t.{c}" for c in _COLS)}) IS DISTINCT FROM ({", ".join(f"EXCLUDED.{c}" for c in _COLS)})
RETURNING (xmax = 0) AS inserted
"""
_RUN_COLUMNS = {"requests", "sessions_inserted", "sessions_updated", "sessions_deleted", "days_loaded", "failed_days",
                "hour_windows_from", "empty_dates", "last_day_at"}


def connect(dsn: str) -> psycopg.Connection[tuple[Any, ...]]:
    return psycopg.connect(dsn, autocommit=True)


def stored_dates(conn: psycopg.Connection[Any]) -> set[date]:
    """UTC dates that have sessions. AT TIME ZONE 'UTC' keeps the server's own time zone out of it."""
    sql = "SELECT DISTINCT (session_start AT TIME ZONE 'UTC')::date FROM atc_stats_session"
    return {r[0] for r in conn.execute(sql).fetchall()}


def write_day(conn: psycopg.Connection[Any], rows: list[dict[str, Any]]) -> tuple[int, int]:
    """Upsert a whole day in ONE transaction: it is all in the table or none of it. Returns (inserted, updated)."""
    inserted = updated = 0
    with conn.transaction(), conn.cursor() as cur:
        for row in rows:
            changed = cur.execute(_UPSERT, row).fetchone()  # no row back: it was there and nothing differed
            if changed:
                inserted, updated = inserted + bool(changed[0]), updated + (not changed[0])
    return inserted, updated


def delete_before(conn: psycopg.Connection[Any], cutoff: datetime, batch: int) -> int:
    """Delete sessions that started before `cutoff`, in batches. Returns the number deleted."""
    total = 0
    while True:
        done = conn.execute(
            "DELETE FROM atc_stats_session WHERE connection_id IN "
            "(SELECT connection_id FROM atc_stats_session WHERE session_start < %s LIMIT %s)", (cutoff, batch)).rowcount
        total += done
        if done < batch:
            return total


def start_run(conn: psycopg.Connection[Any], mode: str, now: datetime) -> int:
    """Times come from the loader's clock (UTC), not the database's, so one clock decides every comparison."""
    row = conn.execute("INSERT INTO atc_stats_run_log (mode, started_at) VALUES (%s, %s) RETURNING id",
                       (mode, now)).fetchone()
    assert row
    return int(row[0])


def update_run(conn: psycopg.Connection[Any], run_id: int, **columns: Any) -> None:
    assert columns.keys() <= _RUN_COLUMNS
    sets = ", ".join(f"{c} = %({c})s" for c in columns)
    conn.execute(f"UPDATE atc_stats_run_log SET {sets} WHERE id = %(id)s", {**columns, "id": run_id})


def finish_run(conn: psycopg.Connection[Any], run_id: int, status: str, error: str | None, now: datetime) -> None:
    conn.execute("UPDATE atc_stats_run_log SET status = %s, error = %s, finished_at = %s WHERE id = %s",
                 (status, error, now, run_id))


def mark_interrupted(conn: psycopg.Connection[Any], now: datetime) -> int:
    """A row still `running` when the loader starts belongs to a run that crashed."""
    return conn.execute(
        "UPDATE atc_stats_run_log SET status = 'interrupted', finished_at = %s, "
        "error = 'the loader stopped without finishing this run (a crash or a kill)' WHERE status = 'running'",
        (now,)).rowcount


def run_memory(conn: psycopg.Connection[Any]) -> tuple[set[date], date | None]:
    """From earlier runs: dates confirmed empty on all 24 hours, and the newest date that needed hour windows."""
    empty = {r[0] for r in conn.execute("SELECT DISTINCT unnest(empty_dates) FROM atc_stats_run_log").fetchall()}
    row = conn.execute("SELECT max(hour_windows_from) FROM atc_stats_run_log").fetchone()
    return empty, row[0] if row else None


def newest_status(conn: psycopg.Connection[Any]) -> str | None:
    row = conn.execute("SELECT status FROM atc_stats_run_log ORDER BY id DESC LIMIT 1").fetchone()
    return str(row[0]) if row else None


def run_done_on(conn: psycopg.Connection[Any], day: date) -> bool:
    """Has a daily run finished (or any run failed) on this UTC date? A stopped or interrupted run does not count."""
    row = conn.execute(
        "SELECT 1 FROM atc_stats_run_log WHERE (finished_at AT TIME ZONE 'UTC')::date = %s "
        "AND (status = 'failed' OR (status = 'success' AND mode = 'daily')) LIMIT 1", (day,)).fetchone()
    return row is not None


def healthy(conn: psycopg.Connection[Any], now: datetime, hours: int) -> bool:
    """A run finished successfully in the last `hours` hours, or a run in progress has written a day in that time."""
    since = now - timedelta(hours=hours)
    row = conn.execute(
        "SELECT 1 FROM atc_stats_run_log WHERE (status = 'success' AND finished_at > %(s)s) "
        "OR (status = 'running' AND last_day_at > %(s)s) LIMIT 1", {"s": since}).fetchone()
    return row is not None
