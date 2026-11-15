"""Retention: keep STATS_RETENTION_YEARS years, delete older sessions. Called only at the end of a clean run."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from . import store
from .windows import day_window, oldest_allowed

BATCH = 10_000  # rows per DELETE, so one statement never holds a long lock


def apply(conn: psycopg.Connection[Any], now: datetime, years: int) -> int:
    """Delete every session that started before the oldest allowed UTC date (a whole date, so no day is half deleted).
    There is no guard: lowering the setting deletes the older sessions on the next clean run, and raising it again makes
    the missing-date search load them back."""
    return store.delete_before(conn, day_window(oldest_allowed(now, years))[0], BATCH)
