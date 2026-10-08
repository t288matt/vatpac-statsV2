"""Collector hardening from the T30 data-integrity audit (GitHub issues 34, 36, 37, 41, 43)."""
from __future__ import annotations

import copy

import pytest

from vatpac.model import parse_snapshot


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
