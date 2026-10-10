"""SPEC s5 / s7: parse_frequency_hz, home_icao, clamp_pilot."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from vatpac.model import MAX_INDEXED_BYTES, _pilot, cap_text, clamp_pilot, home_icao, parse_frequency_hz


# ------------------------------------------------------------------ parse_frequency_hz
@pytest.mark.parametrize(
    "text,hz",
    [
        ("121.700", 121_700_000),
        ("118.050", 118_050_000),
        ("118.275", 118_275_000),
        ("134.600", 134_600_000),
        ("123.450", 123_450_000),
        ("121.7", 121_700_000),
        ("136.975", 136_975_000),
        ("108.000", 108_000_000),
    ],
)
def test_frequency_conversion(text: str, hz: int) -> None:
    result = parse_frequency_hz(text)
    assert result == hz and isinstance(result, int)


def test_frequency_conversion_is_exact_for_every_whitelist_frequency(whitelist_path: Path) -> None:
    """Guards against float truncation (e.g. 123.45 * 1e6 = 123450000.00000001)."""
    checked = 0
    for line in whitelist_path.read_text(encoding="utf-8").splitlines():
        if "," not in line:
            continue
        text = line.split(",", 1)[1].strip()
        assert parse_frequency_hz(text) == int(Decimal(text) * 1_000_000), text
        checked += 1
    assert checked > 100


def test_frequency_199_998_means_none() -> None:
    assert parse_frequency_hz("199.998") is None


@pytest.mark.parametrize("bad", [None, "", "   ", "abc", "12x.5", "121,700", "nan", "--1",
                                 "inf", "Infinity", "-Infinity", "-inf", float("inf"),
                                 "1e999999", "1E+999999999", "-1e999999", "-1E+999999999"])
def test_frequency_invalid_is_none(bad: str | float | None) -> None:
    assert parse_frequency_hz(bad) is None


# ------------------------------------------------------------------ cap_text (byte cap on indexed text, issue 111)
def _pilot_with(**over: object) -> object:
    raw = {"cid": 1, "name": "N", "callsign": "QFA1", "latitude": 1.0, "longitude": 2.0, "altitude": 0,
           "groundspeed": 0, "logon_time": "2026-10-10T14:00:00Z",
           "flight_plan": {"departure": "YSSY", "arrival": "YMML"}}
    raw.update(over)
    return _pilot(raw)


def test_indexed_text_is_capped_by_utf8_bytes_and_counted_once() -> None:
    p = _pilot_with(callsign="漢" * 2000)
    out = cap_text(p, 2000)
    assert 0 < len(out.callsign.encode()) <= MAX_INDEXED_BYTES
    assert set(out.callsign) == {"漢"}  # cut on a character boundary, no replacement characters
    assert out.rejected == p.rejected + 1


def test_indexed_text_within_the_byte_cap_is_unchanged() -> None:
    p = _pilot_with(callsign="QFA1", name="漢" * 1000)  # non-indexed text keeps the character cap only
    assert cap_text(p, 2000) is p


def test_departure_and_arrival_are_byte_capped_too() -> None:
    p = _pilot_with(flight_plan={"departure": "漢" * 900, "arrival": "é" * 900})
    out = cap_text(p, 2000)
    assert len(out.departure.encode()) <= MAX_INDEXED_BYTES and len(out.arrival.encode()) <= MAX_INDEXED_BYTES
    assert out.rejected == p.rejected + 2


# ------------------------------------------------------------------ home_icao
@pytest.mark.parametrize(
    "name,expected",
    [
        ("Pilot 1001 YSSY", "YSSY"),
        ("1721144 KMIA", "KMIA"),
        ("Pilot 1001 ABCD EFGH", "EFGH"),
        ("Pilot 1002", None),
        ("Pilot 1001 yssy", None),
        ("Pilot 1001 YSS", None),
        ("Pilot 1001 YSSYX", None),
        ("Pilot 1001 Y55Y", None),
        ("Pilot 1001 YSSY-", None),
        ("Pilot YSSY 1001", None),
        ("YSSY", None),  # SPEC s6: the token must follow a space
        ("", None),
        (None, None),
    ],
)
def test_home_icao(name: str | None, expected: str | None) -> None:
    assert home_icao(name) == expected


# ------------------------------------------------------------------ clamp_pilot
@pytest.mark.parametrize(
    "gs,alt",
    [(0, 0), (700, 65000), (0, -1500), (350, 35000), (1, -1), (699, 64999)],
)
def test_clamp_in_range_unchanged(gs: int, alt: int) -> None:
    assert clamp_pilot(gs, alt) == (gs, alt, 0)


@pytest.mark.parametrize("gs", [701, 30702, -1, 100000])
def test_clamp_groundspeed_rejected(gs: int) -> None:
    assert clamp_pilot(gs, 10000) == (None, 10000, 1)


@pytest.mark.parametrize("alt", [65001, -1501, -5000, 1_000_000])
def test_clamp_altitude_rejected(alt: int) -> None:
    assert clamp_pilot(250, alt) == (250, None, 1)


def test_clamp_both_rejected_counts_two() -> None:
    assert clamp_pilot(30702, -5000) == (None, None, 2)


def test_clamp_negative_altitude_within_cap_is_kept() -> None:
    """-157 ft is plausible: only below -1500 is rejected."""
    assert clamp_pilot(100, -157) == (100, -157, 0)


def test_clamp_none_is_not_a_rejection() -> None:
    assert clamp_pilot(None, None) == (None, None, 0)
    assert clamp_pilot(None, 5000) == (None, 5000, 0)
    assert clamp_pilot(250, None) == (250, None, 0)
    assert clamp_pilot(30702, None) == (None, None, 1)
