"""Collector health state and the /health and /metrics HTTP endpoints (aiohttp).

/health is 200 only when the last "ok" poll is under 60 s old AND the newest stored snapshot is under 120 s old.
"skipped_unchanged" never counts as a successful poll (it does not move last_ok_wall), so a frozen upstream feed
turns /health 503 although HTTP polls succeed. The body also has reload_rejected and last_reload_error (SIGHUP).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from . import __version__

HEALTH_MAX_AGE_S = 60.0
SNAPSHOT_MAX_AGE_S = 120.0


@dataclass(slots=True)
class Stats:
    """Mutable runtime state shared by the poll loop and the HTTP handlers."""

    polls_ok: int = 0
    polls_error: int = 0
    polls_skipped: int = 0
    consecutive_failures: int = 0
    breaker_open: bool = False
    last_ok_wall: float | None = None
    last_snapshot_ts: datetime | None = None
    positions_written: int = 0
    pilots_seen: int = 0
    pilots_inside: int = 0
    pilots_kept: int = 0
    controllers_seen: int = 0
    controllers_kept: int = 0
    rejected_values: int = 0
    near_misses: int = 0
    reload_rejected: int = 0
    last_reload_error: str | None = None
    reference: dict[str, str] = field(default_factory=dict)

    def last_ok_age(self, now: float | None = None) -> float | None:
        return None if self.last_ok_wall is None else (now or time.time()) - self.last_ok_wall

    def healthy(self, now: float | None = None) -> bool:
        age, snap_age = self.last_ok_age(now), self.snapshot_age(now)
        return age is not None and age < HEALTH_MAX_AGE_S and snap_age is not None and snap_age < SNAPSHOT_MAX_AGE_S

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
    }


def render_metrics(stats: Stats) -> str:
    age = stats.snapshot_age()
    rows: list[tuple[str, str, str, float]] = [
        ("polls_total", "counter", 'status="ok"', stats.polls_ok),
        ("polls_total", "counter", 'status="error"', stats.polls_error),
        ("polls_total", "counter", 'status="skipped_unchanged"', stats.polls_skipped),
        ("positions_written_total", "counter", "", stats.positions_written),
        ("rejected_values_total", "counter", "", stats.rejected_values),
        ("near_miss_controllers_total", "counter", "", stats.near_misses),
        ("reference_reload_rejected_total", "counter", "", stats.reload_rejected),
        ("circuit_breaker_open", "gauge", "", int(stats.breaker_open)),
        ("last_snapshot_age_seconds", "gauge", "", age if age is not None else -1),
        *((n, "gauge", "", getattr(stats, n)) for n in ("consecutive_failures", "pilots_seen", "pilots_inside",
                                                         "pilots_kept", "controllers_seen", "controllers_kept")),
    ]
    lines: list[str] = []
    typed: set[str] = set()
    for name, kind, labels, value in rows:
        full = f"vatpac_{name}"
        if full not in typed:
            typed.add(full)
            lines.append(f"# TYPE {full} {kind}")
        lines.append(f"{full}{{{labels}}} {value}" if labels else f"{full} {value}")
    return "\n".join(lines) + "\n"


def build_app(stats: Stats) -> web.Application:
    async def health(_: web.Request) -> web.Response:
        return web.json_response(health_body(stats), status=200 if stats.healthy() else 503)

    async def metrics(_: web.Request) -> web.Response:
        return web.Response(text=render_metrics(stats), content_type="text/plain")

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/metrics", metrics)
    return app


async def start_server(stats: Stats, port: int) -> web.AppRunner:
    runner = web.AppRunner(build_app(stats), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()  # noqa: S104  (container-internal)
    return runner
