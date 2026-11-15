"""QA for issue 387: the watchdog `stats_loader` check against a real run log, read as the watchdog_ro role.

From design section 4 (Checks and visibility): WARN while the newest finished run is interrupted or failed; CRIT with no good run
for 36 hours; a running run is good while coalesce(last_day_at, started_at) is within 36 hours; `stopped` never warns; an empty
run log is OK until 36 hours after the watchdog's first start, then CRIT.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qa_stats_loader_support as sup  # noqa: E402

REPO = sup.REPO
spec = importlib.util.spec_from_file_location("qa_watchdog_under_test", REPO / "ops" / "watchdog" / "watchdog.py")
assert spec and spec.loader
wd = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(REPO / "ops" / "watchdog"))
sys.modules["qa_watchdog_under_test"] = wd
spec.loader.exec_module(wd)

DB = "qa_watchdog_test"
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def su() -> Iterator[psycopg.Connection]:
    sup.need_db()
    sup.build_db(DB)
    with psycopg.connect(sup.dsn(DB), autocommit=True) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean(su: psycopg.Connection) -> None:
    su.execute("TRUNCATE atc_stats_run_log RESTART IDENTITY")


def add(su: psycopg.Connection, status: str, started_h: float, finished_h: float | None = None, last_day_h: float | None = None) -> None:
    su.execute("INSERT INTO atc_stats_run_log (started_at, finished_at, last_day_at, status) VALUES "
               "(now() - make_interval(hours => %s), now() - make_interval(hours => %s), now() - make_interval(hours => %s), %s)",
               (started_h, finished_h, last_day_h, status))


def level(first_run_hours_ago: float | None = None) -> tuple[str, str]:
    first = None if first_run_hours_ago is None else time.time() - first_run_hours_ago * 3600
    with psycopg.connect(sup.dsn(DB, "watchdog_ro")) as c, c.cursor() as cur:
        return wd.check_stats_loader(cur, first)


@pytest.mark.parametrize(("rows", "expect"), [
    # a good run and nothing wrong
    ([("success", 40, 10, None)], "ok"),
    ([("success", 40, 35, None)], "ok"),
    # no good run for 36 hours
    ([("success", 60, 37, None)], "crit"),
    ([("failed", 3, 2, None), ("success", 60, 50, None)], "crit"),
    ([("failed", 3, 2, None)], "crit"),
    ([("interrupted", 3, 2, None)], "crit"),
    # failed or interrupted newest finished run, but a good run within 36 hours: WARN
    ([("success", 40, 10, None), ("failed", 5, 4, None)], "warn"),
    ([("success", 40, 10, None), ("interrupted", 5, 4, None)], "warn"),
    # stopped never warns
    ([("success", 40, 10, None), ("stopped", 5, 4, None)], "ok"),
    ([("success", 40, 10, None), ("failed", 9, 8, None), ("stopped", 5, 4, None)], "ok"),  # newest finished is stopped
    # a running run
    ([("running", 10, None, None)], "ok"),                       # started within 36 h, nothing written yet
    ([("running", 40, None, None)], "crit"),                     # started 40 h ago and saved nothing
    ([("running", 100, None, 10)], "ok"),                        # a long run that wrote a day 10 h ago
    ([("running", 100, None, 40)], "crit"),                      # a long run that wrote nothing for 40 h
    ([("running", 1, None, 40)], "crit"),                        # last_day_at wins over started_at
    ([("failed", 9, 8, None), ("running", 5, None, 1)], "warn"), # failed before, a good running run now
    ([("success", 60, 50, None), ("running", 5, None, 1)], "ok"),
])
def test_levels(su: psycopg.Connection, rows: list, expect: str) -> None:
    for r in rows:
        add(su, *r)
    assert level(first_run_hours_ago=500)[0] == expect


def test_a_stopped_run_alone_never_warns_while_inside_the_window(su: psycopg.Connection) -> None:
    add(su, "stopped", 3, 2)
    add(su, "success", 20, 15)
    assert level(500)[0] == "ok"


def test_empty_run_log_is_ok_until_36_hours_after_the_watchdogs_first_start_then_crit() -> None:
    assert level(1)[0] == "ok"
    assert level(35.5)[0] == "ok"
    assert level(36.5)[0] == "crit"
    assert level(500)[0] == "crit"


def test_the_check_reads_the_run_log_as_the_read_only_role_and_returns_a_level_and_text() -> None:
    lvl, text = level(1)
    assert lvl in {"ok", "warn", "crit"} and isinstance(text, str) and text
