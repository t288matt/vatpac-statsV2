"""SPEC s4.3 / s5 / s6: the `flight` view (gate, normalisation, no cut inside a connection, airborne minutes).

Since issue 89 phase 3 (not yet deployed) the view reads flight_record, which write_finished_flights() fills once the pilot has
been quiet for 8 hours of stored data (helpers.flights does that). v1's rule applies: one connection is never cut, so a long
silence or a long session is ONE flight (design docs/DESIGN_89_FINAL.md 1.4); the earlier 120-minute and 8-hour splits live on
only in flight_legacy. Sessions are inserted synthetically, so these tests do not depend on the collector code. They DO assume the view exposes the columns
named in SPEC s6 (airborne_minutes, sample_count, is_circuit, is_zzzz, touches_home_region) plus
cid, departure and arrival. SPEC does not list the view's full column set (see tests/README.md).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from helpers import T0, flights, make_session, run

pytestmark = pytest.mark.integration

def col(row: dict, *names: str):
    for n in names:
        if n in row:
            return row[n]
    pytest.fail(f"flight view has none of the columns {names}; has {sorted(row)}")


# ------------------------------------------------------------------ gate
def test_complete_plan_is_a_flight(conn) -> None:
    make_session(conn, 9200001, run(20))
    rows = flights(conn, 9200001)
    assert len(rows) == 1
    assert col(rows[0], "sample_count") == 20


@pytest.mark.parametrize(
    "dep,arr",
    [("YSSY", ""), ("YSSY", "   "), ("YSSY", None), (None, "YMML"), ("", "YMML"), ("  ", "  "), (None, None)],
    ids=["empty-arr", "blank-arr", "null-arr", "null-dep", "empty-dep", "blank-both", "null-both"],
)
def test_gate_excludes_missing_departure_or_arrival(conn, dep, arr) -> None:
    make_session(conn, 9200002, run(20), dep=dep, arr=arr)
    assert flights(conn, 9200002) == []


def test_departure_and_arrival_normalised_upper_trim(conn) -> None:
    make_session(conn, 9200003, run(5), dep="  ysSy ", arr=" ymml\t")
    rows = flights(conn, 9200003)
    assert len(rows) == 1
    assert col(rows[0], "departure", "dep", "dep_icao") == "YSSY"
    assert col(rows[0], "arrival", "arr", "arr_icao") == "YMML"


def test_flags(conn) -> None:
    make_session(conn, 9200010, run(5), dep="YSSY", arr="YSSY")
    make_session(conn, 9200011, run(5), dep="ZZZZ", arr="YMML")
    make_session(conn, 9200012, run(5), dep="EGLL", arr="KJFK")
    make_session(conn, 9200013, run(5), dep="KLAX", arr="YSSY")
    a, b, c, d = (flights(conn, x)[0] for x in (9200010, 9200011, 9200012, 9200013))
    assert col(a, "is_circuit") is True and col(b, "is_circuit") is False
    assert col(b, "is_zzzz") is True and col(a, "is_zzzz") is False
    assert col(a, "touches_home_region") is True  # home prefix Y
    assert col(c, "touches_home_region") is False
    assert col(d, "touches_home_region") is True  # arrives in the home region


# ------------------------------------------------------------------ no cut inside a connection (v1 rule, issue 89)
def test_gap_over_120_minutes_inside_one_session_is_one_flight(conn) -> None:
    first = run(10)
    resume = first[-1][0] + timedelta(minutes=121)
    make_session(conn, 9200020, first + run(10, start=resume))
    rows = flights(conn, 9200020)
    assert len(rows) == 1
    assert col(rows[0], "sample_count") == 20
    assert float(col(rows[0], "airborne_minutes")) == pytest.approx(18 * 15 / 60, abs=0.01)   # nothing counted across the silence


def test_gap_of_exactly_120_minutes_is_one_flight(conn) -> None:
    first = run(10)
    resume = first[-1][0] + timedelta(minutes=120)
    make_session(conn, 9200021, first + run(10, start=resume))
    rows = flights(conn, 9200021)
    assert len(rows) == 1 and col(rows[0], "sample_count") == 20


def test_span_over_8_hours_is_one_flight(conn) -> None:
    samples = run(19, step=30 * 60)  # 18 half-hour gaps = 9 hours, every gap well under 120 min
    make_session(conn, 9200022, samples)
    rows = flights(conn, 9200022)
    assert len(rows) == 1
    assert col(rows[0], "sample_count") == 19
    assert col(rows[0], "duration_minutes") == 540


def test_span_of_exactly_8_hours_is_one_flight(conn) -> None:
    make_session(conn, 9200023, run(17, step=30 * 60))  # 16 gaps = 8 hours exactly
    assert len(flights(conn, 9200023)) == 1


def test_the_view_lists_only_written_flights_a_pilot_still_connected_is_not_in_it(conn) -> None:
    """Nothing is shown in progress: a flight appears once its pilot has been quiet for 8 hours of stored data."""
    make_session(conn, 9200024, run(10))
    conn.execute("INSERT INTO network_snapshot VALUES (%s,1,1,1,1)", (T0 + timedelta(hours=7, minutes=59),))
    conn.execute("SELECT write_finished_flights()")
    assert conn.execute("SELECT count(*) AS n FROM flight WHERE cid = 9200024").fetchone()["n"] == 0
    assert len(flights(conn, 9200024)) == 1


def test_the_old_columns_are_gone(conn) -> None:
    cols = {r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'flight'").fetchall()}
    assert {"gap_segment", "span_part", "is_in_progress"}.isdisjoint(cols)
    assert {"session_id", "cid", "callsign", "logon_time", "start_time", "end_time", "duration", "duration_minutes",
            "airborne_minutes", "max_gs_kt", "max_alt_ft", "sample_count", "is_circuit", "is_zzzz", "touches_home_region"} <= cols


# ------------------------------------------------------------------ airborne minutes
def test_airborne_minutes_sums_gaps_with_gs_at_least_60(conn) -> None:
    make_session(conn, 9200030, run(20, gs=250))
    row = flights(conn, 9200030)[0]
    assert float(col(row, "airborne_minutes")) == pytest.approx(19 * 15 / 60, abs=0.01)  # 4.75


def test_airborne_minutes_uses_gs_at_earlier_sample(conn) -> None:
    t = T0
    make_session(conn, 9200031, [(t, 59), (t + timedelta(seconds=15), 250), (t + timedelta(seconds=30), 250)])
    row = flights(conn, 9200031)[0]
    assert float(col(row, "airborne_minutes")) == pytest.approx(15 / 60, abs=0.01)  # only the 2nd gap


def test_airborne_gap_is_capped_at_the_published_setting(conn) -> None:
    t = T0
    conn.execute("INSERT INTO app_setting (key, value) VALUES ('airborne_gap_cap_seconds', '75') "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")  # the collector publishes 2.5 x the poll interval
    make_session(conn, 9200032, [(t, 250), (t + timedelta(seconds=100), 250)])
    row = flights(conn, 9200032)[0]
    assert float(col(row, "airborne_minutes")) == pytest.approx(75 / 60, abs=0.01)


def test_ground_flight_has_zero_airborne_minutes(conn) -> None:
    make_session(conn, 9200033, run(20, gs=0))
    assert float(col(flights(conn, 9200033)[0], "airborne_minutes")) == 0.0


def test_null_groundspeed_is_not_airborne(conn) -> None:
    make_session(conn, 9200034, run(20, gs=None))
    assert float(col(flights(conn, 9200034)[0], "airborne_minutes")) == 0.0
