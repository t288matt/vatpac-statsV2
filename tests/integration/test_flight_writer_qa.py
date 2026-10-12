"""Issue #89 phase 1 (#138): scenarios added from the independent QA list (branch p1-qa, docs/qa_89/scenarios.md).

Ids in the test names (J7, A3 ...) are the QA scenario ids. Cadence 30 s and a 75 s airborne cap, as the coordinator decided.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from test_flight_writer import BASE, HOUR, MIN, extend, flown, mk, records, snap, times, write

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration


def cap75(conn: Any) -> None:
    conn.execute("UPDATE app_setting SET value = '75' WHERE key = 'airborne_gap_cap_seconds'")


def test_j7_the_join_uses_the_running_maximum_of_earlier_ends_not_the_previous_unit(conn: Any) -> None:
    s1 = times(BASE, 301)                                          # 00:00 to 05:00
    mk(conn, 400, s1)
    mk(conn, 400, times(BASE + HOUR, 61), logon=BASE + HOUR)      # overlaps S1, ends 02:00
    mk(conn, 400, times(BASE + 6 * HOUR, 20), logon=BASE + 6 * HOUR)   # 60 min after 05:00 but 240 after 02:00
    mk(conn, 401, s1, callsign="QFA2")
    mk(conn, 401, times(BASE + HOUR, 61), callsign="QFA2", logon=BASE + HOUR)
    mk(conn, 401, times(BASE + 7 * HOUR + MIN, 20), callsign="QFA2", logon=BASE + 7 * HOUR + MIN)   # 121 min after 05:00
    snap(conn, BASE + 30 * HOUR)
    write(conn)
    assert len(records(conn, 400)) == 1 and len(records(conn, 400)[0]["session_ids"]) == 3
    assert sorted(len(r["session_ids"]) for r in records(conn, 401)) == [1, 2]


@pytest.mark.parametrize("silence_min", [119, 120, 121])
def test_j12_a_silence_inside_a_connection_never_cuts(conn: Any, silence_min: int) -> None:
    a = times(BASE, 30)
    b = times(a[-1] + silence_min * MIN, 30)
    mk(conn, 410, a + b)
    snap(conn, b[-1] + 9 * HOUR)
    assert write(conn) == 1


def test_j13_the_flight_id_is_the_session_with_the_first_position_not_the_lowest_id(conn: Any) -> None:
    low_id = mk(conn, 411, times(BASE + 90 * MIN, 30), logon=BASE + 90 * MIN)
    first = mk(conn, 411, times(BASE, 30))
    assert low_id < first
    snap(conn, BASE + 30 * HOUR)
    write(conn)
    (r,) = records(conn, 411)
    assert r["session_id"] == first and set(r["session_ids"]) == {low_id, first} and r["logon_time"] == BASE


def test_j14_logon_time_is_the_earliest_logon_even_from_the_later_session(conn: Any) -> None:
    a = mk(conn, 412, times(BASE + 20 * MIN, 30), logon=BASE)
    b = mk(conn, 412, times(BASE + 5 * MIN, 30), logon=BASE + 10 * MIN)
    snap(conn, BASE + 30 * HOUR)
    write(conn)
    (r,) = records(conn, 412)
    assert r["session_id"] == b and set(r["session_ids"]) == {a, b} and r["logon_time"] == BASE


def test_j10_airports_are_normalised_before_joining(conn: Any) -> None:
    mk(conn, 413, times(BASE, 30), dep="YSSY", arr="YMML")
    mk(conn, 413, times(BASE + 40 * MIN, 30), logon=BASE + 40 * MIN, dep="\r\nysSy ", arr=" ymml\t")
    snap(conn, BASE + 30 * HOUR)
    write(conn)
    (r,) = records(conn, 413)
    assert (r["departure"], r["arrival"], len(r["session_ids"])) == ("YSSY", "YMML", 2)


def test_j16_a_no_plan_session_between_two_planned_ones_takes_no_part_in_the_join(conn: Any) -> None:
    a = mk(conn, 414, times(BASE, 30))
    mid = mk(conn, 414, times(BASE + 60 * MIN, 30), logon=BASE + 60 * MIN, dep=None, arr=None)
    c = mk(conn, 414, times(BASE + 130 * MIN, 30), logon=BASE + 130 * MIN)
    snap(conn, BASE + 30 * HOUR)
    write(conn)
    (r,) = records(conn, 414)
    assert set(r["session_ids"]) == {a, c} and flown(conn, mid) is not None


def test_p3_an_amended_plan_across_a_reconnect_is_two_flights_as_in_v1(conn: Any) -> None:
    mk(conn, 415, times(BASE, 30), arr="YMML")
    mk(conn, 415, times(BASE + 45 * MIN, 30), logon=BASE + 45 * MIN, arr="YBBN")
    snap(conn, BASE + 30 * HOUR)
    assert write(conn) == 2


def test_q7_a_no_plan_session_still_counts_as_pilot_activity(conn: Any) -> None:
    mk(conn, 416, times(BASE, 30))
    other = mk(conn, 416, times(BASE + 6 * HOUR, 30), logon=BASE + 6 * HOUR, dep=None, arr=None)
    snap(conn, BASE + 9 * HOUR)
    assert write(conn) == 0
    snap(conn, BASE + 15 * HOUR)
    assert write(conn) == 1 and flown(conn, other) is not None


def test_q8_a_missing_setting_defaults_to_8_hours_and_a_non_numeric_one_raises(conn: Any) -> None:
    ts = times(BASE, 20)
    mk(conn, 417, ts)
    conn.execute("DELETE FROM app_setting WHERE key = 'flight_quiet_hours'")
    snap(conn, ts[-1] + 8 * HOUR - MIN)
    assert write(conn) == 0
    snap(conn, ts[-1] + 8 * HOUR)
    assert write(conn) == 1
    conn.execute("INSERT INTO app_setting VALUES ('flight_quiet_hours', 'soon')")
    with pytest.raises(psycopg.errors.Error):
        write(conn)


def test_p7_a_single_position_flight_has_start_equal_end_and_no_airborne_time(conn: Any) -> None:
    mk(conn, 418, [BASE])
    snap(conn, BASE + 9 * HOUR)
    write(conn)
    (r,) = records(conn, 418)
    assert r["start_time"] == r["end_time"] and r["sample_count"] == 1 and float(r["airborne_minutes"]) == 0


@pytest.mark.parametrize("gap_s, expected_s", [(30, 30), (60, 60), (75, 75), (100, 75), (119 * 60, 75), (120 * 60, 75),
                                               (120 * 60 + 30, 0), (121 * 60, 0)])
def test_a3_missed_polls_are_capped_and_a_pair_over_120_minutes_counts_zero(conn: Any, gap_s: int, expected_s: int) -> None:
    cap75(conn)
    mk(conn, 419, [BASE, BASE + timedelta(seconds=gap_s)], gs=100)
    snap(conn, BASE + 12 * HOUR)
    write(conn)
    assert float(records(conn, 419)[0]["airborne_minutes"]) == round(expected_s / 60.0, 2)


@pytest.mark.parametrize("cid, gs, expected_s", [(420, [60, 0, 0], 30), (421, [59, 0, 0], 0), (422, [0, 80, 0], 30),
                                                 (423, [None, None, None], 0), (424, [80, 0, 80], 30)])
def test_a2_the_threshold_is_60_kt_at_the_earlier_sample_and_null_is_not_airborne(conn: Any, cid: int, gs: list, expected_s: int) -> None:
    cap75(conn)
    mk(conn, cid, times(BASE, 3, 30), gs=gs)
    snap(conn, BASE + 12 * HOUR)
    write(conn)
    assert float(records(conn, cid)[0]["airborne_minutes"]) == round(expected_s / 60.0, 2)


def test_a4_a_pair_never_spans_two_sessions_of_one_flight(conn: Any) -> None:
    cap75(conn)
    a = times(BASE, 41, 30)                                       # 20 airborne minutes
    b = times(a[-1] + 60 * MIN, 41, 30)
    mk(conn, 425, a)
    mk(conn, 425, b, logon=b[0])
    snap(conn, BASE + 30 * HOUR)
    write(conn)
    assert float(records(conn, 425)[0]["airborne_minutes"]) == 40.00


def test_a5_airborne_is_rounded_once_per_flight_not_per_session(conn: Any) -> None:
    cap75(conn)
    seq = [BASE + timedelta(seconds=s) for s in (0, 30, 60, 90, 100)]       # 30 + 30 + 30 + 10 = 100 s, the last counts 0
    mk(conn, 426, seq, gs=200)
    seq2 = [t + 30 * MIN for t in seq]
    mk(conn, 426, seq2, gs=200, logon=seq2[0])
    snap(conn, BASE + 30 * HOUR)
    write(conn)
    assert float(records(conn, 426)[0]["airborne_minutes"]) == 3.33         # round(200/60, 2); two rounded halves would give 3.34


def test_a6_maxima_ignore_nulls_and_are_null_when_all_are_null(conn: Any) -> None:
    ts = times(BASE, 3, 30)
    s1 = mk(conn, 427, ts, gs=[120, 480, None])
    conn.execute("UPDATE pilot_position SET alt_ft = CASE gs_kt WHEN 120 THEN 3000 WHEN 480 THEN 36000 END WHERE session_id = %s", (s1,))
    s2 = mk(conn, 428, ts, gs=[None, None, None])
    conn.execute("UPDATE pilot_position SET alt_ft = NULL WHERE session_id = %s", (s2,))
    snap(conn, BASE + 12 * HOUR)
    write(conn)
    r = records(conn, 427)[0]
    assert (r["max_gs_kt"], r["max_alt_ft"]) == (480, 36000)
    r = records(conn, 428)[0]
    assert (r["max_gs_kt"], r["max_alt_ft"]) == (None, None)


def test_r2_a_session_with_no_positions_is_marked_and_the_pilot_is_not_picked_again(conn: Any) -> None:
    sid = conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
                       "VALUES (430, 'EMPTY1', %s, %s, %s, 'YSSY', 'YMML') RETURNING id", (BASE, BASE, BASE)).fetchone()["id"]
    snap(conn, BASE + 9 * HOUR)
    assert write(conn) == 0 and flown(conn, sid) == BASE and records(conn) == []


def test_r5_r6_only_positions_after_flown_upto_and_up_to_last_seen_are_used(conn: Any) -> None:
    ts = times(BASE, 20)
    sid = mk(conn, 431, ts)
    snap(conn, ts[-1] + 9 * HOUR)
    write(conn)
    back = times(ts[-1] + 10 * HOUR, 10)
    extend(conn, sid, back)
    conn.execute("INSERT INTO pilot_position (observed_at, session_id, gs_kt) VALUES (%s, %s, 100)",
                 (back[-1] + timedelta(seconds=30), sid))                   # a stray position past last_seen
    snap(conn, back[-1] + 9 * HOUR)
    assert write(conn) == 1
    assert [r["sample_count"] for r in records(conn, 431)] == [20, 10]
    assert flown(conn, sid) == back[-1]


def test_r7_a_skipped_conflict_pilot_is_not_counted_in_the_return_value(conn: Any) -> None:
    ts = times(BASE, 20)
    a = mk(conn, 432, ts)
    mk(conn, 433, ts, callsign="VOZ1")
    mk(conn, 434, ts, callsign="JST1")
    conn.execute("INSERT INTO flight_record (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, "
                 "logon_time, sample_count) VALUES (%s, %s, %s, 432, 'QFA1', 'YSSY', 'YMML', ARRAY[%s::bigint], %s, 20)",
                 (a, ts[0], ts[-1], a, ts[0]))
    snap(conn, BASE + 20 * HOUR)
    assert write(conn) == 2


# ------------------------------------------------------------------ Opus second pass (PR 144)
def test_opus1_a_session_inserted_after_the_lock_is_neither_written_nor_marked(conn: Any, dsn: str) -> None:
    """FOR UPDATE does not block an INSERT. Hold ACCESS EXCLUSIVE on pilot_position so the writer stops at its main statement (after
    the lock and the quiet re-check), insert a new session of the same pilot in that window, then let it go."""
    import threading
    import time

    ts = times(BASE, 20)
    a = mk(conn, 440, ts)
    snap(conn, ts[-1] + 9 * HOUR)
    out: dict[str, int] = {}

    def run() -> None:
        with psycopg.connect(dsn, autocommit=True) as c:
            out["n"] = c.execute("SELECT write_finished_flights()").fetchone()[0]

    holder = psycopg.connect(dsn)
    holder.execute("LOCK TABLE pilot_position IN ACCESS EXCLUSIVE MODE")
    t = threading.Thread(target=run)
    t.start()
    try:
        for _ in range(100):
            if conn.execute("SELECT 1 AS x FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query LIKE '%write_finished_flights%' "
                            "AND pid <> pg_backend_pid()").fetchone():
                break
            time.sleep(0.05)
        else:
            pytest.fail("the writer never reached the blocked statement")
        late = conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
                            "VALUES (440, 'QFA9', %s, %s, %s, 'YSSY', 'YMML') RETURNING id",
                            (ts[-1] + 9 * HOUR, ts[-1] + 9 * HOUR, ts[-1] + 9 * HOUR)).fetchone()["id"]
    finally:
        holder.rollback()
        holder.close()
        t.join(10)
    assert out["n"] == 1
    assert [r["session_ids"] for r in records(conn, 440)] == [[a]]
    assert flown(conn, a) == ts[-1] and flown(conn, late) is None            # the late session waits for its own quiet period


def test_opus2_pilots_with_a_key_conflict_do_not_starve_the_rest(conn: Any) -> None:
    ts = times(BASE, 20)
    for i in range(50):
        sid = mk(conn, 500 + i, ts)
        conn.execute("INSERT INTO flight_record (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, "
                     "logon_time, sample_count) VALUES (%s, %s, %s, %s, 'QFA1', 'YSSY', 'YMML', ARRAY[%s::bigint], %s, 20)",
                     (sid, ts[0], ts[-1], 500 + i, sid, ts[0]))
    later = times(BASE + 2 * HOUR, 20)
    mk(conn, 600, later, logon=later[0])
    snap(conn, BASE + 30 * HOUR)
    notices: list[str] = []
    conn.add_notice_handler(lambda d: notices.append(d.message_primary or ""))
    assert write(conn, 50) == 1                                              # the normal pilot is written in the same call
    assert len(records(conn, 600)) == 1
    assert any("50 pilot(s) skipped" in n for n in notices), notices         # and the conflicts are still reported


def test_opus6_a_future_dated_snapshot_cannot_make_everyone_quiet(conn: Any) -> None:
    from datetime import UTC, datetime
    now = datetime.now(UTC).replace(microsecond=0)
    mk(conn, 601, times(now - 30 * MIN, 20))                                 # connected until a few minutes ago
    snap(conn, now + timedelta(days=30))                                     # a bogus future feed timestamp
    assert write(conn) == 0
