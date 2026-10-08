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
