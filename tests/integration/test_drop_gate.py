"""Issue #89 phase 4 (#140): the raw-position drop gate reconciles against WRITTEN flights.

Design: docs/DESIGN_89_FINAL.md section 3; migration db/migrations/20261013000210_drop_gate_reconciles_flights.sql; the
requirement-to-test table is docs/CONFORMANCE_89_PHASE4.md. A daily partition of pilot_position may be dropped only when, for
every session with positions in it, (1) the flight writer has processed them (flown_upto), (2) a written flight covers them if
the session has a flight plan, and (3) each flight wholly inside the day has the right sample_count. Anything else keeps the
partition (fail safe) with a NOTICE that starts "DROP REFUSED".

Every date is synthetic: the function takes p_today, so no test waits for a real date. The lock behaviour is in
test_partition_drop_locks.py; the rehearsal script is tested at the end of this file.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from drop_gate_helpers import process_flights, unreconciled
from helpers import REPO, partition_for

pytestmark = pytest.mark.integration

TODAY = date(2026, 10, 7)
FN = "drop_old_pilot_position_partitions"
REFUSED = "DROP REFUSED"


def day(offset: int, hour: int = 12) -> datetime:
    return datetime(2026, 10, 7, hour, tzinfo=timezone.utc) - timedelta(days=offset)


def pname(offset: int) -> str:
    return f"pilot_position_{day(offset):%Y%m%d}"


def exists(conn, offset: int) -> bool:
    return conn.execute("SELECT to_regclass(%s) AS r", (pname(offset),)).fetchone()["r"] is not None


def mk(conn, cid: int, ts: list[datetime], *, dep: str | None = "YSSY", arr: str | None = "YMML", gs: int = 250) -> int:
    """A pilot_session with one position per time (first_seen and last_seen follow them)."""
    for t in ts:
        partition_for(conn, t)
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (cid, f"G{cid}", ts[0].replace(microsecond=0), ts[0], ts[-1], dep, arr)).fetchone()["id"]
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES (%s,%s,5000,%s)",
                        [(t, sid, gs) for t in ts])
    return sid


def mins(start: datetime, n: int, step: int = 1) -> list[datetime]:
    return [start + timedelta(minutes=step * i) for i in range(n)]


def call(conn, retention: int = 90) -> int:
    return conn.execute(f"SELECT {FN}(%s, %s) AS n", (retention, TODAY)).fetchone()["n"]


def notices(conn) -> list[str]:
    seen: list[str] = []
    conn.add_notice_handler(lambda d: seen.append(d.message_primary))
    return seen


def refused(seen: list[str], offset: int) -> list[str]:
    return [s for s in seen if REFUSED in s and pname(offset) in s]


def span(offset: int) -> tuple[datetime, datetime]:
    start = day(offset, 0)
    return start, start + timedelta(days=1)


# ------------------------------------------------------------------ 1 processed: the writer has been through the positions
def test_unprocessed_positions_keep_the_partition_and_after_the_writer_it_drops(conn) -> None:
    sid = mk(conn, 9840001, mins(day(200), 10))
    seen = notices(conn)
    assert call(conn) == 0
    assert exists(conn, 200)
    msg = refused(seen, 200)
    assert len(msg) == 1 and str(sid) in msg[0] and "not yet processed" in msg[0]       # a clear line: partition, reason, session id
    assert process_flights(conn) == 1
    assert call(conn) == 1 and not exists(conn, 200)


def test_a_database_with_no_snapshot_writes_nothing_so_nothing_is_dropped(conn) -> None:
    """Fail safe: if the writer has not run (cold start, stopped collector), nothing is processed and nothing is dropped."""
    mk(conn, 9840002, mins(day(200), 10))
    assert conn.execute("SELECT write_finished_flights(500) AS n").fetchone()["n"] == 0
    assert call(conn) == 0 and exists(conn, 200)


def test_a_pilot_not_yet_quiet_keeps_the_partition(conn) -> None:
    ts = mins(day(200), 10)
    mk(conn, 9840003, ts)
    assert process_flights(conn, ts[-1] + timedelta(hours=7, minutes=59)) == 0          # the writer's clock: 7 h 59 min after
    assert call(conn) == 0 and exists(conn, 200)
    assert process_flights(conn, ts[-1] + timedelta(hours=8)) == 1
    assert call(conn) == 1 and not exists(conn, 200)


def test_a_session_without_a_plan_needs_only_the_marker(conn) -> None:
    sid = mk(conn, 9840004, mins(day(200), 10), dep=None, arr=None)
    assert call(conn) == 0 and exists(conn, 200)                                          # not marked yet
    assert process_flights(conn) == 0                                                     # no flight is written for it
    assert conn.execute("SELECT flown_upto IS NOT NULL AS m FROM pilot_session WHERE id = %s", (sid,)).fetchone()["m"]
    assert call(conn) == 1 and not exists(conn, 200)


def test_a_blank_plan_counts_as_no_plan_like_the_writer(conn) -> None:
    mk(conn, 9840005, mins(day(200), 10), dep="  ", arr="YMML")
    assert process_flights(conn) == 0
    assert call(conn) == 1


def test_a_marker_before_the_last_position_keeps_the_partition(conn) -> None:
    ts = mins(day(200), 10)
    sid = mk(conn, 9840006, ts)
    process_flights(conn)
    conn.execute("UPDATE pilot_session SET flown_upto = %s WHERE id = %s", (ts[-1] - timedelta(seconds=1), sid))
    seen = notices(conn)
    assert call(conn) == 0 and refused(seen, 200)


def test_a_marker_exactly_at_the_last_position_is_enough(conn) -> None:
    ts = mins(day(200), 10)
    sid = mk(conn, 9840007, ts)
    process_flights(conn)
    assert conn.execute("SELECT flown_upto FROM pilot_session WHERE id = %s", (sid,)).fetchone()["flown_upto"] == ts[-1]
    assert call(conn) == 1


# ------------------------------------------------------------------ 2 covered: a written flight holds every position
def test_a_marked_session_whose_flight_is_missing_keeps_the_partition(conn) -> None:
    """A writer bug: the session is marked but its flight row is gone. The marker alone is not trusted."""
    sid = mk(conn, 9840010, mins(day(200), 10))
    process_flights(conn)
    conn.execute("DELETE FROM flight_record WHERE session_id = %s", (sid,))
    seen = notices(conn)
    assert call(conn) == 0 and exists(conn, 200)
    assert "not inside a written flight" in refused(seen, 200)[0]


def test_a_flight_that_stops_short_of_a_position_keeps_the_partition(conn) -> None:
    ts = mins(day(200), 10)
    mk(conn, 9840011, ts)
    process_flights(conn)
    conn.execute("UPDATE flight_record SET end_time = %s", (ts[-1] - timedelta(seconds=1),))   # the last position is outside
    assert call(conn) == 0 and exists(conn, 200)
    conn.execute("UPDATE flight_record SET end_time = %s", (ts[-1],))                         # the boundary is inclusive
    assert call(conn) == 1


def test_a_flight_that_starts_after_a_position_keeps_the_partition(conn) -> None:
    ts = mins(day(200), 10)
    mk(conn, 9840012, ts)
    process_flights(conn)
    conn.execute("UPDATE flight_record SET start_time = %s", (ts[0] + timedelta(seconds=1),))
    assert call(conn) == 0
    conn.execute("UPDATE flight_record SET start_time = %s", (ts[0],))
    assert call(conn) == 1


def test_a_flight_of_another_session_does_not_cover(conn) -> None:
    """Coverage is by session: a flight of the same period belonging to someone else is no cover."""
    ts = mins(day(200), 10)
    a = mk(conn, 9840013, ts)
    b = mk(conn, 9840014, ts)
    process_flights(conn)
    conn.execute("DELETE FROM flight_record WHERE session_id = %s", (a,))
    conn.execute("UPDATE flight_record SET session_ids = ARRAY[%s] WHERE session_id = %s", (b, b))
    assert call(conn) == 0 and exists(conn, 200)


def test_a_session_covered_as_a_later_member_of_a_joined_flight_is_covered(conn) -> None:
    """A reconnect joins the flight (v1 rule): the second session is listed in session_ids but is not the flight's session_id."""
    first = mk(conn, 9840015, mins(day(200), 10))
    second = mk(conn, 9840015, mins(day(200) + timedelta(minutes=30), 10))
    # the second session's logon_time must be inside 120 minutes of the first one's last position: mk() sets it to its first position
    process_flights(conn)
    rec = conn.execute("SELECT session_id, session_ids FROM flight_record").fetchall()
    assert len(rec) == 1 and rec[0]["session_ids"] == [first, second]
    assert call(conn) == 1


def test_a_flight_across_two_days_is_covered_in_both_and_the_second_day_still_drops_after_the_first_is_gone(conn) -> None:
    """The positions of one flight lie in two partitions. Dropping the older day must not make the newer day undroppable (the
    flight's sample_count then no longer matches the stored positions, which is why that count is checked for flights wholly
    inside a day only)."""
    ts = mins(day(200, 22), 240)                              # 22:00 to 02:00 next day
    mk(conn, 9840016, ts)
    process_flights(conn)
    assert conn.execute("SELECT sample_count FROM flight_record").fetchone()["sample_count"] == 240
    assert unreconciled(conn, *span(200)) == [] and unreconciled(conn, *span(199)) == []
    conn.execute(f"DROP TABLE {pname(200)}")                  # as if the older day had gone in an earlier run
    assert unreconciled(conn, *span(199)) == []
    assert call(conn) == 1 and not exists(conn, 199)


def test_a_returning_session_keeps_only_the_partitions_with_its_new_positions(conn) -> None:
    """The session was processed, then comes back and flies on the next day. The old day is still reconciled and drops; the day
    with the new positions waits for the writer; after the writer it drops too."""
    first = mins(day(200), 10)
    sid = mk(conn, 9840018, first)
    process_flights(conn)
    again = mins(day(199), 10)
    partition_for(conn, again[0])
    for t in again:
        conn.execute("INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES (%s,%s,5000,250)", (t, sid))
    conn.execute("UPDATE pilot_session SET last_seen = %s WHERE id = %s", (again[-1], sid))
    seen = notices(conn)
    assert call(conn) == 1                                         # day 200 goes
    assert not exists(conn, 200) and exists(conn, 199)
    assert refused(seen, 199) and not refused(seen, 200)
    assert process_flights(conn) == 1                              # the returning part is a new flight
    assert call(conn) == 1 and not exists(conn, 199)


def test_a_plan_filed_after_the_first_part_was_processed_keeps_the_partition(conn) -> None:
    """Design section 3: a returning session whose plan arrived only after its first part was processed has positions that no
    flight covers (the first part made no flight). Kept for ever until a person looks (fail safe)."""
    ts = mins(day(200), 10)
    sid = mk(conn, 9840019, ts, dep=None, arr=None)
    process_flights(conn)                                          # processed, no flight
    conn.execute("UPDATE pilot_session SET departure = 'YSSY', arrival = 'YMML' WHERE id = %s", (sid,))
    seen = notices(conn)
    assert call(conn) == 0 and exists(conn, 200)
    assert "not inside a written flight" in refused(seen, 200)[0]


# ------------------------------------------------------------------ 3 counted: sample_count against the stored positions
def test_a_flight_wholly_inside_the_day_with_a_wrong_sample_count_keeps_the_partition(conn) -> None:
    sid = mk(conn, 9840020, mins(day(200), 10))
    process_flights(conn)
    conn.execute("UPDATE flight_record SET sample_count = sample_count + 1")
    seen = notices(conn)
    assert call(conn) == 0 and exists(conn, 200)
    assert "sample_count" in refused(seen, 200)[0] and str(sid) in refused(seen, 200)[0]
    conn.execute("UPDATE flight_record SET sample_count = sample_count - 1")
    assert call(conn) == 1


def test_a_flight_with_a_sample_count_of_zero_is_not_counted(conn) -> None:
    """Carried-over placeholders (migration 20) have sample_count 0 and no positions; they never block a drop."""
    mk(conn, 9840021, mins(day(200), 10))
    process_flights(conn)
    conn.execute("INSERT INTO flight_record (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, "
                 "logon_time, sample_count) VALUES (999999, %s, %s, 1, 'X', 'A', 'B', ARRAY[999999], %s, 0)",
                 (day(200, 3), day(200, 4), day(200, 3)))
    assert call(conn) == 1


# ------------------------------------------------------------------ the old rule is gone
def test_the_pieces_table_is_not_a_precondition_either_way(conn) -> None:
    """The old finalisation (flight_summary.final) neither allows nor prevents a drop any more."""
    ts = mins(day(200), 10)
    sid = mk(conn, 9840030, ts)
    conn.execute("SELECT refresh_flight_summary(NULL)")
    conn.execute("UPDATE flight_summary SET final = true")                                 # every piece 'final'
    assert call(conn) == 0 and exists(conn, 200)                                          # still unprocessed: kept
    process_flights(conn)
    conn.execute("DELETE FROM flight_summary")                                             # no pieces at all
    assert call(conn) == 1 and not exists(conn, 200)
    assert conn.execute("SELECT count(*) AS n FROM flight_summary WHERE session_id = %s", (sid,)).fetchone()["n"] == 0


def test_the_function_no_longer_mentions_the_old_finalisation(conn) -> None:
    body = conn.execute("SELECT pg_get_functiondef('drop_old_pilot_position_partitions(int,date)'::regprocedure) AS d").fetchone()["d"]
    for word in ("refresh_flight_summary", "flight_summary", "final"):
        assert word not in body, word
    assert "unreconciled_partition_sessions" in body


# ------------------------------------------------------------------ unchanged behaviour
def test_pinning_still_keeps_a_processed_and_covered_partition(conn) -> None:
    sid = mk(conn, 9840040, mins(day(200), 10))
    process_flights(conn)
    conn.execute("UPDATE pilot_session SET last_seen = %s WHERE id = %s", (day(10), sid))   # seen inside the retention window
    assert call(conn) == 0 and exists(conn, 200)


def test_an_empty_partition_is_dropped(conn) -> None:
    partition_for(conn, day(200))
    assert call(conn) == 1 and not exists(conn, 200)


def test_a_beyond_twice_the_retention_kept_partition_says_so(conn) -> None:
    mk(conn, 9840041, mins(day(200), 10))
    seen = notices(conn)
    assert call(conn, 90) == 0
    assert any("kept beyond twice the retention" in s and pname(200) in s for s in seen)
    seen.clear()
    assert call(conn, 150) == 0                                                           # 200 days is not twice 150
    assert refused(seen, 200) and not any("kept beyond twice" in s for s in seen)


def test_one_bad_partition_does_not_stop_the_good_ones(conn) -> None:
    mk(conn, 9840042, mins(day(201), 10))
    bad = mk(conn, 9840043, mins(day(200), 10))
    mk(conn, 9840044, mins(day(199), 10))
    process_flights(conn)
    conn.execute("DELETE FROM flight_record WHERE session_id = %s", (bad,))
    assert call(conn) == 2
    assert not exists(conn, 201) and exists(conn, 200) and not exists(conn, 199)


def test_a_catch_up_through_the_collector_loop_drops_every_reconciled_day(conn) -> None:
    from vatpac.maintenance import drop_old_partitions
    for i in range(25):
        mk(conn, 9841000 + i, mins(day(300 - i), 5))
    process_flights(conn)
    assert drop_old_partitions(conn, TODAY, 90) == 25


# ------------------------------------------------------------------ the reconciliation function itself
def test_the_reconciliation_names_each_problem_with_its_session(conn) -> None:
    ts = mins(day(200), 10)
    a = mk(conn, 9840050, ts)                       # will be unprocessed
    b = mk(conn, 9840051, ts)                       # will lose its flight
    c = mk(conn, 9840052, ts)                       # fine
    d = mk(conn, 9840053, ts)                       # flight with a wrong count
    process_flights(conn)
    conn.execute("UPDATE pilot_session SET flown_upto = NULL WHERE id = %s", (a,))
    conn.execute("DELETE FROM flight_record WHERE session_id = %s", (b,))
    conn.execute("UPDATE flight_record SET sample_count = 1 WHERE session_id = %s", (d,))
    got = dict(unreconciled(conn, *span(200)))
    assert set(got) == {a, b, d} and c not in got
    assert "not yet processed" in got[a] and "not inside a written flight" in got[b] and "sample_count" in got[d]


def test_a_day_with_no_positions_has_nothing_unreconciled(conn) -> None:
    partition_for(conn, day(200))
    assert unreconciled(conn, *span(200)) == []


def test_a_position_with_no_session_row_is_unreconciled(conn) -> None:
    """Defence in depth: the foreign key normally forbids it; if it were ever absent the gate must not call it reconciled."""
    fk = conn.execute("SELECT conname FROM pg_constraint WHERE conrelid = 'pilot_position'::regclass AND contype = 'f'").fetchall()
    if not fk:
        pytest.skip("no foreign key to remove")
    sid = mk(conn, 9840054, mins(day(200), 3))
    process_flights(conn)
    for r in fk:
        conn.execute(f"ALTER TABLE pilot_position DROP CONSTRAINT {r['conname']}")
    try:
        conn.execute("DELETE FROM pilot_session WHERE id = %s", (sid,))
        assert dict(unreconciled(conn, *span(200))) == {sid: "session row missing"}
    finally:
        conn.execute("TRUNCATE pilot_position, pilot_session, flight_record RESTART IDENTITY CASCADE")
        conn.execute("ALTER TABLE pilot_position ADD FOREIGN KEY (session_id) REFERENCES pilot_session (id)")


def test_the_reconciliation_is_not_granted_to_anyone(conn) -> None:
    assert conn.execute("SELECT has_function_privilege('public', 'unreconciled_partition_sessions(timestamptz, timestamptz)', 'EXECUTE') AS p").fetchone()["p"] is False


def test_a_busy_day_is_reconciled_in_a_few_seconds(conn) -> None:
    """Set-based, not a probe per position: 60 sessions of 1,500 positions each (90,000 positions, about one live day at
    this scale of traffic) must reconcile in seconds, twice per call (steps 1 and 2) and ten times per call at most."""
    start = day(200, 0)
    sids = []
    for i in range(60):
        ts = [start + timedelta(seconds=30 * k + i) for k in range(1500)]
        for t in ts[:1]:
            partition_for(conn, t)
        sid = conn.execute(
            "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
            "VALUES (%s,%s,%s,%s,%s,'YSSY','YMML') RETURNING id",
            (9850000 + i, f"B{i}", ts[0].replace(microsecond=0), ts[0], ts[-1])).fetchone()["id"]
        sids.append(sid)
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES (%s,%s,5000,250)",
                            [(t, sid) for t in ts])
    process_flights(conn)
    t0 = time.time()
    assert unreconciled(conn, *span(200)) == []
    assert time.time() - t0 < 10
    assert call(conn) == 1


# ------------------------------------------------------------------ the rehearsal script (ops/rehearse_drop_gate.sql)
PSQL = shutil.which("psql")


def psql(dsn: str, *args: str) -> subprocess.CompletedProcess[str]:
    assert PSQL, "psql is needed for the rehearsal script test"
    return subprocess.run([PSQL, "-X", "-q", "-v", "ON_ERROR_STOP=1", *args, dsn], capture_output=True, text=True, timeout=120)


@pytest.mark.skipif(PSQL is None, reason="psql not installed (CI has it)")
@pytest.mark.parametrize("as_role", [None, "collector"])
def test_the_rehearsal_script_predicts_exactly_what_the_function_drops(conn, dsn, as_role) -> None:
    """The script reports each partition, runs the drop loop the way the collector does, and checks the outcome against its own
    prediction. Here: three days that reconcile, one that does not (its flight is missing), one pinned by a recent session."""
    mk(conn, 9860001, mins(day(200), 10))
    bad = mk(conn, 9860002, mins(day(199), 10))
    mk(conn, 9860003, mins(day(198), 10))
    pinned = mk(conn, 9860004, mins(day(197), 10))
    process_flights(conn)
    conn.execute("DELETE FROM flight_record WHERE session_id = %s", (bad,))
    conn.execute("UPDATE pilot_session SET last_seen = %s WHERE id = %s", (day(5), pinned))
    extra = ["-v", f"as_role={as_role}"] if as_role else []
    if as_role and not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (as_role,)).fetchone():
        pytest.skip(f"role {as_role} not created")
    r = psql(dsn, "-v", "retention=90", "-v", f"today={TODAY}", *extra, "-f", str(REPO / "ops" / "rehearse_drop_gate.sql"))
    assert r.returncode == 0, r.stderr
    assert "REHEARSAL RESULT: OK" in r.stdout, r.stdout
    assert "gate_too_lax" in r.stdout and (as_role is None or f"as role {as_role}" in r.stdout)
    assert not exists(conn, 200) and not exists(conn, 198)
    assert exists(conn, 199) and exists(conn, 197)


@pytest.mark.skipif(PSQL is None, reason="psql not installed (CI has it)")
def test_the_rehearsal_script_refuses_a_database_not_named_for_testing(dsn) -> None:
    from urllib.parse import urlsplit, urlunsplit
    parts = urlsplit(dsn)                       # the query (time zone options) can itself contain a slash: split properly
    other = urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, ""))
    r = psql(other, "-v", "retention=90", "-v", f"today={TODAY}", "-f", str(REPO / "ops" / "rehearse_drop_gate.sql"))
    assert "REHEARSAL REFUSED" in r.stdout + r.stderr
    assert "REHEARSAL RESULT" not in r.stdout
    assert os.path.exists(REPO / "ops" / "rehearse_drop_gate.sql")


# ------------------------------------------------------------------ review fixes (13-10-2026)
def test_a_temp_table_named_like_a_real_table_cannot_make_the_gate_drop_a_day(conn) -> None:
    """The function is SECURITY DEFINER. With pg_temp first in the search path a caller could shadow pilot_position and
    pilot_session with empty temp tables, so the gate would see nothing to protect and drop the real day."""
    mk(conn, 9870001, mins(day(200), 10))                          # NOT processed: the real day must be kept
    conn.execute("CREATE TEMP TABLE pilot_position (LIKE public.pilot_position)")
    conn.execute("CREATE TEMP TABLE pilot_session (LIKE public.pilot_session INCLUDING DEFAULTS)")
    conn.execute("CREATE TEMP TABLE flight_record (LIKE public.flight_record)")
    assert call(conn) == 0
    conn.execute("DROP TABLE pg_temp.pilot_position, pg_temp.pilot_session, pg_temp.flight_record")
    assert exists(conn, 200)
    cfg = conn.execute("SELECT proconfig FROM pg_proc WHERE oid IN ('drop_old_pilot_position_partitions(int,date)'::regprocedure, "
                       "'unreconciled_partition_sessions(timestamptz,timestamptz)'::regprocedure)").fetchall()
    assert all("search_path=public, pg_temp" in r["proconfig"] for r in cfg) and len(cfg) == 2


def test_the_gate_does_not_depend_on_the_date_style(conn) -> None:
    mk(conn, 9870002, mins(day(200), 10))
    process_flights(conn)
    conn.execute("SET DateStyle = 'SQL, DMY'")
    try:
        assert call(conn) == 1 and not exists(conn, 200)
    finally:
        conn.execute("RESET DateStyle")


def test_a_late_position_inside_a_midnight_spanning_flight_keeps_both_days(conn) -> None:
    """A position lies inside the flight's time range, so per-position coverage passes, but the writer never counted it. While both
    days are stored the count catches it; once the other day is gone the check is skipped and coverage alone stands."""
    ts = mins(day(200, 22), 240)                                   # 22:00 to 02:00 next day
    sid = mk(conn, 9870003, ts)
    process_flights(conn)
    late = ts[10] + timedelta(seconds=30)
    conn.execute("INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES (%s,%s,5000,250)", (late, sid))
    got = dict(unreconciled(conn, *span(200)))
    assert got == {sid: "flight sample_count differs from its stored positions"}
    assert dict(unreconciled(conn, *span(199))) == got
    seen = notices(conn)
    assert call(conn) == 0 and exists(conn, 200) and exists(conn, 199)
    assert refused(seen, 200) and refused(seen, 199)
    conn.execute("UPDATE flight_record SET sample_count = sample_count + 1")        # the count now agrees
    assert call(conn) == 2


def test_a_refusal_is_recorded_for_the_watchdog_and_cleared_when_the_day_reconciles_or_goes(conn) -> None:
    sid = mk(conn, 9870004, mins(day(200), 10))
    assert call(conn) == 0
    row = conn.execute("SELECT part_name, first_refused, last_refused, detail FROM drop_gate_refusal").fetchall()
    assert [r["part_name"] for r in row] == [pname(200)] and "unreconciled" in row[0]["detail"]
    conn.execute("UPDATE drop_gate_refusal SET first_refused = first_refused - interval '5 days'")
    assert call(conn) == 0
    again = conn.execute("SELECT first_refused, last_refused FROM drop_gate_refusal").fetchone()
    assert again["last_refused"] > again["first_refused"] + timedelta(days=4)       # first_refused kept, last_refused moves
    process_flights(conn)
    assert call(conn) == 1
    assert conn.execute("SELECT count(*) AS n FROM drop_gate_refusal").fetchone()["n"] == 0
    assert sid


def test_the_refusal_table_is_readable_by_the_watchdog_role_only_for_reading(conn) -> None:
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'watchdog_ro'").fetchone():
        pytest.skip("role watchdog_ro not created")
    conn.execute("SET ROLE watchdog_ro")
    try:
        conn.execute("SELECT count(*) FROM drop_gate_refusal").fetchone()
        with pytest.raises(Exception):
            conn.execute("DELETE FROM drop_gate_refusal")
    finally:
        conn.execute("RESET ROLE")


# ------------------------------------------------------------------ second review fixes (13-10-2026)
def test_a_position_added_by_a_session_not_in_the_earlier_list_between_check_and_lock_keeps_the_day(conn) -> None:
    """The in-lock check is rebuilt from the partition, not from the sessions listed before the lock. Session x was never in day 199;
    while day 200 is being dropped it receives an old-timestamp position there (modelled by an event trigger on the first DROP)."""
    add = mk(conn, 9880001, mins(day(200), 10))
    mk(conn, 9880002, mins(day(199), 10))
    x = mk(conn, 9880003, mins(day(200, 3), 10))                  # in day 200 only
    process_flights(conn)
    late = day(199, 6)
    partition_for(conn, late)
    conn.execute(f"""
        CREATE FUNCTION add_old_position() RETURNS event_trigger LANGUAGE plpgsql AS $f$
        BEGIN
            INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES ('{late:%Y-%m-%d %H:%M:%S+00}', {x}, 5000, 250)
            ON CONFLICT DO NOTHING;
        END $f$""")
    conn.execute("CREATE EVENT TRIGGER add_old_position_trg ON ddl_command_start WHEN TAG IN ('DROP TABLE') EXECUTE FUNCTION add_old_position()")
    seen = notices(conn)
    try:
        assert call(conn) == 1
    finally:
        conn.execute("DROP EVENT TRIGGER add_old_position_trg")
        conn.execute("DROP FUNCTION add_old_position()")
    assert not exists(conn, 200) and exists(conn, 199) and add
    assert refused(seen, 199)
    assert conn.execute("SELECT count(*) AS n FROM drop_gate_refusal WHERE part_name = %s", (pname(199),)).fetchone()["n"] == 1


def refusal_rows(conn) -> dict[str, str]:
    return {r["part_name"]: r["detail"] for r in conn.execute("SELECT part_name, detail FROM drop_gate_refusal").fetchall()}


def test_a_day_that_cannot_be_locked_is_refused_on_the_record_and_cleared_when_it_goes(conn, dsn) -> None:
    import psycopg
    mk(conn, 9880010, mins(day(200), 10))
    process_flights(conn)
    reader = psycopg.connect(dsn, autocommit=False)
    reader.execute(f"SELECT count(*) FROM {pname(200)}")
    seen = notices(conn)
    try:
        assert call(conn) == 0
    finally:
        reader.rollback()
        reader.close()
    assert refused(seen, 200) and "could not lock" in refused(seen, 200)[0]
    assert "could not lock" in refusal_rows(conn)[pname(200)]
    assert call(conn) == 1 and refusal_rows(conn) == {}


def test_refusal_rows_stay_until_the_day_is_dropped_or_gone_or_not_due(conn) -> None:
    mk(conn, 9880011, mins(day(200), 10))                          # refused (not processed)
    sid = mk(conn, 9880012, mins(day(199), 10))
    process_flights(conn)
    conn.execute("UPDATE pilot_session SET last_seen = %s WHERE id = %s", (day(5), sid))   # day 199 pinned
    conn.execute("DELETE FROM flight_record WHERE session_ids <> ARRAY[0::bigint] AND start_time < %s", (day(199, 23),))
    for part in (pname(200), pname(199), "pilot_position_20200101", pname(3)):
        conn.execute("INSERT INTO drop_gate_refusal (part_name, first_refused, last_refused, detail) VALUES (%s, now() - interval '9 days', now(), 'old')", (part,))
    call(conn)
    rows = refusal_rows(conn)
    assert pname(200) in rows and rows[pname(200)] != "old"        # refreshed, still there (flight deleted: kept)
    assert pname(199) not in rows                                    # pinned is the rule working, not a refusal
    assert "pilot_position_20200101" not in rows                     # partition no longer exists
    assert pname(3) not in rows                                      # not due
