"""SPEC s4 / s11: golden test. Apply polygon + whitelist to the anonymised fixture and compare
with tests/fixtures/expected.json (generated independently by tests/tools/make_fixture.py)."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from vatpac.filters import inside_polygon, is_near_miss, load_polygon, load_whitelist
from vatpac.model import parse_snapshot


@pytest.fixture(scope="module")
def ctx(fixture_payload: dict, polygon_path: Path, whitelist_path: Path):
    snap = parse_snapshot(copy.deepcopy(fixture_payload))
    poly = load_polygon(polygon_path)
    wl = load_whitelist(whitelist_path)
    kept_p = [p for p in snap.pilots if inside_polygon(poly, p.latitude, p.longitude)]
    kept_c = [c for c in snap.controllers if c.callsign in wl]
    return snap, poly, wl, kept_p, kept_c


def test_pilots_kept_by_polygon(ctx, expected: dict) -> None:
    _, _, _, kept_p, _ = ctx
    assert len(kept_p) == expected["pilots_kept_by_polygon"]
    assert sorted(p.callsign for p in kept_p) == expected["pilots_kept_callsigns"]
    assert len(ctx[0].pilots) - len(kept_p) == expected["pilots_outside_polygon"]


def test_at_least_14_real_or_synthetic_pilots_inside(ctx) -> None:
    assert len(ctx[3]) >= 14


def test_edge_pilots_excluded(ctx, expected: dict) -> None:
    kept = {p.callsign for p in ctx[3]}
    for cs in expected["pilots_on_polygon_edge"]:
        assert cs not in kept
    assert "TESTJUSTIN" in kept
    assert "TESTJUSTOUT" not in kept


def test_no_position_pilot_passes(ctx, expected: dict) -> None:
    kept = {p.callsign for p in ctx[3]}
    assert set(expected["pilots_no_position"]) <= kept


@pytest.mark.parametrize(
    "callsign,kept",
    [("TESTYPDN", True), ("TESTCOCOS", True), ("TESTNZAA", False), ("TESTJAKARTA", False), ("TESTHEMI", False)],
)
def test_named_pilots(ctx, callsign: str, kept: bool) -> None:
    assert (callsign in {p.callsign for p in ctx[3]}) is kept


def test_outside_set_covers_nz_indonesia_northern_hemisphere(ctx) -> None:
    snap, poly, *_ = ctx
    outside = [p for p in snap.pilots if not inside_polygon(poly, p.latitude, p.longitude)]
    assert any(-48 <= p.latitude <= -33 and 165 <= p.longitude <= 179 for p in outside)  # NZ
    assert any(-11 <= p.latitude <= 6 and 95 <= p.longitude <= 141 for p in outside)  # Indonesia
    assert any(p.latitude > 30 for p in outside)  # Northern Hemisphere


def test_controllers_kept_by_whitelist(ctx, expected: dict) -> None:
    kept_c = ctx[4]
    assert len(kept_c) == expected["controllers_kept_by_whitelist"]
    assert sorted(c.callsign for c in kept_c) == expected["controllers_kept_callsigns"]
    assert len(kept_c) >= 3


def test_obs_and_atis_never_kept(ctx, expected: dict) -> None:
    kept = {c.callsign for c in ctx[4]}
    assert not kept & set(expected["controllers_obs"])
    assert not kept & set(expected["atis_callsigns"])
    assert not any(cs.endswith("_OBS") or cs.endswith("_ATIS") for cs in kept)
    assert len(expected["controllers_obs"]) >= 2


def test_atis_array_never_reaches_controllers(ctx, expected: dict) -> None:
    snap = ctx[0]
    assert not {c.callsign for c in snap.controllers} & set(expected["atis_callsigns"])


def test_no_frequency_controller_is_still_whitelisted(ctx, expected: dict) -> None:
    kept = {c.callsign for c in ctx[4]}
    assert "BN_GND" in expected["controllers_no_frequency"] and "BN_GND" in kept


def test_near_miss_list(ctx, expected: dict) -> None:
    snap, _, wl, _, _ = ctx
    near = sorted(c.callsign for c in snap.controllers if is_near_miss(c.callsign, wl))
    assert near == expected["controllers_near_miss"]
    assert "ML_XYZ_CTR" in near
    assert "SY_OBS" not in near


def test_near_miss_never_kept(ctx) -> None:
    snap, _, wl, _, kept_c = ctx
    kept = {c.callsign for c in kept_c}
    for c in snap.controllers:
        if is_near_miss(c.callsign, wl):
            assert c.callsign not in kept


def test_non_australian_controllers_present_and_dropped(ctx) -> None:
    snap, _, wl, _, _ = ctx
    foreign = [c for c in snap.controllers if c.callsign not in wl and not c.callsign.endswith("_OBS")
               and not is_near_miss(c.callsign, wl)]
    assert len(foreign) >= 5


def test_flight_gate_candidates(ctx, expected: dict) -> None:
    """Gate (view only, SPEC s4.3): kept pilots with non-empty dep and arr after trim."""
    kept_p = ctx[3]
    gated = sorted(
        p.callsign for p in kept_p if (p.departure or "").strip() and (p.arrival or "").strip()
    )
    assert gated == expected["pilots_passing_flight_gate_inside"]
    assert set(expected["pilots_no_plan_inside"]) & set(gated) == set()
    assert set(expected["pilots_empty_arrival_inside"]) & set(gated) == set()
