"""Collector supervisor: poll loop, maintenance loop, reference reload (SIGHUP), health server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from . import __version__
from .archive import Archive
from .errors import ApplyBusyError, FeedError, RateLimitedError, ReferenceDataError
from .feed import FeedClient
from .filters import ReferenceData, load_reference
from .health import Stats, start_server
from .logsetup import setup_logging
from .maintenance import FLIGHT_REFRESH_INTERVAL_S, refresh_flights, run_maintenance
from .model import Snapshot, parse_snapshot, update_timestamp
from .pollstats import update_stats
from .resume import db_last_ts, load_last_ts
from .settings import Settings
from .store import ApplyResult, apply_snapshot, record_reference_change, write_poll_log

log = logging.getLogger("vatpac")
UNCHANGED_RETRY_S = 30.0  # no faster than the feed floor (feed.MIN_REQUEST_GAP_S)
MAINTENANCE_INTERVAL_S = 86_400.0
MAINTENANCE_RETRY_S = 60.0
APPLY_TIMEOUT_S = 25.0  # the poll stops waiting after this
APPLY_DEADLINE_S = 20.0  # the worker stops itself after this (transaction scope), so it cannot outlive the wait
MAX_MALFORMED_FRACTION, MIN_WHITELIST_FRACTION = 0.2, 0.7
# fail fast on a stuck statement or lock; Collector._maintain raises statement_timeout for its own session
DB_OPTIONS = "-c statement_timeout=20000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=30000"


@dataclass(slots=True)
class PollOutcome:
    status: str
    retry_in: float | None = None


class Collector:
    def __init__(self, settings: Settings, ref: ReferenceData, pool: ConnectionPool, feed: FeedClient) -> None:
        self.settings, self.ref, self.pool, self.feed = settings, ref, pool, feed
        self.stats = Stats(reference=self._versions(ref), poll_interval_s=settings.poll_interval_seconds)
        self.archive = Archive(settings, self.stats)
        self.stop = asyncio.Event()
        self.last_ts: datetime | None = None
        self._apply_lock = threading.Lock()  # held by the apply worker thread: two applies never overlap
        self._tasks: set[asyncio.Task[None]] = set()  # strong refs so SIGHUP reload tasks are not collected

    @staticmethod
    def _versions(ref: ReferenceData) -> dict[str, str]:
        return {"polygon_sha": ref.polygon_sha, "whitelist_sha": ref.whitelist_sha}

    # ---- blocking DB helpers (run in worker threads) ----
    def _apply(self, snapshot: Snapshot, ref: ReferenceData) -> ApplyResult:
        if not self._apply_lock.acquire(blocking=False):  # a timed-out apply may still be running: skip, never overlap
            raise ApplyBusyError("previous snapshot apply is still running, poll skipped")
        try:
            with self.pool.connection() as conn:
                return apply_snapshot(conn, snapshot, ref.polygon, ref.whitelist, polygon_sha=ref.polygon_sha,
                                      whitelist_sha=ref.whitelist_sha, prefixes=ref.prefixes,
                                      deadline=time.monotonic() + APPLY_DEADLINE_S)
        finally:
            self._apply_lock.release()

    def _db_last_ts(self) -> datetime | None:
        return db_last_ts(self.pool, self.settings.feed_max_future_seconds)

    async def load_last_ts(self) -> None:
        self.last_ts = await load_last_ts(self.pool, self.settings.feed_max_future_seconds, self.last_ts)

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
            conn.execute("SET statement_timeout = '15min'")  # lock_timeout stays 5 s
            try:
                out = run_maintenance(conn, self.settings)
            finally:
                conn.execute("RESET statement_timeout")
        self._record_reference(self.ref)  # idempotent; also covers a database that was down at startup
        return out

    # ---- polling ----
    async def poll_once(self) -> PollOutcome:
        t0, ref = time.monotonic(), self.ref
        status, http, snap_ts, result, err = "error", None, None, None, None
        seen_ts = None  # the feed's own timestamp, kept even when rejected as too far ahead (for the archive only)
        retry_in: float | None = None
        try:
            fetched = await self.feed.fetch()
            http, payload = fetched.http_status, fetched.payload
            if payload is not None:
                ts, bound = update_timestamp(payload), self.settings.feed_max_future_seconds
                seen_ts = ts
                if ts - datetime.now(UTC) > timedelta(seconds=bound):  # one future stamp would skip every real update
                    raise FeedError(f"snapshot time {ts:%Y-%m-%dT%H:%M:%SZ} is over {bound} s ahead of the clock", 200)
                snap_ts = ts
            if payload is None or (self.last_ts is not None and snap_ts is not None and snap_ts <= self.last_ts):
                status, retry_in = "skipped_unchanged", max(UNCHANGED_RETRY_S, min(fetched.max_age or 0, 5.0))
            else:
                snapshot = parse_snapshot(payload, self.settings.text_max_length)
                if not snapshot.pilots_global and not snapshot.controllers_global:
                    raise FeedError("feed has no pilots or controllers (treated as an outage)", 200)
                total = snapshot.pilots_global + snapshot.controllers_global
                if snapshot.malformed > MAX_MALFORMED_FRACTION * total:
                    raise FeedError(f"{snapshot.malformed} of {total} feed entries malformed", 200)
                if snapshot.bad_plans > MAX_MALFORMED_FRACTION * snapshot.pilots_global:  # the plan format changed
                    raise FeedError(f"{snapshot.bad_plans} of {snapshot.pilots_global} pilots have a flight_plan "
                                    "that is not an object", 200)
                if snapshot.malformed:
                    err = f"{snapshot.malformed} malformed feed entries skipped"
                result = await asyncio.wait_for(asyncio.to_thread(self._apply, snapshot, ref), APPLY_TIMEOUT_S)
                self.last_ts, status = snapshot.observed_at, "ok"
        except RateLimitedError as exc:
            err, http, retry_in = str(exc), 429, exc.retry_after
        except ApplyBusyError as exc:
            err = str(exc)
            log.error("poll skipped", extra={"error": err})
        except FeedError as exc:
            err, http = str(exc), exc.http_status
        except psycopg.errors.CheckViolation as exc:  # no partition for observed_at: create them now
            err = f"CheckViolation: {exc}"
            log.error("snapshot rejected by database check, running maintenance", extra={"error": str(exc)})
            await self.maintain_once()
        except TimeoutError:  # the worker thread is ended by the database statement/lock timeouts
            err = "apply timed out"
            log.error("snapshot apply timed out after %ss", APPLY_TIMEOUT_S)
        except Exception as exc:  # a bad poll must never kill the process
            err = f"{type(exc).__name__}: {exc}"
            log.exception("poll failed")
        ms = int((time.monotonic() - t0) * 1000)
        await asyncio.to_thread(self._log_poll, status, ref, snapshot_ts=snap_ts, duration_ms=ms, http_status=http,
                                result=result, error=err)
        self._update_stats(status, result, snap_ts, err)
        # queued for the archive thread: never blocks or raises
        self.archive.submit(getattr(self.feed, "last_raw", None), seen_ts, status, http, err)
        return PollOutcome(status, retry_in)

    def _update_stats(self, status: str, result: ApplyResult | None, snap_ts: datetime | None, err: str | None) -> None:
        update_stats(self.stats, self.settings, status, result, snap_ts, err)

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

    def _refresh_flights(self) -> None:
        with self.pool.connection() as conn:
            refresh_flights(conn)

    async def flight_summary_loop(self) -> None:
        """Write finished flights to flight_summary every 5 minutes; a failure only makes charts stale."""
        while not await self._sleep(FLIGHT_REFRESH_INTERVAL_S):
            try:
                await asyncio.to_thread(self._refresh_flights)
            except Exception:
                log.exception("flight writer failed")

    def request_reload(self) -> None:
        task = asyncio.get_running_loop().create_task(self.reload_reference())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _reject_reload(self, error: str, **extra: Any) -> None:
        self.stats.reload_rejected += 1
        self.stats.last_reload_error = error
        log.error("reference reload rejected, keeping previous files", extra={"error": error, "alert": True, **extra})

    async def reload_reference(self) -> None:
        s = self.settings
        try:
            new = await asyncio.to_thread(load_reference, s.polygon_path, s.whitelist_path)
        except ReferenceDataError as exc:
            self._reject_reload(str(exc))
            return
        old_wl, new_wl = self.ref.whitelist, new.whitelist
        if len(new_wl) < MIN_WHITELIST_FRACTION * len(old_wl):
            self._reject_reload(f"whitelist shrank from {len(old_wl)} to {len(new_wl)} entries",
                                dropped=sorted(old_wl - new_wl)[:20])
            return
        self.stats.last_reload_error = None
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
                          kwargs={"autocommit": True, "connect_timeout": 10, "options": DB_OPTIONS})
    pool.open(wait=False)  # keeps retrying in the background if the database is not up yet
    feed = FeedClient(settings)
    collector = Collector(settings, ref, pool, feed)
    feed.stop = collector.stop
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
        await collector.load_last_ts()
        log.info("collector started", extra={"version": __version__, **collector.stats.reference})
        await asyncio.gather(collector.poll_loop(), collector.maintenance_loop(first_ok),
                             collector.flight_summary_loop())
    finally:
        collector.archive.stopping.set()  # an in-flight archive write must not hold the exit past the grace period
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
