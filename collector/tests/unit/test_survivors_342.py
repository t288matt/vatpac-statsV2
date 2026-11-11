"""Issue 342: tests for mutation survivors found in the CI test review of 11-11-2026 (ids A06, B04, B06, B09 and the
48-hour poll-timing run). Each one fails against its mutant (listed in the test) and passes on the real code."""
from __future__ import annotations

import asyncio
import random
from typing import Any
from unittest.mock import MagicMock

import httpx
import psycopg
import pytest

from vatpac import main as vmain
from vatpac import store
from vatpac.feed import RETRY_AFTER_CAP_S, FeedClient, RateLimitedError, parse_retry_after
from vatpac.main import Collector, PollOutcome
from vatpac.settings import Settings

GAP = 30.0
EPS = 1e-6


def test_retry_after_is_capped_at_120_seconds() -> None:
    """B09 (RETRY_AFTER_CAP_S = 1200): a hostile or buggy Retry-After must not park the collector for 20 minutes."""
    assert RETRY_AFTER_CAP_S == 120.0
    assert parse_retry_after("1200") == 120.0
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after("121") == 120.0
    assert parse_retry_after("0") == 1.0 and parse_retry_after("45") == 45.0 and parse_retry_after(None) == 30.0


class _LimitedFeed:
    last_wait_ms, last_request_ms, last_request_at = None, None, None

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after

    async def fetch(self) -> Any:
        raise RateLimitedError(self.retry_after)


def test_a_429_hands_the_feeds_retry_after_to_the_loop() -> None:
    """B06 (retry_in = None on a 429): without the value the loop would retry at the normal cadence, inside the ban."""
    col = Collector(Settings(_env_file=None), MagicMock(), MagicMock(), _LimitedFeed(77.0))  # type: ignore[call-arg]
    col._log_poll = lambda *a, **k: None  # type: ignore[method-assign]
    outcome = asyncio.run(col.poll_once())
    assert outcome.status == "error" and outcome.retry_in == 77.0


def _drive(monkeypatch: pytest.MonkeyPatch, polls: int, work_s: float = 0.0, jitter_s: float = 0.002,
           seed: int = 7) -> tuple[list[float], list[float]]:
    """The real poll_loop and FeedClient on a virtual clock. Every sleep wakes late by a random amount up to
    ``jitter_s``; each poll then spends ``work_s`` of virtual time after its request (database work).
    Returns (send times, floor waits)."""
    clock, rng = [1000.0], random.Random(seed)
    starts: list[float] = []
    sends: list[float] = []

    async def late(seconds: float) -> None:
        clock[0] += max(seconds, 0) + rng.uniform(0, jitter_s)

    monkeypatch.setattr(vmain.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(asyncio, "sleep", late)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    feed = FeedClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(304))))
    col = Collector(settings, MagicMock(), MagicMock(), feed)

    async def sleep(seconds: float) -> bool:
        await late(seconds)
        return False

    async def poll_once() -> PollOutcome:
        starts.append(clock[0])
        await feed.fetch()
        sends.append(clock[0])
        clock[0] += work_s
        if len(starts) >= polls:
            col.stop.set()
        return PollOutcome("ok")

    col._sleep, col.poll_once = sleep, poll_once  # type: ignore[method-assign]
    asyncio.run(col.poll_loop())
    return sends, [s - t for s, t in zip(sends, starts, strict=True)]


def test_an_overrunning_poll_skips_to_the_next_whole_tick_not_a_burst(monkeypatch: pytest.MonkeyPatch) -> None:
    """B04 (math.floor instead of math.ceil): a poll whose work takes 40 s (1.33 intervals) must be followed by the
    next tick on the grid, 60 s after the last one. With floor the loop starts again at once (40 s), a burst."""
    sends, _ = _drive(monkeypatch, 20, work_s=40.0, jitter_s=0.0)
    gaps = [b - a for a, b in zip(sends, sends[1:], strict=False)]
    assert all(abs(g - 60.0) < 0.01 for g in gaps), gaps


def test_48_hours_of_random_wake_up_jitter_never_breaks_the_floor_or_ratchets_the_wait(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The #261 class: 5760 polls (48 hours at 30 s) with every wake-up up to 1.5 s late at random. The 30 s floor
    between sends always holds, the throttle wait never grows, and the long-run cadence stays close to 30 s."""
    sends, waits = _drive(monkeypatch, 5760, jitter_s=1.5, seed=261)
    gaps = [b - a for a, b in zip(sends, sends[1:], strict=False)]
    assert min(gaps) >= GAP - EPS
    assert max(waits) < 0.05, f"throttle wait ratcheted up to {max(waits):.3f} s"
    mean = (sends[-1] - sends[0]) / (len(sends) - 1)
    assert GAP <= mean < GAP + 1.6, mean   # late wake-ups add at most their own size each poll
    first, last = waits[:1000], waits[-1000:]
    assert abs(sum(last) / 1000 - sum(first) / 1000) < 0.01   # no drift between the first and the last day


class _Cur:
    """A cursor that fails like PostgreSQL does for a value too big for an index, whenever the batch holds BAD."""
    rowcount = 1

    def __init__(self) -> None:
        self.ran: list[list[str]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        if not isinstance(params, dict):
            return  # the deadline guard's set_config
        names = [r["callsign"] for r in params["rows"].obj]
        self.ran.append(names)
        if "BAD" in names:
            raise psycopg.errors.ProgramLimitExceeded("index row size exceeds the maximum")

    def fetchall(self) -> list[Any]:
        return []


class _Conn:
    def transaction(self) -> Any:
        from contextlib import nullcontext
        return nullcontext()


def test_a_batch_failing_with_program_limit_exceeded_is_bisected_down_to_the_bad_record() -> None:
    """A06 (ProgramLimitExceeded removed from the bisect catch, the #111 regression): it is not a DataError or an
    IntegrityError, so without the entry one oversized callsign would lose the whole snapshot."""
    assert not issubclass(psycopg.errors.ProgramLimitExceeded, psycopg.DataError | psycopg.IntegrityError)
    rows = [{"callsign": c} for c in ("A", "B", "BAD", "C", "D")]
    cur = _Cur()
    n, _, bad = store._write(_Conn(), cur, "SQL", rows, None, False)  # type: ignore[arg-type]
    assert bad == ["BAD"]
    assert n >= 1   # the good halves ran and reported their row counts
