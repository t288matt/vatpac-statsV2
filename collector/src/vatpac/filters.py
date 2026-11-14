"""The ingest filters (pilot polygon, controller whitelist), reference loading and unlisted callsigns."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import shapely
from shapely.geometry import Polygon

from .errors import ReferenceDataError

log = logging.getLogger(__name__)

_CALLSIGN_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _read(path: str | Path, what: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError as exc:
        raise ReferenceDataError(f"{what} file {path} missing or unreadable: {exc}") from exc


def load_polygon(path: str | Path, raw: bytes | None = None) -> Polygon:
    """Load a GeoJSON Polygon (lon,lat order), prepared for fast point tests; `raw` = bytes the caller already read."""
    raw = _read(path, "polygon") if raw is None else raw
    try:
        geo = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
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


def load_whitelist(path: str | Path, raw: bytes | None = None) -> frozenset[str]:
    """Lines `CALLSIGN, FREQ` or bare `CALLSIGN`; blanks and `#` comments skipped. Fails closed."""
    raw = _read(path, "whitelist") if raw is None else raw
    try:
        lines = raw.decode("utf-8-sig").splitlines()
    except UnicodeError as exc:
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


def listed_prefixes(whitelist: frozenset[str]) -> frozenset[str]:
    return frozenset(c.split("_", 1)[0] for c in whitelist if "_" in c)


def is_unlisted_callsign(callsign: str, whitelist: frozenset[str], prefixes: frozenset[str] | None = None) -> bool:
    """Unlisted controller whose prefix (text before the first `_`) matches a listed prefix (`prefixes` precomputed)."""
    if callsign in whitelist or callsign.endswith(("_OBS", "_SUP")) or "_" not in callsign:
        return False
    return callsign.split("_", 1)[0] in (listed_prefixes(whitelist) if prefixes is None else prefixes)


@dataclass(frozen=True, slots=True)
class ReferenceData:
    polygon: Polygon
    whitelist: frozenset[str]
    polygon_sha: str
    whitelist_sha: str
    prefixes: frozenset[str] = field(init=False)  # derived once per load, so no per-controller rebuild

    def __post_init__(self) -> None:
        object.__setattr__(self, "prefixes", listed_prefixes(self.whitelist))


def load_reference(polygon_path: str | Path, whitelist_path: str | Path) -> ReferenceData:
    """Load and validate both files. Raises ReferenceDataError if either is bad."""
    raw_polygon, raw_whitelist = _read(polygon_path, "polygon"), _read(whitelist_path, "whitelist")  # read once
    return ReferenceData(load_polygon(polygon_path, raw_polygon), load_whitelist(whitelist_path, raw_whitelist),
                         hashlib.sha256(raw_polygon).hexdigest(), hashlib.sha256(raw_whitelist).hexdigest())
