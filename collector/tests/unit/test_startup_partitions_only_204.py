"""Issue 204: startup and the in-poll CheckViolation recovery run only the tasks they need, not the daily job."""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from vatpac import main as vmain
from vatpac import maintenance as m
from vatpac.main import Collector
from vatpac.settings import Settings

ALL_TASKS = ["settings", "partitions_created", "flights_refreshed", "partitions_dropped", "poll_log_trimmed"]


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture()
def called(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(m, "sync_settings", lambda *a, **k: calls.append("settings"))
    monkeypatch.setattr(m, "ensure_partitions", lambda *a, **k: calls.append("partitions_created") or 1)
    monkeypatch.setattr(m, "refresh_flights", lambda *a, **k: calls.append("flights_refreshed") or 0)
    monkeypatch.setattr(m, "drop_old_partitions", lambda *a, **k: calls.append("partitions_dropped") or 0)
    monkeypatch.setattr(m, "trim_poll_log", lambda *a, **k: calls.append("poll_log_trimmed") or 0)
    return calls


def test_the_scheduled_daily_job_still_runs_every_task_in_order(called: list[str]) -> None:
    out = m.run_maintenance(MagicMock(), _settings())
    assert called == ALL_TASKS and out["failed"] == 0


def test_startup_tasks_are_settings_then_partitions_only(called: list[str]) -> None:
    out = m.run_maintenance(MagicMock(), _settings(), m.STARTUP_TASKS)
    assert called == ["settings", "partitions_created"] and out["failed"] == 0


def test_startup_and_recovery_task_names_are_real_tasks() -> None:
    # A typo would match nothing, so startup would run no task and still report success.
    assert set(m.STARTUP_TASKS) <= set(ALL_TASKS) and set(m.PARTITION_TASKS) <= set(ALL_TASKS)


def test_recovery_tasks_are_partitions_only(called: list[str]) -> None:
    m.run_maintenance(MagicMock(), _settings(), m.PARTITION_TASKS)
    assert called == ["partitions_created"]


def test_a_failing_startup_task_is_counted_and_does_not_block_the_other(monkeypatch: pytest.MonkeyPatch,
                                                                        called: list[str]) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise psycopg.OperationalError("down")

    monkeypatch.setattr(m, "sync_settings", boom)
    out = m.run_maintenance(MagicMock(), _settings(), m.STARTUP_TASKS)
    assert out["failed"] == 1 and called == ["partitions_created"]


class _CheckFeed:
    last_wait_ms, last_request_ms, last_request_at, last_raw = None, None, None, None

    async def fetch(self) -> Any:
        raise psycopg.errors.CheckViolation("no partition of relation found for row")


def test_checkviolation_recovery_runs_partition_creation_only(monkeypatch: pytest.MonkeyPatch) -> None:
    col = Collector(_settings(), MagicMock(), MagicMock(), _CheckFeed())
    col._log_poll = lambda *a, **k: None  # type: ignore[method-assign]
    seen: list[Any] = []

    def fake_maintain(only: Any = None) -> dict[str, int]:
        seen.append(only)
        return {"failed": 0}

    monkeypatch.setattr(col, "_maintain", fake_maintain)
    outcome = asyncio.run(col.poll_once())
    assert seen == [vmain.PARTITION_TASKS] and outcome.status == "error"


def test_maintain_once_reports_failure_and_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    col = Collector(_settings(), MagicMock(), MagicMock(), MagicMock())

    def explode(only: Any = None) -> dict[str, int]:
        raise psycopg.OperationalError("down")

    monkeypatch.setattr(col, "_maintain", explode)
    assert asyncio.run(col.maintain_once(vmain.STARTUP_TASKS)) is False
    monkeypatch.setattr(col, "_maintain", lambda only=None: {"failed": 1})
    assert asyncio.run(col.maintain_once(vmain.STARTUP_TASKS)) is False


def test_full_job_runs_one_retry_delay_after_startup_then_daily() -> None:
    """A partitions-only startup must not starve drop/trim/views: the first full run comes after MAINTENANCE_RETRY_S."""
    col = Collector(_settings(), MagicMock(), MagicMock(), MagicMock())
    sleeps: list[float] = []
    runs: list[Any] = []

    async def fake_sleep(delay: float) -> bool:
        sleeps.append(delay)
        return len(sleeps) > 2  # stop after two full runs

    async def fake_maintain(only: Any = None) -> bool:
        runs.append(only)
        return True

    col._sleep = fake_sleep  # type: ignore[method-assign]
    col.maintain_once = fake_maintain  # type: ignore[method-assign]
    asyncio.run(col.maintenance_loop())
    assert runs == [None, None]
    assert sleeps == [vmain.MAINTENANCE_RETRY_S, vmain.MAINTENANCE_INTERVAL_S, vmain.MAINTENANCE_INTERVAL_S]
