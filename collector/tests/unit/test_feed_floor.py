"""No two requests to VATSIM are ever closer than 30 s, whatever the path (retries, 304s, discovery)."""

import asyncio

import httpx
import pytest

from vatpac import feed as vfeed
from vatpac import main as vmain
from vatpac.errors import RateLimitedError
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


def test_retries_and_429_keep_the_floor_between_real_sends_with_wake_overshoot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every attempt (retry, 429, the call after a 429) is a real send, and none is closer than 30 s to the last,
    even though every wait wakes a few ms (and once 3 s) late: the stamp is the actual send time, not the target."""
    now: list[float] = [1000.0]
    starts: list[float] = []
    late = iter([0.004, 3.0, 0.004, 0.004, 0.004, 0.004])

    async def fake_sleep(seconds: float) -> None:
        now[0] += seconds + next(late, 0.004)

    monkeypatch.setattr(vfeed.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(vfeed.asyncio, "sleep", fake_sleep)
    responses = iter([500, 500, 200, 429, 200])
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: (starts.append(now[0]), httpx.Response(next(responses), content=b"{}"))[1]))
    feed = vfeed.FeedClient(Settings(_env_file=None), client)  # type: ignore[call-arg]

    async def go() -> None:
        await feed.fetch()  # 500, 500, 200
        with pytest.raises(RateLimitedError):
            await feed.fetch()
        await feed.fetch()

    asyncio.run(go())
    assert len(starts) == 5
    assert all(b - a >= vfeed.MIN_REQUEST_GAP_S - 1e-9 for a, b in zip(starts, starts[1:], strict=False))


def test_last_wait_ms_and_last_request_ms_are_reported_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow poll's log can tell the floor wait from time on the wire; the wait is the time actually waited."""
    now = [1000.0]

    async def fake_sleep(seconds: float) -> None:
        now[0] += seconds + 0.5  # wakes half a second late: last_wait_ms reports the real wait, not the target

    monkeypatch.setattr(vfeed.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(vfeed.asyncio, "sleep", fake_sleep)

    def handler(_req: httpx.Request) -> httpx.Response:
        now[0] += 0.05  # the GET itself takes 50 ms
        return httpx.Response(200, content=b"{}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    feed = vfeed.FeedClient(Settings(_env_file=None), client)  # type: ignore[call-arg]

    async def go() -> None:
        await feed.fetch()
        assert feed.last_wait_ms == 0.0 and feed.last_request_ms == pytest.approx(50.0, abs=0.5)
        await feed.fetch()
        assert feed.last_wait_ms == pytest.approx(29_950.0 + 500.0, abs=0.5)
        assert feed.last_request_ms == pytest.approx(50.0, abs=0.5)  # not inflated by the wait

    asyncio.run(go())


def test_last_request_ms_is_set_even_when_the_get_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(vfeed.time, "monotonic", lambda: now[0])

    def handler(request: httpx.Request) -> httpx.Response:
        now[0] += 0.7
        raise httpx.ConnectTimeout("timed out", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    feed = vfeed.FeedClient(Settings(_env_file=None), client)  # type: ignore[call-arg]
    feed.last_request_ms = 50.0  # the previous poll's number must not survive a timed-out GET
    with pytest.raises(httpx.ConnectTimeout):
        asyncio.run(feed._get_once())
    assert feed.last_request_ms == pytest.approx(700.0, abs=0.5)

