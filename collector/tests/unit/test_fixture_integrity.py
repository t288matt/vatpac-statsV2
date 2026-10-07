"""The fixture itself must be anonymised and self-consistent. Pure JSON; needs no vatpac code."""
from __future__ import annotations

import re

import pytest
from shapely.geometry import Point, shape

NAME = re.compile(r"^(Pilot|Controller) \d{4}( [A-Z]{4})?$")


def entries(payload: dict):
    return payload["pilots"] + payload["controllers"] + payload["atis"]


def test_names_are_anonymised(fixture_payload: dict) -> None:
    assert all(NAME.match(e["name"]) for e in entries(fixture_payload))


def test_cids_in_reserved_range(fixture_payload: dict) -> None:
    assert all(9_000_000 <= e["cid"] <= 9_999_999 for e in entries(fixture_payload))


def test_remarks_neutral_and_no_free_text(fixture_payload: dict) -> None:
    for p in fixture_payload["pilots"]:
        if p["flight_plan"]:
            assert p["flight_plan"]["remarks"] == "RMK/TEST"
    for c in fixture_payload["controllers"]:
        assert c["text_atis"] is None
    for a in fixture_payload["atis"]:
        assert a["text_atis"] == ["TEST ATIS"]
    assert fixture_payload["prefiles"] == [] and fixture_payload["servers"] == []


def test_sizes(fixture_payload: dict) -> None:
    assert 50 <= len(fixture_payload["pilots"]) <= 70
    assert 20 <= len(fixture_payload["controllers"]) <= 30
    assert len(fixture_payload["atis"]) == 5
    assert fixture_payload["general"]["update_timestamp"].endswith("Z")


def test_name_with_and_without_icao(fixture_payload: dict) -> None:
    names = [p["name"] for p in fixture_payload["pilots"]]
    assert any(re.search(r" [A-Z]{4}$", n) for n in names)
    assert any(not re.search(r" [A-Z]{4}$", n) for n in names)


def test_expected_matches_independent_polygon_check(fixture_payload: dict, expected: dict, polygon_path) -> None:
    import json

    poly = shape(json.loads(polygon_path.read_text(encoding="utf-8")))
    inside = [
        p["callsign"]
        for p in fixture_payload["pilots"]
        if p["latitude"] is None or poly.contains(Point(p["longitude"], p["latitude"]))
    ]
    assert sorted(inside) == expected["pilots_kept_callsigns"]
    assert len(inside) >= 14


def test_edge_pilot_really_on_edge(fixture_payload: dict, polygon_path) -> None:
    import json

    poly = shape(json.loads(polygon_path.read_text(encoding="utf-8")))
    edge = next(p for p in fixture_payload["pilots"] if p["callsign"] == "TESTEDGE")
    pt = Point(edge["longitude"], edge["latitude"])
    assert poly.touches(pt) and not poly.contains(pt)


@pytest.mark.parametrize("cs", ["TESTGS", "TESTALT", "TESTALTLOW", "TESTNOPOS", "TESTNOPLAN1", "TESTNOARR1"])
def test_synthetic_pilots_marked_test(fixture_payload: dict, cs: str) -> None:
    assert any(p["callsign"] == cs for p in fixture_payload["pilots"])
