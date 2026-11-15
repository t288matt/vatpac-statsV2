"""Independent fake VATSIM history API and fake clock, written from the approved design (version 4), not from the builders' code.

The fake behaves as measured on 15-11-2026: both window ends inclusive, whole-second starts, newest first, `count` for the
whole window, dates in the form 2026-11-10T00:00:00, windows longer than an hour older than a cut-off time out. It never
touches the network: it is an httpx.MockTransport handler.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx
from vatsim_stats.api import Api, Clock, Stopped

UTC_NOW = datetime(2026, 11, 15, 6, 0, 0, tzinfo=UTC)
DATE_FORM = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")


class FakeClock(Clock):
    """Virtual time: sleep advances it, nothing really waits. `now()` is UTC."""

    def __init__(self, start: datetime = UTC_NOW, stop: threading.Event | None = None) -> None:
        super().__init__(stop)
        self.start, self.t = start, 0.0

    def monotonic(self) -> float:
        return self.t

    def now(self) -> datetime:
        return self.start + timedelta(seconds=self.t)

    def sleep(self, seconds: float) -> None:
        if self.stop.is_set():
            raise Stopped
        self.t += max(seconds, 0.0)
        if self.stop.is_set():
            raise Stopped


@dataclass
class Sess:
    id: int
    cid: int
    callsign: str
    start: datetime
    end: datetime | None = None
    rating: int = 3
    server: str = "SYD"


@dataclass
class Req:
    t: float  # virtual monotonic seconds when the request arrived
    start: datetime
    end: datetime
    offset: int
    limit: int
    status: int


def at(day: date, hh: int = 12, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=UTC)


def make(i: int, start: datetime, callsign: str | None = None, dur: int | None = 1800) -> Sess:
    return Sess(id=i, cid=1_000_000 + i % 50, callsign=callsign or f"ML_{['TWR', 'APP', 'CTR', 'GND'][i % 4]}", start=start,
                end=None if dur is None else start + timedelta(seconds=dur))


def item(s: Sess) -> dict[str, object]:
    fmt = "%Y-%m-%dT%H:%M:%S"
    return {
        "connection_id": {"id": s.id, "vatsim_id": s.cid, "type": 3, "rating": s.rating, "callsign": s.callsign,
                          "start": s.start.strftime(fmt), "end": s.end.strftime(fmt) if s.end else None, "server": s.server},
        "aircrafttracked": s.id % 7, "aircraftseen": s.id % 11, "flightsamended": 1, "handoffsinitiated": 2,
        "handoffsreceived": 3, "handoffsrefused": 0, "squawksassigned": 4, "cruisealtsmodified": 5,
        "tempaltsmodified": 6, "scratchpadmods": s.id % 5,
    }


class FakeApi:
    def __init__(self, clock: FakeClock, sessions: list[Sess] | None = None) -> None:
        self.clock = clock
        self.sessions: list[Sess] = list(sessions or [])
        self.log: list[Req] = []
        self.cap: int | None = None  # the most items one page returns, whatever `limit` says (a short page)
        self.timeout_before: date | None = None  # windows longer than 1 h that start before this date time out
        self.timeout_mode = "504"  # or "none": raise a read timeout (no answer within 30 s)
        self.script: list[int] = []  # statuses to answer the next requests with, before any real answer
        self.shift_over_hour = False  # tie order flips between requests for windows longer than 1 h
        self.shift_always = False  # ... for every window
        self.extra_count: dict[date, int] = {}  # days whose count is wrong by this much in every window
        self.drift: Callable[[int, int], int] | None = None  # (request number, count) -> count reported
        self.on_request: Callable[[Req], None] | None = None
        self.stop_at_request: tuple[int, threading.Event] | None = None
        self.client = httpx.Client(transport=httpx.MockTransport(self._handle), timeout=30.0)

    def api(self, interval: float = 5.0, page_size: int = 5000) -> Api:
        return Api(self.client, self.clock, interval, page_size)

    @property
    def n(self) -> int:
        return len(self.log)

    def _answer(self, status: int, req: httpx.Request, start: datetime, end: datetime, offset: int, limit: int) -> httpx.Response:
        r = Req(self.clock.monotonic(), start, end, offset, limit, status)
        self.log.append(r)
        if self.on_request:
            self.on_request(r)
        if self.stop_at_request and self.n == self.stop_at_request[0]:
            self.stop_at_request[1].set()
        return httpx.Response(status, request=req)

    def _handle(self, req: httpx.Request) -> httpx.Response:
        q = dict(req.url.params)
        assert req.url.path == "/v2/atc/history", req.url.path
        assert DATE_FORM.match(q["start_date"]) and DATE_FORM.match(q["end_date"]), q
        assert req.headers.get("accept-encoding") == "gzip"
        start = datetime.fromisoformat(q["start_date"]).replace(tzinfo=UTC)
        end = datetime.fromisoformat(q["end_date"]).replace(tzinfo=UTC)
        offset, limit = int(q["offset"]), int(q["limit"])
        long_window = (end - start) > timedelta(hours=1)
        if self.script:
            status = self.script.pop(0)
            return self._answer(status, req, start, end, offset, limit)
        if long_window and self.timeout_before and start.date() < self.timeout_before:
            self._answer(504 if self.timeout_mode == "504" else 0, req, start, end, offset, limit)
            if self.timeout_mode == "504":
                return httpx.Response(504, request=req)
            raise httpx.ReadTimeout("no answer in 30 s", request=req)
        number = self.n + 1
        hit = sorted((s for s in self.sessions if start <= s.start <= end), key=lambda s: (-s.start.timestamp(), s.id))
        if self.shift_always or (self.shift_over_hour and long_window):
            hit = sorted(hit, key=lambda s: (-s.start.timestamp(), s.id if number % 2 else -s.id))
        count = len(hit) + self.extra_count.get(start.date(), 0)
        if self.drift:
            count = self.drift(number, count)
        page = hit[offset: offset + min(limit, self.cap or limit)]
        r = Req(self.clock.monotonic(), start, end, offset, limit, 200)
        self.log.append(r)
        if self.on_request:
            self.on_request(r)
        if self.stop_at_request and self.n == self.stop_at_request[0]:
            self.stop_at_request[1].set()
        return httpx.Response(200, json={"count": count, "items": [item(s) for s in page]}, request=req)

    def requests_between(self, since: int = 0) -> list[Req]:
        return self.log[since:]


def gaps(log: list[Req]) -> list[float]:
    return [b.t - a.t for a, b in zip(log, log[1:], strict=False)]
