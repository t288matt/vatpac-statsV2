"""The replay tool's own logic and the committed replay fixtures (issue 103). No database."""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for sub in ("collector/src", "tests/tools"):
    if str(REPO / sub) not in sys.path:
        sys.path.insert(0, str(REPO / sub))

import replay as rp  # noqa: E402
from vatpac.filters import load_reference  # noqa: E402
from vatpac.model import parse_snapshot  # noqa: E402

FIX = REPO / "tests" / "fixtures" / "replay"
REF = load_reference(REPO / "config" / "australian_airspace_polygon.json", REPO / "config" / "controller_callsigns_list.txt")


def test_the_committed_replay_set_is_small_and_real() -> None:
    files = sorted(FIX.glob("*.json.gz"))
    assert [f.name for f in files] == ["real_compact.json.gz", "real_full.json.gz"]
    assert sum(f.stat().st_size for f in files) < 100_000               # the set stays small enough to review
    full = json.loads(gzip.decompress((FIX / "real_full.json.gz").read_bytes()))
    compact = json.loads(gzip.decompress((FIX / "real_compact.json.gz").read_bytes()))
    assert full["general"]["update_timestamp"] < compact["general"]["update_timestamp"]
    assert "flight_plan" in full["pilots"][0] and "flight_plan" not in compact["pilots"][0]   # full vs compact shapes
    assert any(not p.get("flight_plan") for p in full["pilots"])        # real pilots without a plan are in the set
    shared = {(p["cid"], p["callsign"]) for p in full["pilots"]} & {(p["cid"], p["callsign"]) for p in compact["pilots"]}
    assert len(shared) >= 20                                            # the two snapshots overlap (real continuity)


def test_collect_reads_folders_and_files_oldest_first() -> None:
    found = rp.collect([FIX, REPO / "tests" / "fixtures" / "vatsim-data.sample.json"])
    assert [n for n, _ in found] == ["vatsim-data.sample.json", "real_full.json.gz", "real_compact.json.gz"]
    stamps = [p["general"]["update_timestamp"] for _, p in found]
    assert stamps == sorted(stamps)


def test_the_independent_count_agrees_with_the_parser_on_real_data() -> None:
    """Two separate implementations of 'who is inside the region' agree on the real snapshots."""
    from vatpac.filters import inside_polygon

    for name, payload in rp.collect([FIX]):
        exp = rp.expected(payload, REF.polygon, REF.whitelist)
        snap = parse_snapshot(payload)
        inside = {(p.cid, p.callsign, f"{p.logon_time:%Y-%m-%dT%H:%M:%S}") for p in snap.pilots
                  if inside_polygon(REF.polygon, p.latitude, p.longitude)}
        listed = {(c.cid, c.callsign, f"{c.logon_time:%Y-%m-%dT%H:%M:%S}") for c in snap.controllers
                  if c.callsign in REF.whitelist}
        assert exp.pilot_keys == inside and exp.controller_keys == listed, name
        assert exp.pilots_global == len(payload["pilots"]) and len(inside) >= 20 and listed


def test_diff_dumps_is_empty_for_equal_dumps_and_names_the_changed_table() -> None:
    a = {"pilot_session": ['{"a": 1}', '{"a": 2}'], "network_snapshot": ['{"n": 1}']}
    assert rp.diff_dumps(a, {k: list(v) for k, v in a.items()}) == []
    b = {"pilot_session": ['{"a": 1}', '{"a": 3}'], "network_snapshot": ['{"n": 1}']}
    out = rp.diff_dumps(a, b)
    assert out[0].startswith("pilot_session: 2 rows vs 2 rows; only in A 1, only in B 1")
    assert any('A> {"a": 2}' in line for line in out) and any('B> {"a": 3}' in line for line in out)
    assert not any("network_snapshot" in line for line in out)
