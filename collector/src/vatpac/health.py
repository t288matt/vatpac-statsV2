"""Collector health state and the /health HTTP endpoint (aiohttp).

/health is 200 only when the last "ok" poll is under max(60, 2 x POLL_INTERVAL_SECONDS) s old AND the newest stored
snapshot is under max(120, 4 x POLL_INTERVAL_SECONDS) s old (60 s and 120 s at the default 30 s; ADR-024).
"skipped_unchanged" never counts as a successful poll (it does not move last_ok_wall), so a frozen upstream feed
turns /health 503 although HTTP polls succeed. The body also has reload_rejected and last_reload_error (SIGHUP).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from . import __version__

HEALTH_MAX_AGE_S, SNAPSHOT_MAX_AGE_S = 60.0, 120.0  # floors, reached at the default 30 s poll interval


@dataclass(slots=True)
class Stats:
    """Mutable runtime state shared by the poll loop and the HTTP handlers."""

    consecutive_failures: int = 0
    breaker_open: bool = False
    last_ok_wall: float | None = None
    last_snapshot_ts: datetime | None = None
    reload_rejected: int = 0
    last_reload_error: str | None = None
    reference: dict[str, str] = field(default_factory=dict)
    poll_interval_s: float = 30.0  # the ages below scale with it (ADR-024)
    archive_info: Callable[[], dict[str, Any]] | None = None  # set by archive.Archive

    def last_ok_age(self, now: float | None = None) -> float | None:
        return None if self.last_ok_wall is None else (now or time.time()) - self.last_ok_wall

    def healthy(self, now: float | None = None) -> bool:
        age, snap_age = self.last_ok_age(now), self.snapshot_age(now)
        return (age is not None and age < max(HEALTH_MAX_AGE_S, 2 * self.poll_interval_s) and snap_age is not None
                and snap_age < max(SNAPSHOT_MAX_AGE_S, 4 * self.poll_interval_s))

    def snapshot_age(self, now: float | None = None) -> float | None:
        if self.last_snapshot_ts is None:
            return None
        return (now or time.time()) - self.last_snapshot_ts.timestamp()


def health_body(stats: Stats) -> dict[str, Any]:
    return {
        "status": "ok" if stats.healthy() else "unhealthy",
        "version": __version__,
        "last_ok": datetime.fromtimestamp(stats.last_ok_wall, UTC).isoformat() if stats.last_ok_wall else None,
        "last_ok_age_seconds": stats.last_ok_age(),
        "last_snapshot_ts": stats.last_snapshot_ts.isoformat() if stats.last_snapshot_ts else None,
        "consecutive_failures": stats.consecutive_failures,
        "breaker_open": stats.breaker_open,
        "reload_rejected": stats.reload_rejected,
        "last_reload_error": stats.last_reload_error,
        "reference": stats.reference,
        "archive": stats.archive_info() if stats.archive_info else {"enabled": False},
    }


def build_app(stats: Stats) -> web.Application:
    async def health(_: web.Request) -> web.Response:
        return web.json_response(health_body(stats), status=200 if stats.healthy() else 503)

    app = web.Application()
    app.router.add_get("/health", health)
    return app


async def start_server(stats: Stats, port: int) -> web.AppRunner:
    runner = web.AppRunner(build_app(stats), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()  # noqa: S104  (container-internal)
    return runner
