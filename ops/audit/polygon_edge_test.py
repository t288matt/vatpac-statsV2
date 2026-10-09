"""Compare the audit's exact-arithmetic point-in-polygon with shapely (what the collector uses) on synthetic points:
vertices, edge points, one-ulp neighbours of edge points, a dense grid and random points. Also checks a polygon with a
hole (shapely only, to document the behaviour the collector would have if the file ever gained a hole).

Read-only, no network. Usage: python polygon_edge_test.py
"""

from __future__ import annotations

import math
import random
from fractions import Fraction
from pathlib import Path

import shapely
from independent import classify, load_ring
from shapely.geometry import Point, Polygon

ROOT = Path(__file__).resolve().parents[2]
ring = load_ring(ROOT / "config/australian_airspace_polygon.json")
import json  # noqa: E402

coords = json.loads((ROOT / "config/australian_airspace_polygon.json").read_text())["coordinates"][0]
poly = Polygon(coords)
shapely.prepare(poly)

stats = {"points": 0, "disagree_contains_xy": 0, "disagree_point_contains": 0, "edge": 0, "inside": 0, "outside": 0}
examples: list[str] = []


def check(lon: float, lat: float, tag: str) -> None:
    mine = classify(ring, lon, lat)
    mine_in = mine == "inside"
    a = bool(shapely.contains_xy(poly, lon, lat))  # exactly what filters.inside_polygon does
    b = poly.contains(Point(lon, lat))
    stats["points"] += 1
    stats[mine] += 1
    if a != mine_in:
        stats["disagree_contains_xy"] += 1
        examples.append(f"{tag} ({lon!r},{lat!r}) mine={mine} contains_xy={a}")
    if b != mine_in:
        stats["disagree_point_contains"] += 1
        examples.append(f"{tag} ({lon!r},{lat!r}) mine={mine} Point.contains={b}")


# 1. every vertex, and its four one-ulp neighbours
for lon, lat in coords[:-1]:
    check(lon, lat, "vertex")
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        check(
            math.nextafter(lon, math.inf if dx > 0 else -math.inf) if dx else lon,
            math.nextafter(lat, math.inf if dy > 0 else -math.inf) if dy else lat,
            "vertex-ulp",
        )

# 2. points on every edge (rational interpolation rounded to float, so some are exactly on the edge, e.g. axis-aligned
#    edges and midpoints) and their one-ulp neighbours in both axes
for (x1, y1), (x2, y2) in zip(coords, coords[1:], strict=False):
    for k in range(1, 20):
        t = Fraction(k, 20)
        lon = float(Fraction(x1) + (Fraction(x2) - Fraction(x1)) * t)
        lat = float(Fraction(y1) + (Fraction(y2) - Fraction(y1)) * t)
        check(lon, lat, "edge")
        for d in (-1, 1):
            check(math.nextafter(lon, d * math.inf), lat, "edge-ulp-x")
            check(lon, math.nextafter(lat, d * math.inf), "edge-ulp-y")

# 3. dense grid at 0.05 degrees over the bounding box plus margin, and 200k random points
for i in range(0, int((170 - 70) / 0.05) + 1):
    lon = 70 + i * 0.05
    for j in range(0, int((0 + 62) / 0.25) + 1):
        check(lon, -62 + j * 0.25, "grid")
rnd = random.Random(30)
for _ in range(200_000):
    check(rnd.uniform(70, 170), rnd.uniform(-62, 0), "random")

# 4. airports named in SPEC and a known outside point
for name, lon, lat in (
    ("YPDN", 130.8727, -12.4147),
    ("YSSY", 151.1753, -33.9461),
    ("NZAA", 174.7917, -37.0082),
    ("WSSS", 103.9915, 1.3502),
    ("equator-0-0", 0.0, 0.0),
):
    check(lon, lat, name)

# 5. a polygon with a hole (not present in the real file): shapely contains excludes the hole interior and hole edge
holed = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)], [[(4, 4), (6, 4), (6, 6), (4, 6)]])
shapely.prepare(holed)
hole_cases = {"in-hole": (5, 5), "on-hole-edge": (4, 5), "in-shell-outside-hole": (2, 2), "on-shell-edge": (0, 5)}
print("hole behaviour (shapely contains_xy):", {k: bool(shapely.contains_xy(holed, *v)) for k, v in hole_cases.items()})

print("shapely", shapely.__version__, "points tested", stats["points"], stats)
print("DISAGREEMENTS:", len(examples))
for e in examples[:20]:
    print("  ", e)
