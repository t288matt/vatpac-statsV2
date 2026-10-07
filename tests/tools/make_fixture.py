"""Build the anonymised feed fixture and the golden expectations file.

Usage:
    python tests/tools/make_fixture.py [--raw raw_feed.json]   # fetch if --raw omitted
    python tests/tools/make_fixture.py --raw raw.json --verify  # also prove no real PII remains

The raw snapshot is never written inside the repository. Output:
    tests/fixtures/vatsim-data.sample.json
    tests/fixtures/expected.json

Anonymisation (deterministic given the same raw input):
  * every name becomes "Pilot NNNN" / "Controller NNNN" (a trailing 4-letter ICAO token is kept)
  * every cid becomes a number in 9000000..9999999 via a seeded permutation (not reversible)
  * remarks become "RMK/TEST"; ATIS/controller free text is dropped
  * callsigns and positions are kept
Synthetic (hand-crafted) entries are marked: pilot callsigns start with "TEST"; synthetic
controllers are listed under "synthetic_controllers" in expected.json (their callsigns must
match the whitelist, so they cannot carry a prefix).

This script is test tooling only; it must not live in collector/src.
"""
from __future__ import annotations

import argparse
import copy
import json
import random
import re
import sys
import urllib.request
from pathlib import Path

from shapely.geometry import Point, shape

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
POLYGON = ROOT / "config" / "australian_airspace_polygon.json"
WHITELIST = ROOT / "config" / "controller_callsigns_list.txt"
FEED_URL = "https://data.vatsim.net/v3/vatsim-data.json"
SEED = 20261007
NAME_RE = re.compile(r"^(Pilot|Controller) \d{4}( [A-Z]{4})?$")


# --------------------------------------------------------------------------- helpers
def load_polygon():
    return shape(json.loads(POLYGON.read_text(encoding="utf-8")))


def load_whitelist() -> set[str]:
    out = set()
    for line in WHITELIST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(line.split(",")[0].strip())
    return out


def prefix(callsign: str) -> str:
    return callsign.split("_", 1)[0]


def is_near_miss(callsign: str, wl: set[str]) -> bool:
    if callsign in wl or callsign.endswith("_OBS"):
        return False
    return prefix(callsign) in {prefix(w) for w in wl}


def inside(poly, p) -> bool:
    if p["latitude"] is None or p["longitude"] is None:
        return True  # SPEC: no position passes
    return bool(poly.contains(Point(p["longitude"], p["latitude"])))


def icao_token(name: str) -> str | None:
    parts = name.rsplit(" ", 1)
    if len(parts) == 2 and re.fullmatch(r"[A-Z]{4}", parts[1]):
        return parts[1]
    return None


# --------------------------------------------------------------------------- synthetic
def synth_pilot(callsign, lat, lon, gs=250, alt=10000, dep="YSSY", arr="YMML",
                plan=True, name_icao=None, hdg=90, logon="2026-10-07T10:00:00Z"):
    fp = None
    if plan:
        fp = {
            "flight_rules": "I", "aircraft": "B738/M-SDE2E3FGHIRWY/LB1", "aircraft_faa": "B738/L",
            "aircraft_short": "B738", "departure": dep, "arrival": arr, "alternate": "",
            "cruise_tas": "450", "altitude": "35000", "deptime": "1000", "enroute_time": "0130",
            "fuel_time": "0300", "route": "DCT", "revision_id": 1, "assigned_transponder": "2000",
            "remarks": "RMK/TEST",
        }
    return {
        "cid": 0, "name": "x" + (f" {name_icao}" if name_icao else ""), "callsign": callsign,
        "server": "AUSTRALIA", "pilot_rating": 0, "military_rating": 0, "latitude": lat,
        "longitude": lon, "altitude": alt, "groundspeed": gs, "transponder": "2000",
        "heading": hdg, "qnh_i_hg": 29.92, "qnh_mb": 1013, "flight_plan": fp,
        "logon_time": logon, "last_updated": "2026-10-07T16:11:17.0000000Z",
    }


def synthetic_pilots():
    p = [
        synth_pilot("TESTYPDN", -12.4147, 130.8770, gs=0, alt=100, dep="YPDN", arr="YBCS", name_icao="YPDN"),
        synth_pilot("TESTCOCOS", -12.1883, 96.8269, gs=0, alt=10, dep="YPCC", arr="YPCC"),
        synth_pilot("TESTGS", -33.90, 151.20, gs=30702, alt=5000, hdg=360),
        synth_pilot("TESTALT", -33.80, 151.10, gs=120, alt=-157, hdg=0),   # negative but within cap
        synth_pilot("TESTALTLOW", -33.70, 151.00, gs=120, alt=-5000),      # below the -1500 cap
        synth_pilot("TESTNOPOS", None, None, gs=0, alt=0),
        synth_pilot("TESTNOPLAN1", -27.40, 153.10, plan=False),
        synth_pilot("TESTNOPLAN2", -31.90, 115.90, plan=False, name_icao="YPPH"),
        synth_pilot("TESTNOARR1", -34.00, 151.00, arr=""),
        synth_pilot("TESTNOARR2", -37.60, 144.80, arr="  ", dep="YMML"),
        synth_pilot("TESTEDGE", -12.0, 150.0, gs=0, alt=0),       # on the -12 latitude edge
        synth_pilot("TESTVERTEX", -12.0, 155.0, gs=0, alt=0),     # exactly a polygon vertex
        synth_pilot("TESTJUSTIN", -12.0001, 150.0, gs=0, alt=0),  # just inside the edge
        synth_pilot("TESTJUSTOUT", -11.9999, 150.0, gs=0, alt=0), # just outside the edge
        synth_pilot("TESTNZAA", -37.0081, 174.7917, gs=0, alt=23, dep="NZAA", arr="NZWN"),
        synth_pilot("TESTJAKARTA", -6.1256, 106.6559, gs=0, alt=33, dep="WIII", arr="WSSS"),
        synth_pilot("TESTHEMI", 51.4700, -0.4543, gs=0, alt=83, dep="EGLL", arr="KJFK"),
        synth_pilot("TESTCIRCUIT", -33.9461, 151.1772, gs=140, alt=1500, dep="YSSY", arr="YSSY"),
    ]
    return p


def synthetic_controllers():
    def c(callsign, freq, rating=3, facility=3, logon="2026-10-07T09:00:00Z", name="x"):
        return {"cid": 0, "name": name, "callsign": callsign, "frequency": freq, "facility": facility,
                "rating": rating, "server": "AUSTRALIA", "visual_range": 50, "text_atis": None,
                "last_updated": "2026-10-07T16:11:15.0000000Z", "logon_time": logon}
    return [
        c("SY_TWR", "120.500", facility=4),
        c("ML_APP", "132.000", rating=5, facility=5),
        c("BN_GND", "199.998", facility=3),          # whitelisted, no frequency set
        c("ML_XYZ_CTR", "132.800", rating=5, facility=6),   # near-miss (prefix ML is whitelisted)
        c("AD_XYZ_TWR", "120.500", facility=4),             # near-miss (prefix AD)
        c("SY_OBS", "199.998", rating=1, facility=0),       # _OBS never a near-miss
    ]


# --------------------------------------------------------------------------- build
def build(raw: dict) -> tuple[dict, dict]:
    poly = load_polygon()
    wl = load_whitelist()
    rng = random.Random(SEED)

    pilots = raw["pilots"]
    real_in = [p for p in pilots if inside(poly, p) and p["latitude"] is not None]
    real_out = [p for p in pilots if not inside(poly, p)]

    def in_box(p, la0, la1, lo0, lo1):
        return la0 <= p["latitude"] <= la1 and lo0 <= p["longitude"] <= lo1

    nz = [p for p in real_out if in_box(p, -48, -33, 165, 179)][:4]
    indo = [p for p in real_out if in_box(p, -11, 6, 95, 141)][:4]
    north = [p for p in real_out if p["latitude"] > 30][:10]
    noplan_out = [p for p in real_out if not p["flight_plan"]][:2]
    chosen = real_in + nz + indo + north + noplan_out
    seen = {id(p) for p in chosen}
    rest = [p for p in real_out if id(p) not in seen]
    rng.shuffle(rest)
    chosen += rest[: max(0, 44 - len(chosen))]
    pilots_out = [copy.deepcopy(p) for p in chosen] + synthetic_pilots()

    ctrls = raw["controllers"]
    real_wl = [c for c in ctrls if c["callsign"] in wl]
    obs = [c for c in ctrls if c["callsign"].endswith("_OBS") and c["callsign"][0].isalpha()][:2]
    no_freq = [c for c in ctrls if c["frequency"] == "199.998" and not c["callsign"].endswith("_OBS")][:2]
    others = [c for c in ctrls if c["callsign"] not in wl and not c["callsign"].endswith("_OBS")
              and c["frequency"] != "199.998" and not is_near_miss(c["callsign"], wl)]
    rng.shuffle(others)
    ctrl_real = real_wl + obs + no_freq + others[:12]
    ctrls_out = [copy.deepcopy(c) for c in ctrl_real] + synthetic_controllers()
    synth_ctrl_calls = [c["callsign"] for c in synthetic_controllers()]

    atis = [copy.deepcopy(a) for a in raw["atis"][:5]]

    # ---- anonymise
    originals_cid = sorted({e["cid"] for e in pilots_out + ctrls_out + atis if e["cid"]})
    pool = list(range(9000000, 10000000))
    rng2 = random.Random(SEED + 1)
    chosen_ids = rng2.sample(pool, len(originals_cid) + 40)
    cid_map = dict(zip(originals_cid, chosen_ids))
    synth_ids = iter(chosen_ids[len(originals_cid):])

    def new_cid(e):
        if e["cid"]:
            return cid_map[e["cid"]]
        return next(synth_ids)

    for i, p in enumerate(sorted(pilots_out, key=lambda x: x["callsign"])):
        tok = icao_token(p["name"])
        p["name"] = f"Pilot {1001 + i}" + (f" {tok}" if tok else "")
        p["cid"] = new_cid(p)
        if p["flight_plan"]:
            p["flight_plan"]["remarks"] = "RMK/TEST"
    for i, c in enumerate(sorted(ctrls_out, key=lambda x: x["callsign"])):
        tok = icao_token(c["name"])
        c["name"] = f"Controller {2001 + i}" + (f" {tok}" if tok else "")
        c["cid"] = new_cid(c)
        c["text_atis"] = None
    for i, a in enumerate(atis):
        a["name"] = f"Controller {3001 + i}"
        a["cid"] = new_cid(a)
        a["text_atis"] = ["TEST ATIS"]

    pilots_out.sort(key=lambda x: x["callsign"])
    ctrls_out.sort(key=lambda x: x["callsign"])

    out = {
        "general": raw["general"],
        "pilots": pilots_out,
        "controllers": ctrls_out,
        "atis": atis,
        "servers": [],
        "prefiles": [],
        "facilities": raw.get("facilities", []),
        "ratings": raw.get("ratings", []),
        "pilot_ratings": raw.get("pilot_ratings", []),
        "military_ratings": raw.get("military_ratings", []),
    }

    # ---- golden expectations (own implementation of the SPEC rules)
    kept_p = [p["callsign"] for p in pilots_out if inside(poly, p)]
    edge_cs = {"TESTEDGE", "TESTVERTEX"}
    assert not (edge_cs & set(kept_p)), "edge pilots must be excluded"
    kept_c = [c["callsign"] for c in ctrls_out if c["callsign"] in wl]
    near = [c["callsign"] for c in ctrls_out if is_near_miss(c["callsign"], wl)]
    with_plan = [p["callsign"] for p in pilots_out if p["flight_plan"]]
    gate = [
        p["callsign"] for p in pilots_out
        if inside(poly, p) and p["flight_plan"]
        and (p["flight_plan"]["departure"] or "").strip() and (p["flight_plan"]["arrival"] or "").strip()
    ]
    expected = {
        "_comment": "Generated by tests/tools/make_fixture.py; do not edit by hand.",
        "observed_at_utc": raw["general"]["update_timestamp"],
        "pilots_global": len(pilots_out),
        "controllers_global": len(ctrls_out),
        "atis_entries": len(atis),
        "pilots_kept_by_polygon": len(kept_p),
        "pilots_kept_callsigns": sorted(kept_p),
        "pilots_outside_polygon": len(pilots_out) - len(kept_p),
        "pilots_on_polygon_edge": sorted(edge_cs),
        "pilots_no_position": ["TESTNOPOS"],
        "pilots_no_plan": sorted(p["callsign"] for p in pilots_out if not p["flight_plan"]),
        "pilots_no_plan_inside": sorted(
            p["callsign"] for p in pilots_out if not p["flight_plan"] and p["callsign"] in kept_p),
        "pilots_empty_arrival_inside": sorted(
            p["callsign"] for p in pilots_out
            if p["flight_plan"] and not (p["flight_plan"]["arrival"] or "").strip()
            and p["callsign"] in kept_p),
        "pilots_with_plan": len(with_plan),
        "pilots_passing_flight_gate_inside": sorted(gate),
        "implausible_rejected": {"TESTGS": {"groundspeed": 30702}, "TESTALTLOW": {"altitude": -5000}},
        "negative_altitude_within_cap": {"TESTALT": -157},
        "rejected_values_expected": 2,
        "controllers_kept_by_whitelist": len(kept_c),
        "controllers_kept_callsigns": sorted(kept_c),
        "synthetic_controllers": sorted(synth_ctrl_calls),
        "controllers_obs": sorted(c["callsign"] for c in ctrls_out if c["callsign"].endswith("_OBS")),
        "controllers_no_frequency": sorted(c["callsign"] for c in ctrls_out if c["frequency"] == "199.998"),
        "controllers_near_miss": sorted(near),
        "atis_callsigns": sorted(a["callsign"] for a in atis),
    }
    return out, expected


def verify(raw: dict, fixture_text: str) -> None:
    """Prove no original name or cid survives. Prints counts only, never values."""
    fx = json.loads(fixture_text)
    bad = 0
    for e in fx["pilots"] + fx["controllers"] + fx["atis"]:
        if not NAME_RE.match(e["name"]):
            bad += 1
    orig_cids = {str(e["cid"]) for k in ("pilots", "controllers", "atis", "prefiles") for e in raw.get(k, [])
                 if "cid" in e}
    orig_names = {e["name"].strip() for k in ("pilots", "controllers", "atis") for e in raw[k]
                  if e.get("name")}
    cid_hits = sum(1 for c in orig_cids if re.search(rf'(?<!\d){re.escape(c)}(?!\d)', fixture_text))
    # an original name must not appear as any JSON string value in the fixture
    values = {json.dumps(n) for n in orig_names}
    name_hits = sum(1 for v in values if f": {v}" in fixture_text or f"[{v}" in fixture_text)
    # original name tokens (length >= 4) appearing in a fixture name field (ICAO suffix and
    # the Pilot/Controller words are stripped first: ICAO tokens are deliberately kept)
    name_fields = " ".join(
        re.sub(r"^(Pilot|Controller) \d{4}( [A-Z]{4})?$", "", e["name"])
        for e in fx["pilots"] + fx["controllers"] + fx["atis"]).lower()
    icao_tokens = {t.lower() for n in orig_names for t in [n.rsplit(" ", 1)[-1]] if re.fullmatch(r"[A-Z]{4}", t)}
    toks = {t.lower() for n in orig_names for t in re.split(r"\W+", n) if len(t) >= 4 and not t.isdigit()}
    toks -= {"pilot", "controller"} | icao_tokens
    tok_hits = sum(1 for t in toks if t in name_fields)
    remarks = {p["flight_plan"]["remarks"] for p in fx["pilots"] if p["flight_plan"]}
    print(f"verify: names_not_matching_pattern={bad} original_cid_hits={cid_hits} "
          f"original_full_name_hits={name_hits} original_name_token_hits_in_name_fields={tok_hits} "
          f"distinct_remarks={sorted(remarks)} "
          f"checked(orig cids={len(orig_cids)}, orig names={len(orig_names)})")
    cids = [e["cid"] for e in fx["pilots"] + fx["controllers"] + fx["atis"]]
    assert all(9000000 <= c <= 9999999 for c in cids)
    if bad or cid_hits or name_hits or tok_hits:
        sys.exit("verify FAILED")
    print("verify: OK")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", help="path to a raw feed JSON (kept outside the repo); fetched if omitted")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    if args.raw:
        raw = json.loads(Path(args.raw).read_text(encoding="utf-8"))
    else:
        req = urllib.request.Request(FEED_URL, headers={"User-Agent": "vatpac-stats-qa/0.1"})
        raw = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
    out, expected = build(raw)
    text = json.dumps(out, indent=1, ensure_ascii=True, sort_keys=False) + "\n"
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "vatsim-data.sample.json").write_text(text, encoding="utf-8", newline="\n")
    (FIXTURES / "expected.json").write_text(
        json.dumps(expected, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"pilots={len(out['pilots'])} controllers={len(out['controllers'])} atis={len(out['atis'])}")
    print(f"kept pilots={expected['pilots_kept_by_polygon']} kept controllers="
          f"{expected['controllers_kept_by_whitelist']} near_miss={expected['controllers_near_miss']}")
    if args.verify:
        verify(raw, text)


if __name__ == "__main__":
    main()
