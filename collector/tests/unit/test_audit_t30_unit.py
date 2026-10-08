"""T30 test audit: unit tests added to kill mutants that survived the existing suite (docs/TEST_AUDIT.md)."""
from __future__ import annotations

import pytest

from vatpac.model import parse_frequency_hz


@pytest.mark.parametrize("text", ["0", "0.0", "0.000", "-1.5"])
def test_zero_or_negative_frequency_is_none(text: str) -> None:
    """Mutant C22 (`hz < 0` instead of `hz <= 0`): a frequency of 0.000 must not be stored as 0 Hz."""
    assert parse_frequency_hz(text) is None


def _payload(fixture_payload: dict) -> dict:
    import copy

    return copy.deepcopy(fixture_payload)


def test_heading_of_360_is_kept(fixture_payload: dict) -> None:
    """Mutant C24 (`h < 360`): the database allows 0..360 and the feed can report 360."""
    from vatpac.model import parse_snapshot

    p = _payload(fixture_payload)
    p["pilots"][0]["heading"] = 360
    snap = parse_snapshot(p)
    assert next(x for x in snap.pilots if x.cid == p["pilots"][0]["cid"]).heading == 360


def test_global_pilot_count_includes_malformed_entries(fixture_payload: dict) -> None:
    """Mutant C25: pilots_global is the feed's own count (network-wide), whatever the collector could parse."""
    from vatpac.model import parse_snapshot

    p = _payload(fixture_payload)
    broken = dict(p["pilots"][0])
    del broken["callsign"]
    p["pilots"].append(broken)
    snap = parse_snapshot(p)
    assert snap.malformed == 1 and snap.pilots_global == len(p["pilots"])


def test_update_timestamp_with_an_offset_is_converted_to_utc(fixture_payload: dict) -> None:
    """Mutant C26 (offset ignored and the wall-clock time labelled UTC)."""
    from datetime import UTC, datetime

    from vatpac.model import parse_snapshot

    p = _payload(fixture_payload)
    p["general"]["update_timestamp"] = "2026-10-07T18:11:19+02:00"
    assert parse_snapshot(p).observed_at == datetime(2026, 10, 7, 16, 11, 19, tzinfo=UTC)
