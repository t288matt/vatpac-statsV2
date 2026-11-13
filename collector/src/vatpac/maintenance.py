"""Maintenance: settings sync, partitions, retention, writing finished flights, view refresh, poll_log trimming.
run_maintenance runs every task daily (and a minute after startup); startup and the in-poll recovery run only
STARTUP_TASKS and PARTITION_TASKS; refresh_flights alone runs every FLIGHT_REFRESH_INTERVAL_S."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import psycopg
from psycopg.rows import tuple_row

from .settings import Settings

log = logging.getLogger(__name__)

POLL_LOG_RETENTION_DAYS = 90
PARTITIONS_AHEAD = 7
FLIGHT_REFRESH_INTERVAL_S = 300.0  # main.py: how often to call refresh_flights
FLIGHT_WRITE_BATCH = 50  # pilots per call: small, so the 20 s statement_timeout is never close
FLIGHT_SKIP_S = 6 * 3600.0  # a pilot whose own batch timed out is left out of the 5-minute job this long
MAX_FLIGHT_ATTEMPTS = 8  # per call: halvings plus demotions (each can cost one statement timeout)
DEMOTED: dict[int, float] = {}  # cid -> time.monotonic() until which the 5-minute job leaves the pilot out
MAX_DROP_CALLS = 600  # safety bound for the drop loop (one partition per call, so 600 partitions)
STARTUP_TASKS = ("settings", "partitions_created")  # what must be true before the first poll
PARTITION_TASKS = ("partitions_created",)  # what the in-poll CheckViolation recovery needs
DROP_PAUSE_S = 0.5  # between two drops: the parent table lock of the first is released and polls can write


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
    """Drop partitions older than retention: no recently active session, every position in a written flight.

    Returns the number dropped. The SQL function raises a NOTICE for a partition pinned beyond twice the
    retention, and "DROP REFUSED" when it keeps a partition because the flight writer has not covered it
    (issue 89 phase 4); both are logged here as warnings.
    """

    def on_notice(diag: Any) -> None:
        log.warning("partition maintenance notice", extra={"notice": diag.message_primary})

    conn.add_notice_handler(on_notice)
    try:
        total = 0
        for _ in range(MAX_DROP_CALLS):  # SQL drops at most ONE per call: a short transaction, a pause, the next day
            dropped = _scalar(conn, "SELECT drop_old_pilot_position_partitions(%s, %s)", (retention_days, today))
            total += dropped
            if dropped == 0:
                break
            time.sleep(DROP_PAUSE_S)
        return total
    finally:
        conn.remove_notice_handler(on_notice)


def _write_flights(conn: psycopg.Connection[Any], batch: int, skip: set[int], picked: list[list[int]]) -> int:
    """One call of the writer. ``skip`` goes in as the session setting for this one statement. The pilots each round
    picked come back in ``picked`` (from the writer's NOTICE), so a cancelled call still says who it was working on."""

    def on_notice(diag: Any) -> None:
        picked.append([int(c) for c in re.findall(r"\d+", (diag.message_primary or "").split("picked", 1)[-1])])

    conn.add_notice_handler(on_notice)
    try:
        return _scalar(
            conn,
            "WITH s AS MATERIALIZED (SELECT set_config('vatpac.flight_writer_skip', %s, true)) "
            "SELECT write_finished_flights(%s) FROM s",
            ("{" + ",".join(map(str, sorted(skip))) + "}", batch),
        )
    finally:
        conn.remove_notice_handler(on_notice)


def refresh_flights(conn: psycopg.Connection[Any], batch: int = FLIGHT_WRITE_BATCH, *, guarded: bool = True) -> int:
    """Write finished flights for up to ``batch`` pilots; returns how many. A timeout is logged, not raised.

    ``guarded`` (the 5-minute job): a timeout rolls the whole call back and the next call would pick the same oldest
    pilots again, so the batch is halved and retried, and a pilot that times out ALONE is left out of this job's calls
    for FLIGHT_SKIP_S. The daily maintenance (``guarded=False``, 15 minute timeout) still tries every pilot and
    clears the list once it succeeds."""
    start, now = batch, time.monotonic()
    skip = {c for c, until in list(DEMOTED.items()) if until > now} if guarded else set()
    for _ in range(MAX_FLIGHT_ATTEMPTS if guarded else 1):
        picked: list[list[int]] = []
        try:
            n = _write_flights(conn, batch, skip, picked)
            if not guarded:
                DEMOTED.clear()
            return n
        except psycopg.errors.QueryCanceled:
            conn.rollback()  # a no-op in autocommit; frees an open transaction otherwise
            last = picked[-1] if picked else []
            if batch == 1 and len(last) == 1 and last[0] not in skip:  # only a batch of one names the culprit
                DEMOTED[last[0]] = time.monotonic() + FLIGHT_SKIP_S
                skip.add(last[0])
                batch = start
                log.warning("write_finished_flights cancelled for one pilot; skipped for now", extra={"cid": last[0]})
            elif last and batch > 1:  # the last round named may be one that finished: narrow down, never blame
                batch = max(1, min(batch, len(last)) // 2)
                log.warning("write_finished_flights cancelled (timeout?); fewer pilots", extra={"batch": batch})
            else:
                break
    log.warning("write_finished_flights cancelled (statement timeout?); the next run continues")
    return 0


def trim_poll_log(conn: psycopg.Connection[Any], days: int = POLL_LOG_RETENTION_DAYS) -> int:
    cur = conn.execute("DELETE FROM poll_log WHERE polled_at < now() - make_interval(days => %s)", (days,))
    return max(cur.rowcount, 0)


def run_maintenance(conn: psycopg.Connection[Any], settings: Settings, only: tuple[str, ...] = ()) -> dict[str, int]:
    """Run every task (or just those in ``only``); a failing task never blocks the others. Needs autocommit."""
    today = datetime.now(UTC).date()

    def settings_task() -> int:
        sync_settings(conn, settings)
        return 0

    tasks: dict[str, Callable[[], int]] = {
        "settings": settings_task,
        "partitions_created": lambda: ensure_partitions(conn, today),
        "flights_refreshed": lambda: refresh_flights(conn, guarded=False),
        "partitions_dropped": lambda: drop_old_partitions(conn, today, settings.position_retention_days),
        "poll_log_trimmed": lambda: trim_poll_log(conn),
    }
    out: dict[str, int] = {}
    failed = 0
    for name, task in tasks.items():
        if not only or name in only:
            try:
                out[name] = task()
            except psycopg.Error:
                failed += 1
                log.exception("maintenance task failed", extra={"task": name})
    out["failed"] = failed
    log.info("maintenance finished", extra={**out, "only": bool(only)})
    return out
