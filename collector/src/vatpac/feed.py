"""Feed access: URL discovery, conditional GET, retries with jitter, 429 handling."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from . import __version__
from .errors import FeedError, RateLimitedError
from .settings import Settings

log = logging.getLogger(__name__)

RETRIES = 3
BACKOFF_BASE_S = 1.0
RETRY_AFTER_CAP_S = 120.0
MIN_REQUEST_GAP_S = 30.0  # hard floor between any two requests to VATSIM, retries and discovery included
_MAX_AGE_RE = re.compile(r"max-age=(\d+)")


@dataclass(frozen=True, slots=True)
class FetchResult:
    payload: dict[str, Any] | None  # None means 304 Not Modified
    http_status: int
    max_age: float | None = None  # seconds, from Cache-Control


def backoff_delay(attempt: int, rng: random.Random | None = None) -> float:
    """Jittered exponential backoff: base * 2^attempt scaled by 0.5..1.5."""
    return BACKOFF_BASE_S * float(2**attempt) * (rng or random).uniform(0.5, 1.5)


def parse_retry_after(value: str | None, default: float = 30.0) -> float:
    try:
        seconds = default if value is None else float(value)
    except ValueError:
        seconds = default
    return min(max(seconds, 1.0), RETRY_AFTER_CAP_S)


class FeedClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None,
                 min_gap_s: float = MIN_REQUEST_GAP_S, stop: asyncio.Event | None = None) -> None:
        self._settings = settings
        self._min_gap_s = min_gap_s
        self.stop = stop  # set on SIGTERM: waits end at once so the container stops inside its grace period
        self._last_request_at: float | None = None
        self.url = settings.vatsim_feed_url
        self._last_modified: str | None = None
        self._etag: str | None = None
        self.last_raw: bytes | None = None  # body of the newest 200 response, for the archive; else None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout_seconds, connect=min(5.0, settings.http_timeout_seconds)),
            headers={"User-Agent": f"vatpac-stats/{__version__}", "Accept-Encoding": "gzip"},
            follow_redirects=True,
        )

    async def _wait(self, seconds: float) -> None:
        if self.stop is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(self.stop.wait(), seconds)
        except TimeoutError:
            return
        raise FeedError("collector is stopping")

    async def _throttle(self) -> None:
        """Wait until min_gap_s has passed since the previous request started, then stamp this one."""
        if self._last_request_at is not None:
            wait = self._last_request_at + self._min_gap_s - time.monotonic()
            if wait > 0:
                await self._wait(wait)
        self._last_request_at = time.monotonic()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def discover(self) -> str:
        """Feed URL from the status endpoint (data.v3[0]); falls back to the configured URL."""
        try:
            await self._throttle()
            resp = await self._client.get(self._settings.vatsim_status_url)
            resp.raise_for_status()
            url = resp.json()["data"]["v3"][0]
            if not isinstance(url, str) or not url.startswith("https://"):
                raise ValueError(f"unexpected feed url {url!r}")
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("feed discovery failed, using fallback url", extra={"error": str(exc)})
            url = self._settings.vatsim_feed_url
        if url != self.url:
            self._last_modified = self._etag = None
        self.url = url
        return str(url)

    async def _get_once(self) -> FetchResult:
        conditional = (("If-Modified-Since", self._last_modified), ("If-None-Match", self._etag))
        headers = {k: v for k, v in conditional if v}
        self.last_raw = None
        await self._throttle()
        resp = await self._client.get(self.url, headers=headers)
        if resp.status_code == 304:
            return FetchResult(None, 304)
        if resp.status_code == 429:
            raise RateLimitedError(parse_retry_after(resp.headers.get("Retry-After")))
        if resp.status_code != 200:
            raise FeedError(f"HTTP {resp.status_code}", resp.status_code)
        self.last_raw = resp.content
        try:
            payload = resp.json()
        except ValueError as exc:
            raise FeedError(f"invalid JSON: {exc}", 200) from exc
        if not isinstance(payload, dict):
            raise FeedError("feed is not a JSON object", 200)
        self._last_modified = resp.headers.get("Last-Modified")
        self._etag = resp.headers.get("ETag")
        m = _MAX_AGE_RE.search(resp.headers.get("Cache-Control", ""))
        return FetchResult(payload, 200, float(m.group(1)) if m else None)

    async def fetch(self) -> FetchResult:
        """One logical fetch: up to 3 retries on network errors/5xx. 429 is raised, never retried here."""
        last: FeedError | None = None
        for attempt in range(RETRIES + 1):
            if attempt:
                await self._wait(backoff_delay(attempt - 1))
            try:
                return await self._get_once()
            except RateLimitedError:
                raise
            except FeedError as exc:
                if exc.http_status is not None and exc.http_status < 500:
                    raise
                last = exc
            except httpx.HTTPError as exc:
                last = FeedError(f"{type(exc).__name__}: {exc}")
            log.warning("feed fetch failed", extra={"attempt": attempt + 1, "error": str(last)})
        assert last is not None
        raise last
