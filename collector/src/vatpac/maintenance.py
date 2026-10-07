"""Daily maintenance: settings sync, partitions, retention, view refresh, poll_log trimming."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import tuple_row

from .settings import Settings

log = logging.getLogger(__name__)

POLL_LOG_RETENTION_DAYS = 400
PARTITIONS_AHEAD = 7


def sync_settings(conn: psycopg.Connection[Any], settings: Settings) -> None:
    """Publish settings the SQL views depend on (flight view reads app_setting)."""
    for key, value in (
        ("home_icao_prefixes", settings.home_icao_prefixes),
        ("airborne_gap_cap_seconds", str(settings.airborne_gap_cap_seconds)),
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
    """Drop partitions older than retention that hold no recently active session. Returns the number dropped."""
    return _scalar(conn, "SELECT drop_old_pilot_position_partitions(%s, %s)", (retention_days, today))


def trim_poll_log(conn: psycopg.Connection[Any], days: int = POLL_LOG_RETENTION_DAYS) -> int:
    cur = conn.execute("DELETE FROM poll_log WHERE polled_at < now() - make_interval(days => %s)", (days,))
    return max(cur.rowcount, 0)


def refresh_materialized_views(conn: psycopg.Connection[Any]) -> int:
    """Refresh every materialized view in public (concurrently when it has a unique index)."""
    names = [r[0] for r in conn.execute("SELECT matviewname FROM pg_matviews WHERE schemaname = 'public'")]
    for name in names:
        ident = sql.Identifier("public", name)
        try:
            conn.execute(sql.SQL("REFRESH MATERIALIZED VIEW CONCURRENTLY {}").format(ident))
        except psycopg.errors.ObjectNotInPrerequisiteState:
            conn.execute(sql.SQL("REFRESH MATERIALIZED VIEW {}").format(ident))
    return len(names)


def run_maintenance(conn: psycopg.Connection[Any], settings: Settings) -> dict[str, int]:
    """Run every task; one failing task never blocks the others. Needs an autocommit connection."""
    today = datetime.now(UTC).date()

    def settings_task() -> int:
        sync_settings(conn, settings)
        return 0

    tasks: dict[str, Callable[[], int]] = {
        "settings": settings_task,
        "partitions_created": lambda: ensure_partitions(conn, today),
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
