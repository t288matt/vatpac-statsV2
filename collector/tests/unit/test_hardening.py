"""Collector hardening from the T30 data-integrity audit (GitHub issues 34, 36, 37, 41, 43)."""
from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from vatpac.feed import FetchResult
from vatpac.filters import load_reference
from vatpac.main import Collector
from vatpac.model import parse_snapshot
from vatpac.settings import Settings
from vatpac.store import ApplyResult


class _Feed:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def fetch(self) -> FetchResult:
        return FetchResult(self.payload, 200)


def _collector(settings: Settings, payload: dict[str, Any]) -> Collector:
    repo = Path(__file__).resolve().parents[3]
    ref = load_reference(repo / "config" / "australian_airspace_polygon.json",
                         repo / "config" / "controller_callsigns_list.txt")
    col = Collector(settings, ref, MagicMock(), _Feed(payload))  # type: ignore[arg-type]
    col._log_poll = lambda *_a, **_k: None  # type: ignore[method-assign]
    return col


def _payload(fixture_payload: dict, **pilot_changes: object) -> dict:
    payload = copy.deepcopy(fixture_payload)
    payload["pilots"][0].update(pilot_changes)
    return payload


# issue 37 (F5): an unparsable coordinate is a malformed record, not "no position"
@pytest.mark.parametrize("field", ["latitude", "longitude"])
@pytest.mark.parametrize("bad", ["abc", "", [1], {"a": 1}])
def test_unparsable_coordinate_is_malformed(fixture_payload: dict, field: str, bad: object) -> None:
    snap = parse_snapshot(_payload(fixture_payload, **{field: bad}))
    assert snap.malformed == 1
    assert len(snap.pilots) == len(fixture_payload["pilots"]) - 1
    assert fixture_payload["pilots"][0]["callsign"] not in {p.callsign for p in snap.pilots}


@pytest.mark.parametrize("field", ["latitude", "longitude"])
def test_missing_coordinate_is_still_no_position(fixture_payload: dict, field: str) -> None:
    snap = parse_snapshot(_payload(fixture_payload, **{field: None}))
    assert snap.malformed == 0
    assert getattr(snap.pilots[0], field) is None


def test_numeric_string_coordinate_still_parses(fixture_payload: dict) -> None:
    snap = parse_snapshot(_payload(fixture_payload, latitude="-33.5", longitude=151))
    assert (snap.pilots[0].latitude, snap.pilots[0].longitude) == (-33.5, 151.0)


# issue 36 (F4): values dropped by parsing are counted, not silent
def test_out_of_range_values_are_counted_on_the_record(fixture_payload: dict) -> None:
    clean = parse_snapshot(_payload(fixture_payload)).pilots[0]
    assert clean.rejected == 0
    bad = parse_snapshot(_payload(fixture_payload, altitude=3_000_000_000, heading=400)).pilots[0]
    assert (bad.altitude, bad.heading, bad.rejected) == (None, None, 2)
    junk = parse_snapshot(_payload(fixture_payload, groundspeed="fast", pilot_rating=2**40)).pilots[0]
    assert (junk.groundspeed, junk.pilot_rating, junk.rejected) == (None, None, 2)


def test_absent_values_are_not_rejected(fixture_payload: dict) -> None:
    pilot = parse_snapshot(_payload(fixture_payload, altitude=None, heading=None)).pilots[0]
    assert pilot.rejected == 0


def test_controller_values_dropped_are_counted(fixture_payload: dict) -> None:
    payload = copy.deepcopy(fixture_payload)
    payload["controllers"][0].update(rating=2**40, facility="x", visual_range=None)
    assert parse_snapshot(payload).controllers[0].rejected == 2


# issue 43 (F11): free text is cut to a length cap at ingest and each cut is counted
def _long(fixture_payload: dict) -> dict:
    payload = _payload(fixture_payload, name="N" * 100_000)
    payload["pilots"][0]["flight_plan"] = {**(payload["pilots"][0].get("flight_plan") or {}),
                                           "route": "R" * 5000, "remarks": "M" * 3000, "departure": "YSSY"}
    payload["controllers"][0]["name"] = "C" * 50_000
    return payload


def test_long_text_is_truncated_and_counted(fixture_payload: dict) -> None:
    snap = parse_snapshot(_long(fixture_payload), 2000)
    p, c = snap.pilots[0], snap.controllers[0]
    assert len(p.name or "") == 2000 and len(p.route or "") == 2000 and len(p.remarks or "") == 2000
    assert p.departure == "YSSY" and p.rejected == 3          # three fields cut, the short one untouched
    assert len(c.name or "") == 2000 and c.rejected == 1


def test_default_cap_is_2000_and_a_short_text_is_unchanged(fixture_payload: dict) -> None:
    assert len(parse_snapshot(_long(fixture_payload)).pilots[0].name or "") == 2000
    exact = parse_snapshot(_payload(fixture_payload, name="x" * 2000)).pilots[0]
    assert len(exact.name or "") == 2000 and exact.rejected == 0


def test_cap_is_the_setting(fixture_payload: dict) -> None:
    seen: list[Any] = []
    col = _collector(Settings(text_max_length=150), _long(fixture_payload))
    col._apply = lambda snap, _ref: seen.append(snap) or ApplyResult()  # type: ignore[method-assign]
    asyncio.run(col.poll_once())
    assert len(seen[0].pilots[0].name or "") == 150


def test_cap_setting_bounds() -> None:
    assert Settings().text_max_length == 2000
    with pytest.raises(ValueError):
        Settings(text_max_length=10)


# issue 34 (F2): a snapshot dated well ahead of the wall clock is a feed error and must not freeze ingestion
def _stamped(fixture_payload: dict, offset_s: float) -> dict:
    payload = copy.deepcopy(fixture_payload)
    when = datetime.now(UTC) + timedelta(seconds=offset_s)
    payload["general"]["update_timestamp"] = when.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    return payload


def _poll_sequence(col: Collector, feed: _Feed, payloads: list[dict]) -> tuple[list[str], list[dict], list[Any]]:
    applied: list[Any] = []
    rows: list[dict] = []
    col._apply = lambda snap, _ref: applied.append(snap) or ApplyResult()  # type: ignore[method-assign]
    col._log_poll = lambda status, _ref, **kw: rows.append({"status": status, **kw})  # type: ignore[method-assign]
    statuses = []
    for payload in payloads:
        feed.payload = payload
        statuses.append(asyncio.run(col.poll_once()).status)
    return statuses, rows, applied


def test_future_dated_snapshot_is_an_error_and_does_not_freeze_ingestion(fixture_payload: dict) -> None:
    feed = _Feed(fixture_payload)
    col = _collector(Settings(), fixture_payload)
    col.feed = feed  # type: ignore[assignment]
    future = _stamped(fixture_payload, 3 * 86400)
    genuine = _stamped(fixture_payload, 0)
    statuses, rows, applied = _poll_sequence(col, feed, [future, genuine])
    assert statuses == ["error", "ok"]                       # the old code: ok, then skipped_unchanged
    assert len(applied) == 1 and applied[0].observed_at < datetime.now(UTC) + timedelta(seconds=5)
    assert "ahead of the clock" in rows[0]["error"] and rows[0]["snapshot_ts"] is None
    assert col.last_ts == applied[0].observed_at


@pytest.mark.parametrize(("offset", "expected"),
                         [(-3600, "ok"), (60, "ok"), (110, "ok"), (150, "error"), (600, "error")])
def test_future_bound_defaults_to_120_seconds(fixture_payload: dict, offset: float, expected: str) -> None:
    col = _collector(Settings(), fixture_payload)
    statuses, *_ = _poll_sequence(col, col.feed, [_stamped(fixture_payload, offset)])  # type: ignore[arg-type]
    assert statuses == [expected]


def test_rejected_future_snapshot_reaches_the_archive_with_its_own_timestamp(fixture_payload: dict) -> None:
    """Issue 66: the poll is an error, but the archive must see the feed's timestamp so it can tag ts_future."""
    col = _collector(Settings(), fixture_payload)
    col.archive = MagicMock()
    stamped = _stamped(fixture_payload, 600)
    _poll_sequence(col, col.feed, [stamped])  # type: ignore[arg-type]
    _raw, ts, status, *_ = col.archive.submit.call_args.args
    assert status == "error" and ts is not None and ts > datetime.now(UTC) + timedelta(seconds=500)


def test_future_bound_is_the_setting(fixture_payload: dict) -> None:
    col = _collector(Settings(feed_max_future_seconds=600), fixture_payload)
    assert _poll_sequence(col, col.feed, [_stamped(fixture_payload, 500)])[0] == ["ok"]  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Settings(feed_max_future_seconds=1)
    assert Settings().feed_max_future_seconds == 120
