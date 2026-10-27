"""No two requests to VATSIM are ever closer than 30 s, whatever the path (retries, 304s, discovery)."""

import asyncio
import random

import httpx
import pytest

from vatpac import feed as vfeed
from vatpac import main as vmain
from vatpac.settings import Settings


def test_floor_is_30_seconds_everywhere() -> None:
    assert vfeed.MIN_REQUEST_GAP_S == 30.0
    assert vmain.UNCHANGED_RETRY_S >= vfeed.MIN_REQUEST_GAP_S
    assert vfeed.FeedClient(Settings(_env_file=None))._min_gap_s == 30.0  # type: ignore[call-arg]


def test_requests_are_never_closer_than_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    starts: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(vfeed.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(vfeed.asyncio, "sleep", fake_sleep)
    responses = iter([500, 500, 200, 304, 304])  # two failures, then ok, then two 304s
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: (starts.append(now[0]), httpx.Response(next(responses), content=b"{}"))[1]))
    feed = vfeed.FeedClient(Settings(_env_file=None), client)  # type: ignore[call-arg]

    async def go() -> None:
        await feed.fetch()  # 500, 500, 200 (retries inside one fetch)
        await feed.fetch()
        await feed.fetch()

    asyncio.run(go())
    assert len(starts) == 5
    assert all(b - a >= vfeed.MIN_REQUEST_GAP_S for a, b in zip(starts, starts[1:], strict=False))


def test_stop_ends_the_wait_at_once() -> None:
    from vatpac.errors import FeedError

    async def go() -> None:
        stop = asyncio.Event()
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(304)))
        feed = vfeed.FeedClient(Settings(_env_file=None), client, stop=stop)  # type: ignore[call-arg]
        await feed.fetch()  # first request is immediate
        stop.set()
        with pytest.raises(FeedError, match="stopping"):
            await asyncio.wait_for(feed.fetch(), 2)  # would wait 30 s without the stop

    asyncio.run(go())


def test_throttle_wait_does_not_ratchet_into_a_sawtooth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #261 regression (deterministic, no real sleeping): replays the real poll_loop grid (main.py:
    `next_at += interval`, exact arithmetic, never reads the clock back) against the real `_throttle`, with a
    small independent jitter on each simulated wake (as real event-loop/timer overshoot gives every real sleep).

    Before the fix, `_throttle` stamped the wake time, so each wait's own overshoot carried into the next poll's
    gap and the wait ratcheted up towards the full `min_gap_s` within about a hundred polls (reproduced separately
    against the pre-fix code below: it reaches the full gap and stays there). After the fix it stamps the time
    aimed for, so the wait stays bounded by the jitter alone, however many polls run."""
    clock = [0.0]
    jitter = random.Random(12345)  # a small, independent wake jitter: same order of magnitude as a real overshoot

    async def fake_sleep(seconds: float) -> None:
        clock[0] += max(seconds, 0) + 0.004  # the throttle's own wait always overshoots by a few ms, like a real one

    monkeypatch.setattr(vfeed.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(vfeed.asyncio, "sleep", fake_sleep)
    gap = 0.3
    feed = vfeed.FeedClient(Settings(_env_file=None), client=object(), min_gap_s=gap)  # type: ignore[call-arg]

    async def poll_grid(n: int) -> list[float]:
        next_at, waits = 0.0, []
        for _ in range(n):
            clock[0] = max(clock[0], next_at) + jitter.uniform(0, 0.002)  # the real poll_loop's own wake jitter
            before = clock[0]
            await feed._throttle()
            waits.append((clock[0] - before) * 1000)
            next_at += gap  # the real poll_loop: pure arithmetic, never derived from the actual wake time
        return waits

    waits = asyncio.run(poll_grid(2000))
    # must stay bounded by the jitter, not climb towards the 300 ms gap the way the pre-fix code does by poll ~100
    assert max(waits[200:]) < gap * 1000 * 0.2, f"throttle wait ratcheted up: max after settling = {max(waits[200:])}"


def test_throttle_still_enforces_the_floor_when_already_overdue(monkeypatch: pytest.MonkeyPatch) -> None:
    """A poll that is naturally late (the previous one took longer than min_gap_s) must not wait at all, and must
    not borrow time from the next one either: the floor is relative to when requests actually started."""
    clock = [0.0]

    async def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(vfeed.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(vfeed.asyncio, "sleep", fake_sleep)  # real asyncio.sleep would hang: the event loop's own
    # timer also reads the now-frozen time.monotonic, so a real callback would never be scheduled to fire
    feed = vfeed.FeedClient(Settings(_env_file=None), client=object(), min_gap_s=1.0)  # type: ignore[call-arg]

    async def go() -> None:
        await feed._throttle()  # first call: never waits
        assert feed.last_wait_ms == 0.0
        clock[0] += 5.0  # far later than the 1 s floor
        await feed._throttle()
        assert feed.last_wait_ms == 0.0  # already overdue: no wait
        await feed._throttle()  # immediately again: now the floor applies, relative to the actual last request
        assert feed.last_wait_ms == pytest.approx(1000.0, abs=1.0)

    asyncio.run(go())


def test_last_wait_ms_and_last_request_ms_are_reported_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #261 instrumentation: a slow poll's log can tell the throttle wait from time spent on the wire,
    rather than a single duration_ms that could be either."""
    now = [1000.0]

    async def fake_sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(vfeed.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(vfeed.asyncio, "sleep", fake_sleep)

    def handler(_req: httpx.Request) -> httpx.Response:
        now[0] += 0.05  # the GET itself takes 50 ms
        return httpx.Response(200, content=b"{}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    feed = vfeed.FeedClient(Settings(_env_file=None), client)  # type: ignore[call-arg]

    async def go() -> None:
        await feed.fetch()  # first request: no wait, the floor has nothing to measure against yet
        assert feed.last_wait_ms == 0.0
        assert 40.0 <= feed.last_request_ms <= 100.0
        await feed.fetch()  # second request: must wait out almost the whole 30 s floor
        assert feed.last_wait_ms >= 29_900.0
        assert 40.0 <= feed.last_request_ms <= 100.0  # the GET time is reported on its own, not inflated by the wait

    asyncio.run(go())


def test_pre_fix_throttle_does_ratchet_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins down what #261 actually was: replays the SAME grid as the regression test above against a standalone
    copy of the old (pre-fix) `_throttle`, proving the harness itself would have caught it. Not the shipped code."""

    class PreFixFeed:
        def __init__(self, min_gap_s: float) -> None:
            self._min_gap_s, self._last_request_at = min_gap_s, None

        async def _throttle(self) -> None:  # the exact pre-fix logic, for comparison only
            if self._last_request_at is not None:
                wait = self._last_request_at + self._min_gap_s - vfeed.time.monotonic()
                if wait > 0:
                    await vfeed.asyncio.sleep(wait)
            self._last_request_at = vfeed.time.monotonic()

    clock = [0.0]
    jitter = random.Random(12345)

    async def fake_sleep(seconds: float) -> None:
        clock[0] += max(seconds, 0) + 0.004

    monkeypatch.setattr(vfeed.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(vfeed.asyncio, "sleep", fake_sleep)
    gap = 0.3
    feed = PreFixFeed(gap)

    async def poll_grid(n: int) -> list[float]:
        next_at, waits = 0.0, []
        for _ in range(n):
            clock[0] = max(clock[0], next_at) + jitter.uniform(0, 0.002)
            before = clock[0]
            await feed._throttle()
            waits.append((clock[0] - before) * 1000)
            next_at += gap
        return waits

    waits = asyncio.run(poll_grid(200))
    assert max(waits) > gap * 1000 * 0.9  # it does climb to (almost) the full gap, unlike the fixed code above
