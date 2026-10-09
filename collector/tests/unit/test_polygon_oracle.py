"""Issue #54: an independent point-in-polygon oracle.

The production filter (`vatpac.filters.inside_polygon`) uses shapely, and `tests/fixtures/expected.json` was made with
shapely too, so a shapely behaviour change, or a wrong use of it (swapped arguments, `covers` for `contains`), would
move production and its expected numbers together. This file holds a second implementation that does not use shapely:
a ray-casting test in exact rational arithmetic, written from the SPEC rule (a pilot with no position is kept; a point
strictly inside is kept; a point on an edge or a vertex is outside). The production filter is compared with it on the
fixture, on every vertex and edge, and on a few thousand generated points, many of them within a hair of an edge.
"""
from __future__ import annotations

import json
import random
from fractions import Fraction
from pathlib import Path

import pytest
import shapely

from vatpac.filters import inside_polygon, load_polygon

Ring = list[tuple[Fraction, Fraction]]


def load_ring(path: Path) -> Ring:
    geo = json.loads(path.read_text(encoding="utf-8"))
    assert geo["type"] == "Polygon" and len(geo["coordinates"]) == 1  # one ring, no holes: the oracle does not do holes
    ring = [(Fraction(x), Fraction(y)) for x, y in geo["coordinates"][0]]
    assert ring[0] == ring[-1]
    return ring


def locate(ring: Ring, lon: float, lat: float) -> str:
    """'inside', 'edge' or 'outside' by exact arithmetic. x is longitude, y is latitude. Crossing-number test: count the
    edges that a ray from the point towards +x crosses; an edge counts when exactly one of its ends is above the ray."""
    px, py = Fraction(lon), Fraction(lat)
    crossings = 0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False):
        on_line = (x2 - x1) * (py - y1) == (y2 - y1) * (px - x1)
        if on_line and min(x1, x2) <= px <= max(x1, x2) and min(y1, y2) <= py <= max(y1, y2):
            return "edge"
        if (y1 > py) != (y2 > py):
            x_at_ray = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if px < x_at_ray:
                crossings += 1
    return "inside" if crossings % 2 == 1 else "outside"


def kept(ring: Ring, lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return True
    return locate(ring, lon, lat) == "inside"


@pytest.fixture(scope="module")
def ring(polygon_path: Path) -> Ring:
    return load_ring(polygon_path)


@pytest.fixture(scope="module")
def production(polygon_path: Path):
    return load_polygon(polygon_path)


SQUARE: Ring = [(Fraction(x), Fraction(y)) for x, y in [(130, -20), (140, -20), (140, -10), (130, -10), (130, -20)]]
L_SHAPE: Ring = [(Fraction(x), Fraction(y)) for x, y in [(0, 0), (4, 0), (4, 2), (2, 2), (2, 4), (0, 4), (0, 0)]]


# ------------------------------------------------------------------ the oracle checks itself first
@pytest.mark.parametrize("ring_,lon,lat,want", [
    (SQUARE, 135.0, -15.0, "inside"), (SQUARE, 145.0, -15.0, "outside"), (SQUARE, 135.0, -5.0, "outside"),
    (SQUARE, 135.0, -10.0, "edge"), (SQUARE, 130.0, -15.0, "edge"), (SQUARE, 140.0, -20.0, "edge"),  # sides
    (SQUARE, 130.0, -10.0, "edge"), (SQUARE, 140.0, -20.0, "edge"),                                     # corners
    (SQUARE, 129.9999999, -15.0, "outside"), (SQUARE, 135.0, -10.0000001, "inside"),
    (L_SHAPE, 1.0, 3.0, "inside"), (L_SHAPE, 3.0, 1.0, "inside"), (L_SHAPE, 3.0, 3.0, "outside"),  # the notch of the L
    (L_SHAPE, 2.0, 3.0, "edge"), (L_SHAPE, 3.0, 2.0, "edge"), (L_SHAPE, 2.0, 2.0, "edge"),            # reflex corner
    (L_SHAPE, -1.0, 2.0, "outside"), (L_SHAPE, 5.0, 1.0, "outside"),
])
def test_the_oracle_is_right_on_shapes_with_known_answers(ring_: Ring, lon: float, lat: float, want: str) -> None:
    assert locate(ring_, lon, lat) == want


def test_the_oracle_is_right_on_known_places(ring: Ring) -> None:
    for name, lat, lon, want in [
        ("YPDN Darwin", -12.4147, 130.8770, "inside"), ("YSSY Sydney", -33.9461, 151.1772, "inside"),
        ("YPPH Perth", -31.9403, 115.9669, "inside"), ("Cocos (Keeling)", -12.1883, 96.8269, "inside"),
        ("NZAA Auckland", -37.0081, 174.7917, "outside"), ("WIII Jakarta", -6.1256, 106.6559, "outside"),
        ("EGLL London", 51.4700, -0.4543, "outside"), ("KLAX Los Angeles", 33.9425, -118.4081, "outside"),
        ("on the horizontal edge", -12.0, 150.0, "edge"), ("vertex", -12.0, 155.0, "edge"),
    ]:
        assert locate(ring, lon, lat) == want, name


# ------------------------------------------------------------------ the fixture
def test_oracle_and_production_agree_on_every_fixture_pilot(fixture_payload: dict, ring: Ring, production) -> None:
    for p in fixture_payload["pilots"]:
        lat, lon = p["latitude"], p["longitude"]
        assert kept(ring, lat, lon) == inside_polygon(production, lat, lon), p["callsign"]


def test_oracle_reproduces_the_expected_fixture_numbers(fixture_payload: dict, expected: dict, ring: Ring) -> None:
    """expected.json came from shapely; the oracle reaches the same callsigns without it."""
    callsigns = sorted(p["callsign"] for p in fixture_payload["pilots"] if kept(ring, p["latitude"], p["longitude"]))
    assert callsigns == expected["pilots_kept_callsigns"]
    on_edge = sorted(p["callsign"] for p in fixture_payload["pilots"]
                     if p["latitude"] is not None and locate(ring, p["longitude"], p["latitude"]) == "edge")
    assert on_edge == sorted(expected["pilots_on_polygon_edge"])


# ------------------------------------------------------------------ generated points
def edge_points(ring: Ring) -> list[tuple[float, float, str]]:
    """Every vertex, and every edge midpoint that is exactly representable as a float (so it is exactly on the edge)."""
    pts = [(float(x), float(y), "vertex") for x, y in ring[:-1]]
    for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False):
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        if Fraction(float(mx)) == mx and Fraction(float(my)) == my:
            pts.append((float(mx), float(my), "midpoint"))
    return pts


def test_vertices_and_exact_edge_midpoints_are_outside_in_both(ring: Ring, production) -> None:
    pts = edge_points(ring)
    assert len(pts) >= 30
    for lon, lat, what in pts:
        assert locate(ring, lon, lat) == "edge", (what, lon, lat)
        assert inside_polygon(production, lat, lon) is False, (what, lon, lat)


def test_production_matches_the_oracle_next_to_every_vertex_and_edge(ring: Ring, production) -> None:
    n_inside = n_outside = 0
    for lon, lat, what in edge_points(ring):
        for d in (1e-9, 1e-7, 1e-4, 0.05):
            for dx, dy in ((d, 0), (-d, 0), (0, d), (0, -d), (d, d), (-d, -d), (d, -d), (-d, d)):
                x, y = lon + dx, lat + dy
                want = locate(ring, x, y)
                n_inside += want == "inside"
                n_outside += want == "outside"
                assert inside_polygon(production, y, x) == (want == "inside"), (what, x, y, want)
    assert n_inside > 100 and n_outside > 100  # the sweep really straddles the boundary


def test_production_matches_the_oracle_on_random_points(ring: Ring, production) -> None:
    rng = random.Random(20261009)
    xs, ys = [x for x, _ in ring], [y for _, y in ring]
    lo_x, hi_x, lo_y, hi_y = float(min(xs)), float(max(xs)), float(min(ys)), float(max(ys))
    counts = {"inside": 0, "outside": 0, "edge": 0}
    for _ in range(4000):
        x = rng.uniform(lo_x - 5, hi_x + 5)
        y = rng.uniform(lo_y - 5, hi_y + 5)
        want = locate(ring, x, y)
        counts[want] += 1
        assert inside_polygon(production, y, x) == (want == "inside"), (x, y, want)
    assert counts["inside"] > 500 and counts["outside"] > 500


def test_production_matches_the_oracle_within_a_hair_of_the_edges(ring: Ring, production) -> None:
    """Random points on each edge, pushed sideways by 1e-12 to 1e-6 degrees, both ways."""
    rng = random.Random(7)
    for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False):
        for _ in range(60):
            t = rng.random()
            x, y = float(x1 + (x2 - x1) * Fraction(t)), float(y1 + (y2 - y1) * Fraction(t))
            for push in (1e-12, 1e-9, 1e-6):
                for sx, sy in ((push, 0), (-push, 0), (0, push), (0, -push)):
                    px, py = x + sx, y + sy
                    want = locate(ring, px, py)
                    assert inside_polygon(production, py, px) == (want == "inside"), (px, py, want)


def test_a_pilot_with_no_position_is_kept_by_both(ring: Ring, production) -> None:
    for lat, lon in [(None, None), (None, 135.0), (-15.0, None)]:
        assert kept(ring, lat, lon) is True and inside_polygon(production, lat, lon) is True


# ------------------------------------------------------------------ the oracle tells a wrong filter from the right one
def test_the_oracle_notices_edges_counted_as_inside(ring: Ring, polygon_path: Path) -> None:
    """Mutant C01 of docs/TEST_AUDIT.md (`contains` replaced by `covers`): edge points become inside."""
    poly = shapely.Polygon([(float(x), float(y)) for x, y in ring])
    wrong = [(lon, lat) for lon, lat, _ in edge_points(ring) if shapely.covers(poly, shapely.Point(lon, lat))]
    assert wrong and all(locate(ring, lon, lat) == "edge" for lon, lat in wrong)


def test_the_oracle_notices_swapped_latitude_and_longitude(ring: Ring, production) -> None:
    """Mutant C03 (arguments swapped): the Australian points disagree with the oracle."""
    disagree = [(lon, lat) for lon, lat in [(151.1772, -33.9461), (130.877, -12.4147), (115.9669, -31.9403)]
                if inside_polygon(production, lon, lat) != kept(ring, lat, lon)]
    assert len(disagree) == 3
