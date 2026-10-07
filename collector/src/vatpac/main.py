"""Collector supervisor: poll loop, maintenance loop, reference reload (SIGHUP), health server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from . import __version__
from .errors import FeedError, RateLimitedError, ReferenceDataError
from .feed import FeedClient
from .filters import ReferenceData, load_reference
from .health import Stats, start_server
from .maintenance import run_maintenance
from .model import Snapshot, parse_snapshot, update_timestamp
from .settings import Settings
from .store import ApplyResult, apply_snapshot, record_reference_change, write_poll_log

log = logging.getLogger("vatpac")
_STD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
UNCHANGED_RETRY_S = 2.0
MAINTENANCE_INTERVAL_S = 86_400.0
MAINTENANCE_RETRY_S = 60.0


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
        }
        out.update({k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass(slots=True)
class PollOutcome:
    status: str
    retry_in: float | None = None


class Collector:
    def __init__(self, settings: Settings, ref: ReferenceData, pool: ConnectionPool, feed: FeedClient) -> None:
        self.settings, self.ref, self.pool, self.feed = settings, ref, pool, feed
        self.stats = Stats(reference=self._versions(ref))
        self.stop = asyncio.Event()
        self.last_ts: datetime | None = None
        self._tasks: set[asyncio.Task[None]] = set()  # strong refs so SIGHUP reload tasks are not collected

    @staticmethod
    def _versions(ref: ReferenceData) -> dict[str, str]:
        return {"polygon_sha": ref.polygon_sha, "whitelist_sha": ref.whitelist_sha}

    # ---- blocking DB helpers (run in worker threads) ----
    def _apply(self, snapshot: Snapshot, ref: ReferenceData) -> ApplyResult:
        with self.pool.connection() as conn:
            return apply_snapshot(conn, snapshot, ref.polygon, ref.whitelist,
                                  polygon_sha=ref.polygon_sha, whitelist_sha=ref.whitelist_sha)

    def _log_poll(self, status: str, ref: ReferenceData, **kw: Any) -> None:
        try:
            with self.pool.connection() as conn:
                write_poll_log(conn, status, polygon_sha=ref.polygon_sha, whitelist_sha=ref.whitelist_sha, **kw)
        except Exception:
            log.exception("poll_log write failed")

    def _record_reference(self, ref: ReferenceData) -> None:
        with self.pool.connection() as conn:
            for name, sha in (("australian_airspace_polygon.json", ref.polygon_sha),
                              ("controller_callsigns_list.txt", ref.whitelist_sha)):
                if record_reference_change(conn, name, sha):
                    log.info("reference data changed", extra={"file": name, "sha": sha})

    def _maintain(self) -> dict[str, int]:
        with self.pool.connection() as conn:
            out = run_maintenance(conn, self.settings)
        self._record_reference(self.ref)  # idempotent; also covers a database that was down at startup
        return out

    # ---- polling ----
    async def poll_once(self) -> PollOutcome:
        t0, ref = time.monotonic(), self.ref
        status, http, snap_ts, result, err = "error", None, None, None, None
        retry_in: float | None = None
        try:
            fetched = await self.feed.fetch()
            http, payload = fetched.http_status, fetched.payload
            if payload is not None:
                snap_ts = update_timestamp(payload)
            if payload is None or (self.last_ts is not None and snap_ts is not None and snap_ts <= self.last_ts):
                status, retry_in = "skipped_unchanged", max(UNCHANGED_RETRY_S, min(fetched.max_age or 0, 5.0))
            else:
                snapshot = parse_snapshot(payload)
                if not snapshot.pilots_global and not snapshot.controllers_global:
                    raise FeedError("feed has no pilots or controllers (treated as an outage)", 200)
                result = await asyncio.to_thread(self._apply, snapshot, ref)
                self.last_ts, status = snapshot.observed_at, "ok"
        except RateLimitedError as exc:
            err, http, retry_in = str(exc), 429, exc.retry_after
        except FeedError as exc:
            err, http = str(exc), exc.http_status
        except psycopg.errors.CheckViolation as exc:  # no partition for observed_at: create them now
            err = f"CheckViolation: {exc}"
            log.error("snapshot rejected by database check, running maintenance", extra={"error": str(exc)})
            await self.maintain_once()
        except Exception as exc:  # a bad poll must never kill the process
            err = f"{type(exc).__name__}: {exc}"
            log.exception("poll failed")
        ms = int((time.monotonic() - t0) * 1000)
        await asyncio.to_thread(self._log_poll, status, ref, snapshot_ts=snap_ts, duration_ms=ms,
                                http_status=http, result=result, error=err)
        self._update_stats(status, result, snap_ts, err)
        return PollOutcome(status, retry_in)

    def _update_stats(self, status: str, result: ApplyResult | None, snap_ts: datetime | None, err: str | None) -> None:
        st, s = self.stats, self.settings
        if status == "error":
            st.polls_error += 1
            st.consecutive_failures += 1
            log.warning("poll error", extra={"error": err, "consecutive_failures": st.consecutive_failures})
        else:
            st.polls_skipped += status == "skipped_unchanged"
            st.polls_ok += status == "ok"
            st.consecutive_failures = 0
            st.last_ok_wall = time.time()
        if result is not None and snap_ts is not None:
            st.last_snapshot_ts = snap_ts
            st.positions_written += result.positions_inserted
            st.rejected_values += result.rejected_values
            st.near_misses += len(result.new_near_misses)
            for name in ("pilots_seen", "pilots_inside", "pilots_kept", "controllers_seen", "controllers_kept"):
                setattr(st, name, getattr(result, name))
            for callsign in result.new_near_misses:
                log.warning("near-miss controller callsign not on whitelist", extra={"callsign": callsign})
        was_open = st.breaker_open
        st.breaker_open = st.consecutive_failures >= s.circuit_failure_threshold
        if st.breaker_open != was_open:
            log.log(logging.ERROR if st.breaker_open else logging.INFO,
                    "circuit breaker %s", "opened" if st.breaker_open else "closed")

    async def _sleep(self, seconds: float) -> bool:
        """Sleep unless stopping. Returns True if asked to stop."""
        try:
            await asyncio.wait_for(self.stop.wait(), max(seconds, 0))
            return True
        except TimeoutError:
            return False

    async def poll_loop(self) -> None:
        interval = self.settings.poll_interval_seconds
        next_at = time.monotonic()
        while not self.stop.is_set():
            if await self._sleep(next_at - time.monotonic()):
                return
            was_open = self.stats.breaker_open
            outcome = await self.poll_once()
            now = time.monotonic()
            if self.stats.breaker_open and not was_open:
                await self.feed.discover()  # the URL may have moved
            if outcome.retry_in is not None:
                next_at = now + outcome.retry_in
            elif self.stats.breaker_open:
                next_at = now + self.settings.circuit_slow_interval_seconds
            else:
                next_at += interval
                if next_at <= now:  # overran: skip missed ticks instead of bursting
                    next_at += interval * math.ceil((now - next_at) / interval + 1e-9)

    async def maintain_once(self) -> bool:
        try:
            return (await asyncio.to_thread(self._maintain))["failed"] == 0
        except Exception:
            log.exception("maintenance failed")
            return False

    async def maintenance_loop(self, first_ok: bool) -> None:
        """Daily after the startup run (retry after a minute while it keeps failing)."""
        ok = first_ok
        while not await self._sleep(MAINTENANCE_INTERVAL_S if ok else MAINTENANCE_RETRY_S):
            ok = await self.maintain_once()

    def request_reload(self) -> None:
        task = asyncio.get_running_loop().create_task(self.reload_reference())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def reload_reference(self) -> None:
        s = self.settings
        try:
            new = await asyncio.to_thread(load_reference, s.polygon_path, s.whitelist_path)
        except ReferenceDataError as exc:
            self.stats.reload_rejected += 1
            log.error("reference reload rejected, keeping previous files", extra={"error": str(exc), "alert": True})
            return
        if (new.polygon_sha, new.whitelist_sha) != (self.ref.polygon_sha, self.ref.whitelist_sha):
            self.ref, self.stats.reference = new, self._versions(new)
            log.info("reference data reloaded", extra=self.stats.reference)
        try:
            await asyncio.to_thread(self._record_reference, new)
        except Exception:
            log.exception("could not record reference_data_change")


async def run(settings: Settings, ref: ReferenceData) -> None:
    pool = ConnectionPool(settings.dsn, min_size=1, max_size=4, timeout=10, open=False,
                          check=ConnectionPool.check_connection,
                          kwargs={"autocommit": True, "connect_timeout": 10})
    pool.open(wait=False)  # keeps retrying in the background if the database is not up yet
    feed = FeedClient(settings)
    collector = Collector(settings, ref, pool, feed)
    runner = await start_server(collector.stats, settings.health_port)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows has no signal handlers
            loop.add_signal_handler(sig, collector.stop.set)
    if hasattr(signal, "SIGHUP"):
        loop.add_signal_handler(signal.SIGHUP, collector.request_reload)
    try:
        await feed.discover()
        first_ok = await collector.maintain_once()  # partitions must exist before the first poll
        log.info("collector started", extra={"version": __version__, **collector.stats.reference})
        await asyncio.gather(collector.poll_loop(), collector.maintenance_loop(first_ok))
    finally:
        await runner.cleanup()
        await feed.aclose()
        pool.close()


def main() -> int:
    try:
        settings = Settings()
    except Exception as exc:
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2
    setup_logging(settings.log_level)
    try:
        ref = load_reference(Path(settings.polygon_path), Path(settings.whitelist_path))
    except ReferenceDataError as exc:
        log.critical("cannot start: %s", exc)  # fail closed
        return 2
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings, ref))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
