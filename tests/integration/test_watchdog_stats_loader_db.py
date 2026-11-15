"""Issue 387: the watchdog's stats_loader check against a real database, as the least-privilege role watchdog_ro.

The run-log rows are written as the owner (the loader's own tests cover how the loader writes them). The check must give the same
answer as the loader's own healthcheck rule: a success that finished, or a run in progress that wrote a day, in the last 36 hours."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
import psycopg.rows  # noqa: E402, F811
pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("watchdog_under_test_statsdb2", REPO / "ops" / "watchdog" / "watchdog.py")
assert spec and spec.loader
wd = importlib.util.module_from_spec(spec)
sys.modules["watchdog_under_test_statsdb2"] = wd
spec.loader.exec_module(wd)

ROW = ("INSERT INTO atc_stats_run_log (status, started_at, finished_at, last_day_at) VALUES "
       "(%(s)s::text, now() - make_interval(secs => %(start)s::float8 * 3600), "
       "CASE WHEN %(s)s::text = 'running' THEN NULL ELSE now() - make_interval(secs => %(end)s::float8 * 3600) END, "
       "CASE WHEN %(day)s::float8 IS NULL THEN NULL ELSE now() - make_interval(secs => %(day)s::float8 * 3600) END)")


@pytest.fixture()
def runs(conn):
    conn.execute("TRUNCATE atc_stats_run_log RESTART IDENTITY")

    def add(status: str, start: float, end: float = 0.0, day: float | None = None) -> None:
        conn.execute(ROW, {"s": status, "start": start, "end": end, "day": day})

    yield add
    conn.execute("TRUNCATE atc_stats_run_log RESTART IDENTITY")


def level(conn) -> str:
    """The check as watchdog_ro, the role the watchdog really connects as."""
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'watchdog_ro'").fetchone() is None:
        pytest.skip("role watchdog_ro not created (db/roles.sql creates it; see the CI step)")
    conn.execute("SET ROLE watchdog_ro")
    try:
        with conn.cursor(row_factory=psycopg.rows.tuple_row) as cur:
            return wd.check_stats_loader(cur)[0]
    finally:
        conn.execute("RESET ROLE")


def test_no_run_at_all_is_critical(conn, runs) -> None:
    assert level(conn) == wd.CRIT


def test_a_recent_success_is_ok(conn, runs) -> None:
    runs("success", 3, 2)
    assert level(conn) == wd.OK


def test_a_success_older_than_36_hours_is_critical(conn, runs) -> None:
    runs("success", 40, 38)
    assert level(conn) == wd.CRIT


@pytest.mark.parametrize("status", ["interrupted", "failed"])
def test_a_bad_newest_run_after_a_recent_success_is_a_warning(conn, runs, status: str) -> None:
    runs("success", 10, 9)
    runs(status, 2, 1)
    assert level(conn) == wd.WARN


def test_a_stopped_newest_run_never_warns(conn, runs) -> None:
    runs("success", 10, 9)
    runs("stopped", 2, 1)
    assert level(conn) == wd.OK


def test_a_bad_run_with_no_good_run_is_critical(conn, runs) -> None:
    runs("failed", 2, 1)
    assert level(conn) == wd.CRIT


def test_a_running_run_counts_only_if_it_wrote_a_day_in_the_last_36_hours(conn, runs) -> None:
    runs("running", 100, day=5)          # a long first load, still writing days
    assert level(conn) == wd.OK
    conn.execute("TRUNCATE atc_stats_run_log RESTART IDENTITY")
    runs("running", 100, day=40)         # started long ago and stuck: its last day is older than 36 hours
    assert level(conn) == wd.CRIT


def test_an_interrupted_run_beside_a_running_one_still_warns(conn, runs) -> None:
    runs("interrupted", 6, 5)
    runs("running", 4, day=1)
    assert level(conn) == wd.WARN


def test_watchdog_ro_can_read_the_run_log_but_not_write_it(conn) -> None:
    conn.execute("SET ROLE watchdog_ro")
    conn.execute("SELECT count(*) FROM atc_stats_run_log")
    for sql in ("INSERT INTO atc_stats_run_log (status) VALUES ('success')", "DELETE FROM atc_stats_run_log",
                "SELECT 1 FROM atc_stats_session"):
        with pytest.raises(psycopg.errors.Error):
            conn.execute(sql)
    conn.execute("RESET ROLE")
