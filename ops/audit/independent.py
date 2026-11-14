"""Independent re-implementation of the VATPAC v3 ingest rules, for the T30 data-integrity audit.

Deliberately imports NOTHING from collector/src/vatpac and does not use shapely: the point-in-polygon test is an
exact-arithmetic ray cast written from the SPEC text. Read-only, no network, no database.

Rules (SPEC sections 4 and 5):
  pilot kept   = no position (lat or lon missing) OR strictly inside the polygon (edge and vertex points are outside)
  controller   = callsign exactly in the whitelist (case-sensitive)
  unlisted     = unlisted callsign with an underscore, not ending _OBS, whose text before the first '_' equals the text
                 before the first '_' of some whitelisted callsign that has an underscore
  frequency    = integer Hz; 199.998, unparseable or <= 0 means NULL
  caps         = groundspeed 0..700 and altitude -1500..65000, outside -> NULL
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

CTRL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def load_ring(path: str | Path) -> list[tuple[Fraction, Fraction]]:
    geo = json.loads(Path(path).read_text(encoding="utf-8"))
    assert geo["type"] == "Polygon" and len(geo["coordinates"]) == 1, "this auditor supports one ring (no holes)"
    ring = [(Fraction(x), Fraction(y)) for x, y in geo["coordinates"][0]]
    assert ring[0] == ring[-1]
    return ring


def classify(ring: list[tuple[Fraction, Fraction]], lon: float, lat: float) -> str:
    """Return 'inside', 'edge' or 'outside' using exact rational arithmetic (x = lon, y = lat)."""
    px, py = Fraction(lon), Fraction(lat)
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:], strict=False):
        cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
        if cross == 0 and min(x1, x2) <= px <= max(x1, x2) and min(y1, y2) <= py <= max(y1, y2):
            return "edge"
        if (y1 > py) != (y2 > py):
            xint = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if px < xint:
                inside = not inside
    return "inside" if inside else "outside"


def pilot_kept(ring, lat, lon) -> bool:
    if lat is None or lon is None:
        return True
    return classify(ring, lon, lat) == "inside"


def load_whitelist(path: str | Path) -> set[str]:
    out = set()
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        cs = t.split(",", 1)[0].strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", cs):
            out.add(cs)
    return out


def unlisted_controller_callsign(cs: str, wl: set[str]) -> bool:
    if cs in wl or cs.endswith("_OBS") or "_" not in cs:
        return False
    pre = cs.split("_", 1)[0]
    return any("_" in w and w.split("_", 1)[0] == pre for w in wl)


def freq_hz(text) -> int | None:
    try:
        hz = int(Decimal(str(text).strip()) * 1_000_000)
    except Exception:
        return None
    return None if hz <= 0 or hz == 199_998_000 else hz


def cap(gs, alt):
    g = None if gs is None or not 0 <= gs <= 700 else gs
    a = None if alt is None or not -1500 <= alt <= 65000 else alt
    return g, a


def clean(s):
    """Text normalisation the SPEC implies: tab/newline -> space, control chars dropped, empty -> None."""
    if s is None:
        return None
    t = CTRL_CHARS.sub("", re.sub(r"[\t\n]+", " ", str(s)))
    return t if t.strip() else None


def home_icao(name):
    m = re.search(r"\s([A-Z]{4})$", (name or "").rstrip())
    return m.group(1) if m else None


def analyse(payload: dict, ring, wl: set[str]) -> dict:
    """Everything the audit needs from one raw feed payload, computed only from the raw JSON."""
    pilots = payload.get("pilots") or []
    ctrls = payload.get("controllers") or []
    kept, nopos, edge = [], 0, 0
    for p in pilots:
        lat, lon = p.get("latitude"), p.get("longitude")
        if lat is None or lon is None:
            nopos += 1
        elif classify(ring, float(lon), float(lat)) == "edge":
            edge += 1
        if pilot_kept(ring, None if lat is None else float(lat), None if lon is None else float(lon)):
            fp = p.get("flight_plan") or {}
            gs, alt = cap(p.get("groundspeed"), p.get("altitude"))
            kept.append(
                {
                    "cid": p["cid"],
                    "callsign": clean(p["callsign"]).strip(),
                    "logon_time": p["logon_time"],
                    "lat": lat,
                    "lon": lon,
                    "gs": gs,
                    "alt": alt,
                    "hdg": p.get("heading"),
                    "name": clean(p.get("name")),
                    "dep": clean(fp.get("departure")),
                    "arr": clean(fp.get("arrival")),
                    "plan": {
                        "aircraft_icao": clean(fp.get("aircraft_short")),
                        "aircraft_faa": clean(fp.get("aircraft_faa")),
                        "flight_rules": clean(fp.get("flight_rules")),
                        "route": clean(fp.get("route")),
                        "remarks": clean(fp.get("remarks")),
                        "planned_alt": clean(fp.get("altitude")),
                        "deptime": clean(fp.get("deptime")),
                        "server": clean(p.get("server")),
                        "pilot_rating": p.get("pilot_rating"),
                        "military_rating": p.get("military_rating"),
                        "home_icao": home_icao(clean(p.get("name"))),
                    },
                    "rejected": int(p.get("groundspeed") is not None and gs is None) + int(p.get("altitude") is not None and alt is None),
                }
            )
    ck, nm = [], []
    for c in ctrls:
        cs = c["callsign"]
        if cs in wl:
            ck.append(
                {
                    "cid": c["cid"],
                    "callsign": cs,
                    "logon_time": c["logon_time"],
                    "freq_hz": freq_hz(c.get("frequency")),
                    "rating": c.get("rating"),
                    "facility": c.get("facility"),
                    "name": clean(c.get("name")),
                }
            )
        elif unlisted_controller_callsign(cs, wl):
            nm.append(cs)
    with_plan = sum(1 for k in kept if (k["dep"] or "").strip() and (k["arr"] or "").strip())
    return {
        "update_timestamp": payload["general"]["update_timestamp"],
        "pilots_global": len(pilots),
        "controllers_global": len(ctrls),
        "pilots_inside": len(kept),
        "controllers_kept": len(ck),
        "pilots_no_position": nopos,
        "pilots_on_edge": edge,
        "pilots_with_plan": with_plan,
        "rejected_values": sum(k["rejected"] for k in kept),
        "unlisted_controller_callsign": sorted(nm),
        "obs_or_atis_in_controllers": sorted(c["callsign"] for c in ctrls if c["callsign"].endswith("_OBS") or "ATIS" in c["callsign"]),
        "kept_pilots": kept,
        "kept_controllers": ck,
    }
