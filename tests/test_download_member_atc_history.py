"""ops/download_member_atc_history.py (issue 379) against a fake VATSIM API, a fake clock and a fake ssh. Nothing real is called or slept."""
from __future__ import annotations

import csv
import importlib.util
import json
import random
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ops"))
spec = importlib.util.spec_from_file_location("download_member_atc_history", REPO / "ops" / "download_member_atc_history.py")
dl = importlib.util.module_from_spec(spec)
sys.modules["download_member_atc_history"] = dl
spec.loader.exec_module(dl)


def sess(i: int, rating: int = 3) -> dict[str, Any]:
    return {"connection_id": {"id": i, "vatsim_id": 1, "rating": rating, "start": f"2024-01-{1 + i % 28:02d}T10:00:00",
                              "end": f"2024-01-{1 + i % 28:02d}T11:00:00"}, "aircrafttracked": i}


class FakeApi:
    def __init__(self, histories: dict[int, list[dict[str, Any]]], stop_at_call: int | None = None, cap: int = 100) -> None:
        self.histories, self.stop_at_call, self.cap, self.urls = histories, stop_at_call, cap, []

    def __call__(self, url: str) -> Any:
        self.urls.append(url)
        if self.stop_at_call is not None and len(self.urls) >= self.stop_at_call:
            raise dl.ApiStop("HTTP 429")
        cid = int(url.split("/members/")[1].split("/")[0])
        if cid not in self.histories:
            raise dl.ApiNotFound(url)
        off = int(url.split("offset=")[1])
        return {"items": self.histories[cid][off:off + self.cap], "count": len(self.histories[cid])}


class Clock:
    """A fake clock: sleep() moves time forward and records the pause."""

    def __init__(self) -> None:
        self.t = datetime(2026, 11, 16, 8, 0, tzinfo=UTC)
        self.pauses: list[float] = []

    def now(self) -> datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.pauses.append(s)
        self.t += timedelta(seconds=s)


def go(argv: list[str], api: FakeApi, out: Path, ssh: Any = None, clock: Clock | None = None) -> int:
    c = clock or Clock()
    return dl.main(["--out", str(out), *argv], fetch=api, sleep=c.sleep, now=c.now, ssh=ssh or (lambda h, cmd: ""),
                   rng=random.Random(1))


def manifest(out: Path) -> list[dict[str, str]]:
    with (out / "manifest.csv").open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_pages_until_count_with_cap_of_100(tmp_path: Path) -> None:
    items = [sess(i) for i in range(250)]
    api = FakeApi({1: items})
    assert go(["--cid", "1"], api, tmp_path) == 0
    assert len(api.urls) == 3 and "offset=200" in api.urls[2]
    data = json.loads((tmp_path / "1.json").read_text(encoding="utf-8"))
    assert data["cid"] == 1 and data["count"] == 250 and data["sessions"] == items
    row = manifest(tmp_path)[0]
    assert row["sessions"] == "250" and row["status"] == "ok" and row["ratings_seen"] == "3"
    assert (tmp_path / "README.txt").exists()


def test_empty_page_stops(tmp_path: Path) -> None:
    api = FakeApi({1: []})
    assert go(["--cid", "1"], api, tmp_path) == 0
    assert len(api.urls) == 1 and json.loads((tmp_path / "1.json").read_text())["count"] == 0


def test_404_gives_manifest_row_and_no_file(tmp_path: Path) -> None:
    assert go(["--cid", "9"], FakeApi({}), tmp_path) == 0
    assert not (tmp_path / "9.json").exists()
    assert manifest(tmp_path)[0]["status"] == "not_found" and manifest(tmp_path)[0]["cid"] == "9"


def test_429_stops_keeps_files_and_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeApi({1: [sess(1)], 2: [sess(2)], 3: [sess(3)]}, stop_at_call=2)
    assert go(["--cid", "1", "--cid", "2", "--cid", "3"], api, tmp_path) == 3
    assert (tmp_path / "1.json").exists() and not (tmp_path / "2.json").exists() and not (tmp_path / "3.json").exists()
    assert "run the same command again" in capsys.readouterr().out
    assert [r["cid"] for r in manifest(tmp_path)] == ["1"]


def test_resume_skips_existing_and_refresh_overwrites(tmp_path: Path) -> None:
    api = FakeApi({1: [sess(1)]})
    go(["--cid", "1"], api, tmp_path)
    assert len(api.urls) == 1
    go(["--cid", "1"], api, tmp_path)
    assert len(api.urls) == 1  # skipped
    api.histories[1] = [sess(1), sess(2)]
    go(["--cid", "1", "--refresh"], api, tmp_path)
    assert len(api.urls) == 2 and json.loads((tmp_path / "1.json").read_text())["count"] == 2


def test_write_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    go(["--cid", "1"], FakeApi({1: [sess(1)]}), tmp_path)
    good = (tmp_path / "1.json").read_text()

    def boom(src: Any, dst: Any) -> None:
        raise OSError("disk")

    monkeypatch.setattr(dl.os, "replace", boom)
    with pytest.raises(OSError):
        go(["--cid", "1", "--refresh"], FakeApi({1: [sess(1), sess(2)]}), tmp_path)
    assert (tmp_path / "1.json").read_text() == good  # the old file is untouched, never half-written


def test_cids_file_parsing_and_dedupe(tmp_path: Path) -> None:
    f = tmp_path / "cids.txt"
    f.write_text("# header\n5\n\n  6  # note\n5\n", encoding="utf-8")
    assert dl.parse_cids(f.read_text()) == [5, 6, 5]
    api = FakeApi({5: [sess(1)], 6: [sess(2)]})
    assert go(["--cids-file", str(f)], api, tmp_path / "o") == 0
    assert len(api.urls) == 2


def test_from_vm_uses_ssh_host_and_read_only_command(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    def ssh(host: str, cmd: str) -> str:
        seen.append((host, cmd))
        return "7\n8\n"

    api = FakeApi({7: [sess(1)], 8: [sess(2)]})
    assert go(["--from-vm", "--ssh-host", "myhost"], api, tmp_path, ssh) == 0
    assert seen[0][0] == "myhost" and "default_transaction_read_only=on" in seen[0][1] and "controller_session" in seen[0][1]
    assert (tmp_path / "7.json").exists() and (tmp_path / "8.json").exists()


def cids_file(tmp_path: Path, n: int) -> Path:
    f = tmp_path / "c.txt"
    f.write_text("\n".join(str(i) for i in range(1, n + 1)), encoding="utf-8")
    return f


def test_limit_default_is_25_and_explicit_limit(tmp_path: Path) -> None:
    api = FakeApi({i: [sess(i)] for i in range(1, 31)})
    go(["--cids-file", str(cids_file(tmp_path, 30))], api, tmp_path / "o")
    assert len(list((tmp_path / "o").glob("*.json"))) == 25
    api2 = FakeApi({i: [sess(i)] for i in range(1, 6)})
    go(["--cids-file", str(cids_file(tmp_path, 5)), "--limit", "2"], api2, tmp_path / "p")
    assert sorted(p.name for p in (tmp_path / "p").glob("*.json")) == ["1.json", "2.json"]


def test_dry_run_calls_nothing_and_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeApi({1: [sess(1)]})
    out = tmp_path / "o"
    assert go(["--cid", "1", "--dry-run"], api, out) == 0
    assert api.urls == [] and not out.exists()
    assert "1: would download" in capsys.readouterr().out


# ---- pacing ----

def test_parse_duration() -> None:
    assert dl.parse_duration("90m") == 5400 and dl.parse_duration("6h") == 21600
    assert dl.parse_duration("2d") == 172800 and dl.parse_duration("30s") == 30 and dl.parse_duration("1.5h") == 5400
    for bad in ("", "90", "m", "-5m", "0h", "2 weeks", "1x"):
        with pytest.raises(ValueError):
            dl.parse_duration(bad)


def test_bad_duration_exits_2(tmp_path: Path) -> None:
    assert go(["--cid", "1", "--spread-over", "soon"], FakeApi({1: []}), tmp_path) == 2


def test_spread_pause_calculation() -> None:
    assert dl.spread_pause(1000, 10) == 100
    assert dl.spread_pause(20, 10) == 5  # too short: the floor of 5 seconds
    assert dl.spread_pause(100, 0) == 100  # no calls left does not divide by zero


def test_jitter_stays_within_30_percent_and_varies() -> None:
    rng = random.Random(7)
    values = [dl.jittered(10.0, rng) for _ in range(500)]
    assert all(7.0 <= v <= 13.0 for v in values)
    assert len({round(v, 3) for v in values}) > 100 and max(values) > 12 and min(values) < 8


def test_default_pause_is_5_seconds_with_jitter_between_every_call_including_pages(tmp_path: Path) -> None:
    clock = Clock()
    api = FakeApi({1: [sess(i) for i in range(250)], 2: [sess(1)]})  # 3 pages + 1 page = 4 calls, so 3 pauses
    assert go(["--cid", "1", "--cid", "2"], api, tmp_path, clock=clock) == 0
    assert len(api.urls) == 4 and len(clock.pauses) == 3  # none after the last call
    assert all(3.5 <= p <= 6.5 for p in clock.pauses)


def test_spread_over_fills_the_period_and_stays_in_bounds(tmp_path: Path) -> None:
    clock = Clock()
    n = 40
    api = FakeApi({i: [sess(i)] for i in range(1, n + 1)})
    assert go(["--cids-file", str(cids_file(tmp_path, n)), "--spread-over", "2h"], api, tmp_path / "o", clock=clock) == 0
    assert len(list((tmp_path / "o").glob("*.json"))) == n  # no --limit: all pending
    elapsed = (clock.t - datetime(2026, 11, 16, 8, 0, tzinfo=UTC)).total_seconds()
    assert 0.8 * 7200 <= elapsed <= 1.05 * 7200  # recomputed as it goes, so close to the period
    assert all(0.7 * 5 <= p for p in clock.pauses) and max(clock.pauses) <= 1.3 * 7200 / (n - 1) * 1.6


def test_spread_too_short_uses_5_seconds_and_says_so(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = Clock()
    api = FakeApi({i: [sess(i)] for i in range(1, 11)})
    assert go(["--cids-file", str(cids_file(tmp_path, 10)), "--spread-over", "10s"], api, tmp_path / "o", clock=clock) == 0
    assert "too short" in capsys.readouterr().out
    assert all(p >= 3.5 for p in clock.pauses)  # 5 seconds less the 30 percent jitter
    assert (clock.t - datetime(2026, 11, 16, 8, 0, tzinfo=UTC)).total_seconds() >= 9 * 3.5


def test_low_explicit_sleep_warns_and_is_honoured(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = Clock()
    api = FakeApi({1: [sess(1)], 2: [sess(2)]})
    assert go(["--cid", "1", "--cid", "2", "--sleep", "1"], api, tmp_path, clock=clock) == 0
    assert "WARNING" in capsys.readouterr().out and all(0.7 <= p <= 1.3 for p in clock.pauses)


def test_expected_finish_is_printed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    go(["--cids-file", str(cids_file(tmp_path, 3)), "--dry-run"], FakeApi({}), tmp_path / "o")
    out = capsys.readouterr().out
    assert "Expected finish" in out and "UTC" in out and "local" in out and "16-11-2026 08:00" in out  # 3 calls x 5 s, same minute


def test_ctrl_c_finishes_the_file_in_progress_then_stops(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    clock = Clock()
    api = FakeApi({1: [sess(i) for i in range(250)], 2: [sess(2)]})

    def fetch(url: str) -> Any:
        if len(api.urls) == 1:
            signal.raise_signal(signal.SIGINT)  # Ctrl+C during the second page of controller 1
        return api(url)

    rc = dl.main(["--out", str(tmp_path), "--cid", "1", "--cid", "2"], fetch=fetch, sleep=clock.sleep, now=clock.now,
                 rng=random.Random(1), handle_ctrl_c=True)
    assert rc == 130
    assert json.loads((tmp_path / "1.json").read_text())["count"] == 250 and not (tmp_path / "2.json").exists()
    assert len(clock.pauses) == 2  # the pauses between the pages stay; none before the next controller
    assert "Run the same command again" in capsys.readouterr().out
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
