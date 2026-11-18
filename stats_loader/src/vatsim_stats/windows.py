"""Dates, windows and fetching one day. Everything is UTC. Daily only: one request per day, always; there is no
hourly fallback at any point.

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

from .api import Api, BadResponse, WindowTimeout, bare_connection_id, has_no_member_id, is_atis, parse_session

log = logging.getLogger(__name__)
FIRST_RUN_DAYS = 3  # on the very first run: the last 3 days as daily windows, then everything older


class DayFailed(Exception):
    """A day could not be fetched in full and checked; nothing is written for it. The text is the reason."""


@dataclass(slots=True)
class Day:
    rows: list[dict[str, Any]] = field(default_factory=list)
    empty: bool = False  # the day's single request reported a count of 0


def day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1, seconds=-1)


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

    Sessions with no member ID cannot be stored or tied to a controller: they are skipped and counted, and logged once
    per window. The checks: every page reports the same count, and distinct ids plus skipped ATIS and no-member sessions
    equal that count.
    Raises DayFailed when they do not (a late-closing session, a shifted page order, or a short answer)."""
    rows: dict[int, dict[str, Any]] = {}
    fetched = skipped = no_member = requests = biggest = 0
    seen: set[int] = set()
    count: int | None = None
    while count is None or fetched < count:
        # A count that cannot be reached must not loop for ever: allow the pages the count needs at the page size
        # actually received, plus two, and stop when a page brings no connection id not seen before.
        if count is not None and requests > -(-count // biggest) + 2:
            raise BadResponse(f"the API reported {count} sessions but its pages do not reach that")
        page_count, items = api.page(start, end, fetched)
        requests += 1
        if count is not None and page_count != count:
            raise DayFailed(f"the count changed from {count} to {page_count} while paging (a late-closing session)")
        count = page_count
        if not items:
            break
        fetched += len(items)  # offset moves by what was actually returned, never by the page size
        biggest = max(biggest, len(items))
        new = False
        for item in items:
            if has_no_member_id(item):
                new = bare_connection_id(item) not in seen or new
                seen.add(bare_connection_id(item))
                no_member += 1
                continue
            row = parse_session(item)
            new = new or row["connection_id"] not in seen
            seen.add(row["connection_id"])
            if is_atis(row):
                skipped += 1
            else:
                rows[row["connection_id"]] = row
        if not new:
            raise BadResponse("a page brought no session not already seen")
    assert count is not None
    if len(rows) + skipped + no_member != count:
        raise DayFailed(f"{len(rows)} distinct sessions plus {skipped} ATIS plus {no_member} with no member ID, "
                        f"but the API reported {count}")
    if no_member:
        log.info("%s: skipped %d sessions with no member ID", start.strftime("%d-%m-%Y %H:%M"), no_member)
    return count, list(rows.values())


def fetch_day(api: Api, day: date) -> Day:
    """A whole day in memory, checked. One request for the whole day, always: a timeout or a failed count/paging check
    simply fails the day (raises DayFailed), with no retry and no hourly fallback. A count of 0 from that single
    request is trusted immediately as a genuinely empty day."""
    try:
        count, rows = fetch_window(api, *day_window(day))
    except WindowTimeout:
        raise DayFailed("the day window timed out") from None
    except BadResponse as exc:
        raise DayFailed(str(exc)) from None
    return Day(rows=rows, empty=count == 0)
