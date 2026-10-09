"""Collector hardening from the T30 data-integrity audit (GitHub issues 34, 36, 37, 41, 43), against Postgres."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from helpers import apply, at, ctrl, partition_for, pilot

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


# issue 43 (F11): free text is capped before it is stored
def test_long_text_is_stored_cut_and_counted(db, payload, refdata) -> None:
    base = apply(db, at(payload), refdata).rejected_values
    snap = at(payload, 15)
    p = pilot(snap, "TESTCIRCUIT")
    p["name"] = "N" * 100_000
    p["flight_plan"] = {**(p.get("flight_plan") or {}), "route": "R" * 4000, "remarks": "M" * 2001}
    ctrl(snap, "ML_APP")["name"] = "C" * 100_000
    res = apply(db, snap, refdata)
    assert res.rejected_values == base + 4
    row = db.execute("SELECT length(name) AS n, length(route) AS r, length(remarks) AS m FROM pilot_session "
                     "WHERE callsign = 'TESTCIRCUIT'").fetchone()
    assert (row["n"], row["r"], row["m"]) == (2000, 2000, 2000)
    row = db.execute("SELECT length(name) AS n FROM controller_session WHERE callsign = 'ML_APP'").fetchone()
    assert row["n"] == 2000


# issue 41 (F9): an older snapshot applied after a newer one must not overwrite its values
def test_older_snapshot_does_not_overwrite_newer_plan_fields(db, payload, refdata) -> None:
    newer = at(payload, 30)
    p = pilot(newer, "TESTCIRCUIT")
    p["name"] = "New Name YMML"
    p["server"] = "NEWSERVER"
    p["flight_plan"] = {**(p.get("flight_plan") or {}), "departure": "YMML", "arrival": "YBBN",
                        "route": "NEW ROUTE", "remarks": "NEW REMARKS", "aircraft_short": "A333"}
    apply(db, newer, refdata)
    q = "SELECT * FROM pilot_session WHERE callsign = 'TESTCIRCUIT'"
    after_newer = db.execute(q).fetchone()
    assert after_newer["departure"] == "YMML"
    apply(db, at(payload), refdata)                                    # 30 s older, original plan
    row = db.execute(q).fetchone()
    for col in ("name", "server", "departure", "arrival", "route", "remarks", "aircraft_icao", "plan_changed_at",
                "home_icao", "last_seen"):
        assert row[col] == after_newer[col], col
    assert row["first_seen"] < after_newer["first_seen"]                 # first_seen still moves back


def test_newer_snapshot_still_updates_and_replay_is_a_no_op(db, payload, refdata) -> None:
    apply(db, at(payload), refdata)
    newer = at(payload, 15)
    pilot(newer, "TESTCIRCUIT")["flight_plan"] = {**(pilot(newer, "TESTCIRCUIT").get("flight_plan") or {}),
                                                  "arrival": "YPAD"}
    apply(db, newer, refdata)
    q = "SELECT arrival, plan_changed_at, xmin::text AS x FROM pilot_session WHERE callsign = 'TESTCIRCUIT'"
    first = db.execute(q).fetchone()
    assert first["arrival"] == "YPAD" and first["plan_changed_at"] is not None
    apply(db, newer, refdata)                                          # the same snapshot again
    assert db.execute(q).fetchone() == first                            # untouched row, same xmin


def _collector(dsn, payload, refdata):
    from psycopg_pool import ConnectionPool

    from vatpac.feed import FetchResult
    from vatpac.filters import ReferenceData
    from vatpac.main import Collector
    from vatpac.settings import Settings

    class Feed:
        def __init__(self) -> None:
            self.payload = payload

        async def fetch(self):
            return FetchResult(self.payload, 200)

    pool = ConnectionPool(dsn, min_size=1, max_size=2, open=True, kwargs={"autocommit": True})
    return Collector(Settings(), ReferenceData(*refdata, "p" * 64, "w" * 64), pool, Feed()), pool


def test_replay_after_a_restart_is_logged_as_skipped(db, dsn, payload, refdata) -> None:
    import asyncio

    first, pool1 = _collector(dsn, at(payload), refdata)
    second, pool2 = _collector(dsn, at(payload), refdata)               # a fresh process: last_ts starts empty
    try:
        assert asyncio.run(first.poll_once()).status == "ok"
        asyncio.run(second.load_last_ts())                               # what run() does at startup
        assert asyncio.run(second.poll_once()).status == "skipped_unchanged"
        second.feed.payload = at(payload, 15)
        assert asyncio.run(second.poll_once()).status == "ok"           # a genuinely newer snapshot is still applied
    finally:
        pool1.close()
        pool2.close()
    rows = db.execute("SELECT status FROM poll_log ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["ok", "skipped_unchanged", "ok"]


def test_startup_ignores_a_future_dated_stored_snapshot_and_an_empty_database(db, dsn, payload, refdata) -> None:
    import asyncio

    col, pool = _collector(dsn, at(payload), refdata)
    try:
        asyncio.run(col.load_last_ts())
        assert col.last_ts is None                                      # nothing stored yet
        apply(db, at(payload), refdata)
        db.execute("INSERT INTO network_snapshot (observed_at, pilots_in_region, controllers_in_region, "
                   "pilots_global, controllers_global) VALUES (now() + interval '3 days', 1, 1, 1, 1)")
        asyncio.run(col.load_last_ts())
        assert col.last_ts == datetime(2026, 10, 7, 16, 11, 19, tzinfo=timezone.utc)  # not the poisoned row
    finally:
        pool.close()
