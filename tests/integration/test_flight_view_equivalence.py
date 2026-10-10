"""Issue #89: the `flight` view (migration 20261009000170, fast path for single-session groups) returns exactly what the
previous definition (migration 20261009000130, one chain computation over every row) returned.

The previous definition is read from its migration file and created as `flight_old` for the duration of each test, with
`flight_ext_old`, `movements_old` and `cohorts_old` on top of it (the dependants' current text, pointed at the old
view). Rows are compared both ways with EXCEPT ALL, so duplicates count, and column names, order and types are compared.
Cases are written out by hand (the 120 minute boundary, three-session chains, changed plans, circuits, ZZZZ, blank
plans, placeholder rows, span and gap splits, overlap, in progress) and then a seeded random mix.
"""
from __future__ import annotations

import random
import re
from datetime import datetime, timedelta, timezone

import pytest

from helpers import REPO

pytestmark = pytest.mark.integration

OLD_MIGRATION = REPO / "db" / "migrations" / "20261009000130_flight_in_progress.sql"
T0 = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
NOW = datetime.now(timezone.utc)


@pytest.fixture()
def old_views(conn):
    text = OLD_MIGRATION.read_text(encoding="utf-8")
    m = re.search(r"CREATE OR REPLACE VIEW flight AS(.*?GROUP BY c\.cid, c\.callsign, c\.departure, c\.arrival, c\.chain);", text, re.S)
    assert m, "old flight definition not found"
    conn.execute("CREATE OR REPLACE VIEW flight_old AS" + m.group(1))
    for new, old, pat, rep in (("flight_ext", "flight_ext_old", r"\mflight\M", "flight_old"),
                               ("movements", "movements_old", r"\mflight_ext\M", "flight_ext_old"),
                               ("cohorts", "cohorts_old", r"\mflight_ext\M", "flight_ext_old")):
        body = conn.execute("SELECT pg_get_viewdef(%s::regclass) AS d", (new,)).fetchone()["d"]
        conn.execute(f"CREATE OR REPLACE VIEW {old} AS {re.sub(pat, rep, body)}")
    try:
        yield
    finally:
        conn.execute("DROP VIEW IF EXISTS cohorts_old, movements_old, flight_ext_old, flight_old")


def add(conn, cid: int, cs: str, start: datetime, end: datetime, dep: str | None = "YSSY", arr: str | None = "YMML",
        parts: list[tuple[int, int, datetime, datetime]] | None = None, airborne: float | None = 30.0, **kw) -> int:
    """A pilot_session with its flight_summary rows. parts = [(gap_segment, span_part, start, end)]."""
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival, name, home_icao, server,"
        " aircraft_icao, remarks) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (cid, cs, start.replace(microsecond=0), start, end, dep, arr, kw.get("name", f"P{cid}"), kw.get("home", "YSSY"),
         "SYD", kw.get("acft", "B738"), kw.get("remarks")),
    ).fetchone()["id"]
    for gap, span, s, e in parts or [(1, 0, start, end)]:
        conn.execute(
            "INSERT INTO flight_summary (session_id, gap_segment, span_part, cid, start_time, end_time, sample_count, "
            "airborne_minutes, max_gs_kt, max_alt_ft, final) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,250,30000,true)",
            (sid, gap, span, cid, s, e, 0 if airborne is None else int((e - s).total_seconds() // 15), airborne))
    return sid


def mins(m: float) -> timedelta:
    return timedelta(minutes=m)


def assert_identical(conn) -> None:
    for v in ("flight", "flight_ext", "movements", "cohorts"):
        cols = [conn.execute(
            "SELECT string_agg(a.attname || ':' || format_type(a.atttypid, a.atttypmod), ',' ORDER BY a.attnum) AS c "
            "FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped", (name,)).fetchone()["c"]
            for name in (v, v + "_old")]
        assert cols[0] == cols[1], f"{v}: columns differ"
        n = conn.execute(f"SELECT count(*) AS n FROM {v}").fetchone()["n"]
        o = conn.execute(f"SELECT count(*) AS n FROM {v}_old").fetchone()["n"]
        a = conn.execute(f"SELECT count(*) AS n FROM (SELECT * FROM {v} EXCEPT ALL SELECT * FROM {v}_old) x").fetchone()["n"]
        b = conn.execute(f"SELECT count(*) AS n FROM (SELECT * FROM {v}_old EXCEPT ALL SELECT * FROM {v}) x").fetchone()["n"]
        assert n == o and a == 0 and b == 0, f"{v}: new={n} old={o} only_new={a} only_old={b}"


def count(conn, cid: int) -> int:
    return conn.execute("SELECT count(*) AS n FROM flight WHERE cid = %s", (cid,)).fetchone()["n"]


def test_hand_written_cases_are_identical(conn, old_views) -> None:
    t = T0
    add(conn, 100, "AAA1", t, t + mins(90))                                                    # single session
    # reconnect, gap 30 min: merged; gap exactly 120 min: merged; 121 min: not merged
    add(conn, 101, "AAA2", t, t + mins(60)); add(conn, 101, "AAA2", t + mins(90), t + mins(150))
    add(conn, 102, "AAA3", t, t + mins(60)); add(conn, 102, "AAA3", t + mins(180), t + mins(240))
    add(conn, 103, "AAA4", t, t + mins(60)); add(conn, 103, "AAA4", t + mins(181), t + mins(240))
    # chain of three, then a fourth after more than 120 minutes (a new flight)
    add(conn, 104, "AAA5", t, t + mins(60)); add(conn, 104, "AAA5", t + mins(100), t + mins(160))
    add(conn, 104, "AAA5", t + mins(200), t + mins(260)); add(conn, 104, "AAA5", t + mins(500), t + mins(560))
    # changed plan: a different flight
    add(conn, 105, "AAA6", t, t + mins(60), arr="YMML"); add(conn, 105, "AAA6", t + mins(70), t + mins(130), arr="YBBN")
    # circuit, ZZZZ, blank and whitespace/lower-case plans
    add(conn, 106, "AAA7", t, t + mins(30), dep="YSSY", arr="YSSY")
    add(conn, 107, "AAA8", t, t + mins(30), dep="YSSY", arr="ZZZZ"); add(conn, 107, "AAA8", t + mins(40), t + mins(80), dep="YSSY", arr="ZZZZ")
    add(conn, 108, "AAA9", t, t + mins(30), dep=" ", arr="YMML")
    add(conn, 109, "AAB1", t, t + mins(30), dep=None, arr=None)
    add(conn, 110, "AAB2", t, t + mins(30), dep=" ysSy\t", arr="ymml\n"); add(conn, 110, "AAB2", t + mins(40), t + mins(90), dep="YSSY", arr="YMML")
    # placeholder rows (no positions: airborne NULL, no samples), alone and inside a chain
    add(conn, 111, "AAB3", t, t + mins(30), airborne=None)
    add(conn, 112, "AAB4", t, t + mins(30), airborne=None); add(conn, 112, "AAB4", t + mins(50), t + mins(90), airborne=12.5)
    # a session split by a gap (two segments) and by 8 hour spans: parts of one session are never joined
    add(conn, 113, "AAB5", t, t + mins(800), parts=[(1, 0, t, t + mins(100)), (2, 0, t + mins(300), t + mins(780)), (2, 1, t + mins(781), t + mins(800))])
    add(conn, 113, "AAB5", t + mins(850), t + mins(900))                                      # a reconnect after the split session
    add(conn, 114, "AAB6", t, t + mins(120)); add(conn, 114, "AAB6", t + mins(60), t + mins(150))   # overlap
    # an earlier part with a long end hides a short later one: prev_end is the running maximum
    add(conn, 115, "AAB7", t, t + mins(600)); add(conn, 115, "AAB7", t + mins(100), t + mins(130)); add(conn, 115, "AAB7", t + mins(700), t + mins(760))
    add(conn, 116, "AAB8", t, t + mins(60)); add(conn, 117, "AAB8", t + mins(10), t + mins(70))       # two pilots, one callsign
    add(conn, 118, "AAB9", t, t + mins(60)); add(conn, 118, "AAC1", t + mins(10), t + mins(70))       # one pilot, two callsigns
    # the latest part supplies the plan and pilot fields
    add(conn, 119, "AAC2", t, t + mins(60), name="Old", acft="B738", remarks="SEL/ABCD"); add(conn, 119, "AAC2", t + mins(70), t + mins(130), name="New", acft="A320", remarks=None)
    add(conn, 120, "AAC3", t, t + mins(60), dep="EGLL", arr="KJFK"); add(conn, 120, "AAC3", t + mins(70), t + mins(100), dep="EGLL", arr="KJFK")
    # in progress: online_now needs a recent snapshot and a session seen within 30 s of it
    conn.execute("INSERT INTO network_snapshot VALUES (%s,1,1,1,1)", (NOW - timedelta(seconds=5),))
    add(conn, 121, "AAC4", NOW - mins(300), NOW - timedelta(seconds=5))                       # online now, alone
    add(conn, 122, "AAC5", NOW - mins(400), NOW - mins(300)); add(conn, 122, "AAC5", NOW - mins(290), NOW - timedelta(seconds=5))   # merged, newest online
    add(conn, 123, "AAC6", NOW - mins(400), NOW - timedelta(seconds=5)); add(conn, 123, "AAC6", NOW - mins(3000), NOW - mins(2900))
    assert conn.execute("SELECT count(*) AS n FROM flight WHERE is_in_progress").fetchone()["n"] == 3
    assert_identical(conn)
    # the merge rule really fired, so the test does not pass by comparing two views that never merge
    assert (count(conn, 101), count(conn, 102), count(conn, 103), count(conn, 104)) == (1, 1, 2, 2)
    assert count(conn, 113) >= 3 and count(conn, 100) == 1


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_mix_is_identical(conn, old_views, seed) -> None:
    rnd = random.Random(seed)
    airports = ["YSSY", "YMML", "YBBN", "ZZZZ", "EGLL", " ", "ymml"]
    for cid in range(200, 330):
        for cs in rnd.sample(["QFA1", "VOZ2", "JST3"], rnd.randint(1, 2)):
            dep, arr = rnd.choice(airports), rnd.choice(airports)
            t = T0 + timedelta(days=rnd.randint(0, 200), minutes=rnd.randint(0, 1400))
            for _ in range(rnd.choice([1, 1, 1, 2, 2, 3, 4])):
                dur = rnd.choice([10, 45, 120, 300, 600, 900])
                end = t + mins(dur)
                parts = None
                if dur >= 600 and rnd.random() < 0.7:
                    parts = [(1, 0, t, t + mins(480)), (1, 1, t + mins(481), end)]
                elif rnd.random() < 0.15:
                    parts = [(1, 0, t, t + mins(dur / 2)), (2, 0, t + mins(dur / 2 + 130), end + mins(130))]
                    end = end + mins(130)
                add(conn, cid, cs, t, end, dep=dep, arr=arr, parts=parts,
                    airborne=None if rnd.random() < 0.05 else round(dur * 0.6, 2), name=f"N{rnd.randint(0, 3)}")
                if rnd.random() < 0.1:
                    arr = rnd.choice(airports)              # the plan changes part-way
                t = end + mins(rnd.choice([0, 20, 119, 120, 121, 200, 1000]))
    assert conn.execute("SELECT count(*) AS n FROM flight_part").fetchone()["n"] > 400
    assert_identical(conn)
