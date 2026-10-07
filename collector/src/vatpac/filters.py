"""The ingest filters (pilot polygon, controller whitelist), reference loading and near-misses."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import shapely
from shapely.geometry import Polygon

from .errors import ReferenceDataError

log = logging.getLogger(__name__)

_CALLSIGN_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def load_polygon(path: str | Path) -> Polygon:
    """Load a GeoJSON Polygon (lon,lat order) and prepare it for fast point tests."""
    try:
        geo = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReferenceDataError(f"polygon file {path} unreadable: {exc}") from exc
    if not isinstance(geo, dict) or geo.get("type") != "Polygon":
        raise ReferenceDataError(f"polygon file {path} is not a GeoJSON Polygon")
    try:
        rings = [[(float(x), float(y)) for x, y in ring] for ring in geo["coordinates"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise ReferenceDataError(f"polygon file {path} has malformed coordinates") from exc
    if not rings or any(len(r) < 4 or r[0] != r[-1] for r in rings):
        raise ReferenceDataError(f"polygon file {path}: rings must be closed with at least 4 points")
    if any(not (-180 <= x <= 180 and -90 <= y <= 90) for r in rings for x, y in r):
        raise ReferenceDataError(f"polygon file {path}: coordinates outside lon -180..180 / lat -90..90")
    polygon = Polygon(rings[0], rings[1:])
    if polygon.is_empty or not polygon.is_valid:
        raise ReferenceDataError(f"polygon file {path} is not a valid polygon")
    shapely.prepare(polygon)
    return polygon


def inside_polygon(polygon: Polygon, lat: float | None, lon: float | None) -> bool:
    """True if inside. A pilot with no position passes (v1 behaviour). Edge points are outside."""
    if lat is None or lon is None:
        return True
    return bool(shapely.contains_xy(polygon, lon, lat))


def load_whitelist(path: str | Path) -> frozenset[str]:
    """Lines `CALLSIGN, FREQ` or bare `CALLSIGN`; blanks and `#` comments skipped. Fails closed."""
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ReferenceDataError(f"whitelist file {path} missing or unreadable: {exc}") from exc
    seen: set[str] = set()
    duplicates: list[str] = []
    for n, line in enumerate(lines, 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        callsign = text.split(",", 1)[0].strip()
        if not _CALLSIGN_RE.match(callsign):
            log.warning("whitelist line skipped (malformed)", extra={"line": n})
            continue
        if callsign in seen:
            duplicates.append(callsign)
        seen.add(callsign)
    if not seen:
        raise ReferenceDataError(f"whitelist file {path} contains no callsigns")
    if duplicates:
        log.warning("whitelist has duplicate callsigns", extra={"duplicates": sorted(set(duplicates))})
    return frozenset(seen)


def is_near_miss(callsign: str, whitelist: frozenset[str]) -> bool:
    """Unlisted controller whose prefix (text before the first `_`) matches a listed prefix."""
    if callsign in whitelist or callsign.endswith("_OBS") or "_" not in callsign:
        return False
    return callsign.split("_", 1)[0] in {c.split("_", 1)[0] for c in whitelist if "_" in c}


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class ReferenceData:
    polygon: Polygon
    whitelist: frozenset[str]
    polygon_sha: str
    whitelist_sha: str


def load_reference(polygon_path: str | Path, whitelist_path: str | Path) -> ReferenceData:
    """Load and validate both files. Raises ReferenceDataError if either is bad."""
    polygon = load_polygon(polygon_path)
    whitelist = load_whitelist(whitelist_path)
    return ReferenceData(polygon, whitelist, file_sha256(polygon_path), file_sha256(whitelist_path))
