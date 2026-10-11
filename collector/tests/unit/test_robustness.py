"""Reviewer fixes: string sanitising, health rules, reload rejection, apply timeout, malformed threshold."""
from __future__ import annotations

import asyncio
import copy
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from vatpac import main as vmain
from vatpac.feed import FetchResult
from vatpac.filters import load_reference
from vatpac.health import Stats, health_body
from vatpac.main import Collector
from vatpac.model import _s, parse_snapshot
from vatpac.settings import Settings


def test_s_replaces_lone_surrogates_and_drops_controls() -> None:
    assert _s("ab\ud800cd") == "ab?cd"
    assert _s("a\x00b\x07c") == "abc"
    assert _s("a\tb\nc") == "a b c"
    assert _s("\x00\x01  ") is None
    assert _s("naïve ✈") == "naïve ✈"


def test_surrogate_in_remarks_and_whitespace_callsign(fixture_payload: dict) -> None:
    payload = copy.deepcopy(fixture_payload)
    pilot = payload["pilots"][0]
    pilot["callsign"] = f"  {pilot['callsign']} \n"
    pilot["flight_plan"] = {**(pilot.get("flight_plan") or {}), "remarks": "RMK/\udc80 bad", "route": "A\x00B"}
    parsed = parse_snapshot(payload).pilots[0]
    assert parsed.callsign == pilot["callsign"].strip()
    assert parsed.remarks == "RMK/? bad" and parsed.route == "AB"
    parsed.remarks.encode("utf-8")  # must be valid UTF-8 for jsonb


def test_healthy_needs_fresh_ok_poll_and_fresh_snapshot() -> None:
    now = time.time()
    st = Stats(last_ok_wall=now - 5, last_snapshot_ts=datetime.fromtimestamp(now - 30, UTC))
    assert st.healthy(now)
    st.last_snapshot_ts = datetime.fromtimestamp(now - 130, UTC)  # frozen feed
    assert not st.healthy(now)
    st.last_snapshot_ts, st.last_ok_wall = datetime.fromtimestamp(now - 10, UTC), now - 61
    assert not st.healthy(now)
    assert not Stats(last_ok_wall=now).healthy(now)  # no snapshot stored yet


def test_health_body_reports_reload_state() -> None:
    body = health_body(Stats(reload_rejected=2, last_reload_error="boom"))
    assert body["reload_rejected"] == 2 and body["last_reload_error"] == "boom"
    assert health_body(Stats())["last_reload_error"] is None


def _collector(tmp_path: Path, polygon: Path, whitelist: Path, feed: Any = None) -> Collector:
    settings = Settings(polygon_path=str(polygon), whitelist_path=str(whitelist))
    return Collector(settings, load_reference(polygon, whitelist), MagicMock(), feed or MagicMock())


@pytest.fixture()
def ref_files(tmp_path: Path, polygon_path: Path, whitelist_path: Path) -> tuple[Path, Path]:
    poly = tmp_path / "poly.json"
    wl = tmp_path / "wl.txt"
    poly.write_bytes(polygon_path.read_bytes())
    wl.write_bytes(whitelist_path.read_bytes())
    return poly, wl


def test_bad_reference_file_is_rejected_and_old_data_stays(tmp_path: Path, ref_files: tuple[Path, Path]) -> None:
    poly, wl = ref_files
    col = _collector(tmp_path, poly, wl)
    old = col.ref
    good = poly.read_bytes()
    poly.write_text("{not json", encoding="utf-8")
    asyncio.run(col.reload_reference())
    assert col.ref is old
    assert col.stats.reload_rejected == 1 and col.stats.last_reload_error
    poly.write_bytes(good)  # a later good reload clears the error but keeps the counter
    asyncio.run(col.reload_reference())
    assert col.stats.reload_rejected == 1 and col.stats.last_reload_error is None


def test_whitelist_shrinking_below_70_percent_is_rejected(
    tmp_path: Path, ref_files: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    poly, wl = ref_files
    col = _collector(tmp_path, poly, wl)
    old = col.ref
    kept = sorted(old.whitelist)[: len(old.whitelist) // 2]
    wl.write_text("\n".join(kept) + "\n", encoding="utf-8")
    with caplog.at_level("ERROR", logger="vatpac"):
        asyncio.run(col.reload_reference())
    assert col.ref is old and col.stats.reload_rejected == 1
    dropped = next(r for r in caplog.records if hasattr(r, "dropped")).dropped
    assert len(dropped) == min(20, len(old.whitelist) - len(kept)) and set(dropped) <= old.whitelist - set(kept)
    wl.write_text("\n".join(sorted(old.whitelist)[:-1]) + "\n", encoding="utf-8")  # a small trim is accepted
    asyncio.run(col.reload_reference())
    assert col.ref is not old and len(col.ref.whitelist) == len(old.whitelist) - 1


class _Feed:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def fetch(self) -> FetchResult:
        return FetchResult(self.payload, 200)


def _poll(col: Collector, monkeypatch: pytest.MonkeyPatch) -> tuple[str, dict[str, Any]]:
    seen: dict[str, Any] = {}

    def log_poll(status: str, _ref: Any, **kw: Any) -> None:
        seen.update(kw, status=status)

    monkeypatch.setattr(col, "_log_poll", log_poll)
    outcome = asyncio.run(col.poll_once())
    return outcome.status, seen


def test_stuck_apply_becomes_error_poll(tmp_path: Path, ref_files: tuple[Path, Path], fixture_payload: dict,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    col = _collector(tmp_path, *ref_files, feed=_Feed(fixture_payload))
    monkeypatch.setattr(vmain, "APPLY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(col, "_apply", lambda *_: time.sleep(0.3))
    status, row = _poll(col, monkeypatch)
    assert status == "error" and row["error"] == "apply timed out"
    assert col.stats.polls_error == 1 and col.stats.last_ok_wall is None


def test_malformed_over_20_percent_is_an_error_poll(tmp_path: Path, ref_files: tuple[Path, Path],
                                                    fixture_payload: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = copy.deepcopy(fixture_payload)
    for pilot in payload["pilots"][: len(payload["pilots"]) // 2 + 1]:
        pilot["callsign"] = ""
    col = _collector(tmp_path, *ref_files, feed=_Feed(payload))
    monkeypatch.setattr(col, "_apply", lambda *_: pytest.fail("must not write"))
    status, row = _poll(col, monkeypatch)
    assert status == "error" and "malformed" in row["error"]


def test_few_malformed_entries_are_recorded_in_poll_log(tmp_path: Path, ref_files: tuple[Path, Path],
                                                        fixture_payload: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = copy.deepcopy(fixture_payload)
    payload["pilots"][0]["callsign"] = ""
    col = _collector(tmp_path, *ref_files, feed=_Feed(payload))
    monkeypatch.setattr(col, "_apply", lambda *_: None)
    status, row = _poll(col, monkeypatch)
    assert status == "ok" and row["error"] == "1 malformed feed entries skipped"


def test_skipped_unchanged_does_not_count_as_successful_poll(tmp_path: Path, ref_files: tuple[Path, Path],
                                                             fixture_payload: dict,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    col = _collector(tmp_path, *ref_files, feed=_Feed(fixture_payload))
    col.last_ts = datetime.now(UTC) + timedelta(days=1)  # feed timestamp is not newer
    status, _ = _poll(col, monkeypatch)
    assert status == "skipped_unchanged" and col.stats.polls_skipped == 1 and col.stats.last_ok_wall is None


def test_stalled_feed_retries_at_the_normal_interval(tmp_path: Path, ref_files: tuple[Path, Path],
                                                     fixture_payload: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    col = _collector(tmp_path, *ref_files, feed=_Feed(fixture_payload))
    col.last_ts = datetime.now(UTC) + timedelta(days=1)  # feed timestamp is not newer
    monkeypatch.setattr(col, "_log_poll", lambda *a, **k: None)
    fresh = asyncio.run(col.poll_once())
    assert fresh.retry_in == vmain.UNCHANGED_RETRY_S  # just started: keep the fast retry
    col._last_new_at -= col.settings.poll_interval_seconds  # nothing new for a full interval
    stalled = asyncio.run(col.poll_once())
    assert stalled.retry_in == col.settings.poll_interval_seconds


def test_pool_options_set_timeouts() -> None:
    assert "statement_timeout=20000" in vmain.DB_OPTIONS and "lock_timeout=5000" in vmain.DB_OPTIONS
    assert "idle_in_transaction_session_timeout=30000" in vmain.DB_OPTIONS
