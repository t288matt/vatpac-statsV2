"""The loader: waits for its run time, loads whole days, applies retention. Run with `python -m vatsim_stats.app`;
`python -m vatsim_stats.app health` is the container healthcheck (reads the run log; exit 0 healthy, 1 not)."""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import psycopg

from . import retention, store
from .api import TIMEOUT_SECONDS, Api, Clock, RateLimited, ServerErrors, Stopped
from .settings import Settings, SettingsError, load
from .windows import DayFailed, fetch_day, oldest_allowed, plan

log = logging.getLogger("vatsim_stats")
START_WAIT_SECONDS = 60.0  # after any start, so a crash loop cannot break the 5-second rule
HEALTH_HOURS = 36
FAILED_DAYS_IN_A_ROW = 3


class TooManyFailedDays(Exception):
    """Three days in a row failed their count check even as hour windows: something is wrong, so the run stops."""


def run_once(conn: psycopg.Connection[Any], api: Api, clock: Clock, cfg: Settings) -> str:
    """One run: the recent days, then every missing date newest first. Returns the final status."""
    now = clock.now()
    stored = store.stored_dates(conn)
    empty, hour_from = store.run_memory(conn)
    recent, missing = plan(stored, now.date(), oldest_allowed(now, cfg.retention_years), cfg.overlap_days, empty)
    run_id = store.start_run(conn, now)
    log.info("run %s: %d recent days, %d missing dates", run_id, len(recent), len(missing))
    base, ins, upd, loaded = api.requests, 0, 0, 0
    failed: list[date] = []
    streak = 0  # failed days in a row
    reasons: list[str] = []
    new_empty: list[date] = []
    oldest_stored = min(stored) if stored else None
    status, error = "success", None
    try:
        for day in recent + missing:
            try:
                result = fetch_day(api, day, hour_from is not None and day <= hour_from)
            except DayFailed as exc:
                failed.append(day)
                streak += 1
                reasons.append(f"{day}: {exc}")
                log.warning("%s not written: %s", day, exc)
                store.update_run(conn, run_id, requests=api.requests - base, failed_days=failed)
                if streak >= FAILED_DAYS_IN_A_ROW:
                    raise TooManyFailedDays(f"{FAILED_DAYS_IN_A_ROW} failed days in a row; the run stopped") from None
                continue
            streak = 0
            if result.timed_out:
                hour_from = day
                store.update_run(conn, run_id, hour_windows_from=day)
            if result.empty:
                new_empty.append(day)
                store.update_run(conn, run_id, requests=api.requests - base, empty_dates=new_empty)
                if day in missing and oldest_stored is not None and day < oldest_stored:
                    log.info("%s and every older hour has no sessions: the start of VATSIM's history", day)
                    break
                continue
            i, u = store.write_day(conn, result.rows)
            ins, upd, loaded = ins + i, upd + u, loaded + 1
            oldest_stored = day if oldest_stored is None else min(oldest_stored, day)
            store.update_run(conn, run_id, requests=api.requests - base, sessions_inserted=ins, sessions_updated=upd,
                             days_loaded=loaded, last_day_at=clock.now())
            log.info("%s written: %d new, %d changed, %d requests so far", day, i, u, api.requests - base)
        if failed:
            status, error = "failed", "; ".join(reasons[:20])
        else:
            deleted = retention.apply(conn, clock.now(), cfg.retention_years)
            store.update_run(conn, run_id, sessions_deleted=deleted)
    except Stopped:
        status, error = "stopped", "stopped by the container's stop signal"
    except (RateLimited, ServerErrors, TooManyFailedDays) as exc:
        status, error = "failed", f"{exc}; the run stopped"
    except Exception as exc:
        log.exception("run %s failed", run_id)
        status, error = "failed", f"{type(exc).__name__}: {exc}"
    store.update_run(conn, run_id, requests=api.requests - base)
    store.finish_run(conn, run_id, status, error, clock.now())
    log.info("run %s %s: %d new, %d changed, %d days, %d failed days, %d requests", run_id, status, ins, upd, loaded,
             len(failed), api.requests - base)
    return status


def next_run_at(now: datetime, cfg: Settings) -> datetime:
    """The next STATS_RUN_AT (UTC) after now."""
    at = datetime.combine(now.date(), cfg.run_at, UTC)
    return at if at > now else at + timedelta(days=1)


def serve(conn: psycopg.Connection[Any], api: Api, clock: Clock, cfg: Settings) -> None:
    """One process, one run at a time. A run starts at STATS_RUN_AT each UTC day (one still going then is not doubled).
    At start-up (crash or deploy) it runs at once, after the 60 s wait, unless a run already succeeded today;
    a run that succeeded earlier today means the next one is tomorrow's, even before STATS_RUN_AT."""
    store.mark_interrupted(conn, clock.now())
    api.hold(START_WAIT_SECONDS)
    first = True
    try:
        while not clock.stop.is_set():
            now = clock.now()
            wait = now if first and not store.success_on(conn, now.date()) else next_run_at(now, cfg)
            if wait > now and store.success_on(conn, wait.date()):
                wait += timedelta(days=1)  # a run already succeeded on that UTC day: never a second one
            first = False
            clock.sleep((wait - now).total_seconds())
            run_once(conn, api, clock, cfg)
    except Stopped:
        pass


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.Formatter.converter = time.gmtime  # log times are UTC
    try:
        cfg = load(os.environ)
    except SettingsError as exc:
        log.error("refusing to start: %s", exc)
        return 2
    conn = store.connect(cfg.database_url)
    if argv[1:] == ["health"]:
        return 0 if store.healthy(conn, datetime.now(UTC), HEALTH_HOURS) else 1
    clock = Clock(threading.Event())
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: clock.stop.set())
    with httpx.Client(timeout=TIMEOUT_SECONDS, headers={"User-Agent": "vatpac-stats-loader"}) as client:
        serve(conn, Api(client, clock, cfg.min_interval_seconds, cfg.page_size), clock, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
