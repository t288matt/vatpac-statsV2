"""Issue 82: one odd feed entry must never drop the whole snapshot (parse_snapshot never raises per entry)."""
from __future__ import annotations

import copy

import pytest

from vatpac.model import parse_snapshot


def _p(payload: dict) -> dict:
    p = copy.deepcopy(payload)
    p["pilots"] = p["pilots"][:5]
    p["controllers"] = p["controllers"][:3]
    return p


@pytest.mark.parametrize("junk", [None, "a string", ["a", "list"], 42, 1.5, True])
def test_a_non_object_entry_is_one_malformed_entry(fixture_payload: dict, junk: object) -> None:
    for key, n in (("pilots", 5), ("controllers", 3)):
        p = _p(fixture_payload)
        p[key].insert(1, junk)
        snap = parse_snapshot(p)
        assert snap.malformed == 1
        assert (len(snap.pilots) if key == "pilots" else len(snap.controllers)) == n
        assert (snap.pilots_global if key == "pilots" else snap.controllers_global) == n + 1


@pytest.mark.parametrize("plan", ["a string", ["a", "list"], 7, True, ""])
def test_a_flight_plan_that_is_not_an_object_means_no_plan_and_one_rejected_value(
        fixture_payload: dict, plan: object) -> None:
    p = _p(fixture_payload)
    p["pilots"][0]["flight_plan"] = plan
    snap = parse_snapshot(p)
    first = snap.pilots[0]
    assert snap.malformed == 0 and len(snap.pilots) == 5
    assert first.departure is None and first.arrival is None and first.route is None and first.remarks is None
    assert first.rejected == parse_snapshot(_p(fixture_payload)).pilots[0].rejected + 1


@pytest.mark.parametrize("field", ["cid", "callsign", "logon_time", "latitude", "longitude", "altitude", "name"])
@pytest.mark.parametrize("junk", [{"a": {"b": [1]}}, [[], {}], [None], {"x": 1}])
def test_nested_junk_values_never_raise(fixture_payload: dict, field: str, junk: object) -> None:
    p = _p(fixture_payload)
    p["pilots"][0][field] = junk
    snap = parse_snapshot(p)
    assert snap.pilots_global == 5 and len(snap.pilots) + snap.malformed == 5 and len(snap.controllers) == 3


@pytest.mark.parametrize("junk", [{"a": [1, {"b": 2}]}, [[1], [2]], "text"])
def test_nested_junk_inside_the_flight_plan_never_raises(fixture_payload: dict, junk: object) -> None:
    p = _p(fixture_payload)
    p["pilots"][0]["flight_plan"] = {"departure": junk, "route": junk, "remarks": junk, "altitude": junk}
    snap = parse_snapshot(p)
    assert len(snap.pilots) == 5 and snap.malformed == 0


@pytest.mark.parametrize("junk", ["pilots", {"a": 1}, 7, None])
def test_a_pilots_or_controllers_list_that_is_not_a_list_counts_as_empty(fixture_payload: dict, junk: object) -> None:
    p = _p(fixture_payload)
    p["pilots"] = junk
    snap = parse_snapshot(p)
    assert snap.pilots == [] and snap.pilots_global == 0 and len(snap.controllers) == 3


def test_junk_entries_still_feed_the_malformed_fraction_rule(fixture_payload: dict) -> None:
    p = _p(fixture_payload)
    p["pilots"] += [None, "x", []]
    snap = parse_snapshot(p)
    total = snap.pilots_global + snap.controllers_global
    assert snap.malformed == 3 and total == 11 and snap.malformed > 0.2 * total
