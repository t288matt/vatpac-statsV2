"""SPEC s4.1 / s7: load_polygon and inside_polygon."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from vatpac.errors import ReferenceDataError
from vatpac.filters import inside_polygon, load_polygon

SQUARE = [[130.0, -20.0], [140.0, -20.0], [140.0, -10.0], [130.0, -10.0], [130.0, -20.0]]


def write(tmp_path: Path, obj: object, name: str = "poly.json") -> Path:
    p = tmp_path / name
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return p


def poly_json(ring: list[list[float]]) -> dict:
    return {"type": "Polygon", "coordinates": [ring]}


# ------------------------------------------------------------------ load_polygon
def test_load_valid_polygon(tmp_path: Path) -> None:
    poly = load_polygon(write(tmp_path, poly_json(SQUARE)))
    assert isinstance(poly, Polygon)
    assert poly.bounds == (130.0, -20.0, 140.0, -10.0)


def test_load_accepts_str_path(tmp_path: Path) -> None:
    assert isinstance(load_polygon(str(write(tmp_path, poly_json(SQUARE)))), Polygon)


def test_load_real_config_polygon(polygon_path: Path) -> None:
    poly = load_polygon(polygon_path)
    assert isinstance(poly, Polygon)
    assert poly.is_valid
    minx, miny, maxx, maxy = poly.bounds
    assert -180 <= minx <= maxx <= 180 and -90 <= miny <= maxy <= 90


def test_load_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ReferenceDataError):
        load_polygon(tmp_path / "nope.json")


@pytest.mark.parametrize(
    "obj",
    [
        {"type": "Point", "coordinates": [135.0, -25.0]},
        {"type": "LineString", "coordinates": SQUARE},
        {"type": "MultiPolygon", "coordinates": [[SQUARE]]},
        {"type": "Feature", "properties": {}, "geometry": poly_json(SQUARE)},
        {"type": "Polygon"},
        {"type": "Polygon", "coordinates": []},
        [],
    ],
    ids=["point", "linestring", "multipolygon", "feature", "no-coords", "empty-coords", "list"],
)
def test_load_not_a_polygon(tmp_path: Path, obj: object) -> None:
    with pytest.raises(ReferenceDataError):
        load_polygon(write(tmp_path, obj))


def test_load_unclosed_ring(tmp_path: Path) -> None:
    with pytest.raises(ReferenceDataError):
        load_polygon(write(tmp_path, poly_json(SQUARE[:-1])))


@pytest.mark.parametrize(
    "bad",
    [[181.0, -15.0], [-180.5, -15.0], [135.0, 90.5], [135.0, -91.0]],
    ids=["lon>180", "lon<-180", "lat>90", "lat<-90"],
)
def test_load_out_of_range_coordinates(tmp_path: Path, bad: list[float]) -> None:
    ring = [[130.0, -20.0], bad, [140.0, -10.0], [130.0, -10.0], [130.0, -20.0]]
    with pytest.raises(ReferenceDataError):
        load_polygon(write(tmp_path, poly_json(ring)))


def test_load_invalid_json_is_reference_error(tmp_path: Path) -> None:
    """SPEC is silent on malformed JSON; failing closed with ReferenceDataError is the only
    sensible reading of 'validated at startup'."""
    with pytest.raises(ReferenceDataError):
        load_polygon(write(tmp_path, "{not json"))


# ------------------------------------------------------------------ inside_polygon (square)
@pytest.fixture()
def square() -> Polygon:
    return Polygon(SQUARE)


def test_inside_square(square: Polygon) -> None:
    assert inside_polygon(square, -15.0, 135.0) is True


def test_outside_square(square: Polygon) -> None:
    assert inside_polygon(square, -15.0, 145.0) is False
    assert inside_polygon(square, 5.0, 135.0) is False


@pytest.mark.parametrize("lat,lon", [(-10.0, 135.0), (-20.0, 135.0), (-15.0, 130.0), (-15.0, 140.0)])
def test_edge_points_are_outside(square: Polygon, lat: float, lon: float) -> None:
    assert inside_polygon(square, lat, lon) is False


@pytest.mark.parametrize("lat,lon", [(-10.0, 130.0), (-20.0, 140.0)])
def test_vertices_are_outside(square: Polygon, lat: float, lon: float) -> None:
    assert inside_polygon(square, lat, lon) is False


@pytest.mark.parametrize(
    "lat,lon", [(None, None), (None, 135.0), (-15.0, None), (None, 500.0)]
)
def test_missing_coordinate_passes(square: Polygon, lat: float | None, lon: float | None) -> None:
    assert inside_polygon(square, lat, lon) is True


def test_lat_lon_order_is_not_swapped(square: Polygon) -> None:
    """Polygon is lon,lat; the function takes (lat, lon). Swapped args would be outside."""
    assert inside_polygon(square, -15.0, 135.0) is True
    assert inside_polygon(square, 135.0, -15.0) is False


# ------------------------------------------------------------------ inside_polygon (real config)
@pytest.fixture(scope="module")
def au(polygon_path: Path) -> Polygon:
    return load_polygon(polygon_path)


@pytest.mark.parametrize(
    "name,lat,lon",
    [
        ("YPDN Darwin", -12.4147, 130.8770),
        ("YSSY Sydney", -33.9461, 151.1772),
        ("YMML Melbourne", -37.6733, 144.8430),
        ("YPPH Perth", -31.9403, 115.9669),
        ("Cocos (Keeling)", -12.1883, 96.8269),
    ],
)
def test_real_polygon_inside(au: Polygon, name: str, lat: float, lon: float) -> None:
    assert inside_polygon(au, lat, lon) is True, name


@pytest.mark.parametrize(
    "name,lat,lon",
    [
        ("NZAA Auckland", -37.0081, 174.7917),
        ("WIII Jakarta", -6.1256, 106.6559),
        ("EGLL London", 51.4700, -0.4543),
        ("KLAX Los Angeles", 33.9425, -118.4081),
    ],
)
def test_real_polygon_outside(au: Polygon, name: str, lat: float, lon: float) -> None:
    assert inside_polygon(au, lat, lon) is False, name


def test_real_polygon_edge_point_is_false(au: Polygon) -> None:
    """(-12.0, 150.0) lies on the horizontal edge from (144,-12) to (155,-12)."""
    assert inside_polygon(au, -12.0, 150.0) is False
    assert inside_polygon(au, -12.0001, 150.0) is True
    assert inside_polygon(au, -11.9999, 150.0) is False


def test_real_polygon_vertex_is_false(au: Polygon) -> None:
    assert inside_polygon(au, -12.0, 155.0) is False
