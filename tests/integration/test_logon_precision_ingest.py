"""T36 / ADR-021: the feed sometimes sends logon_time with fractional seconds and sometimes without. A session is
identified by (cid, callsign, whole-second logon_time), so a flip in either direction must not create a second one."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from helpers import apply, at, ctrl, events, partition_for, pilot

pytestmark = pytest.mark.integration

T1 = datetime(2026, 10, 7, 16, 11, 19, tzinfo=timezone.utc)
WHOLE = datetime(2026, 10, 7, 3, 30, 22, tzinfo=timezone.utc)
SHAPES = {"six": "2026-10-07T03:30:22.516991Z", "seven": "2026-10-07T03:30:22.5169912Z",
          "none": "2026-10-07T03:30:22Z", "high": "2026-10-07T03:30:22.9999999Z"}


@pytest.fixture()
def db(conn):
    partition_for(conn, T1)
    return conn


def sessions(db, table: str, callsign: str) -> list[dict]:
    return db.execute(f"SELECT * FROM {table} WHERE callsign = %s ORDER BY id", (callsign,)).fetchall()


@pytest.mark.parametrize("sequence", [
    ("seven", "none", "seven"),    # the incident, then the feed puts the fraction back
    ("none", "six", "none"),       # the reverse
    ("six", "seven", "high"),      # different fractions of the same second
])
def test_precision_flip_keeps_one_pilot_session_and_its_positions(db, payload, refdata, sequence) -> None:
    for i, shape in enumerate(sequence):
        snap = at(payload, 15 * i)
        pilot(snap, "TESTCIRCUIT")["logon_time"] = SHAPES[shape]
        apply(db, snap, refdata)
    rows = sessions(db, "pilot_session", "TESTCIRCUIT")
    assert len(rows) == 1
    assert rows[0]["logon_time"] == WHOLE and rows[0]["logon_time"].microsecond == 0
    assert (rows[0]["last_seen"] - rows[0]["first_seen"]).total_seconds() == 15 * (len(sequence) - 1)
    n = db.execute("SELECT count(*) AS n FROM pilot_position WHERE session_id = %s", (rows[0]["id"],)).fetchone()["n"]
    assert n == len(sequence)                                    # positions continue on the same session


@pytest.mark.parametrize("sequence", [("seven", "none", "seven"), ("none", "six", "none")])
def test_precision_flip_keeps_one_controller_session_and_its_events(db, payload, refdata, sequence) -> None:
    for i, shape in enumerate(sequence):
        snap = at(payload, 15 * i)
        ctrl(snap, "ML_APP")["logon_time"] = SHAPES[shape]
        apply(db, snap, refdata)
    rows = sessions(db, "controller_session", "ML_APP")
    assert len(rows) == 1 and rows[0]["logon_time"] == WHOLE
    assert (rows[0]["last_seen"] - rows[0]["first_seen"]).total_seconds() == 15 * (len(sequence) - 1)
    # events are written on change only, so the flip must not look like a new session with a first event
    assert len(events(db, "controller_rating_event", "ML_APP")) == 1
    assert len(events(db, "controller_frequency_event", "ML_APP")) == 1


def test_a_different_second_is_still_a_new_session(db, payload, refdata) -> None:
    first = at(payload, 0)
    pilot(first, "TESTCIRCUIT")["logon_time"] = "2026-10-07T10:00:00.9999999Z"
    apply(db, first, refdata)
    snap = at(payload, 15)
    pilot(snap, "TESTCIRCUIT")["logon_time"] = "2026-10-07T10:00:01.0000001Z"   # one whole second later
    apply(db, snap, refdata)
    assert len(sessions(db, "pilot_session", "TESTCIRCUIT")) == 2
