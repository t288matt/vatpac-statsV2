"""Small helpers shared by the QA tests."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg
from qa_fakes import FakeApi, FakeClock, Sess, make
from vatsim_stats import app, settings

TODAY = date(2026, 11, 15)


def cfg(**over: str) -> settings.Settings:
    env = {"DATABASE_URL": "postgresql://unused", "STATS_RUN_AT": "06:00", "STATS_MIN_INTERVAL_SECONDS": "5",
           "STATS_PAGE_SIZE": "5000", "STATS_OVERLAP_DAYS": "2", "STATS_RETENTION_YEARS": "1"}
    env.update(over)
    return settings.load(env)


def daily(first: date, last: date, per_day: int = 2, base: int = 0, hours: tuple[int, ...] = (3, 15)) -> list[Sess]:
    out: list[Sess] = []
    d = first
    while d <= last:
        for k in range(per_day):
            h = hours[k % len(hours)]
            out.append(make(base + d.toordinal() * 10 + k, datetime(d.year, d.month, d.day, h, k, 0, tzinfo=UTC)))
        d += timedelta(days=1)
    return out


def run(conn: psycopg.Connection, fake: FakeApi, c: settings.Settings | None = None, page_size: int | None = None) -> str:
    c = c or cfg()
    return app.run_once(conn, fake.api(c.min_interval_seconds, page_size or c.page_size), fake.clock, c)


def stored(su: psycopg.Connection) -> set[date]:
    return {r[0] for r in su.execute("SELECT DISTINCT (session_start AT TIME ZONE 'UTC')::date FROM atc_stats_session")}


def n_rows(su: psycopg.Connection) -> int:
    return int(su.execute("SELECT count(*) FROM atc_stats_session").fetchone()[0])  # type: ignore[index]


def last_run(su: psycopg.Connection) -> dict[str, Any]:
    cur = su.execute("SELECT * FROM atc_stats_run_log ORDER BY id DESC LIMIT 1")
    cols = [d.name for d in cur.description or []]
    return dict(zip(cols, cur.fetchone() or (), strict=True))


def fresh(start: datetime | None = None) -> tuple[FakeClock, FakeApi]:
    clock = FakeClock(start) if start else FakeClock()
    return clock, FakeApi(clock)
