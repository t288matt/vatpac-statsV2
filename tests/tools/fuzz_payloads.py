"""Seeded, deterministic generators of malformed feed payloads (issue 103). Test tooling only.

hypothesis is not a dependency of this project, so the cases are generated from fixed lists and a seeded
random.Random: the same seed gives the same cases on every machine, and a failure names its case by label.

A "mutation" replaces ONE entry of ``pilots`` or ``controllers`` (or a part of it) and leaves every other entry alone,
which is what lets the tests state the property: one bad entry never loses the rest of the snapshot.
"""
from __future__ import annotations

import copy
import json
import random
from typing import Any, Iterator

SECTIONS = ("pilots", "controllers")

# values a hostile or changed feed could put in any field
BAD_VALUES: list[tuple[str, Any]] = [
    ("none", None), ("empty", ""), ("blank", "   "), ("word", "abc"), ("zero", 0), ("neg", -1),
    ("huge_int", 10**30), ("huge_neg", -(10**30)), ("int32_over", 2**31), ("int32_under", -(2**31) - 1),
    ("bigint_over", 2**63), ("float_max", 1.7e308), ("inf", float("inf")), ("nan", float("nan")), ("true", True),
    ("list", []), ("list1", [1]), ("dict", {}), ("dict1", {"a": 1}),
    ("surrogate", "ab\ud800cd"), ("nul", "a\x00b"), ("ctrl", "a\x01\x07\x1b\x7f\x85b"), ("rtl", "‮evil‬"),
    ("emoji", "\U0001f600\U0001f9d1‍\U0001f680"), ("combining", "é" * 50), ("tabs_nl", "a\tb\nc\r\nd"),
    ("long_text", "x" * 100_000),
    # incompressible multi-byte text: after the 2000-character cap it is still far over a btree index row (about 2700 bytes)
    ("long_random_cjk", "".join(chr(0x4E00 + (i * 7919) % 20000) for i in range(3000))), ("long_digits", "9" * 400), ("sci_str", "1e999"), ("nan_str", "NaN"),
    ("neg_num_str", "-5"), ("float_str", "12.5"), ("sql", "'; DROP TABLE pilot_session; --"),
    ("bad_ts", "not-a-time"), ("ts_year0", "0000-01-01T00:00:00Z"), ("ts_far", "9999-12-31T23:59:59.9999999Z"),
]

# values that make a non-object flight_plan
BAD_PLANS: list[tuple[str, Any]] = [
    ("list", []), ("list_of_dict", [{"departure": "YSSY"}]), ("str", "YSSY-YMML"), ("int", 7), ("float", 1.5),
    ("true", True), ("false", False), ("zero", 0), ("empty_str", ""), ("none", None),
]

# whole entries that are not objects at all
JUNK_ENTRIES: list[tuple[str, Any]] = [
    ("null", None), ("str", "CALLSIGN"), ("int", 12345), ("float", 1.5), ("true", True), ("false", False),
    ("empty_list", []), ("list", [1, 2, 3]), ("nested_list", [[{"cid": 1}]]), ("empty_obj", {}),
]


def replace_entry(payload: dict[str, Any], section: str, index: int, entry: Any) -> dict[str, Any]:
    out = copy.deepcopy(payload)
    out[section][index] = copy.deepcopy(entry)
    return out


def entry_mutations(section: str, entry: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    """Every (label, replacement) for one entry, in a fixed order."""
    for label, junk in JUNK_ENTRIES:
        yield f"entry={label}", junk
    for key in entry:
        gone = {k: v for k, v in entry.items() if k != key}
        yield f"drop {key}", gone
        for label, bad in BAD_VALUES:
            yield f"{key}={label}", {**entry, key: bad}
    if section == "pilots":
        for label, bad in BAD_PLANS:
            yield f"flight_plan={label}", {**entry, "flight_plan": bad}
        plan = entry.get("flight_plan") if isinstance(entry.get("flight_plan"), dict) else {}
        for key in sorted({*plan, "departure", "arrival", "route", "remarks", "aircraft_short", "altitude"}):
            for label, bad in BAD_VALUES:
                yield f"flight_plan.{key}={label}", {**entry, "flight_plan": {**plan, key: bad}}


def pick_indices(rng: random.Random, payload: dict[str, Any], section: str, inside: set[int], k: int) -> list[int]:
    """k seeded indices of a section: half inside the region (for pilots) so the SQL path is exercised."""
    n = len(payload[section])
    ins = sorted(inside)
    out = rng.sample(ins, min(len(ins), (k + 1) // 2)) if ins else []
    rest = [i for i in range(n) if i not in out]
    return sorted(out + rng.sample(rest, min(len(rest), k - len(out))))


def sample_cases(seed: int, payload: dict[str, Any], inside_pilots: set[int], whitelisted: set[int],
                 count: int) -> list[tuple[str, int, str, Any]]:
    """`count` seeded single-entry cases (section, index, label, replacement) spread over both sections."""
    rng = random.Random(seed)
    cases: list[tuple[str, int, str, Any]] = []
    for section, hot in (("pilots", inside_pilots), ("controllers", whitelisted)):
        for i in pick_indices(rng, payload, section, hot, 8):
            cases += [(section, i, label, rep) for label, rep in entry_mutations(section, payload[section][i])]
    return rng.sample(cases, min(count, len(cases)))


def wire(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload as the collector receives it: through the JSON text (NaN and Infinity survive as the feed's
    literals, lone surrogates as escapes)."""
    loaded = json.loads(json.dumps(payload))
    assert isinstance(loaded, dict)
    return loaded


def with_duplicate_key(text: str, key: str, other_value_json: str) -> str:
    """Repeat the first occurrence of `"key":` in JSON text with a different value before it, so the key appears
    twice in the same object (the later one wins in a JSON parser)."""
    needle = f'"{key}":'
    at = text.index(needle)
    return text[:at] + f'{needle}{other_value_json},' + text[at:]


def truncations(text: bytes, seed: int, count: int) -> list[int]:
    """Cut points: the first byte, the last byte short, the middle of the pilots array, and `count` seeded others."""
    rng = random.Random(seed)
    return sorted({1, len(text) - 1, text.index(b'"pilots"') + 40, *(rng.randrange(1, len(text)) for _ in range(count))})
