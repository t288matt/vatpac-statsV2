"""Collector hardening from the T30 data-integrity audit (GitHub issues 34, 36, 37, 41, 43), against Postgres."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from helpers import apply, at, partition_for
from test_upserts_and_events import ctrl, pilot

pytestmark = pytest.mark.integration

T1 = datetime(2026, 10, 7, 16, 11, 19, tzinfo=timezone.utc)


@pytest.fixture()
def db(conn):
    partition_for(conn, T1)
    return conn


# issue 36 (F4): nulled values and malformed entries reach rejected_values (and so poll_log and the panel)
def test_nulled_and_malformed_values_count_as_rejected(db, payload, refdata) -> None:
    base = apply(db, at(payload), refdata).rejected_values          # the fixture already carries some bad values
    snap = at(payload, 15)
    p = pilot(snap, "TESTCIRCUIT")
    p["altitude"], p["heading"] = 3_000_000_000, 400                # nulled by parsing: 2
    bad = copy.deepcopy(p)
    bad["callsign"] = ""                                             # malformed entry: 1
    snap["pilots"].append(bad)
    ctrl(snap, "ML_APP")["rating"] = 2**40                           # a whitelisted controller's value dropped: 1
    res = apply(db, snap, refdata)
    assert res.rejected_values == base + 4
    row = db.execute("SELECT p.alt_ft, p.hdg FROM pilot_position p JOIN pilot_session s ON s.id = p.session_id "
                     "WHERE s.callsign = 'TESTCIRCUIT' ORDER BY p.observed_at DESC LIMIT 1").fetchone()
    assert row["alt_ft"] is None and row["hdg"] is None              # the pilot is kept, the two values are NULL


def test_values_of_pilots_outside_the_polygon_are_not_counted(db, payload, refdata) -> None:
    base = apply(db, at(payload), refdata).rejected_values
    snap = at(payload, 15)
    outside = next(q for q in snap["pilots"] if (q["latitude"] or 0) > 0)   # northern hemisphere: outside Australia
    outside["altitude"] = 3_000_000_000
    assert apply(db, snap, refdata).rejected_values == base
