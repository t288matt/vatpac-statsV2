"""Issue 88: applies never overlap, a timed-out one is not followed by another, and the deadline guard works."""
from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import vatpac.main as main
from vatpac.feed import FetchResult
from vatpac.filters import load_reference
from vatpac.main import Collector
from vatpac.settings import Settings
from vatpac.store import ApplyResult, _guard


class _Cur:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.calls.append((sql, params))


def test_guard_without_a_deadline_does_nothing() -> None:
    cur = _Cur()
    _guard(cur, None)  # type: ignore[arg-type]
    assert cur.calls == []


def test_guard_sets_the_time_left_as_a_transaction_local_statement_timeout() -> None:
    cur = _Cur()
    _guard(cur, time.monotonic() + 10)  # type: ignore[arg-type]
    (sql, params), = cur.calls
    assert "set_config('statement_timeout'" in sql and sql.rstrip().endswith("true)")
    assert 9000 < int(params[0]) <= 10_001


def test_guard_raises_once_the_deadline_has_passed_and_runs_no_statement() -> None:
    cur = _Cur()
    with pytest.raises(TimeoutError):
        _guard(cur, time.monotonic() - 0.001)  # type: ignore[arg-type]
    assert cur.calls == []


class _Feed:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def fetch(self) -> FetchResult:
        return FetchResult(self.payload, 200)


def _collector(payload: dict[str, Any]) -> tuple[Collector, list[dict[str, Any]]]:
    repo = Path(__file__).resolve().parents[3]
    ref = load_reference(repo / "config" / "australian_airspace_polygon.json",
                         repo / "config" / "controller_callsigns_list.txt")
    pool = MagicMock()

    @contextmanager
    def connection():  # type: ignore[no-untyped-def]
        yield MagicMock()

    pool.connection = connection
    col = Collector(Settings(), ref, pool, _Feed(payload))  # type: ignore[arg-type]
    rows: list[dict[str, Any]] = []
    col._log_poll = lambda status, _ref, **kw: rows.append({"status": status, **kw})  # type: ignore[method-assign]
    return col, rows


def test_a_timed_out_apply_is_never_overlapped_and_the_next_poll_after_it_ends_succeeds(
        fixture_payload: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "APPLY_TIMEOUT_S", 0.2)
    release, started = threading.Event(), threading.Event()
    running, peak, calls = [0], [0], [0]
    guard = threading.Lock()

    def slow_apply(*_a: Any, **_k: Any) -> ApplyResult:
        with guard:
            running[0] += 1
            calls[0] += 1
            peak[0] = max(peak[0], running[0])
        started.set()
        release.wait(10)
        with guard:
            running[0] -= 1
        return ApplyResult()

    monkeypatch.setattr(main, "apply_snapshot", slow_apply)
    col, rows = _collector(fixture_payload)

    async def scenario() -> list[str]:
        out = [(await col.poll_once()).status]  # times out while the worker thread is still blocked
        assert started.is_set()
        out.append((await col.poll_once()).status)  # must be skipped, not overlapped
        release.set()
        for _ in range(100):
            if not col._apply_lock.locked():
                break
            await asyncio.sleep(0.02)
        out.append((await col.poll_once()).status)
        return out

    assert asyncio.run(scenario()) == ["error", "error", "ok"]
    assert "timed out" in rows[0]["error"] and "still running" in rows[1]["error"]
    assert calls[0] == 2 and peak[0] == 1  # the skipped poll never reached apply_snapshot; never two at once


def test_the_lock_is_released_when_the_apply_fails(fixture_payload: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> ApplyResult:
        raise RuntimeError("database went away")

    monkeypatch.setattr(main, "apply_snapshot", boom)
    col, rows = _collector(fixture_payload)
    assert asyncio.run(col.poll_once()).status == "error" and not col._apply_lock.locked()
    assert "database went away" in rows[0]["error"]


def test_the_deadline_is_passed_to_apply_snapshot(fixture_payload: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float | None] = []

    def fake(*_a: Any, deadline: float | None = None, **_k: Any) -> ApplyResult:
        seen.append(deadline)
        return ApplyResult()

    monkeypatch.setattr(main, "apply_snapshot", fake)
    col, _ = _collector(fixture_payload)
    t0 = time.monotonic()
    assert asyncio.run(col.poll_once()).status == "ok"
    assert seen[0] is not None and 0 < seen[0] - t0 <= main.APPLY_DEADLINE_S + 1 and main.APPLY_DEADLINE_S < main.APPLY_TIMEOUT_S
