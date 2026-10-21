"""Issue #89 phase 1 (#137): write_finished_flights() fills flight_summary, one row per finished flight, written once.

Every test builds its own sessions and positions, puts a network_snapshot at the time the collector would have stored, and
calls the writer. "Quiet" is measured against the newest snapshot, never the clock, so nothing here depends on now().
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from helpers import partition_for

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
BASE = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)


# ------------------------------------------------------------------ builders
def times(start: datetime, n: int, step_s: int = 60) -> list[datetime]:
    return [start + timedelta(seconds=step_s * i) for i in range(n)]


def snap(conn: Any, t: datetime) -> None:
    """The newest stored snapshot is the writer's clock."""
    conn.execute("INSERT INTO network_snapshot VALUES (%s, 1, 1, 1, 1) ON CONFLICT DO NOTHING", (t,))


def mk(conn: Any, cid: int, ts: list[datetime], *, callsign: str = "QFA1", logon: datetime | None = None,
       dep: str | None = "YSSY", arr: str | None = "YMML", gs: int | list[int] = 250, alt: int = 5000, **cols: Any) -> int:
    """A pilot_session with one position per time in ``ts`` (first_seen and last_seen follow them)."""
    for t in ts:
        partition_for(conn, t)
    fields = {"cid": cid, "callsign": callsign, "logon_time": logon or ts[0].replace(microsecond=0), "first_seen": ts[0],
              "last_seen": ts[-1], "departure": dep, "arrival": arr, **cols}
    sid = conn.execute(
        f"INSERT INTO pilot_session ({', '.join(fields)}) VALUES ({', '.join(['%s'] * len(fields))}) RETURNING id",  # noqa: S608
        list(fields.values())).fetchone()["id"]
    gss = gs if isinstance(gs, list) else [gs] * len(ts)
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) "
                        "VALUES (%s, %s, -33.9, 151.2, %s, %s, 90)", [(t, sid, alt, g) for t, g in zip(ts, gss)])
    return sid


def extend(conn: Any, sid: int, ts: list[datetime], gs: int = 250) -> None:
    """The collector sees a stored session again: more positions and a later last_seen."""
    for t in ts:
        partition_for(conn, t)
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) "
                        "VALUES (%s, %s, -33.9, 151.2, 5000, %s, 90)", [(t, sid, gs) for t in ts])
    conn.execute("UPDATE pilot_session SET last_seen = %s WHERE id = %s", (ts[-1], sid))


def write(conn: Any, n: int = 500) -> int:
    return conn.execute("SELECT write_finished_flights(%s) AS n", (n,)).fetchone()["n"]


def records(conn: Any, cid: int | None = None) -> list[dict]:
    if cid is None:
        return conn.execute("SELECT * FROM flight_summary ORDER BY cid, start_time").fetchall()
    return conn.execute("SELECT * FROM flight_summary WHERE cid = %s ORDER BY start_time", (cid,)).fetchall()


def flown(conn: Any, sid: int) -> datetime | None:
    return conn.execute("SELECT flown_upto FROM pilot_session WHERE id = %s", (sid,)).fetchone()["flown_upto"]


def last_seen(conn: Any, sid: int) -> datetime:
    return conn.execute("SELECT last_seen FROM pilot_session WHERE id = %s", (sid,)).fetchone()["last_seen"]


def gap_cap(conn: Any) -> float:
    return float(conn.execute("SELECT value FROM app_setting WHERE key = 'airborne_gap_cap_seconds'").fetchone()["value"])


# ------------------------------------------------------------------ the table and the setting
def test_the_table_the_column_the_setting_and_the_indexes_exist(conn: Any) -> None:
    cols = {r["column_name"] for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'flight_summary'").fetchall()}
    assert {"session_id", "start_time", "end_time", "cid", "callsign", "departure", "arrival", "session_ids", "logon_time",
            "name", "home_icao", "server", "aircraft_icao", "aircraft_faa", "flight_rules", "route", "remarks", "planned_alt",
            "deptime", "pilot_rating", "military_rating", "airborne_minutes", "max_gs_kt", "max_alt_ft", "sample_count",
            "written_at"} == cols
    assert conn.execute("SELECT 1 AS x FROM information_schema.columns WHERE table_name = 'pilot_session' "
                        "AND column_name = 'flown_upto'").fetchone()
    assert conn.execute("SELECT value FROM app_setting WHERE key = 'flight_quiet_hours'").fetchone()["value"] == "8"
    idx = {r["indexdef"] for r in conn.execute("SELECT indexdef FROM pg_indexes WHERE tablename = 'flight_summary'").fetchall()}
    for col in ("cid", "start_time", "end_time", "logon_time"):
        assert any(f"({col})" in d for d in idx), col
    assert any("PRIMARY KEY" in r["pg_get_constraintdef"] and "(session_id, start_time)" in r["pg_get_constraintdef"]
               for r in conn.execute("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                                     "WHERE conrelid = 'flight_summary'::regclass AND contype = 'p'").fetchall())
    # issue 89 phase 5: the table, its key, its CHECK and its four indexes carry the final name
    assert {r["indexname"] for r in conn.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'flight_summary'").fetchall()} == {
        "flight_summary_pkey", "flight_summary_cid_idx", "flight_summary_start_time_idx", "flight_summary_end_time_idx",
        "flight_summary_logon_time_idx"}
    assert {r["conname"] for r in conn.execute("SELECT conname FROM pg_constraint WHERE conrelid = 'flight_summary'::regclass").fetchall()} == {
        "flight_summary_pkey", "flight_summary_check"}
    partial = conn.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'pilot_session_unwritten_idx'").fetchone()
    assert partial and "WHERE" in partial["indexdef"] and "flown_upto" in partial["indexdef"]


def test_end_time_may_not_be_before_start_time(conn: Any) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO flight_summary (session_id, start_time, end_time, cid, callsign, departure, arrival, "
                     "session_ids, logon_time, sample_count) VALUES (1, %s, %s, 1, 'X', 'A', 'B', ARRAY[1], %s, 1)",
                     (BASE, BASE - MIN, BASE))


# ------------------------------------------------------------------ a plain flight and its numbers
def test_one_session_becomes_one_flight_with_the_right_numbers(conn: Any) -> None:
    ts = times(BASE, 40, 15)                                    # 15 s apart: below the airborne gap cap
    gs = [0] * 5 + [250] * 30 + [10] * 5                        # taxi, airborne, taxi
    sid = mk(conn, 7, ts, gs=gs, alt=7000, name="Pat", server="SYD", aircraft_icao="B738", route="DCT", deptime="0900",
             planned_alt="F350", flight_rules="I", pilot_rating=1, military_rating=0, remarks="RMK")
    snap(conn, ts[-1] + 8 * HOUR)
    assert write(conn) == 1
    (r,) = records(conn, 7)
    assert (r["session_id"], r["session_ids"], r["start_time"], r["end_time"], r["sample_count"]) == (sid, [sid], ts[0], ts[-1], 40)
    assert (r["departure"], r["arrival"], r["callsign"], r["logon_time"]) == ("YSSY", "YMML", "QFA1", ts[0])
    assert (r["max_gs_kt"], r["max_alt_ft"]) == (250, 7000)
    # airborne = real gaps while gs >= 60 at the earlier position: positions 5..34 are fast, pair 34-35 has gs 250 then 10
    assert float(r["airborne_minutes"]) == round(30 * 15 / 60.0, 2)
    assert (r["name"], r["server"], r["aircraft_icao"], r["route"], r["deptime"], r["planned_alt"], r["flight_rules"],
            r["pilot_rating"], r["military_rating"], r["remarks"]) == ("Pat", "SYD", "B738", "DCT", "0900", "F350", "I", 1, 0, "RMK")
    assert r["written_at"] is not None
    assert flown(conn, sid) == ts[-1]                           # marked processed up to last_seen


def test_the_flight_gate_normalises_the_airports(conn: Any) -> None:
    ts = times(BASE, 5)
    mk(conn, 8, ts, dep=" ysSy\t", arr="ymml\n")
    snap(conn, ts[-1] + 8 * HOUR)
    write(conn)
    (r,) = records(conn, 8)
    assert (r["departure"], r["arrival"]) == ("YSSY", "YMML")


def test_a_session_without_a_plan_makes_no_flight_but_is_marked(conn: Any) -> None:
    ts = times(BASE, 5)
    a = mk(conn, 9, ts, dep=None, arr=None)
    b = mk(conn, 10, ts, dep="YSSY", arr="  \t")
    c = mk(conn, 11, ts, dep="", arr="YMML")
    snap(conn, ts[-1] + 8 * HOUR)
    assert write(conn) == 0
    assert records(conn) == []
    assert flown(conn, a) == flown(conn, b) == flown(conn, c) == ts[-1]
    assert write(conn) == 0                                      # and they are not looked at again


def test_a_plan_filed_late_is_in_the_flight(conn: Any) -> None:
    ts = times(BASE, 20)
    sid = mk(conn, 12, ts, dep=None, arr=None)                   # connected without a plan; filed one afterwards
    snap(conn, ts[-1] + 3 * HOUR)
    conn.execute("UPDATE pilot_session SET departure = 'YPPH', arrival = 'YSSY' WHERE id = %s", (sid,))
    snap(conn, ts[-1] + 8 * HOUR)
    assert write(conn) == 1
    (r,) = records(conn, 12)
    assert (r["departure"], r["arrival"], r["sample_count"]) == ("YPPH", "YSSY", 20)


# ------------------------------------------------------------------ the join rule (v1, section 1.4)
@pytest.mark.parametrize("minutes, expected", [(119, 1), (120, 1), (121, 2)], ids=["119-joins", "120-joins", "121-new"])
def test_a_reconnect_joins_when_its_logon_is_within_120_minutes_of_the_previous_last_position(
        conn: Any, minutes: int, expected: int) -> None:
    first = times(BASE, 30)
    prev_end = first[-1]
    logon = prev_end + minutes * MIN
    second = times(logon + MIN, 20)
    a = mk(conn, 20, first)
    b = mk(conn, 20, second, logon=logon)
    snap(conn, second[-1] + 8 * HOUR)
    assert write(conn) == expected
    rows = records(conn, 20)
    assert len(rows) == expected
    if expected == 1:
        assert rows[0]["session_ids"] == [a, b] and rows[0]["session_id"] == a
        assert rows[0]["sample_count"] == 50 and rows[0]["start_time"] == first[0] and rows[0]["end_time"] == second[-1]
    else:
        assert [r["session_ids"] for r in rows] == [[a], [b]]


@pytest.mark.parametrize("logon_after, first_pos_after, expected", [(100, 150, 1), (130, 150, 2)],
                         ids=["logon-in-window-first-position-later-joins", "logon-outside-window"])
def test_the_join_is_measured_from_logon_time_not_the_first_position(
        conn: Any, logon_after: int, first_pos_after: int, expected: int) -> None:
    """v1's literal test: this row's logon_time minus the previous last position. A pilot who logs on, then takes a long time to
    appear in the kept area, is still the same flight."""
    first = times(BASE, 30)
    logon = first[-1] + logon_after * MIN
    mk(conn, 21, first)
    mk(conn, 21, times(first[-1] + first_pos_after * MIN, 10), logon=logon)
    snap(conn, BASE + 20 * HOUR)
    assert write(conn) == expected


def test_overlapping_sessions_of_one_key_join(conn: Any) -> None:
    a = mk(conn, 22, times(BASE, 60))
    b = mk(conn, 22, times(BASE + 30 * MIN, 60), logon=BASE + 30 * MIN)     # logon 30 min BEFORE the previous end
    snap(conn, BASE + 20 * HOUR)
    assert write(conn) == 1
    (r,) = records(conn, 22)
    assert r["session_ids"] == [a, b] and r["sample_count"] == 120 and r["end_time"] == BASE + 89 * MIN


def test_a_different_callsign_departure_or_arrival_is_a_different_flight(conn: Any) -> None:
    t1 = times(BASE, 20)
    t2 = times(BASE + 30 * MIN, 20)
    mk(conn, 23, t1)
    mk(conn, 23, t2, callsign="QFA2")
    mk(conn, 23, t2, callsign="QFA1", logon=t2[0] + timedelta(seconds=1), dep="YBBN")
    mk(conn, 23, t2, callsign="QFA1", logon=t2[0] + timedelta(seconds=2), arr="YPPH")
    snap(conn, BASE + 20 * HOUR)
    assert write(conn) == 4


def test_two_pilots_never_join(conn: Any) -> None:
    mk(conn, 24, times(BASE, 20))
    mk(conn, 25, times(BASE + 10 * MIN, 20), logon=BASE + 10 * MIN)         # same callsign and route, other CID
    snap(conn, BASE + 20 * HOUR)
    assert write(conn) == 2


def test_a_ten_hour_session_is_one_flight(conn: Any) -> None:
    ts = times(BASE, 61, 600)                                                # 10 hours, a position every 10 minutes
    mk(conn, 26, ts)
    snap(conn, ts[-1] + 8 * HOUR)
    assert write(conn) == 1
    (r,) = records(conn, 26)
    assert r["end_time"] - r["start_time"] == timedelta(hours=10) and r["sample_count"] == 61
    assert float(r["airborne_minutes"]) == round(60 * min(600, gap_cap(conn)) / 60.0, 2)   # each pair capped by the setting


def test_a_three_hour_silence_inside_one_session_is_one_flight_with_no_airborne_time_across_it(conn: Any) -> None:
    a = times(BASE, 20, 15)
    b = times(a[-1] + timedelta(hours=3), 20, 15)
    mk(conn, 27, a + b)
    snap(conn, b[-1] + 8 * HOUR)
    assert write(conn) == 1
    (r,) = records(conn, 27)
    assert r["sample_count"] == 40
    assert float(r["airborne_minutes"]) == round(2 * 19 * 15 / 60.0, 2)      # the pair across the silence counts zero


def test_equal_end_times_take_the_plan_fields_of_the_higher_session_id(conn: Any) -> None:
    a = mk(conn, 28, times(BASE, 40), aircraft_icao="B738", name="Low")
    b = mk(conn, 28, times(BASE + 10 * MIN, 30), logon=BASE + 10 * MIN, aircraft_icao="A320", name="High")
    assert last_seen(conn, a) == last_seen(conn, b)                          # overlapping fixture: both end together
    snap(conn, BASE + 20 * HOUR)
    write(conn)
    (r,) = records(conn, 28)
    assert r["session_ids"] == [a, b] and (r["aircraft_icao"], r["name"]) == ("A320", "High")


def test_plan_fields_are_frozen_route_latest_non_null_deptime_maximum_others_from_the_last_session(conn: Any) -> None:
    t1, t2 = times(BASE, 20), times(BASE + 40 * MIN, 20)
    a = mk(conn, 29, t1, route="DCT ABC", deptime="1100", aircraft_icao="B738", name="First", server="S1", flight_rules="V",
           planned_alt="F100", pilot_rating=1)
    b = mk(conn, 29, t2, logon=t2[0], route=None, deptime="1000", aircraft_icao="A320", name="Second", server="S2",
           flight_rules="I", planned_alt=None, pilot_rating=3)
    snap(conn, BASE + 20 * HOUR)
    assert write(conn) == 1
    (before,) = records(conn, 29)
    assert before["session_ids"] == [a, b]
    assert (before["route"], before["deptime"]) == ("DCT ABC", "1100")      # latest non-null route; maximum deptime
    assert (before["aircraft_icao"], before["name"], before["server"], before["flight_rules"], before["planned_alt"],
            before["pilot_rating"]) == ("A320", "Second", "S2", "I", None, 3)  # from the session that ended last, NULL included
    conn.execute("UPDATE pilot_session SET route = 'CHANGED', aircraft_icao = 'C172', deptime = '2359' WHERE id IN (%s, %s)", (a, b))
    snap(conn, BASE + 40 * HOUR)
    assert write(conn) == 0                                                  # nothing to write, and a written row is never rewritten
    (after,) = records(conn, 29)
    assert after == before


# ------------------------------------------------------------------ the quiet period (section 1.3)
def test_a_pilot_quiet_for_7h59m_is_not_written_and_at_8h_is_written(conn: Any) -> None:
    ts = times(BASE, 20)
    sid = mk(conn, 30, ts)
    snap(conn, ts[-1] + 8 * HOUR - MIN)
    assert write(conn) == 0 and flown(conn, sid) is None
    snap(conn, ts[-1] + 8 * HOUR)
    assert write(conn) == 1 and flown(conn, sid) == ts[-1]


def test_cold_start_with_no_snapshot_writes_nothing(conn: Any) -> None:
    sid = mk(conn, 31, times(BASE, 20))
    assert conn.execute("SELECT count(*) AS n FROM network_snapshot").fetchone()["n"] == 0
    assert write(conn) == 0 and records(conn) == [] and flown(conn, sid) is None


def test_the_clock_is_not_used_a_pilot_last_seen_years_ago_is_not_quiet_until_a_snapshot_says_so(conn: Any) -> None:
    ts = times(BASE, 20)
    mk(conn, 32, ts)
    snap(conn, ts[-1] + 2 * HOUR)                                           # now() is far later; the stored data is not
    assert write(conn) == 0


def test_a_collector_outage_cannot_end_a_live_flight(conn: Any) -> None:
    """No snapshots for 9 hours, then a poll shows the pilot still connected: not written, and one flight afterwards."""
    before = times(BASE, 30)
    sid = mk(conn, 33, before)
    snap(conn, before[-1])                                                   # the last poll before the outage
    assert write(conn) == 0                                                  # during the outage the writer sees no change in ref
    after = times(before[-1] + 9 * HOUR, 30)                                 # the collector is back; the pilot is still there
    extend(conn, sid, after)
    snap(conn, after[-1])
    assert write(conn) == 0 and flown(conn, sid) is None                    # last_seen moved with ref: not quiet
    snap(conn, after[-1] + 8 * HOUR)
    assert write(conn) == 1
    (r,) = records(conn, 33)
    assert (r["session_ids"], r["start_time"], r["end_time"], r["sample_count"]) == ([sid], before[0], after[-1], 60)


def test_a_frozen_feed_with_the_collector_polling_cannot_end_a_live_flight(conn: Any) -> None:
    """The real incident of 11-10-2026: the collector keeps polling for 9 hours but the feed timestamp never moves, so every poll is
    skipped_unchanged against the same snapshot and nothing new is stored. The writer's clock (the newest snapshot) must not move."""
    ts = times(BASE, 30)
    sid = mk(conn, 34, ts)
    snap(conn, ts[-1])
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO poll_log (polled_at, status, snapshot_ts) VALUES (%s, 'skipped_unchanged', %s)",
                        [(ts[-1] + timedelta(seconds=30 * (i + 1)), ts[-1]) for i in range(9 * 120)])
    assert conn.execute("SELECT max(observed_at) AS m FROM network_snapshot").fetchone()["m"] == ts[-1]
    assert write(conn) == 0 and flown(conn, sid) is None                     # 9 h of polling later, still not quiet
    resume = times(ts[-1] + 9 * HOUR, 10)                                    # the feed thaws; the pilot is still connected
    extend(conn, sid, resume)
    snap(conn, resume[-1])
    assert write(conn) == 0
    snap(conn, resume[-1] + 8 * HOUR)
    assert write(conn) == 1 and len(records(conn, 34)) == 1


def test_a_statement_timeout_in_the_writer_is_logged_and_the_next_run_does_the_work(conn: Any, caplog: pytest.LogCaptureFixture) -> None:
    """Phase 5: the collector's 5-minute job calls the writer itself (maintenance.refresh_flights). Its statement_timeout is 20 s; a cancel
    (57014) must be contained there (logged, 0 returned), as the old guarded call inside the pieces refresh contained it."""
    from vatpac.maintenance import refresh_flights

    ts = times(BASE, 20)
    mk(conn, 72, ts)
    snap(conn, ts[-1] + 8 * HOUR)
    conn.execute("CREATE FUNCTION slow_insert() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN PERFORM pg_sleep(2); RETURN NEW; END $$")
    conn.execute("CREATE TRIGGER slow_flight_summary BEFORE INSERT ON flight_summary FOR EACH ROW EXECUTE FUNCTION slow_insert()")
    try:
        conn.execute("SET statement_timeout = '700ms'")
        with caplog.at_level("WARNING"):
            n = refresh_flights(conn)
        conn.execute("RESET statement_timeout")
    finally:
        conn.execute("RESET statement_timeout")
        conn.execute("DROP TRIGGER slow_flight_summary ON flight_summary")
        conn.execute("DROP FUNCTION slow_insert()")
    assert n == 0 and records(conn) == [] and any("cancelled" in r.getMessage() for r in caplog.records), caplog.records
    assert write(conn) == 1                                                  # and the next run simply does the work


def test_a_pilot_who_left_during_an_outage_is_written_after_the_first_stored_poll(conn: Any) -> None:
    ts = times(BASE, 30)
    mk(conn, 35, ts)
    snap(conn, ts[-1])
    assert write(conn) == 0
    snap(conn, ts[-1] + 9 * HOUR)                                            # the collector restarts; nobody has this pilot any more
    assert write(conn) == 1


def test_another_pilots_activity_does_not_delay_the_first(conn: Any) -> None:
    a = mk(conn, 36, times(BASE, 20))
    b = mk(conn, 37, times(BASE + 9 * HOUR, 20), logon=BASE + 9 * HOUR, callsign="VOZ1")   # still flying
    snap(conn, BASE + 9 * HOUR + 19 * MIN)
    assert write(conn) == 1
    assert [r["cid"] for r in records(conn)] == [36] and flown(conn, a) is not None and flown(conn, b) is None


def test_a_multi_leg_pilots_legs_are_each_written_when_that_leg_is_quiet(conn: Any) -> None:
    """Quiet per FLIGHT (owner decision 12-10-2026): leg 1 does not wait for leg 2."""
    leg1 = times(BASE, 30)
    leg2 = times(BASE + 2 * HOUR, 30)
    mk(conn, 38, leg1, dep="YSSY", arr="YMML")
    mk(conn, 38, leg2, logon=leg2[0], dep="YMML", arr="YSSY")
    snap(conn, leg1[-1] + 8 * HOUR - MIN)
    assert write(conn) == 0
    snap(conn, leg1[-1] + 8 * HOUR)
    assert write(conn) == 1                                                  # leg 1 only
    snap(conn, leg2[-1] + 8 * HOUR)
    assert write(conn) == 1                                                  # then leg 2


def test_a_returning_session_forms_a_new_flight_and_leaves_the_written_one_alone(conn: Any) -> None:
    ts = times(BASE, 30)
    sid = mk(conn, 39, ts)
    snap(conn, ts[-1] + 8 * HOUR)
    assert write(conn) == 1
    (first,) = records(conn, 39)
    back = times(ts[-1] + 9 * HOUR, 25)                                      # the same connection seen again after 9 h
    extend(conn, sid, back)
    snap(conn, back[-1])
    assert write(conn) == 0                                                  # not quiet yet
    snap(conn, back[-1] + 8 * HOUR)
    assert write(conn) == 1
    rows = records(conn, 39)
    assert len(rows) == 2 and rows[0] == first
    assert (rows[1]["session_id"], rows[1]["start_time"], rows[1]["end_time"], rows[1]["sample_count"]) == (sid, back[0], back[-1], 25)
    assert flown(conn, sid) == back[-1]


def test_a_returning_session_never_joins_a_neighbouring_session_of_its_key(conn: Any) -> None:
    """The unit coming back keeps its old logon_time, which is earlier than anything else; without the rule that would read as a join."""
    r_old = times(BASE, 20)
    r = mk(conn, 43, r_old)
    snap(conn, r_old[-1] + 9 * HOUR)
    assert write(conn) == 1                                                  # R is written and marked
    other = times(BASE + 9 * HOUR, 20)
    s_id = mk(conn, 43, other, logon=other[0])                               # same key, a new connection
    back = times(other[-1] + 10 * MIN, 20)
    extend(conn, r, back)                                                    # R is seen again after S
    snap(conn, back[-1] + 8 * HOUR)
    assert write(conn) == 2
    rows = records(conn, 43)
    assert [x["session_ids"] for x in rows] == [[r], [s_id], [r]]            # in start order: R's first flight, S, R's second


def test_a_changed_quiet_setting_applies_and_a_value_under_2_5_hours_is_refused(conn: Any) -> None:
    ts = times(BASE, 20)
    sid = mk(conn, 40, ts)
    snap(conn, ts[-1] + 7 * HOUR)
    assert write(conn) == 0
    conn.execute("UPDATE app_setting SET value = '6' WHERE key = 'flight_quiet_hours'")
    assert write(conn) == 1                                                  # 7 h of quiet is enough for 6
    conn.execute("UPDATE app_setting SET value = '2.4' WHERE key = 'flight_quiet_hours'")
    ts2 = times(BASE + 20 * HOUR, 20)
    mk(conn, 41, ts2, logon=ts2[0])
    snap(conn, ts2[-1] + 10 * HOUR)
    with pytest.raises(psycopg.errors.RaiseException, match="2.5"):
        write(conn)
    assert len(records(conn)) == 1 and flown(conn, sid) is not None
    conn.execute("UPDATE app_setting SET value = '2.5' WHERE key = 'flight_quiet_hours'")
    assert write(conn) == 1                                                  # the minimum itself is allowed


# ------------------------------------------------------------------ idempotence, the cap, conflicts
def test_a_second_call_writes_nothing(conn: Any) -> None:
    mk(conn, 42, times(BASE, 20))
    snap(conn, BASE + 20 * HOUR)
    assert write(conn) == 1
    rows = records(conn)
    assert write(conn) == 0 and records(conn) == rows


def test_the_batch_cap_limits_pilots_per_call_oldest_quiet_first(conn: Any) -> None:
    for i in range(5):                                                       # CID 50 left first ... CID 54 last
        mk(conn, 50 + i, times(BASE + i * HOUR, 10), logon=BASE + i * HOUR)
    snap(conn, BASE + 40 * HOUR)
    assert write(conn, 2) == 2
    assert [r["cid"] for r in records(conn)] == [50, 51]
    assert write(conn, 2) == 2 and write(conn, 2) == 1 and write(conn, 2) == 0
    assert [r["cid"] for r in records(conn)] == [50, 51, 52, 53, 54]
    assert write(conn, 0) == 0


def test_a_pre_existing_key_skips_that_pilot_unmarked_with_a_warning_and_the_others_are_written(conn: Any) -> None:
    ts = times(BASE, 20)
    a = mk(conn, 60, ts)
    b = mk(conn, 61, ts, callsign="VOZ1")
    conn.execute("INSERT INTO flight_summary (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, "
                 "logon_time, sample_count) VALUES (%s, %s, %s, 60, 'QFA1', 'YSSY', 'YMML', ARRAY[%s::bigint], %s, 20)",
                 (a, ts[0], ts[-1], a, ts[0]))
    notices: list[str] = []
    conn.add_notice_handler(lambda d: notices.append(d.message_primary or ""))
    snap(conn, BASE + 20 * HOUR)
    assert write(conn) == 1
    assert flown(conn, a) is None and flown(conn, b) == ts[-1]
    assert [r["cid"] for r in records(conn)] == [60, 61]
    assert any("skipped" in n and "60" in n for n in notices), notices


def test_a_second_writer_does_not_wait_it_returns_zero(conn: Any, dsn: str) -> None:
    mk(conn, 62, times(BASE, 20))
    snap(conn, BASE + 20 * HOUR)
    with psycopg.connect(dsn) as other:                                     # holds the writer's advisory lock in an open transaction
        other.execute("SELECT pg_advisory_xact_lock(hashtext('vatpac.flight_writer'))")
        assert write(conn) == 0 and records(conn) == []                      # returned at once, wrote nothing
        other.rollback()
    assert write(conn) == 1


def test_a_session_the_collector_is_updating_right_now_is_skipped_not_waited_for(conn: Any, dsn: str) -> None:
    sid = mk(conn, 63, times(BASE, 20))
    snap(conn, BASE + 20 * HOUR)
    with psycopg.connect(dsn) as other:                                     # the collector's upsert would hold this row lock
        other.execute("SELECT id FROM pilot_session WHERE id = %s FOR UPDATE", (sid,))
        conn.execute("SET lock_timeout = '300ms'")
        assert write(conn) == 0                                              # SKIP LOCKED: no wait, no error, nothing written or marked
        assert records(conn, 63) == [] and flown(conn, sid) is None
        other.rollback()
    conn.execute("RESET lock_timeout")
    assert write(conn) == 1


def test_the_writer_is_not_public_and_runs_with_the_owners_rights(conn: Any) -> None:
    row = conn.execute("SELECT prosecdef, proconfig, proacl::text AS acl FROM pg_proc "
                       "WHERE oid = 'write_finished_flights(int)'::regprocedure").fetchone()
    assert row["prosecdef"] is True and "search_path=public, pg_temp" in row["proconfig"]
    assert row["acl"] is not None and "{=X" not in row["acl"] and ",=X" not in row["acl"]


# ------------------------------------------------------------------ the existing 5 minute job runs it, safely
def test_the_collector_job_writes_flights_in_a_small_batch(conn: Any) -> None:
    """maintenance.refresh_flights is what the 5-minute job runs: write_finished_flights with 50 pilots, returning the flights written."""
    from vatpac.maintenance import FLIGHT_WRITE_BATCH, refresh_flights

    assert FLIGHT_WRITE_BATCH == 50
    ts = times(BASE, 20)
    mk(conn, 70, ts)
    snap(conn, ts[-1] + 8 * HOUR)
    assert refresh_flights(conn) == 1 and len(records(conn, 70)) == 1
    assert refresh_flights(conn) == 0                                        # idempotent


def test_a_writer_fault_reaches_the_caller_which_logs_it_and_tries_again_later(conn: Any) -> None:
    """The old guard swallowed a writer fault so the pieces refresh survived; there are no pieces now, so the fault is raised to
    maintenance/the flight loop (main.flight_summary_loop catches Exception and logs it) and nothing is written."""
    from vatpac.maintenance import refresh_flights

    ts = times(BASE, 20)
    mk(conn, 71, ts)
    snap(conn, ts[-1] + 8 * HOUR)
    conn.execute("UPDATE app_setting SET value = '1' WHERE key = 'flight_quiet_hours'")    # makes the writer raise
    with pytest.raises(psycopg.errors.RaiseException):
        refresh_flights(conn)
    assert records(conn) == []
    conn.execute("UPDATE app_setting SET value = '8' WHERE key = 'flight_quiet_hours'")
    assert refresh_flights(conn) == 1


# ------------------------------------------------------------------ backfill of everything since go-live (section 5.1)
def test_the_backfill_rebuilds_every_flight_in_batches_and_the_script_loop_ends_at_zero(conn: Any) -> None:
    expected = 0
    for d in range(4):                                                       # four days of flights, three pilots a day
        for k in range(3):
            day = BASE - timedelta(days=d)
            ts = times(day + k * HOUR, 20)
            mk(conn, 100 + d * 10 + k, ts, logon=ts[0])
            expected += 1
    mk(conn, 99, times(BASE - 5 * HOUR, 10), dep=None, arr=None)            # a no-plan session: marked, no flight
    snap(conn, BASE + 2 * 24 * HOUR)
    total = batches = 0
    while (n := write(conn, 5)) > 0:                                         # what ops/backfill_flights.sh does
        total += n
        batches += 1
    assert total == expected == len(records(conn)) and batches == 3
    assert conn.execute("SELECT count(*) AS n FROM pilot_session WHERE flown_upto IS NULL OR last_seen > flown_upto").fetchone()["n"] == 0


def test_the_backfill_script_loops_until_no_finished_pilot_is_left_not_until_a_batch_writes_zero() -> None:
    text = (REPO / "ops" / "backfill_flights.sh").read_text(encoding="utf-8")
    assert "write_finished_flights(" in text and 'BATCH="${BATCH:-500}"' in text and "sleep 2" in text and "set -eu" in text
    assert '[ "$left" -eq 0 ] && break' in text                           # stops on the remaining count, not on n = 0
    assert '[ "$n" -eq 0 ] && break' not in text and "stuck" in text      # and gives up after rounds without progress


def test_the_backfill_scripts_remaining_query_counts_exactly_the_finished_unprocessed_flights(conn: Any) -> None:
    import re
    text = (REPO / "ops" / "backfill_flights.sh").read_text(encoding="utf-8")
    sql = re.search(r"REMAINING_SQL=\"\$\(cat <<'SQL'\n(.*?)\nSQL\n\)\"", text, re.S).group(1)
    mk(conn, 80, times(BASE, 20))                                           # finished
    mk(conn, 81, times(BASE + 9 * HOUR, 20), logon=BASE + 9 * HOUR)         # still connected
    mk(conn, 82, times(BASE, 20), dep=None, arr=None)                       # finished, no plan: the writer returns 0 for it
    # pilot 83: a finished flight and a later, different flight that is still active (quiet per FLIGHT: the first one counts)
    mk(conn, 83, times(BASE, 20), dep="YSSY", arr="YMML")
    mk(conn, 83, times(BASE + 9 * HOUR, 20), logon=BASE + 9 * HOUR, dep="YMML", arr="YSSY")
    snap(conn, BASE + 9 * HOUR + 19 * MIN)
    assert conn.execute(sql).fetchone()["count"] == 3
    assert write(conn) == 2                                                  # pilots 80 and 83 each write one flight
    assert conn.execute(sql).fetchone()["count"] == 0


# ------------------------------------------------------------------ carry-over of placeholder flights (section 5.3)
# The carry-over block of migration 20261012000190 ran once at the phase 1 deploy, against the old pieces table (expected: zero rows).
# Its test read the pieces and the legacy view, which phase 5 removed; the block itself is history in that migration.

