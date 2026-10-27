"""Issue #261: the REAL poll_loop and FeedClient on a virtual clock whose every wake is a little late.

Before the fix the throttle stamped its wake time while poll_loop stayed on an exact grid, so the throttle wait
ratcheted up to the full 30 s and reset (the duration_ms sawtooth). Now poll_loop follows the feed's real send
stamp: a poll never starts before the floor has elapsed and the wait stays ~0. No real sleeping."""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from vatpac import main as vmain
from vatpac.feed import FeedClient, FetchResult
from vatpac.main import Collector, PollOutcome
from vatpac.settings import Settings

GAP = 30.0
EPS = 1e-6


def run_loop(monkeypatch: pytest.MonkeyPatch, polls: int, outcomes: dict[int, PollOutcome] | None = None,
             stalls: dict[int, float] | None = None, jitter_s: float = 0.002) -> tuple[list[float], list[float]]:
    """Run poll_loop for `polls` polls; returns (real send times, seconds each poll waited for the floor)."""
    clock, rng = [1000.0], random.Random(7)
    starts: list[float] = []
    sends: list[float] = []

    async def late(seconds: float) -> None:  # every real sleep wakes a little late
        clock[0] += max(seconds, 0) + rng.uniform(0, jitter_s)

    monkeypatch.setattr(vmain.time, "monotonic", lambda: clock[0])  # the one shared `time` module
    monkeypatch.setattr(asyncio, "sleep", late)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    feed = FeedClient(settings, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(304))))
    col = Collector(settings, MagicMock(), MagicMock(), feed)

    async def sleep(seconds: float) -> bool:  # Collector._sleep, plus any scripted stall of the loop itself
        await late(seconds + (stalls or {}).get(len(starts), 0.0))
        return False

    async def poll_once() -> PollOutcome:  # the real FeedClient.fetch, so the real throttle and stamp
        i = len(starts)
        starts.append(clock[0])
        await feed.fetch()
        sends.append(clock[0])  # the mock GET takes no virtual time, so this is the real send time
        if len(starts) >= polls:
            col.stop.set()
        return (outcomes or {}).get(i, PollOutcome("ok"))

    col._sleep, col.poll_once = sleep, poll_once  # type: ignore[method-assign]
    asyncio.run(col.poll_loop())
    return sends, [s - t for s, t in zip(sends, starts, strict=True)]


def floor_holds(sends: list[float]) -> bool:
    return all(b - a >= GAP - EPS for a, b in zip(sends, sends[1:], strict=False))


def test_no_ramp_over_3000_polls_and_cadence_is_last_send_plus_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    sends, waits = run_loop(monkeypatch, 3000)
    assert max(waits) < 0.05, f"throttle wait ratcheted up to {max(waits):.3f} s"  # fails on origin/main
    assert floor_holds(sends)
    assert GAP <= (sends[-1] - sends[0]) / 2999 < GAP + 0.01  # ~30.001 s, not a drift-free grid


@pytest.mark.parametrize("stall", [2.0, 5.0])
def test_a_loop_stall_costs_one_late_poll_not_a_permanent_wait(monkeypatch: pytest.MonkeyPatch, stall: float) -> None:
    sends, waits = run_loop(monkeypatch, 300, stalls={100: stall})
    assert floor_holds(sends) and max(waits[101:]) < 0.05
    assert sends[100] - sends[99] >= GAP + stall - EPS


def test_a_429_recovers_in_one_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    sends, waits = run_loop(monkeypatch, 300, outcomes={100: PollOutcome("error", retry_in=10.0)})
    assert 19.0 < waits[101] < 21.0  # asked to retry in 10 s: the floor makes it wait out the other ~20 s
    assert floor_holds(sends) and max(waits[102:]) < 0.05


def test_an_unchanged_feed_reanchor_recovers_in_one_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    sends, waits = run_loop(monkeypatch, 300, outcomes={100: PollOutcome("skipped_unchanged", retry_in=30.0)})
    assert floor_holds(sends) and max(waits[101:]) < 0.05


class _Feed:
    last_wait_ms, last_request_ms = 20_000.0, 120.5

    def __init__(self, clock: list[float]) -> None:
        self.clock = clock

    async def fetch(self) -> FetchResult:
        self.clock[0] += 3.0
        return FetchResult(None, 304)


class _BareFeed:  # a test double without the new attributes: the WARNING must not raise out of poll_once
    def __init__(self, clock: list[float]) -> None:
        self.clock = clock

    async def fetch(self) -> FetchResult:
        self.clock[0] += 3.0
        return FetchResult(None, 304)


@pytest.mark.parametrize(("feed_cls", "wait", "req_ms"), [(_Feed, 20_000.0, 120.5), (_BareFeed, None, None)])
def test_slow_poll_warning_carries_duration_wait_and_request(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, feed_cls: type, wait: Any,
        req_ms: Any) -> None:
    clock = [1000.0]
    monkeypatch.setattr(vmain.time, "monotonic", lambda: clock[0])
    col = Collector(Settings(_env_file=None), MagicMock(), MagicMock(), feed_cls(clock))  # type: ignore[call-arg]
    col._log_poll = lambda *a, **k: None  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger="vatpac"):
        asyncio.run(col.poll_once())
    rec = next(r for r in caplog.records if r.getMessage() == "slow poll")
    assert (rec.duration_ms, rec.wait_ms, rec.request_ms) == (3000, wait, req_ms)  # type: ignore[attr-defined]
