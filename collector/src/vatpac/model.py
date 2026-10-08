"""Typed feed model, parsing and plausibility caps. Pure functions, no I/O."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

from .errors import FeedError

log = logging.getLogger(__name__)
_T = TypeVar("_T")

NO_FREQUENCY_HZ = 199_998_000
GS_RANGE = (0, 700)
ALT_RANGE = (-1500, 65000)
_HOME_RE = re.compile(r"\s([A-Z]{4})$")
_FRACTION_RE = re.compile(r"(\.\d{6})\d+")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SPACE_RE = re.compile(r"[\t\n]+")


@dataclass(frozen=True, slots=True)
class Pilot:
    cid: int
    name: str | None
    callsign: str
    server: str | None
    logon_time: datetime
    latitude: float | None
    longitude: float | None
    altitude: int | None
    groundspeed: int | None
    heading: int | None
    departure: str | None
    arrival: str | None
    aircraft_icao: str | None
    aircraft_faa: str | None
    flight_rules: str | None
    route: str | None
    remarks: str | None
    planned_alt: str | None
    deptime: str | None
    pilot_rating: int | None
    military_rating: int | None


@dataclass(frozen=True, slots=True)
class Controller:
    cid: int
    name: str | None
    callsign: str
    frequency_text: str | None
    facility: int | None
    rating: int | None
    server: str | None
    visual_range: int | None
    logon_time: datetime


@dataclass(frozen=True, slots=True)
class Snapshot:
    observed_at: datetime
    pilots: list[Pilot]
    controllers: list[Controller]
    pilots_global: int
    controllers_global: int
    malformed: int = field(default=0, compare=False)


def parse_frequency_hz(text: str | None) -> int | None:
    """'121.700' -> 121700000. '199.998' (no frequency), None and junk -> None."""
    try:  # str(None) is not a number either, so None falls through to the except
        hz = int(Decimal(str(text).strip()) * 1_000_000)
    except (InvalidOperation, ValueError):
        return None
    return None if hz <= 0 or hz == NO_FREQUENCY_HZ else hz


def home_icao(name: str | None) -> str | None:
    """Trailing space-separated token of exactly four uppercase letters, else None."""
    m = _HOME_RE.search((name or "").rstrip())
    return m.group(1) if m else None


def clamp_pilot(gs: int | None, alt: int | None) -> tuple[int | None, int | None, int]:
    """Plausibility caps: out-of-range values become None. Third item counts rejected values."""
    bad_gs = gs is not None and not GS_RANGE[0] <= gs <= GS_RANGE[1]
    bad_alt = alt is not None and not ALT_RANGE[0] <= alt <= ALT_RANGE[1]
    return None if bad_gs else gs, None if bad_alt else alt, bad_gs + bad_alt


def _ts(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp (the feed uses 7 fractional digits and a Z suffix) to UTC."""
    text = _FRACTION_RE.sub(r"\1", str(value).strip())
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"bad timestamp {value!r}") from exc
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _logon(value: Any) -> datetime:
    """logon_time identifies a session, so it is kept to whole seconds (truncated, never rounded): the feed
    sometimes sends the fraction and sometimes not (ADR-021, T36). The fraction is dropped on purpose."""
    return _ts(value).replace(microsecond=0)


def update_timestamp(payload: dict[str, Any]) -> datetime:
    """The snapshot time (general.update_timestamp). Raises FeedError if absent or invalid."""
    try:
        return _ts(payload["general"]["update_timestamp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FeedError(f"feed has no valid general.update_timestamp: {exc}") from exc


def _s(value: Any) -> str | None:
    if value is None:
        return None
    # lone surrogates (rejected by jsonb) become '?'; control characters (incl. NUL) are dropped; tab/newline -> space
    text = str(value).encode("utf-8", "replace").decode("utf-8")
    text = _CTRL_RE.sub("", _SPACE_RE.sub(" ", text))
    return text if text.strip() else None


def _i(value: Any) -> int | None:
    """Integer or None; values outside int32 are dropped so one bad field cannot abort a snapshot insert."""
    try:
        n = None if value is None else int(value)
    except (TypeError, ValueError):
        return None
    return n if n is None or -(2**31) <= n < 2**31 else None


def _callsign(value: Any) -> str:
    text = _s(value)  # an empty callsign would violate the table CHECK and abort the whole snapshot
    if text is None:
        raise ValueError("empty callsign")
    return text.strip()


def _f(value: Any) -> float | None:
    """A coordinate: None means no position; anything unparsable raises, so the pilot is a malformed record."""
    return None if value is None else float(value)


def _heading(value: Any) -> int | None:
    h = _i(value)
    return h if h is not None and 0 <= h <= 360 else None


def _pilot(raw: dict[str, Any]) -> Pilot:
    fp = raw.get("flight_plan") or {}
    return Pilot(
        cid=int(raw["cid"]), name=_s(raw.get("name")), callsign=_callsign(raw["callsign"]),
        server=_s(raw.get("server")), logon_time=_logon(raw["logon_time"]),
        latitude=_f(raw.get("latitude")), longitude=_f(raw.get("longitude")),
        altitude=_i(raw.get("altitude")), groundspeed=_i(raw.get("groundspeed")),
        heading=_heading(raw.get("heading")),
        departure=_s(fp.get("departure")), arrival=_s(fp.get("arrival")),
        aircraft_icao=_s(fp.get("aircraft_short")), aircraft_faa=_s(fp.get("aircraft_faa")),
        flight_rules=_s(fp.get("flight_rules")), route=_s(fp.get("route")),
        remarks=_s(fp.get("remarks")), planned_alt=_s(fp.get("altitude")), deptime=_s(fp.get("deptime")),
        pilot_rating=_i(raw.get("pilot_rating")), military_rating=_i(raw.get("military_rating")),
    )


def _controller(raw: dict[str, Any]) -> Controller:
    return Controller(
        cid=int(raw["cid"]), name=_s(raw.get("name")), callsign=_callsign(raw["callsign"]),
        frequency_text=_s(raw.get("frequency")), facility=_i(raw.get("facility")),
        rating=_i(raw.get("rating")), server=_s(raw.get("server")),
        visual_range=_i(raw.get("visual_range")), logon_time=_logon(raw["logon_time"]),
    )


def parse_snapshot(payload: dict[str, Any]) -> Snapshot:
    """Parse pilots and controllers only. Malformed entries are skipped, counted and logged."""
    observed_at = update_timestamp(payload)
    raw_pilots = payload.get("pilots") or []
    raw_ctrls = payload.get("controllers") or []
    malformed = 0

    def parse_all(raws: list[Any], build: Callable[[dict[str, Any]], _T]) -> list[_T]:
        nonlocal malformed
        out: list[_T] = []
        for raw in raws:
            try:
                out.append(build(raw))
            except (KeyError, TypeError, ValueError):
                malformed += 1
        return out

    pilots, controllers = parse_all(raw_pilots, _pilot), parse_all(raw_ctrls, _controller)
    if malformed:
        log.warning("malformed feed entries skipped", extra={"count": malformed})
    return Snapshot(observed_at, pilots, controllers, len(raw_pilots), len(raw_ctrls), malformed)
