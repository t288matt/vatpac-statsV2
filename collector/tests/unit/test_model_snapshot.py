"""SPEC s3 / s7: parse_snapshot against the anonymised fixture."""
from __future__ import annotations

import copy
import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from vatpac.model import Controller, Pilot, Snapshot, clamp_pilot, parse_snapshot


def as_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@pytest.fixture(scope="module")
def snap(fixture_payload: dict) -> Snapshot:
    return parse_snapshot(copy.deepcopy(fixture_payload))


def by_callsign(items: list, callsign: str):
    matches = [i for i in items if i.callsign == callsign]
    assert len(matches) == 1, callsign
    return matches[0]


def test_observed_at_from_update_timestamp_utc(snap: Snapshot) -> None:
    assert snap.observed_at.tzinfo is not None
    assert snap.observed_at.utcoffset() == timedelta(0)
    want = datetime(2026, 10, 7, 16, 11, 19, 143496, tzinfo=UTC)
    assert abs(snap.observed_at - want) < timedelta(milliseconds=1)  # 7-digit fraction in feed


def test_counts(snap: Snapshot, fixture_payload: dict, expected: dict) -> None:
    assert len(snap.pilots) == len(fixture_payload["pilots"]) == expected["pilots_global"]
    assert len(snap.controllers) == len(fixture_payload["controllers"]) == expected["controllers_global"]


def test_global_counts(snap: Snapshot, expected: dict) -> None:
    assert snap.pilots_global == expected["pilots_global"]
    assert snap.controllers_global == expected["controllers_global"]


def test_atis_entries_are_not_controllers(snap: Snapshot, expected: dict) -> None:
    calls = {c.callsign for c in snap.controllers}
    assert not calls & set(expected["atis_callsigns"])
    assert not any(c.callsign.endswith("_ATIS") for c in snap.controllers)


def test_types_and_frozen(snap: Snapshot) -> None:
    p, c = snap.pilots[0], snap.controllers[0]
    assert isinstance(p, Pilot) and isinstance(c, Controller)
    assert dataclasses.is_dataclass(p) and dataclasses.is_dataclass(c)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.callsign = "X"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.callsign = "X"  # type: ignore[misc]


def test_pilot_fields_present() -> None:
    names = {f.name for f in dataclasses.fields(Pilot)}
    assert {
        "cid", "name", "callsign", "server", "logon_time", "latitude", "longitude", "altitude",
        "groundspeed", "heading", "departure", "arrival", "aircraft_icao", "aircraft_faa",
        "flight_rules", "route", "remarks", "planned_alt", "deptime", "pilot_rating", "military_rating",
    } <= names


def test_controller_fields_present() -> None:
    names = {f.name for f in dataclasses.fields(Controller)}
    assert {
        "cid", "name", "callsign", "frequency_text", "facility", "rating", "server", "visual_range",
        "logon_time",
    } <= names


def test_pilot_with_full_plan(snap: Snapshot, fixture_payload: dict) -> None:
    p = by_callsign(snap.pilots, "TESTCIRCUIT")
    raw = next(x for x in fixture_payload["pilots"] if x["callsign"] == "TESTCIRCUIT")
    assert p.cid == raw["cid"] and 9_000_000 <= p.cid <= 9_999_999
    assert p.name == raw["name"] and p.name.startswith("Pilot ")
    assert (p.latitude, p.longitude) == (raw["latitude"], raw["longitude"])
    assert p.departure == "YSSY" and p.arrival == "YSSY"
    assert p.aircraft_icao == "B738"
    assert p.aircraft_faa == "B738/L"
    assert p.flight_rules == "I"
    assert p.route == "DCT"
    assert p.remarks == "RMK/TEST"
    assert p.planned_alt == "35000"
    assert p.deptime == "1000"
    assert p.groundspeed == 140 and p.altitude == 1500
    assert p.heading == 90
    assert as_dt(p.logon_time) == datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
    assert p.pilot_rating == 0 and p.military_rating == 0


def test_pilot_without_plan_has_no_departure_or_arrival(snap: Snapshot) -> None:
    for cs in ("TESTNOPLAN1", "TESTNOPLAN2"):
        p = by_callsign(snap.pilots, cs)
        assert not p.departure and not p.arrival and not p.route


def test_empty_arrival_pilot(snap: Snapshot) -> None:
    p = by_callsign(snap.pilots, "TESTNOARR1")
    assert not (p.arrival or "").strip()
    assert (p.departure or "").strip()


def test_pilot_without_position(snap: Snapshot) -> None:
    p = by_callsign(snap.pilots, "TESTNOPOS")
    assert p.latitude is None and p.longitude is None


def test_implausible_values_survive_parsing_and_are_caught_by_clamp(snap: Snapshot) -> None:
    """The Pilot carries the feed values; clamp_pilot (ingest) is what rejects them."""
    gs_pilot = by_callsign(snap.pilots, "TESTGS")
    assert gs_pilot.groundspeed == 30702
    assert clamp_pilot(gs_pilot.groundspeed, gs_pilot.altitude) == (None, 5000, 1)
    low = by_callsign(snap.pilots, "TESTALTLOW")
    assert clamp_pilot(low.groundspeed, low.altitude) == (120, None, 1)
    neg = by_callsign(snap.pilots, "TESTALT")
    assert neg.altitude == -157
    assert clamp_pilot(neg.groundspeed, neg.altitude) == (120, -157, 0)


def test_total_rejected_values_over_fixture(snap: Snapshot, expected: dict) -> None:
    total = sum(clamp_pilot(p.groundspeed, p.altitude)[2] for p in snap.pilots)
    assert total == expected["rejected_values_expected"]


def test_name_icao_suffix_kept_in_name(snap: Snapshot) -> None:
    assert by_callsign(snap.pilots, "TESTYPDN").name.endswith(" YPDN")


def test_controller_fields(snap: Snapshot, fixture_payload: dict) -> None:
    c = by_callsign(snap.controllers, "ML_APP")
    raw = next(x for x in fixture_payload["controllers"] if x["callsign"] == "ML_APP")
    assert c.frequency_text == "132.000"
    assert c.rating == 5 and c.facility == 5
    assert c.cid == raw["cid"] and c.name.startswith("Controller ")
    assert c.visual_range == 50
    assert as_dt(c.logon_time) == datetime(2026, 10, 7, 9, 0, tzinfo=UTC)
    assert by_callsign(snap.controllers, "BN_GND").frequency_text == "199.998"


@pytest.mark.parametrize(
    "stamp",
    [
        "2026-10-07T16:11:19Z",
        "2026-10-07T16:11:19.123456Z",
        "2026-10-07T16:11:19.1434968Z",
        "2026-10-07T16:11:19+00:00",
    ],
)
def test_timestamp_forms(stamp: str) -> None:
    s = parse_snapshot({"general": {"update_timestamp": stamp}, "pilots": [], "controllers": []})
    assert s.observed_at.utcoffset() == timedelta(0)
    assert s.observed_at.replace(microsecond=0) == datetime(2026, 10, 7, 16, 11, 19, tzinfo=UTC)


def test_empty_lists() -> None:
    s = parse_snapshot({"general": {"update_timestamp": "2026-10-07T16:11:19Z"}, "pilots": [], "controllers": []})
    assert list(s.pilots) == [] and list(s.controllers) == []
    assert s.pilots_global == 0 and s.controllers_global == 0


def test_unknown_top_level_keys_ignored(fixture_payload: dict) -> None:
    payload = copy.deepcopy(fixture_payload)
    payload["something_new"] = {"a": 1}
    payload["prefiles"] = [{"cid": 1, "name": "x"}]
    assert len(parse_snapshot(payload).pilots) == len(fixture_payload["pilots"])


# T36 / ADR-021: logon_time is session identity, kept to whole seconds (truncated, never rounded).
@pytest.mark.parametrize(
    ("raw", "expected_second"),
    [
        ("2026-10-07T03:30:22Z", 22),
        ("2026-10-07T03:30:22.516991Z", 22),     # 6 digits
        ("2026-10-07T03:30:22.5169912Z", 22),    # 7 digits, as the feed sends
        ("2026-10-07T03:30:22.9999999Z", 22),    # truncate, not round: never 23
        ("2026-10-07T03:30:22.000000Z", 22),
        ("2026-10-07T03:30:22.5+00:00", 22),
    ],
)
def test_logon_time_is_truncated_to_whole_seconds_utc(fixture_payload: dict, raw: str, expected_second: int) -> None:
    payload = copy.deepcopy(fixture_payload)
    payload["pilots"][0]["logon_time"] = raw
    payload["controllers"][0]["logon_time"] = raw
    s = parse_snapshot(payload)
    for item in (s.pilots[0], s.controllers[0]):
        assert item.logon_time == datetime(2026, 10, 7, 3, 30, expected_second, tzinfo=UTC)
        assert item.logon_time.microsecond == 0
        assert item.logon_time.utcoffset() == timedelta(0)


def test_logon_time_truncation_leaves_snapshot_time_alone() -> None:
    s = parse_snapshot({"general": {"update_timestamp": "2026-10-07T16:11:19.5Z"}, "pilots": [], "controllers": []})
    assert s.observed_at.microsecond == 500_000


def test_both_payload_shapes_give_the_same_session_key(fixture_payload: dict) -> None:
    a, b = copy.deepcopy(fixture_payload), copy.deepcopy(fixture_payload)
    a["pilots"][0]["logon_time"] = "2026-10-07T03:30:22.516991Z"
    b["pilots"][0]["logon_time"] = "2026-10-07T03:30:22Z"
    assert parse_snapshot(a).pilots[0].logon_time == parse_snapshot(b).pilots[0].logon_time
