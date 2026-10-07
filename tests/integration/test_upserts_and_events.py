"""SPEC s7 upsert rules. UNEXECUTED (no Postgres was available when written)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from helpers import apply, at, counts, partition_for

pytestmark = pytest.mark.integration

T1 = datetime(2026, 10, 7, 16, 11, 19, tzinfo=timezone.utc)


@pytest.fixture()
def db(conn):
    partition_for(conn, T1)
    return conn


def pilot(payload: dict, callsign: str) -> dict:
    return next(p for p in payload["pilots"] if p["callsign"] == callsign)


def ctrl(payload: dict, callsign: str) -> dict:
    return next(c for c in payload["controllers"] if c["callsign"] == callsign)


def session(db, callsign: str) -> dict | None:
    return db.execute("SELECT * FROM pilot_session WHERE callsign = %s", (callsign,)).fetchone()


# ------------------------------------------------------------------ pilots
def test_session_upsert_updates_last_seen_and_plan(db, payload, expected, refdata) -> None:
    apply(db, at(payload, 0), refdata)
    first = session(db, "TESTCIRCUIT")
    assert first["first_seen"] == first["last_seen"]
    assert first["arrival"] == "YSSY"

    p2 = at(payload, 15)
    pilot(p2, "TESTCIRCUIT")["flight_plan"].update(
        arrival="YMML", route="DCT BOL", aircraft_faa="B738/Q", altitude="36000", deptime="1100"
    )
    apply(db, p2, refdata)

    second = session(db, "TESTCIRCUIT")
    assert second["id"] == first["id"]
    assert counts(db)["pilot_session"] == expected["pilots_kept_by_polygon"]
    assert second["first_seen"] == first["first_seen"]
    assert (second["last_seen"] - first["last_seen"]).total_seconds() == 15
    assert second["arrival"] == "YMML" and second["departure"] == "YSSY"
    assert second["route"] == "DCT BOL" and second["aircraft_faa"] == "B738/Q"
    assert second["planned_alt"] == "36000" and second["deptime"] == "1100"
    assert second["plan_changed_at"] == second["last_seen"]  # arrival changed at the second snapshot
    n = db.execute(
        "SELECT count(*) AS n FROM pilot_position p JOIN pilot_session s ON s.id = p.session_id "
        "WHERE s.callsign = 'TESTCIRCUIT'"
    ).fetchone()["n"]
    assert n == 2


def test_plan_fields_keep_latest_non_null(db, payload, refdata) -> None:
    apply(db, at(payload, 0), refdata)
    p2 = at(payload, 15)
    pilot(p2, "TESTCIRCUIT")["flight_plan"] = None  # plan withdrawn / not visible
    apply(db, p2, refdata)
    s = session(db, "TESTCIRCUIT")
    assert s["departure"] == "YSSY" and s["arrival"] == "YSSY" and s["aircraft_faa"] == "B738/L"


def test_plan_changed_at_not_bumped_when_dep_arr_unchanged(db, payload, refdata) -> None:
    apply(db, at(payload, 0), refdata)
    before = session(db, "TESTCIRCUIT")["plan_changed_at"]
    p2 = at(payload, 15)
    pilot(p2, "TESTCIRCUIT")["flight_plan"]["route"] = "DCT XYZ"
    apply(db, p2, refdata)
    assert session(db, "TESTCIRCUIT")["plan_changed_at"] == before


def test_max_gs_and_alt_use_greatest(db, payload, refdata) -> None:
    apply(db, at(payload, 0), refdata)  # TESTCIRCUIT gs 140, alt 1500
    p2 = at(payload, 15)
    pilot(p2, "TESTCIRCUIT").update(groundspeed=90, altitude=800)
    apply(db, p2, refdata)
    s = session(db, "TESTCIRCUIT")
    assert (s["max_gs_kt"], s["max_alt_ft"]) == (140, 1500)
    p3 = at(payload, 30)
    pilot(p3, "TESTCIRCUIT").update(groundspeed=210, altitude=4000)
    apply(db, p3, refdata)
    s = session(db, "TESTCIRCUIT")
    assert (s["max_gs_kt"], s["max_alt_ft"]) == (210, 4000)


def test_implausible_gs_does_not_poison_max(db, payload, refdata) -> None:
    apply(db, at(payload, 0), refdata)
    s = session(db, "TESTGS")
    assert s["max_gs_kt"] is None or s["max_gs_kt"] <= 700


def test_new_logon_time_is_a_new_session(db, payload, refdata) -> None:
    apply(db, at(payload, 0), refdata)
    p2 = at(payload, 15)
    pilot(p2, "TESTCIRCUIT")["logon_time"] = "2026-10-07T16:00:00Z"
    apply(db, p2, refdata)
    n = db.execute("SELECT count(*) AS n FROM pilot_session WHERE callsign = 'TESTCIRCUIT'").fetchone()["n"]
    assert n == 2


def test_pilot_leaving_region_keeps_session_and_stops_updating(db, payload, refdata) -> None:
    apply(db, at(payload, 0), refdata)
    p2 = at(payload, 15)
    pilot(p2, "TESTCIRCUIT").update(latitude=51.47, longitude=-0.45)  # now over London: outside
    apply(db, p2, refdata)
    s = session(db, "TESTCIRCUIT")
    assert s["last_seen"] == s["first_seen"]  # last in-region sighting


def test_pilot_entering_region_is_stored_from_first_inside_snapshot(db, payload, refdata) -> None:
    apply(db, at(payload, 0), refdata)
    assert session(db, "TESTNZAA") is None
    p2 = at(payload, 15)
    pilot(p2, "TESTNZAA").update(latitude=-33.9, longitude=151.2)
    apply(db, p2, refdata)
    s = session(db, "TESTNZAA")
    assert s is not None and s["first_seen"] == s["last_seen"]
    assert (s["first_seen"] - T1).total_seconds() == 15


# ------------------------------------------------------------------ controllers
def events(db, table: str, callsign: str) -> list[dict]:
    return db.execute(
        f"SELECT e.* FROM {table} e JOIN controller_session s ON s.id = e.session_id "
        "WHERE s.callsign = %s ORDER BY e.observed_at",
        (callsign,),
    ).fetchall()


def test_rating_and_frequency_events_only_on_change(db, payload, refdata) -> None:
    apply(db, at(payload, 0), refdata)
    assert len(events(db, "controller_rating_event", "ML_APP")) == 1
    assert len(events(db, "controller_frequency_event", "ML_APP")) == 1
    apply(db, at(payload, 15), refdata)  # unchanged
    apply(db, at(payload, 30), refdata)
    assert len(events(db, "controller_rating_event", "ML_APP")) == 1
    assert len(events(db, "controller_frequency_event", "ML_APP")) == 1

    p4 = at(payload, 45)
    ctrl(p4, "ML_APP")["rating"] = 6  # promoted
    apply(db, p4, refdata)
    assert [e["rating"] for e in events(db, "controller_rating_event", "ML_APP")] == [5, 6]
    assert len(events(db, "controller_frequency_event", "ML_APP")) == 1

    p5 = at(payload, 60)
    ctrl(p5, "ML_APP").update(rating=6, frequency="132.100")
    apply(db, p5, refdata)
    assert len(events(db, "controller_rating_event", "ML_APP")) == 2
    freqs = [e["frequency_hz"] for e in events(db, "controller_frequency_event", "ML_APP")]
    assert freqs == [132_000_000, 132_100_000]
    row = db.execute(
        "SELECT rating, frequency_hz, last_seen FROM controller_session WHERE callsign='ML_APP'"
    ).fetchone()
    assert row["rating"] == 6 and row["frequency_hz"] == 132_100_000
    assert (row["last_seen"] - T1).total_seconds() == 60


def test_frequency_to_199_998_records_null_event(db, payload, refdata) -> None:
    apply(db, at(payload, 0), refdata)
    p2 = at(payload, 15)
    ctrl(p2, "ML_APP")["frequency"] = "199.998"
    apply(db, p2, refdata)
    freqs = [e["frequency_hz"] for e in events(db, "controller_frequency_event", "ML_APP")]
    assert freqs == [132_000_000, None]


def test_event_replay_is_idempotent(db, payload, refdata) -> None:
    p = at(payload, 0)
    ctrl(p, "ML_APP")["rating"] = 7
    apply(db, p, refdata)
    n = counts(db)
    apply(db, p, refdata)
    assert counts(db) == n
