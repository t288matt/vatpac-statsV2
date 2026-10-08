"""Release review additions (08-10-2026): tests that kill survivors of the mutation check, and strict xfail tests that pin
defects found in review (each flips to a failure, and so must be removed, when the defect is fixed). No database."""
from __future__ import annotations

import asyncio
import copy
import gzip
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from vatpac import archive as arc
from vatpac import main as vmain
from vatpac.health import Stats
from vatpac.model import parse_snapshot
from vatpac.settings import Settings

REPO = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((REPO / "tests" / "fixtures" / "vatsim-data.sample.json").read_text(encoding="utf-8"))


# ---- startup: run() must resume from the stored newest snapshot (issue 41), after partitions exist ----
def test_run_loads_the_last_snapshot_time_after_the_first_maintenance(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Pool:
        check_connection = None

        def __init__(self, *a: Any, **k: Any) -> None: ...
        def open(self, **k: Any) -> None: ...
        def close(self) -> None: ...

    class Feed:
        def __init__(self, *a: Any) -> None: ...
        async def discover(self) -> None: calls.append("discover")
        async def aclose(self) -> None: ...

    class Runner:
        async def cleanup(self) -> None: ...

    class Col:
        def __init__(self, *a: Any) -> None:
            self.stats = type("S", (), {"reference": {}})()
            self.stop = asyncio.Event()

        async def maintain_once(self) -> bool:
            calls.append("maintain")
            return True

        async def load_last_ts(self) -> None: calls.append("load_last_ts")
        async def poll_loop(self) -> None: calls.append("poll_loop")
        async def maintenance_loop(self, ok: bool) -> None: ...
        async def flight_summary_loop(self) -> None: ...
        def request_reload(self) -> None: ...

    async def start_server(*a: Any) -> Runner:
        return Runner()

    monkeypatch.setattr(vmain, "ConnectionPool", Pool)
    monkeypatch.setattr(vmain, "FeedClient", Feed)
    monkeypatch.setattr(vmain, "Collector", Col)
    monkeypatch.setattr(vmain, "start_server", start_server)
    s = Settings(_env_file=None, archive_enabled=False)  # type: ignore[call-arg]
    asyncio.run(vmain.run(s, None))  # type: ignore[arg-type]
    assert calls.index("maintain") < calls.index("load_last_ts") < calls.index("poll_loop")


# ---- parsing: a non-finite number is not a value (found in review) ----
@pytest.mark.xfail(strict=True, reason="review finding: 1e999 parses to inf and int(inf) raises OverflowError, which "
                   "parse_snapshot does not catch, so one such pilot fails every poll while it is in the feed")
@pytest.mark.parametrize("field", ["altitude", "groundspeed", "heading", "pilot_rating"])
def test_infinite_integer_field_does_not_abort_the_snapshot(field: str) -> None:
    payload = copy.deepcopy(FIXTURE)
    payload["pilots"][0][field] = float("inf")  # what json.loads gives for the literal 1e999
    snap = parse_snapshot(payload)
    assert snap.pilots_global == len(payload["pilots"])


@pytest.mark.xfail(strict=True, reason="review finding: float('nan') and float('inf') pass _f, so the pilot is treated as "
                   "outside the polygon without being counted as malformed or rejected")
@pytest.mark.parametrize("value", [float("nan"), float("inf"), "nan", "1e999"])
def test_non_finite_coordinate_is_malformed(value: Any) -> None:
    payload = copy.deepcopy(FIXTURE)
    payload["pilots"][0]["latitude"] = value
    assert parse_snapshot(payload).malformed == 1


# ---- archive ----
def make(tmp_path: Path, **kw: Any) -> arc.Archive:
    s = Settings(_env_file=None, archive_enabled=True, archive_dir=str(tmp_path / "arc"), **kw)  # type: ignore[call-arg]
    return arc.Archive(s, Stats())


def age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_hard_cap_removes_oldest_files_first_and_stops_at_the_cap(tmp_path: Path) -> None:
    a = make(tmp_path, archive_max_mb=1)
    root = tmp_path / "arc"
    names = []
    for i in range(6):
        p = root / "compact" / "20261008" / f"00000{i}.json.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(300_000))  # incompressible, 6 x 300 kB = 1.7 MB against a 1 MB cap
        age(p, 3600 - 100 * i)  # file 0 is the oldest
        names.append(p)
    a._prune()
    left = sorted(p.name for p in root.rglob("*.gz"))
    assert left == ["000003.json.gz", "000004.json.gz", "000005.json.gz"] and a.size <= 1_048_576


def test_retention_days_apply_per_kind_and_young_files_survive(tmp_path: Path) -> None:
    a = make(tmp_path, archive_compact_keep_days=1, archive_full_keep_days=1, archive_anomaly_keep_days=10)
    root = tmp_path / "arc"
    for kind, days_old in (("compact", 2), ("full", 2), ("anomaly", 2), ("compact", 0.5)):
        p = root / kind / "20261008" / f"x{days_old}.json.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        age(p, days_old * 86400)
    a._prune()
    assert sorted(str(p.relative_to(root)).replace("\\", "/") for p in root.rglob("*.gz")) == [
        "anomaly/20261008/x2.json.gz", "compact/20261008/x0.5.json.gz"]


@pytest.mark.xfail(strict=True, reason="review finding: with the hard cap the oldest file goes first whatever its kind, so "
                   "90-day anomaly evidence is deleted before the newer compact and full files it is meant to explain")
def test_hard_cap_keeps_anomaly_evidence_before_routine_files(tmp_path: Path) -> None:
    a = make(tmp_path, archive_max_mb=1)
    root = tmp_path / "arc"
    for kind, i in (("anomaly", 0), ("compact", 1), ("compact", 2), ("compact", 3), ("compact", 4)):
        p = root / kind / "20261008" / f"0{i}.json.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(300_000))
        age(p, 3600 - 100 * i)
    a._prune()
    assert (root / "anomaly" / "20261008" / "00.json.gz").exists()


@pytest.mark.xfail(strict=True, reason="review finding: write_gz leaves its partial .tmp behind when the write fails "
                   "(disk full), and the leftover holds space until the next prune after 10 minutes")
def test_failed_write_removes_its_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = Path.write_bytes

    def partial(self: Path, data: bytes) -> int:
        real(self, data[:10])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", partial)
    with pytest.raises(OSError):
        arc.write_gz(tmp_path / "d" / "x.json.gz", b"payload" * 100)
    assert not list((tmp_path / "d").glob("*"))


def test_error_poll_submitted_while_busy_is_dropped_and_counted(tmp_path: Path) -> None:
    """Pins the current design: one job at a time, the next submission is dropped. An error poll that lands on a busy
    worker therefore leaves no anomaly file (rare at a 15 s cadence; common on a slow disk)."""
    a = make(tmp_path)
    a._busy = True
    a.submit(b"{}", None, "error", 200, "x")
    assert a.dropped == 1 and not list((tmp_path / "arc").rglob("*"))


def test_future_dated_snapshot_is_flagged_and_not_a_baseline(tmp_path: Path) -> None:
    a = make(tmp_path)
    ts = datetime.now(UTC) + timedelta(minutes=10)
    raw = json.dumps(dict(FIXTURE, general=dict(FIXTURE["general"], update_timestamp=ts.strftime("%Y-%m-%dT%H:%M:%SZ")))).encode()
    a._process(raw, ts.replace(microsecond=0), "ok", 200, None)
    assert a._last_ts is None
    assert any("ts_future" in p.name for p in (tmp_path / "arc").rglob("*.gz"))
    assert gzip.decompress(next((tmp_path / "arc").rglob("*ts_future.json.gz")).read_bytes()) == raw
