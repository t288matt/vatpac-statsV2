"""Incremental flight_summary (review H1/M1): equivalence with the SPEC section 5 `flight` semantics (read through `flight_legacy`
since issue 89 phase 3: the pieces and their old merging view are kept until phase 5, `flight` now reads flight_record),
idempotent refresh, and flights surviving the raw-position partition drop.

Expected values are computed independently in Python from the synthetic samples (the old live view is
not kept), so a regression in the SQL shows up as a mismatch.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from drop_gate_helpers import process_flights
from helpers import T0, make_session, partition_for, refresh, run

pytestmark = pytest.mark.integration


def summary(conn, cid: int) -> list[dict]:
    return conn.execute(
        "SELECT gap_segment, span_part, start_time, end_time, sample_count, airborne_minutes "
        "FROM flight_legacy WHERE cid = %s ORDER BY gap_segment, span_part", (cid,)).fetchall()


def expected(samples: list[tuple[datetime, int | None]], cap: float) -> list[tuple]:
    """Reference implementation of the SPEC definition: (gap_segment, span_part, start, end, n, airborne_min)."""
    segs: list[list[int]] = []
    for i, (t, _) in enumerate(samples):
        if i == 0 or t - samples[i - 1][0] > timedelta(minutes=120):
            segs.append([])
        segs[-1].append(i)
    out = []
    for gi, idxs in enumerate(segs, start=1):
        t0 = samples[idxs[0]][0]
        parts: dict[int, list[int]] = {}
        for i in idxs:
            k = max(-(-int((samples[i][0] - t0).total_seconds()) // 28800) - 1, 0)
            parts.setdefault(k, []).append(i)
        for k, members in sorted(parts.items()):
            air = 0.0
            for i in members:
                if i + 1 < len(samples) and samples[i][1] is not None and samples[i][1] >= 60:
                    d = samples[i + 1][0] - samples[i][0]
                    if d <= timedelta(minutes=120):
                        air += min(d.total_seconds(), cap)
            out.append((gi, k, samples[members[0]][0], samples[members[-1]][0], len(members), round(air / 60, 2)))
    return out


def check(conn, cid: int, samples) -> None:
    got = [(r["gap_segment"], r["span_part"], r["start_time"], r["end_time"], r["sample_count"],
            float(r["airborne_minutes"])) for r in summary(conn, cid)]
    cap = float(conn.execute("SELECT value FROM app_setting WHERE key = 'airborne_gap_cap_seconds'").fetchone()["value"])
    assert got == expected(samples, cap)


def test_equivalence_on_mixed_sessions(conn) -> None:
    # plain, gap split, span split, mixed groundspeed incl. null, and a gap combined with a long span
    plain = run(40, gs=250)
    gapped = run(10) + run(10, start=run(10)[-1][0] + timedelta(minutes=121))
    long_span = run(19, step=30 * 60)
    mixed = [(T0 + timedelta(seconds=15 * i), (0 if i < 5 else 250 if i < 30 else 59 if i < 35 else None))
             for i in range(40)]
    resume = long_span[-1][0] + timedelta(minutes=150)
    combo = long_span + [(resume + timedelta(minutes=30 * i), 200) for i in range(20)]
    cases = {9300001: plain, 9300002: gapped, 9300003: long_span, 9300004: mixed, 9300005: combo}
    for cid, samples in cases.items():
        make_session(conn, cid, samples)
    refresh(conn)
    for cid, samples in cases.items():
        check(conn, cid, samples)
    assert len(summary(conn, 9300002)) == 2 and len(summary(conn, 9300003)) == 2


def test_gate_and_normalisation_with_tab(conn) -> None:
    make_session(conn, 9300010, run(5), dep=" ysSy\t", arr="\tymml \r\n")
    make_session(conn, 9300011, run(5), dep="YSSY", arr="\t \r\n")
    make_session(conn, 9300012, run(5), dep=None, arr="YMML")
    refresh(conn)
    rows = conn.execute("SELECT cid, departure, arrival FROM flight_legacy WHERE cid BETWEEN 9300010 AND 9300012").fetchall()
    assert [(r["cid"], r["departure"], r["arrival"]) for r in rows] == [(9300010, "YSSY", "YMML")]
    # the summary holds all three sessions: the gate is applied by the view, so a flight plan filed
    # later makes the flight appear without recomputing anything
    assert conn.execute("SELECT count(*) AS n FROM flight_summary WHERE cid BETWEEN 9300010 AND 9300012").fetchone()["n"] == 3
    conn.execute("UPDATE pilot_session SET departure = 'YSCB' WHERE cid = 9300012")
    assert conn.execute("SELECT departure FROM flight_legacy WHERE cid = 9300012").fetchone()["departure"] == "YSCB"


def test_refresh_is_idempotent_and_incremental(conn) -> None:
    samples = run(30)
    sid = make_session(conn, 9300020, samples[:20])
    assert refresh(conn) == 1
    assert refresh(conn) == 0  # nothing changed: no rows rewritten
    # the session continues: new samples and a later last_seen
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) "
            "VALUES (%s,%s,0,0,5000,250,90)", [(t, sid) for t, _ in samples[20:]])
    conn.execute("UPDATE pilot_session SET last_seen = %s WHERE id = %s", (samples[-1][0], sid))
    assert refresh(conn) == 1
    row = summary(conn, 9300020)
    assert len(row) == 1 and row[0]["sample_count"] == 30
    assert float(row[0]["airborne_minutes"]) == pytest.approx(29 * 15 / 60, abs=0.01)
    assert refresh(conn) == 0


def test_default_window_only_touches_recent_sessions(conn) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    make_session(conn, 9300030, run(10, start=now - timedelta(hours=1)))
    make_session(conn, 9300031, run(10, start=now - timedelta(hours=30)))
    assert refresh(conn, "12 hours") == 1
    assert len(summary(conn, 9300030)) == 1 and summary(conn, 9300031) == []


def test_flights_survive_partition_drop(conn) -> None:
    old = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)  # well beyond 90 days before 2026-10-07
    # one flight spans two daily partitions (22:00 -> 02:00), so the drop order matters
    start = datetime(2026, 6, 1, 22, 0, tzinfo=timezone.utc)
    spanning = run(960, start=start, step=15)  # 4 hours
    short = run(40, start=old)
    make_session(conn, 9300040, spanning)
    make_session(conn, 9300041, short)
    assert summary(conn, 9300040) == []  # never refreshed
    refresh(conn)  # the pieces exist, as live (the 5 minute job)
    check(conn, 9300040, spanning)
    assert process_flights(conn) == 2  # issue 89 phase 4: the gate needs the flights WRITTEN, not the pieces finalised
    dropped = conn.execute("SELECT drop_old_pilot_position_partitions(90, DATE '2026-10-07') AS n").fetchone()["n"]
    assert dropped == 2  # 20260601 and 20260602
    assert conn.execute("SELECT count(*) AS n FROM pg_inherits WHERE inhparent = 'pilot_position'::regclass").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM pilot_position").fetchone()["n"] == 0
    # the written flights hold the numbers although the raw rows are gone
    got = {r["cid"]: (r["sample_count"], r["start_time"], r["end_time"]) for r in conn.execute(
        "SELECT cid, sample_count, start_time, end_time FROM flight_record").fetchall()}
    assert got == {9300040: (960, spanning[0][0], spanning[-1][0]), 9300041: (40, short[0][0], short[-1][0])}
    # the old pieces rows are not touched by the drop (no finalising any more). NOT asserted: what a later FULL refresh of
    # the pieces does to sessions whose positions are gone (it deletes their non-final rows); see docs/RUNBOOKS/drop-gate-rehearsal.md.
    check(conn, 9300040, spanning)
    check(conn, 9300041, short)


def test_pinned_partition_is_kept(conn) -> None:
    old = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    p = partition_for(conn, old)
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
        "VALUES (9300050,'PIN',%s,%s,%s,'YSSY','YMML') RETURNING id",
        (old, old, datetime(2026, 10, 7, 11, tzinfo=timezone.utc))).fetchone()["id"]
    conn.execute("INSERT INTO pilot_position (observed_at, session_id, gs_kt) VALUES (%s,%s,250)", (old, sid))
    assert conn.execute("SELECT drop_old_pilot_position_partitions(90, DATE '2026-10-07') AS n").fetchone()["n"] == 0
    assert conn.execute("SELECT to_regclass(%s) AS r", (p,)).fetchone()["r"] is not None
    assert conn.execute("SELECT count(*) AS n FROM flight_summary WHERE final").fetchone()["n"] == 0  # nothing finalises it now either
