"""Fuzz and mutation tests for parse_snapshot and the feed reader (issue 103). No database.

Property under test: ONE bad entry never loses the rest of the snapshot. For every generated variant of a real
payload, parse_snapshot must not raise, must keep every other entry exactly as the clean parse has it, and must either
keep the bad entry (with its bad values nulled or cut and counted) or skip it and count it in `malformed`.

The cases are seeded and deterministic (tests/tools/fuzz_payloads.py); hypothesis is not a dependency. Issue 82 (a null,
a string or a non-object flight_plan raised AttributeError and dropped the whole snapshot) is the defect this would
have caught: tests/README.md shows how the proof was made.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import math
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
for sub in ("collector/src", "tests/tools"):
    if str(REPO / sub) not in sys.path:
        sys.path.insert(0, str(REPO / sub))

import fuzz_payloads as fz  # noqa: E402
from vatpac.errors import FeedError  # noqa: E402
from vatpac.feed import FeedClient  # noqa: E402
from vatpac.filters import load_reference  # noqa: E402
from vatpac.model import MAX_TEXT, Controller, Pilot, parse_snapshot  # noqa: E402
from vatpac.settings import Settings  # noqa: E402

REAL = REPO / "tests" / "fixtures" / "replay" / "real_full.json.gz"
SEED = 103


@pytest.fixture(scope="module")
def base() -> dict[str, Any]:
    data = json.loads(gzip.decompress(REAL.read_bytes()))
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="module")
def clean(base):
    snap = parse_snapshot(base)
    assert snap.malformed == 0  # the real payload is clean, so every difference is the mutation
    assert len(snap.pilots) == len(base["pilots"]) and len(snap.controllers) == len(base["controllers"])
    return snap


@pytest.fixture(scope="module")
def hot_indices(base) -> tuple[set[int], set[int]]:
    ref = load_reference(REPO / "config" / "australian_airspace_polygon.json",
                         REPO / "config" / "controller_callsigns_list.txt")
    import shapely

    inside = {i for i, p in enumerate(base["pilots"])
              if shapely.contains_xy(ref.polygon, p["longitude"], p["latitude"])}
    listed = {i for i, c in enumerate(base["controllers"]) if c["callsign"] in ref.whitelist}
    assert inside and listed
    return inside, listed


def _all_cases(base, hot_indices, per_section: int = 3) -> list[tuple[str, int, str, Any]]:
    import random

    rng = random.Random(SEED)
    cases = []
    for section, hot in zip(fz.SECTIONS, hot_indices):
        for i in fz.pick_indices(rng, base, section, hot, per_section):
            cases += [(section, i, label, rep) for label, rep in fz.entry_mutations(section, base[section][i])]
    return cases


def _check_parsed_entry(rec: Pilot | Controller) -> None:
    """Whatever survived parsing satisfies the invariants the database relies on."""
    assert rec.callsign.strip() == rec.callsign and rec.callsign
    for name in ("name", "server", "callsign"):
        value = getattr(rec, name)
        assert value is None or len(value) <= MAX_TEXT
    for name in ("cid", "facility", "rating", "visual_range", "altitude", "groundspeed", "heading",
                 "pilot_rating", "military_rating"):
        value = getattr(rec, name, None)
        assert value is None or -(2**31) <= value < 2**31 or name == "cid"
    if isinstance(rec, Pilot):
        for coord in (rec.latitude, rec.longitude):
            assert coord is None or math.isfinite(coord)
        assert rec.heading is None or 0 <= rec.heading <= 360
        for text in (rec.route, rec.remarks, rec.departure, rec.arrival, rec.aircraft_icao):
            assert text is None or (len(text) <= MAX_TEXT and "\x00" not in text)
    assert rec.logon_time.microsecond == 0 and rec.logon_time.tzinfo is not None


def _grid(base, clean, hot_indices, per_section: int, minimum: int) -> None:
    cases = _all_cases(base, hot_indices, per_section)
    assert len(cases) > minimum  # a grid, not a handful
    for section, i, label, replacement in cases:
        where = f"{section}[{i}] {label}"
        mutated = fz.wire(fz.replace_entry(base, section, i, replacement))
        try:
            snap = parse_snapshot(mutated)
        except Exception as exc:  # noqa: BLE001 - any raise is the finding
            pytest.fail(f"{where}: parse_snapshot raised {type(exc).__name__}: {exc}")
        parsed, good = (snap.pilots, clean.pilots) if section == "pilots" else (snap.controllers, clean.controllers)
        other = [r for k, r in enumerate(good) if k != i]
        untouched = (snap.controllers, clean.controllers) if section == "pilots" else (snap.pilots, clean.pilots)
        assert untouched[0] == untouched[1], where                       # the other section is identical
        assert (snap.pilots_global, snap.controllers_global) == (len(base["pilots"]), len(base["controllers"])), where
        if snap.malformed:                                                # skipped and counted
            assert snap.malformed == 1 and parsed == other, where
        else:                                                             # kept: everything else is unchanged
            assert len(parsed) == len(good) and parsed[:i] + parsed[i + 1:] == other, where
            _check_parsed_entry(parsed[i])




def test_every_single_bad_entry_keeps_the_rest_of_the_snapshot(base, clean, hot_indices) -> None:
    """One inside pilot and one listed controller, every fault (the quick grid)."""
    _grid(base, clean, hot_indices, 1, 300)


@pytest.mark.slow_fuzz
def test_the_full_grid_of_single_bad_entries(base, clean, hot_indices) -> None:
    """Three entries per section, every fault: about 20 s. CI runs it; skip it locally with -m "not slow_fuzz"."""
    _grid(base, clean, hot_indices, 3, 1000)


@pytest.mark.parametrize("label,junk", fz.JUNK_ENTRIES)
@pytest.mark.parametrize("section", fz.SECTIONS)
def test_an_entry_that_is_not_a_usable_object_is_skipped_and_counted(base, clean, section, label, junk) -> None:
    """Issue 82: a null, a string or a number in place of an entry used to raise AttributeError out of parse_snapshot."""
    snap = parse_snapshot(fz.replace_entry(base, section, 3, junk))
    assert snap.malformed == 1
    kept = snap.pilots if section == "pilots" else snap.controllers
    good = clean.pilots if section == "pilots" else clean.controllers
    assert kept == good[:3] + good[4:]


@pytest.mark.parametrize("label,plan", fz.BAD_PLANS)
def test_a_flight_plan_that_is_not_an_object_keeps_the_pilot_without_a_plan(base, clean, label, plan) -> None:
    """Issue 82: the pilot stays in the snapshot, stored as having no plan; a truthy one is also one rejected value."""
    index = next(i for i, p in enumerate(base["pilots"]) if p.get("flight_plan"))
    snap = parse_snapshot(fz.replace_entry(base, "pilots", index, {**base["pilots"][index], "flight_plan": plan}))
    p = snap.pilots[index]
    assert snap.malformed == 0 and len(snap.pilots) == len(clean.pilots)
    assert p.departure is None and p.arrival is None and p.route is None and p.aircraft_icao is None
    assert p.rejected == (1 if plan else 0)
    assert snap.bad_plans == (1 if plan else 0)
    assert snap.pilots[:index] + snap.pilots[index + 1:] == clean.pilots[:index] + clean.pilots[index + 1:]


@pytest.mark.parametrize("key", ["cid", "callsign", "logon_time"])
@pytest.mark.parametrize("section", fz.SECTIONS)
def test_an_entry_without_its_identity_is_skipped_and_counted(base, clean, section, key) -> None:
    entry = {k: v for k, v in base[section][2].items() if k != key}
    snap = parse_snapshot(fz.replace_entry(base, section, 2, entry))
    assert snap.malformed == 1
    assert len(snap.pilots if section == "pilots" else snap.controllers) == len(base[section]) - 1


def test_many_bad_entries_at_once_leave_every_good_one(base, clean, hot_indices) -> None:
    """The same property with twelve simultaneous faults in both sections, compared EXACTLY: each fault alone decides
    whether its entry is dropped or kept (and how); the combined snapshot must be the clean one with those outcomes."""
    import random

    rng = random.Random(SEED + 1)
    mutated = fz.wire(base)
    faults: list[tuple[str, int, Any]] = []
    for section in fz.SECTIONS:
        for i in rng.sample(range(len(base[section])), 6):
            _, rep = rng.choice(list(fz.entry_mutations(section, base[section][i])))
            mutated[section][i] = rep
            faults.append((section, i, rep))
    expect = {"pilots": list(clean.pilots), "controllers": list(clean.controllers)}
    dropped = {"pilots": set(), "controllers": set()}
    for section, i, rep in faults:                                   # outcome of each fault on its own
        alone = parse_snapshot(fz.wire(fz.replace_entry(base, section, i, rep)))
        if alone.malformed:
            dropped[section].add(i)
        else:
            expect[section][i] = (alone.pilots if section == "pilots" else alone.controllers)[i]
    snap = parse_snapshot(fz.wire(mutated))
    for section in fz.SECTIONS:
        want = [r for k, r in enumerate(expect[section]) if k not in dropped[section]]
        assert (snap.pilots if section == "pilots" else snap.controllers) == want
    assert snap.malformed == len(dropped["pilots"]) + len(dropped["controllers"])


# ---- a changed feed shape is a poll error, never an empty snapshot (issue 82 follow-up, 92)
@pytest.mark.parametrize("section", fz.SECTIONS)
@pytest.mark.parametrize("shape", [{"a": 1}, "text", 7, True])
def test_a_section_that_is_not_a_list_is_a_feed_error(base, section, shape) -> None:
    with pytest.raises(FeedError):
        parse_snapshot({**base, section: shape})


@pytest.mark.parametrize("general", [None, {}, {"update_timestamp": None}, {"update_timestamp": "junk"}, "x", [], 5])
def test_a_missing_or_invalid_snapshot_time_is_a_feed_error(base, general) -> None:
    with pytest.raises(FeedError):
        parse_snapshot({**base, "general": general})


def test_missing_or_null_sections_are_an_empty_snapshot_not_a_crash(base) -> None:
    snap = parse_snapshot({"general": base["general"], "pilots": None})
    assert (snap.pilots, snap.controllers, snap.malformed) == ([], [], 0)


# ---- the bytes on the wire: truncated JSON, duplicated keys, wrong top-level shapes (through FeedClient)
def _fetch(body: bytes) -> Any:
    client = FeedClient(Settings(), httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=body, headers={"Content-Type": "application/json"}))))
    return asyncio.run(client._get_once())


def test_truncated_json_is_a_feed_error_at_every_cut(base) -> None:
    text = json.dumps(base).encode("utf-8")
    cuts = fz.truncations(text, SEED, 40)
    assert len(cuts) >= 40
    for cut in cuts:
        with pytest.raises(FeedError, match="invalid JSON"):
            _fetch(text[:cut])
    assert _fetch(text).payload is not None  # the whole body is fine


@pytest.mark.parametrize("body", [b"", b"null", b"[]", b'"text"', b"123", b"true", b"\xff\xfe\x00", b"{", b"{}{}"])
def test_wrong_top_level_shapes_are_feed_errors(body) -> None:
    with pytest.raises(FeedError):
        _fetch(body)


def test_a_duplicated_key_takes_the_last_value_and_parses(base, clean) -> None:
    """JSON parsers keep the last duplicate; the collector must then see one consistent entry, not two."""
    text = json.dumps(base)
    first = base["pilots"][0]
    dup = fz.with_duplicate_key(text, "callsign", json.dumps("FIRSTWINS"))   # inserted before the first callsign
    fetched = _fetch(dup.encode("utf-8")).payload
    assert fetched is not None
    snap = parse_snapshot(fetched)
    assert snap.malformed == 0 and len(snap.pilots) == len(clean.pilots)
    assert snap.pilots[0].callsign == first["callsign"]                      # the later (real) value won
    dup2 = text.replace(f'"cid": {first["cid"]},', f'"cid": {first["cid"]}, "cid": "oops",', 1)
    assert dup2 != text
    snap2 = parse_snapshot(json.loads(dup2))
    assert snap2.malformed == 1 and len(snap2.pilots) == len(clean.pilots) - 1   # the last value is unusable
    assert snap2.pilots == clean.pilots[1:]


def test_numeric_literals_the_feed_parser_accepts_do_not_abort_the_snapshot(base, clean) -> None:
    """json.loads turns 1e999 into inf and accepts NaN: the entry is kept with the value nulled, or skipped, never fatal."""
    marked = fz.replace_entry(base, "pilots", 1, {**base["pilots"][1], "altitude": 123456789, "groundspeed": 987654321})
    text = json.dumps(marked).replace("123456789", "1e999").replace("987654321", "NaN")
    snap = parse_snapshot(json.loads(text))
    assert snap.malformed == 0 and snap.pilots[1].altitude is None and snap.pilots[1].rejected >= 1
    assert snap.pilots[:1] + snap.pilots[2:] == clean.pilots[:1] + clean.pilots[2:]
