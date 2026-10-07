"""Exception types shared across the collector."""

from __future__ import annotations


class ReferenceDataError(Exception):
    """A reference file (polygon, whitelist) is missing, unreadable or invalid."""


class FeedError(Exception):
    """The feed could not be fetched or parsed. Never fatal to the process."""

    def __init__(self, message: str, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


class RateLimitedError(FeedError):
    """HTTP 429. `retry_after` is the number of seconds the server asked us to wait."""

    def __init__(self, retry_after: float) -> None:
        super().__init__("rate limited (429)", 429)
        self.retry_after = retry_after
