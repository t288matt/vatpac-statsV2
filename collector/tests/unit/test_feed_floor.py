"""No two requests to VATSIM are ever closer than 30 s, whatever the path (retries, 304s, discovery)."""

import asyncio

import httpx
import pytest

from vatpac import feed as vfeed
from vatpac import main as vmain
from vatpac.settings import Settings


def _client(statuses: list[int]) -> httpx.AsyncClient:
    it = iter(statuses)
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(next(it), content=b"{}")))


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
