"""SPEC s4.3 / s5 / s6: the `flight` view (gate, normalisation, gap/span split, airborne minutes).

Run against a real PostgreSQL 16.4 on 07-10-2026 (all pass). Sessions are inserted synthetically, so
these tests do not depend on the collector code. They DO assume the view exposes the columns
named in SPEC s6 (airborne_minutes, sample_count, is_circuit, is_zzzz, touches_home_region) plus
cid, departure and arrival. SPEC does not list the view's full column set (see tests/README.md).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from helpers import partition_for

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)


def make_session(conn, cid: int, samples: list[tuple[datetime, int | None]], dep="YSSY", arr="YMML") -> int:
    """Insert a pilot_session and one position per (observed_at, gs) sample."""
    first, last = samples[0][0], samples[-1][0]
    for t, _ in samples:
        partition_for(conn, t)
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (cid, f"TEST{cid}", first, first, last, dep, arr),
    ).fetchone()["id"]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) "
            "VALUES (%s,%s,-33.9,151.2,5000,%s,90)",
            [(t, sid, gs) for t, gs in samples],
        )
    return sid


def run(n: int, start: datetime = T0, step: int = 15, gs: int | None = 250):
    return [(start + timedelta(seconds=step * i), gs) for i in range(n)]


def flights(conn, cid: int) -> list[dict]:
    mv = conn.execute("SELECT 1 AS x FROM pg_matviews WHERE matviewname = 'flight'").fetchone()
    if mv:
        conn.execute("REFRESH MATERIALIZED VIEW flight")
    return conn.execute("SELECT * FROM flight WHERE cid = %s ORDER BY 1", (cid,)).fetchall()


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


# ------------------------------------------------------------------ split
def test_gap_over_120_minutes_splits(conn) -> None:
    first = run(10)
    resume = first[-1][0] + timedelta(minutes=121)
    make_session(conn, 9200020, first + run(10, start=resume))
    rows = flights(conn, 9200020)
    assert len(rows) == 2
    assert sorted(col(r, "sample_count") for r in rows) == [10, 10]


def test_gap_of_exactly_120_minutes_does_not_split(conn) -> None:
    first = run(10)
    resume = first[-1][0] + timedelta(minutes=120)
    make_session(conn, 9200021, first + run(10, start=resume))
    rows = flights(conn, 9200021)
    assert len(rows) == 1 and col(rows[0], "sample_count") == 20


def test_span_over_8_hours_splits(conn) -> None:
    samples = run(19, step=30 * 60)  # 18 half-hour gaps = 9 hours, every gap well under 120 min
    make_session(conn, 9200022, samples)
    rows = flights(conn, 9200022)
    assert len(rows) == 2
    assert sum(col(r, "sample_count") for r in rows) == 19


def test_span_of_exactly_8_hours_does_not_split(conn) -> None:
    make_session(conn, 9200023, run(17, step=30 * 60))  # 16 gaps = 8 hours exactly
    assert len(flights(conn, 9200023)) == 1


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


def test_airborne_gap_is_capped_at_40_seconds(conn) -> None:
    t = T0
    make_session(conn, 9200032, [(t, 250), (t + timedelta(seconds=100), 250)])
    row = flights(conn, 9200032)[0]
    assert float(col(row, "airborne_minutes")) == pytest.approx(40 / 60, abs=0.01)


def test_ground_flight_has_zero_airborne_minutes(conn) -> None:
    make_session(conn, 9200033, run(20, gs=0))
    assert float(col(flights(conn, 9200033)[0], "airborne_minutes")) == 0.0


def test_null_groundspeed_is_not_airborne(conn) -> None:
    make_session(conn, 9200034, run(20, gs=None))
    assert float(col(flights(conn, 9200034)[0], "airborne_minutes")) == 0.0
