"""Issue 27: the daily system sample. A failed reading is logged and skipped; the other tasks go on."""
from __future__ import annotations

import shutil
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest

from vatpac import maintenance as m
from vatpac.settings import Settings


def _conn(monkeypatch: pytest.MonkeyPatch, recorded: list[tuple[Any, ...]]) -> MagicMock:
    monkeypatch.setattr(m, "_scalar", lambda c, q, params: recorded.append(params) or 1)
    return MagicMock()


def test_the_probe_keeps_the_median_of_three_runs_in_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fails if the median is replaced by the first, fastest or slowest run, or the unit is not milliseconds."""
    recorded: list[tuple[Any, ...]] = []
    monkeypatch.setattr(m.timeit, "repeat", lambda fn, number, repeat: [0.30, 0.01, 0.20])
    monkeypatch.setattr(shutil, "disk_usage", lambda p: (1000, 400, 600))
    assert m.record_system_sample(_conn(monkeypatch, recorded), "/archive") == 1
    assert recorded == [(1000, 400, pytest.approx(200.0))]


def test_the_probe_runs_three_times_inside_one_capped_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _conn(monkeypatch, [])
    m.record_system_sample(conn, ".")
    assert conn.transaction.called
    cap = conn.execute.call_args_list[0].args[0]
    assert "statement_timeout" in cap and ", true)" in cap  # local to the transaction: the 15 minute timeout comes back
    assert conn.execute.call_count == 4  # the cap and three probe runs


def test_a_failing_disk_read_skips_the_sample(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    recorded: list[tuple[Any, ...]] = []
    monkeypatch.setattr(shutil, "disk_usage", MagicMock(side_effect=OSError("gone")))
    assert m.record_system_sample(_conn(monkeypatch, recorded), "/archive") == 0
    assert recorded == [] and "system sample skipped" in caplog.text


def test_a_failing_probe_skips_the_sample(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    recorded: list[tuple[Any, ...]] = []
    conn = _conn(monkeypatch, recorded)
    conn.execute.side_effect = psycopg.errors.QueryCanceled("statement timeout")
    assert m.record_system_sample(conn, ".") == 0
    assert recorded == [] and "system sample skipped" in caplog.text


def test_a_skipped_sample_never_stops_the_other_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    for name in ("sync_settings", "ensure_partitions", "refresh_flights", "drop_old_partitions", "trim_poll_log"):
        monkeypatch.setattr(m, name, lambda *a, _n=name, **k: calls.append(_n) or 0)
    monkeypatch.setattr(shutil, "disk_usage", MagicMock(side_effect=OSError("gone")))
    settings = Settings(_env_file=None, database_url="postgresql://x/y")  # type: ignore[call-arg]
    out = m.run_maintenance(MagicMock(), settings)
    assert out["system_sampled"] == 0 and out["failed"] == 0 and len(calls) == 5
