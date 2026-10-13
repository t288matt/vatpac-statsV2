"""Issue #56: one path from feed payloads to a dashboard number, with no hand-built rows.

The fixture payload is applied as several snapshots through the collector's own apply_snapshot (polygon filter, plausibility
caps, session upserts, positions), flights are written by write_finished_flights (as the collector's five minute job does,
issue 89 phase 3, not yet deployed), and then the flight view and the card 61 panel ("Highest groundspeeds (last 7 days)",
from the dashboard JSON) are read. A mismatch between the two halves (a column the panel reads that the writer does not fill,
a filter that drops what ingest stored) would pass every other test, because those build the rows by hand.

A flight is written only once its pilot has been quiet for 8 hours of stored data and nothing is shown in progress, so the
first test ends the data 9 hours after the last sighting, and the second applies a snapshot in which one pilot has gone for
more than 8 hours while the others are still connected.
"""
from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest
from helpers import TRAFFIC, apply, partition_for, run_panel, set_poll_interval, write_all

pytestmark = pytest.mark.integration

SPEED = "Highest groundspeeds (last 7 days)"


def snapshot_at(payload: dict, when: datetime, drop: tuple[str, ...] = ()) -> dict:
    out = copy.deepcopy(payload)
    out["general"]["update_timestamp"] = when.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    out["pilots"] = [p for p in out["pilots"] if p["callsign"] not in drop]
    return out


def apply_all(conn, payload: dict, refdata, snapshots: list[tuple[datetime, tuple[str, ...]]]) -> None:
    for when, drop in snapshots:
        partition_for(conn, when)
        apply(conn, snapshot_at(payload, when, drop), refdata)


def flight(conn, callsign: str) -> dict:
    return conn.execute("SELECT * FROM flight WHERE callsign = %s", (callsign,)).fetchone()


def test_three_snapshots_become_flights_and_a_panel_row(conn, payload, expected, refdata) -> None:
    t0 = (datetime.now(UTC) - timedelta(days=3)).replace(microsecond=0, second=0)
    apply_all(conn, payload, refdata, [(t0 + timedelta(seconds=15 * i), ()) for i in range(3)])
    assert write_all(conn) > 0                                  # the stored data ends 9 hours after the last sighting

    # the flight view: the flights that pass the gate and the polygon (the audit counted 20), each with three samples
    rows = conn.execute("SELECT callsign, sample_count FROM flight ORDER BY callsign").fetchall()
    assert sorted(r["callsign"] for r in rows) == sorted(expected["pilots_passing_flight_gate_inside"])
    assert {r["sample_count"] for r in rows} == {3}

    qfa = flight(conn, "QFA1")
    assert (qfa["departure"], qfa["arrival"], qfa["aircraft_icao"]) == ("YSSY", "WSSS", "A388")
    assert (qfa["max_gs_kt"], qfa["max_alt_ft"]) == (471, 40000)
    assert qfa["start_time"] == t0 and qfa["end_time"] == t0 + timedelta(seconds=30)
    assert float(qfa["airborne_minutes"]) == 0.5               # two 15 s pairs at 471 kt
    assert float(flight(conn, "TESTYPDN")["airborne_minutes"]) == 0.0      # on the ground at 0 kt
    assert "is_in_progress" not in qfa                          # nothing is shown in progress any more (phase 3)

    # the panel: ranked by peak speed, one row per callsign; implausible and slow pilots are not there
    panel_rows = run_panel(conn, TRAFFIC, SPEED)
    by_callsign = {r["callsign"]: r for r in panel_rows}
    assert by_callsign["QFA1"]["Peak groundspeed (kt)"] == 471 and by_callsign["QFA1"]["Highest altitude (ft)"] == 40000
    assert (by_callsign["QFA1"]["departure"], by_callsign["QFA1"]["arrival"]) == ("YSSY", "WSSS")
    speeds = [r["Peak groundspeed (kt)"] for r in panel_rows]
    assert speeds == sorted(speeds, reverse=True) and len(by_callsign) == len(panel_rows)
    assert "TESTGS" not in by_callsign                          # 30702 kt was stored as NULL by the plausibility cap
    assert "UAE5CL" not in by_callsign                          # peak 32 kt
    assert "TESTNOPOS" not in by_callsign                       # no position, never above 100 kt
    outside = {p["callsign"] for p in payload["pilots"]} - set(expected["pilots_kept_callsigns"])
    assert not outside & set(by_callsign)                       # nobody outside the polygon was ever stored


def test_a_pilot_gone_for_8_hours_is_counted_and_pilots_still_connected_are_not(conn, payload, refdata) -> None:
    set_poll_interval(conn, 30)
    now = datetime.now(UTC).replace(microsecond=0)
    snaps = [(now - timedelta(hours=9, seconds=35), ()), (now - timedelta(hours=9, seconds=5), ()),
             (now - timedelta(seconds=5), ("QFA1",))]           # QFA1 disconnected after the second snapshot, 9 hours ago
    apply_all(conn, payload, refdata, snaps)
    write_all(conn)                                              # the stored data ends 5 s ago

    assert flight(conn, "QFA1") is not None                      # quiet for 9 hours of stored data: written
    assert flight(conn, "UAE10") is None                         # still in the newest snapshot: not quiet, not written
    callsigns = {r["callsign"] for r in run_panel(conn, TRAFFIC, SPEED)}
    assert "QFA1" in callsigns and "UAE10" not in callsigns
