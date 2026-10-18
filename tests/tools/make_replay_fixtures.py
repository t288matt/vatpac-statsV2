"""Build the small committed replay set (tests/fixtures/replay/*.json.gz) from two saved raw-feed files.

Usage (the raw files stay OUTSIDE the repository; copy them from the VM archive read-only, see
docs/RUNBOOKS/feed-archive.md "Read a file"):

    python tests/tools/make_replay_fixtures.py --full full/20261010/150012.json.gz --compact compact/20261010/151527.json.gz

Selection (deterministic, seeded): every pilot inside the region, every whitelisted controller, a seeded
sample of other pilots (all of them are real entries, including ones with a null flight_plan) and of other controllers.
The compact file is cut to the same pilot keys, so the two snapshots overlap and the replay sees real continuity
(same sessions, moved positions). Names and CIDs are public data (ADR-019), so they are kept as received.

Test tooling only: it must not live in collector/src.
"""
from __future__ import annotations

import argparse
import gzip
import json
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "collector" / "src"))

from vatpac.filters import inside_polygon, load_reference  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "replay"
SEED = 20261010
OTHER_PILOTS, OTHER_CONTROLLERS = 60, 15
KEEP_TOP = ("general", "pilots", "controllers")


def _key(entry: dict[str, Any]) -> tuple[Any, Any, Any]:
    return entry["cid"], entry["callsign"], entry["logon_time"]


def _read(path: str) -> dict[str, Any]:
    with gzip.open(path, "rb") as fh:
        data = json.loads(fh.read())
    assert isinstance(data, dict)
    return data


def _write(name: str, payload: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    with open(OUT / name, "wb") as fh:  # mtime=0: the same input gives the same bytes
        fh.write(gzip.compress(raw, 9, mtime=0))
    print(f"{name}: {len(payload['pilots'])} pilots, {len(payload['controllers'])} controllers, "
          f"{(OUT / name).stat().st_size} bytes gzipped")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", required=True)
    ap.add_argument("--compact", required=True)
    args = ap.parse_args()
    ref = load_reference(ROOT / "config" / "australian_airspace_polygon.json",
                         ROOT / "config" / "controller_callsigns_list.txt")
    rng = random.Random(SEED)
    full, compact = _read(args.full), _read(args.compact)

    inside = [p for p in full["pilots"] if inside_polygon(ref.polygon, p.get("latitude"), p.get("longitude"))]
    inside_keys = {_key(p) for p in inside}
    in_compact = {_key(p) for p in compact["pilots"]}
    others = [p for p in full["pilots"] if _key(p) not in inside_keys and _key(p) in in_compact]
    no_plan = [p for p in others if not p.get("flight_plan")]
    picked = rng.sample(no_plan, min(10, len(no_plan)))
    rest = [p for p in others if p not in picked]
    picked += rng.sample(rest, OTHER_PILOTS - len(picked))
    keep = inside_keys | {_key(p) for p in picked}

    listed = [c for c in full["controllers"] if c["callsign"] in ref.whitelist]
    listed_keys = {_key(c) for c in listed}
    rest_c = [c for c in full["controllers"] if _key(c) not in listed_keys]
    ckeys = listed_keys | {_key(c) for c in rng.sample(rest_c, OTHER_CONTROLLERS)}

    def cut(payload: dict[str, Any], pilot_keys: set[Any]) -> dict[str, Any]:
        out = {k: payload[k] for k in KEEP_TOP}
        out["pilots"] = [p for p in payload["pilots"] if _key(p) in pilot_keys]
        out["controllers"] = [c for c in payload["controllers"] if _key(c) in ckeys]
        return out

    _write("real_full.json.gz", cut(full, keep))
    # the compact side also keeps every in-region pilot of its own snapshot (they may have moved in or out)
    ckeep = keep | {_key(p) for p in compact["pilots"]
                    if inside_polygon(ref.polygon, p.get("latitude"), p.get("longitude"))}
    _write("real_compact.json.gz", cut(compact, ckeep))


if __name__ == "__main__":
    main()
