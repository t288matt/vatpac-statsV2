"""The VATSIM history API, behind one limiter.

One request at a time, and after every response (whatever its status) the next request waits at least the minimum
interval. HTTP 429, or three server errors in a row, stop the run. A timeout (HTTP 504 or no answer in 30 s) is not a
server error: it means the window is too long for that age, so it raises WindowTimeout and the caller shrinks
the window.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_URL = "https://api.vatsim.net/v2/atc/history"
TIMEOUT_SECONDS = 30.0
SERVER_ERRORS_IN_A_ROW = 3
COUNTS = ("aircrafttracked", "aircraftseen", "flightsamended", "handoffsinitiated", "handoffsreceived",
          "handoffsrefused", "squawksassigned", "cruisealtsmodified", "tempaltsmodified", "scratchpadmods")


class Stopped(Exception):
    """The container was told to stop (clean stop signal)."""


class RateLimited(Exception):
    """HTTP 429: the run stops, the next scheduled run carries on."""


class ServerErrors(Exception):
    """Three server errors in a row: the run stops."""


class WindowTimeout(Exception):
    """HTTP 504 or no answer in time: the window is too long for that age."""


class BadResponse(Exception):
    """The API answered with something this loader cannot read."""


class Clock:
    """The one source of time (UTC) and of waiting. Tests replace it, so no test waits."""

    def __init__(self, stop: threading.Event | None = None) -> None:
        self.stop = stop or threading.Event()

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        if self.stop.wait(max(seconds, 0.0)):
            raise Stopped


class Api:
    def __init__(self, client: httpx.Client, clock: Clock, interval: float, page_size: int) -> None:
        self.client, self.clock, self.interval, self.page_size = client, clock, interval, page_size
        self.requests = 0
        self._next_ok = 0.0  # monotonic time before which no request may start
        self._server_errors = 0

    def hold(self, seconds: float) -> None:
        """No request for this long from now (the 60 s wait after any start)."""
        self._next_ok = max(self._next_ok, self.clock.monotonic() + seconds)

    def page(self, start: datetime, end: datetime, offset: int) -> tuple[int, list[dict[str, Any]]]:
        """One page of sessions that started in [start, end] (both ends inclusive): (count, items)."""
        fmt = "%Y-%m-%dT%H:%M:%S"
        params: dict[str, str | int] = {"start_date": start.strftime(fmt), "end_date": end.strftime(fmt),
                                        "limit": self.page_size, "offset": offset}
        while True:
            if self.clock.stop.is_set():
                raise Stopped
            self.clock.sleep(self._next_ok - self.clock.monotonic())
            self.requests += 1
            try:
                resp = self.client.get(API_URL, params=params, headers={"Accept-Encoding": "gzip"})
            except httpx.TimeoutException:
                raise WindowTimeout("no answer within the time limit") from None
            except httpx.TransportError as exc:
                status = None
                log.warning("request failed: %s", exc.__class__.__name__)
            else:
                status = resp.status_code
            finally:
                self._next_ok = self.clock.monotonic() + self.interval
            if status == 429:
                raise RateLimited("HTTP 429 from the API")
            if status == 504:
                raise WindowTimeout("HTTP 504 from the API")
            if status == 200:
                self._server_errors = 0
                return _read(resp)
            if status is not None and status < 500:
                raise BadResponse(f"HTTP {status} from the API")
            self._server_errors += 1
            if self._server_errors >= SERVER_ERRORS_IN_A_ROW:
                raise ServerErrors(f"{SERVER_ERRORS_IN_A_ROW} server errors in a row (last: {status or 'no answer'})")


def _read(resp: httpx.Response) -> tuple[int, list[dict[str, Any]]]:
    try:
        body = resp.json()
        count, items = body["count"], body["items"]
        if not (isinstance(count, int) and isinstance(items, list)):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise BadResponse("the response has no count and items") from None
    return count, items


def _utc(text: str) -> datetime:
    when = datetime.fromisoformat(text)
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def has_no_member_id(item: dict[str, Any]) -> bool:
    """True when the item's member ID is missing, null or blank. Such a session cannot be tied to a controller and the
    cid column cannot hold it, so the loader counts and skips it. The server and the callsign play no part."""
    conn = item.get("connection_id") if isinstance(item, dict) else None
    if not isinstance(conn, dict):
        return False  # not the known shape: parse_session rejects it
    value = conn.get("vatsim_id")
    return value is None or (isinstance(value, str) and not value.strip())


def bare_connection_id(item: dict[str, Any]) -> int:
    """The connection id of an item that is skipped before parse_session; BadResponse when it has none."""
    try:
        return int(item["connection_id"]["id"])
    except (KeyError, TypeError, ValueError):
        raise BadResponse("a session in the response could not be read") from None


def parse_session(item: dict[str, Any]) -> dict[str, Any]:
    """One API item as a row for atc_stats_session. Anything unreadable raises BadResponse (the day is not written)."""
    try:
        conn = item["connection_id"]
        end = conn.get("end")
        row: dict[str, Any] = {
            "connection_id": int(conn["id"]), "cid": int(conn["vatsim_id"]), "callsign": str(conn["callsign"]),
            "rating": conn.get("rating"), "server": conn.get("server"),
            "session_start": _utc(conn["start"]), "session_end": _utc(end) if end else None,
        }
        row.update({name: int(item.get(name) or 0) for name in COUNTS})
    except (KeyError, TypeError, ValueError, AttributeError):
        raise BadResponse("a session in the response could not be read") from None
    return row


def is_atis(row: dict[str, Any]) -> bool:
    return str(row["callsign"]).upper().endswith("_ATIS")
