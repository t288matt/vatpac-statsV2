"""Issue 387: the watchdog's stats_loader check (ops/watchdog/watchdog.py) and its alert wording (ops/watchdog/messages.py).

No database or network: the cursor is a stub. The decision is a pure function of three database answers: the status of the
newest finished run, whether a good run exists (a success that finished, or a run in progress that wrote a day, in the last
36 hours), and the hours since the last success. The query itself is run against Postgres in the loader's integration tests."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import psycopg.errors
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ops" / "watchdog"))
spec = importlib.util.spec_from_file_location("watchdog_under_test_stats", REPO / "ops" / "watchdog" / "watchdog.py")
assert spec and spec.loader
wd = importlib.util.module_from_spec(spec)
sys.modules["watchdog_under_test_stats"] = wd
spec.loader.exec_module(wd)
import messages as m  # noqa: E402

OK, WARN, CRIT = wd.OK, wd.WARN, wd.CRIT


@pytest.mark.parametrize(("newest", "good", "level"), [
    ("success", True, OK),
    (None, True, OK),                 # only a run in progress, writing days (the first long run)
    ("stopped", True, OK),            # a deploy or a restart never warns
    ("interrupted", True, WARN),
    ("failed", True, WARN),
    ("success", False, CRIT),         # nothing good in the last 36 hours, whatever the newest run says
    ("stopped", False, CRIT),
    ("interrupted", False, CRIT),     # CRIT wins over WARN
    ("failed", False, CRIT),
    (None, False, CRIT),              # no run at all
])
def test_level_matrix(newest: str | None, good: bool, level: str) -> None:
    assert wd.stats_loader_level(newest, good, 40.0)[0] == level


class Conn:
    def __init__(self) -> None:
        self.rolled_back = 0

    def rollback(self) -> None:
        self.rolled_back += 1


class Cur:
    def __init__(self, row: tuple[Any, ...] | None, fail: Exception | None = None) -> None:
        self.connection = Conn()
        self.row, self.fail = row, fail
        self.sql: list[str] = []
        self.params: list[Any] = []

    def execute(self, sql: str, params: Any = None) -> None:
        if self.fail:
            raise self.fail
        self.sql.append(sql)
        self.params.append(params)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


def test_the_query_reads_the_run_log_with_the_36_hour_window_and_never_writes() -> None:
    cur = Cur(("success", True, 3.0))
    assert wd.check_stats_loader(cur)[0] == OK
    sql = cur.sql[0]
    assert "atc_stats_run_log" in sql and "status <> 'running'" in sql and "last_day_at" in sql
    assert cur.params[0] == {"h": 36} and wd.STATS_GOOD_HOURS == 36
    assert not any(w in sql.upper() for w in ("INSERT", "UPDATE", "DELETE"))
    assert cur.connection.rolled_back == 1       # an earlier failed query on the shared connection must not hide this one


def test_an_empty_run_log_is_a_problem() -> None:
    level, detail = wd.check_stats_loader(Cur((None, False, None)))
    assert level == CRIT and "never" in detail


def test_a_failed_newest_run_with_a_recent_success_is_a_warning() -> None:
    level, detail = wd.check_stats_loader(Cur(("failed", True, 5.0)))
    assert level == WARN and "failed" in detail


def test_a_query_failure_is_a_warning_not_a_crash() -> None:
    level, detail = wd.check_stats_loader(Cur(None, psycopg.errors.InsufficientPrivilege("denied")))
    assert level == WARN and "InsufficientPrivilege" in detail and "denied" not in detail


def test_alerts_on_a_change_of_level_and_again_on_recovery_and_nothing_between() -> None:
    sent: list[str] = []

    class Rec(wd.Alerter):
        def __init__(self) -> None:
            self.confirmed, self.pending = {}, {}
            self.cfg = type("C", (), {"confirm": 2})()

        def send(self, text: str, alert: object = None) -> None:
            sent.append(text)

    a = Rec()
    warn = wd.stats_loader_level("interrupted", True, 5.0)
    crit = wd.stats_loader_level("failed", False, 50.0)
    for res in (warn, warn, warn, crit, crit, crit, wd.stats_loader_level("success", True, 1.0), wd.stats_loader_level("success", True, 1.0)):
        a.update("stats_loader", *res)
    assert [s.split(":")[0] for s in sent] == ["WARN stats_loader", "CRIT stats_loader", "RECOVERED stats_loader (was crit)"][:len(sent)]
    assert len(sent) == 3


@pytest.mark.parametrize(("res", "words"), [
    (wd.stats_loader_level("failed", True, 5.0), ["last run", "failed", "tries again by itself"]),
    (wd.stats_loader_level("interrupted", True, 5.0), ["last run", "cut off", "tries again by itself"]),
    (wd.stats_loader_level("success", False, 40.0), ["over 36 hours", "not affected", "engineer"]),
])
def test_plain_wording(res: Any, words: list[str]) -> None:
    text = m.humanise("stats_loader", "crit" if res[0] == CRIT else "warn", res[1], facts=m.facts_of(res))
    first = text.splitlines()[0]
    assert "monitoring check" not in first and all(w in first for w in words), text
    assert text.isascii()


def test_recovery_wording_is_its_own() -> None:
    first = m.humanise("stats_loader", "ok", "the loader is loading days", previous_level="warn").splitlines()[0]
    assert "ATC statistics loader is loading days again" in first
