"""T30 test audit: unit tests added to kill mutants that survived the existing suite (docs/TEST_AUDIT.md)."""
from __future__ import annotations

import pytest

from vatpac.model import parse_frequency_hz


@pytest.mark.parametrize("text", ["0", "0.0", "0.000", "-1.5"])
def test_zero_or_negative_frequency_is_none(text: str) -> None:
    """Mutant C22 (`hz < 0` instead of `hz <= 0`): a frequency of 0.000 must not be stored as 0 Hz."""
    assert parse_frequency_hz(text) is None
