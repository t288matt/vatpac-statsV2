"""Maintenance: settings sync, partitions, retention, flight summary and view refresh, poll_log trimming.

run_maintenance runs daily (and at startup); refresh_flights alone runs every FLIGHT_REFRESH_INTERVAL_S."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import tuple_row

from .settings import Settings

log = logging.getLogger(__name__)

POLL_LOG_RETENTION_DAYS = 400
PARTITIONS_AHEAD = 7
FLIGHT_REFRESH_INTERVAL_S = 300.0  # main.py: how often to call refresh_flights
FLIGHT_REFRESH_WINDOW = timedelta(hours=12)


def sync_settings(conn: psycopg.Connection[Any], settings: Settings) -> None:
    """Publish settings the SQL views depend on (flight view reads app_setting)."""
    for key, value in (
        ("home_icao_prefixes", settings.home_icao_prefixes),
        ("airborne_gap_cap_seconds", str(settings.airborne_gap_cap_seconds)),
        ("poll_interval_seconds", f"{settings.poll_interval_seconds:g}"),
    ):
        conn.execute(
            "INSERT INTO app_setting (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )


def _scalar(conn: psycopg.Connection[Any], query: str, params: tuple[Any, ...]) -> int:
    with conn.cursor(row_factory=tuple_row) as cur:  # independent of the connection's row factory
        row = cur.execute(query, params).fetchone()
    return int(row[0]) if row else 0


def ensure_partitions(
    conn: psycopg.Connection[Any], today: date | None = None, days_ahead: int = PARTITIONS_AHEAD
) -> int:
    """Create daily partitions for yesterday..today+days_ahead (UTC). Returns the number created."""
    return _scalar(conn, "SELECT ensure_pilot_position_partitions(%s, %s)", (days_ahead, today))


def drop_old_partitions(conn: psycopg.Connection[Any], today: date, retention_days: int) -> int:
    """Finalise flights, then drop partitions older than retention that hold no recently active session.

    Returns the number dropped. The SQL function raises a NOTICE for a partition pinned beyond twice the
    retention; it is logged here as a warning.
    """

    def on_notice(diag: Any) -> None:
        log.warning("partition maintenance notice", extra={"notice": diag.message_primary})

    conn.add_notice_handler(on_notice)
    try:
        return _scalar(conn, "SELECT drop_old_pilot_position_partitions(%s, %s)", (retention_days, today))
    finally:
        conn.remove_notice_handler(on_notice)


def refresh_flights(conn: psycopg.Connection[Any], window: timedelta = FLIGHT_REFRESH_WINDOW) -> int:
    """Recompute flight_summary for sessions seen within ``window``. Idempotent; returns rows changed."""
    return _scalar(conn, "SELECT refresh_flight_summary(%s)", (window,))


def trim_poll_log(conn: psycopg.Connection[Any], days: int = POLL_LOG_RETENTION_DAYS) -> int:
    cur = conn.execute("DELETE FROM poll_log WHERE polled_at < now() - make_interval(days => %s)", (days,))
    return max(cur.rowcount, 0)


def refresh_materialized_views(conn: psycopg.Connection[Any]) -> int:
    """Refresh every materialized view in public (SECURITY DEFINER SQL function: no table rights needed)."""
    return _scalar(conn, "SELECT refresh_materialized_views()", ())


def run_maintenance(conn: psycopg.Connection[Any], settings: Settings) -> dict[str, int]:
    """Run every task; one failing task never blocks the others. Needs an autocommit connection."""
    today = datetime.now(UTC).date()

    def settings_task() -> int:
        sync_settings(conn, settings)
        return 0

    tasks: dict[str, Callable[[], int]] = {
        "settings": settings_task,
        "partitions_created": lambda: ensure_partitions(conn, today),
        "flights_refreshed": lambda: refresh_flights(conn),
        "partitions_dropped": lambda: drop_old_partitions(conn, today, settings.position_retention_days),
        "poll_log_trimmed": lambda: trim_poll_log(conn),
        "views_refreshed": lambda: refresh_materialized_views(conn),
    }
    out: dict[str, int] = {}
    failed = 0
    for name, task in tasks.items():
        try:
            out[name] = task()
        except psycopg.Error:
            failed += 1
            log.exception("maintenance task failed", extra={"task": name})
    out["failed"] = failed
    log.info("maintenance finished", extra=out)
    return out
