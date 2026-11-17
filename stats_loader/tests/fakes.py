"""A fake clock and a fake VATSIM history API for the loader's tests. Nothing here touches the network or waits."""

from __future__ import annotations

import gzip
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any

import httpx

from vatsim_stats.api import Clock, Stopped


def at(y: int, m: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, m, d, h, mi, s, tzinfo=UTC)


class FakeClock(Clock):
    """Time moves only when something sleeps or the fake API answers. `stop_at` sets the stop signal at a wall time."""

    def __init__(self, wall: datetime, stop_at: datetime | None = None) -> None:
        super().__init__(threading.Event())
        self.t, self.wall, self.stop_at = 0.0, wall, stop_at
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def now(self) -> datetime:
        return self.wall + timedelta(seconds=self.t)

    def advance(self, seconds: float) -> None:
        self.t += seconds
        if self.stop_at and self.now() >= self.stop_at:
            self.stop.set()

    def sleep(self, seconds: float) -> None:
        if self.stop.is_set():
            raise Stopped
        if seconds > 0:
            self.slept.append(seconds)
            self.advance(seconds)  # a stop signal that arrives during the wait ends it, as the real clock does
            if self.stop.is_set():
                raise Stopped


@dataclass
class Call:
    started: float
    ended: float
    start: datetime
    end: datetime
    offset: int
    limit: int
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


class FakeApi:
    """Both ends of a window are inclusive; newest first; `count` is the number in the window."""

    def __init__(self, clock: FakeClock, latency: float = 0.3) -> None:
        self.clock, self.latency = clock, latency
        self.items: list[dict[str, Any]] = []
        self.calls: list[Call] = []
        self.max_page: int | None = None  # the server returns fewer than asked for
        self.slow_before: datetime | None = None  # windows longer than an hour that start before this time time out
        self.script: list[Any] = []  # answers used first, in order: an HTTP status, or "timeout"
        self.flip_ties_on: set[int] = set()  # call numbers (1-based) that list equal start times in the other order
        self.on_call: Callable[[int], None] | None = None  # runs after each answer; may change the data
        self._next_id = 1000

    def add(self, cid: int, callsign: str, start: datetime, end: datetime | None = None, **counts: int) -> int:
        self._next_id += 1
        conn = {"id": self._next_id, "vatsim_id": str(cid), "type": 2, "rating": 3, "callsign": callsign,
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "server": "TEST",
                "end": (end or start + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        self.items.append({"connection_id": conn, "aircrafttracked": 0, **counts})
        return self._next_id

    def add_blank(self, start: datetime, member: Any = "", server: Any = "", missing: bool = False) -> int:
        """A session with a blank member ID (`member`, or no key at all when `missing`), shaped like the real ones."""
        self._next_id += 1
        conn = {"id": self._next_id, "vatsim_id": member, "type": 2, "rating": 0, "callsign": "", "start":
                start.strftime("%Y-%m-%dT%H:%M:%SZ"), "server": server, "end": None}
        if missing:
            del conn["vatsim_id"]
        self.items.append({"connection_id": conn, "aircrafttracked": 0})
        return self._next_id

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params
        fmt = "%Y-%m-%dT%H:%M:%S"
        start = datetime.strptime(q["start_date"], fmt).replace(tzinfo=UTC)
        end = datetime.strptime(q["end_date"], fmt).replace(tzinfo=UTC)
        limit, offset = int(q["limit"]), int(q["offset"])
        call = Call(self.clock.t, self.clock.t + self.latency, start, end, offset, limit,
                    {k.lower(): v for k, v in request.headers.items()})
        self.calls.append(call)
        n = len(self.calls)
        self.clock.advance(self.latency)
        try:
            if self.script:
                act = self.script.pop(0)
                if act == "timeout":
                    raise httpx.ReadTimeout("fake timeout")
                return httpx.Response(act)
            if self.slow_before and call.hours > 1 and start < self.slow_before:
                raise httpx.ReadTimeout("fake timeout: the window is too long for that age")
            inside = [i for i in self.items if start <= _start(i) <= end]
            inside.sort(key=lambda i: i["connection_id"]["id"], reverse=n in self.flip_ties_on)
            inside.sort(key=_start, reverse=True)
            page = inside[offset: offset + min(limit, self.max_page or limit)]
            body = gzip.compress(json.dumps({"items": page, "count": len(inside)}).encode())
            return httpx.Response(200, content=body, headers={"Content-Encoding": "gzip"})
        finally:
            if self.on_call:
                self.on_call(n)


def _start(item: dict[str, Any]) -> datetime:
    return _parse(item["connection_id"]["start"])


@cache
def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text)
