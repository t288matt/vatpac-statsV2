"""Dates, windows and fetching one day. Everything is UTC.

Both ends of an API window are inclusive, so a window runs 00:00:00 to 23:59:59 and the next one starts a second
later: a session that starts exactly on a boundary is then in one window only. day_window is the one function that
turns a date into its window; the backfill, the daily run and the retention delete all use it, so retention never cuts
a loaded day in half.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .api import Api, BadResponse, WindowTimeout, is_atis, parse_session

log = logging.getLogger(__name__)
FIRST_RUN_DAYS = 3  # on the very first run: the last 3 days as daily windows, then everything older


class DayFailed(Exception):
    """A day could not be fetched in full and checked; nothing is written for it. The text is the reason."""


@dataclass(slots=True)
class Day:
    rows: list[dict[str, Any]] = field(default_factory=list)
    timed_out: bool = False  # the day window timed out, so older days need hour windows
    empty: bool = False  # all 24 hour windows reported a count of 0


def day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1, seconds=-1)


def hour_windows(day: date) -> list[tuple[datetime, datetime]]:
    start = day_window(day)[0]
    return [(start + timedelta(hours=h), start + timedelta(hours=h, minutes=59, seconds=59)) for h in range(24)]


def oldest_allowed(now: datetime, years: int) -> date:
    """The UTC date `years` years before now (29 February becomes 28 February)."""
    today = now.astimezone(UTC).date()
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        return today.replace(year=today.year - years, day=28)


def plan(stored: set[date], today: date, oldest: date, overlap: int, empty: set[date]) -> tuple[list[date], list[date]]:
    """(recent days, missing days), each newest first.

    Recent: from `overlap` days before the newest stored date (the last FIRST_RUN_DAYS days when nothing is stored) up
    to yesterday. Missing: every date from the day before that back to `oldest` that has no sessions, is not confirmed
    empty, and is newer than the start of VATSIM's history (the newest empty date older than all stored dates)."""
    first = max(today - timedelta(days=FIRST_RUN_DAYS), oldest)
    if stored:
        first = max(max(stored) - timedelta(days=overlap), oldest)
    recent = [today - timedelta(days=n) for n in range(1, (today - first).days + 1)]
    floor = max((d for d in empty if stored and d < min(stored)), default=None)
    missing = []
    day = first - timedelta(days=1)
    while day >= oldest and (floor is None or day > floor):
        if day not in stored and day not in empty:
            missing.append(day)
        day -= timedelta(days=1)
    return recent, missing


def fetch_window(api: Api, start: datetime, end: datetime) -> tuple[int, list[dict[str, Any]]]:
    """Every session in one window: (count, rows without ATIS). Pages until the count is reached; an empty page ends it.

    The checks: every page reports the same count, and distinct ids plus skipped ATIS sessions equal that count.
    Raises DayFailed when they do not (a late-closing session, a shifted page order, or a short answer)."""
    rows: dict[int, dict[str, Any]] = {}
    fetched = skipped = 0
    count: int | None = None
    while count is None or fetched < count:
        page_count, items = api.page(start, end, fetched)
        if count is not None and page_count != count:
            raise DayFailed(f"the count changed from {count} to {page_count} while paging (a late-closing session)")
        count = page_count
        if not items:
            break
        fetched += len(items)  # offset moves by what was actually returned, never by the page size
        for item in items:
            row = parse_session(item)
            if is_atis(row):
                skipped += 1
            else:
                rows[row["connection_id"]] = row
    assert count is not None
    if len(rows) + skipped != count:
        raise DayFailed(f"{len(rows)} distinct sessions plus {skipped} ATIS, but the API reported {count}")
    return count, list(rows.values())


def _hourly(api: Api, day: date) -> Day:
    out, seen, total = Day(), set[int](), 0
    for start, end in hour_windows(day):
        count, rows = fetch_window(api, start, end)
        if seen & {r["connection_id"] for r in rows}:
            raise DayFailed("a session appeared in two one-hour windows")
        seen |= {r["connection_id"] for r in rows}
        out.rows += rows
        total += count
    out.empty = total == 0
    return out


def fetch_day(api: Api, day: date, hourly: bool) -> Day:
    """A whole day in memory, checked. One day window; 24 hour windows when `hourly`, when the day window timed out or
    failed its check, or when it reported 0 (an empty date is confirmed on all 24 hours). Raises DayFailed."""
    timed_out = False
    try:
        if not hourly:
            try:
                count, rows = fetch_window(api, *day_window(day))
                if count:
                    return Day(rows=rows)
            except WindowTimeout:
                timed_out = True
            except DayFailed as exc:
                log.info("%s: %s; fetching it again as one-hour windows", day, exc)
        out = _hourly(api, day)
    except WindowTimeout:
        raise DayFailed("an hour window timed out") from None
    except BadResponse as exc:
        raise DayFailed(str(exc)) from None
    out.timed_out = timed_out
    return out
