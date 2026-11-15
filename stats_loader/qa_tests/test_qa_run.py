# ruff: noqa: E501
"""QA: whole runs against a throwaway Postgres: loading order, retention, failure and stop rules, run log, idempotence."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta

import psycopg
import pytest
from qa_fakes import FakeApi, FakeClock, Req, at, gaps, make
from qa_helpers import TODAY, cfg, daily, fresh, last_run, n_rows, run, stored

pytestmark = pytest.mark.integration
Y = [TODAY - timedelta(days=n) for n in range(1, 400)]  # Y[0] is yesterday (14-11-2026)


def req_days(log: list[Req]) -> list[date]:
    return [r.start.date() for r in log]


# ---- what a run loads --------------------------------------------------------------------------------------------
def test_first_run_loads_newest_first_back_to_the_retention_limit_one_request_a_day(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    fake.sessions = daily(TODAY - timedelta(days=400), TODAY - timedelta(days=1))
    oldest = date(2025, 11, 15)  # 15-11-2026 minus 1 year
    fake.sessions += [make(9_000_001, at(oldest - timedelta(days=1), 23, 59, 59)), make(9_000_002, at(oldest, 0, 0, 0))]
    assert run(conn, fake) == "success"
    got = stored(su)
    assert got == {oldest + timedelta(days=i) for i in range((TODAY - oldest).days)}  # 15-11-2025 .. 14-11-2026
    ids = {r[0] for r in su.execute("SELECT connection_id FROM atc_stats_session")}
    assert 9_000_002 in ids and 9_000_001 not in ids  # the cutoff is a whole UTC date: 00:00:00 kept, the second before it not
    days = req_days(fake.log)
    assert days[0] == TODAY - timedelta(days=1)
    assert days == sorted(days, reverse=True)  # newest first
    assert fake.n == 365  # a whole day answers in one request
    assert min(gaps(fake.log)) >= 5.0


def test_the_run_stops_at_the_first_fully_empty_date_older_than_the_oldest_stored_and_never_asks_for_it_again(
        conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    fake.sessions = daily(TODAY - timedelta(days=30), TODAY - timedelta(days=1))
    assert run(conn, fake) == "success"
    first_empty = TODAY - timedelta(days=31)
    assert min(req_days(fake.log)) == first_empty
    assert sum(1 for r in fake.log if r.start.date() == first_empty) == 25  # the day, then all 24 hours
    assert fake.n == 30 + 25
    assert len(stored(su)) == 30
    # the next day's run: the confirmed-empty date is not asked for again, and nothing older than the overlap is
    clock.t = 86400.0  # 16-11-2026 06:00 UTC
    fake.sessions += daily(TODAY, TODAY)
    before = fake.n
    assert run(conn, fake, page_size=5000) == "success"
    days = req_days(fake.log[before:])
    assert min(days) >= TODAY - timedelta(days=3)  # newest stored 14-11 minus 2 days overlap = 12-11


def test_overlap_reloads_recent_days_and_a_session_that_closed_late_is_updated(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    late = make(777, at(date(2026, 11, 13), 21, 0, 0), dur=None)  # still open on the first load
    fake.sessions = daily(TODAY - timedelta(days=12), TODAY - timedelta(days=1)) + [late]
    assert run(conn, fake) == "success"
    assert su.execute("SELECT session_end, duration_seconds FROM atc_stats_session WHERE connection_id=777").fetchone() == (None, None)
    late.end = late.start + timedelta(hours=4)
    clock.t = 86400.0  # 16-11-2026 06:00 UTC
    n = fake.n
    assert run(conn, fake) == "success"
    days = sorted(set(req_days(fake.log[n:])))
    assert days == [date(2026, 11, d) for d in (12, 13, 14, 15)]  # 2 days before the newest stored (14-11), up to yesterday
    end, dur = su.execute("SELECT session_end, duration_seconds FROM atc_stats_session WHERE connection_id=777").fetchone()  # type: ignore[misc]
    assert end == late.end and dur == 4 * 3600
    assert last_run(su)["sessions_updated"] == 1


def test_loading_the_same_day_twice_gives_the_same_rows_and_changes_nothing(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    fake.sessions = daily(TODAY - timedelta(days=5), TODAY - timedelta(days=1), per_day=3)
    run(conn, fake)
    snap = su.execute("SELECT * FROM atc_stats_session ORDER BY connection_id").fetchall()
    assert len(snap) == 15
    run(conn, fake)
    assert su.execute("SELECT * FROM atc_stats_session ORDER BY connection_id").fetchall() == snap  # including updated_at
    r = last_run(su)
    assert (r["sessions_inserted"], r["sessions_updated"]) == (0, 0)


def test_every_field_is_stored_as_the_api_gave_it(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    s = make(4242, datetime(2026, 11, 13, 23, 30, 5, tzinfo=UTC), callsign="ml_twr", dur=3661)
    s.rating, s.server, s.cid = 5, "AUSTRALIA", 1892342
    fake.sessions = [s, make(4243, at(date(2026, 11, 14), 1), dur=None)]
    run(conn, fake)
    row = su.execute("SELECT cid, callsign, position_type, rating, session_start, session_end, duration_seconds, server, "
                     "aircrafttracked, aircraftseen, flightsamended, handoffsinitiated, handoffsreceived, handoffsrefused, "
                     "squawksassigned, cruisealtsmodified, tempaltsmodified, scratchpadmods "
                     "FROM atc_stats_session WHERE connection_id=4242").fetchone()
    assert row == (1892342, "ml_twr", "TWR", 5, s.start, s.end, 3661, "AUSTRALIA", 4242 % 7, 4242 % 11, 1, 2, 3, 0, 4, 5, 6, 4242 % 5)
    assert su.execute("SELECT session_end FROM atc_stats_session WHERE connection_id=4243").fetchone() == (None,)


def test_sessions_on_window_boundaries_are_stored_once_on_both_the_day_and_the_hour_path(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    for hourly in (False, True):
        su.execute("TRUNCATE atc_stats_session, atc_stats_run_log RESTART IDENTITY")
        clock, fake = fresh()
        if hourly:
            fake.timeout_before = TODAY  # every day window times out: hour windows throughout
        rows = []
        for n in range(1, 8):
            d = TODAY - timedelta(days=n)
            rows += [make(n * 100 + 1, at(d, 0, 0, 0)), make(n * 100 + 2, at(d, 23, 59, 59)), make(n * 100 + 3, at(d, 12, 0, 0)),
                     make(n * 100 + 4, at(d, 11, 59, 59))]
        fake.sessions = rows
        assert run(conn, fake) == "success"
        assert n_rows(su) == len(rows) == 28, hourly
        assert su.execute("SELECT count(DISTINCT connection_id) FROM atc_stats_session").fetchone() == (28,)


def test_an_empty_date_writes_nothing_and_is_not_a_failure(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    fake.sessions = daily(TODAY - timedelta(days=6), TODAY - timedelta(days=1))
    fake.sessions = [s for s in fake.sessions if s.start.date() != TODAY - timedelta(days=3)]  # a hole in the middle
    assert run(conn, fake) == "success"
    assert (TODAY - timedelta(days=3)) not in stored(su)
    assert last_run(su)["failed_days"] == []


# ---- timeouts across a run ----------------------------------------------------------------------------------------
def test_once_a_day_times_out_every_older_day_goes_straight_to_hour_windows(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    fake.sessions = daily(TODAY - timedelta(days=8), TODAY - timedelta(days=1))
    fake.timeout_before = TODAY - timedelta(days=4)  # 10-11 and older time out as a day
    assert run(conn, fake) == "success"
    per_day = {d: [r for r in fake.log if r.start.date() == d] for d in set(req_days(fake.log))}
    assert len(per_day[TODAY - timedelta(days=4)]) == 1
    assert len(per_day[TODAY - timedelta(days=5)]) == 1 + 24  # the day window timed out once, then 24 hours
    for n in (6, 7, 8):
        reqs = per_day[TODAY - timedelta(days=n)]
        assert all(r.end - r.start < timedelta(hours=2) for r in reqs), n  # no day window tried again
        assert len(reqs) == 24


def test_timeouts_do_not_stop_a_run_as_server_errors(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    fake.sessions = daily(TODAY - timedelta(days=5), TODAY - timedelta(days=1))
    fake.timeout_before = TODAY
    assert run(conn, fake) == "success"
    assert last_run(su)["status"] == "success"


# ---- server errors, 429, failed days ---------------------------------------------------------------------------
def test_three_server_errors_in_a_row_end_the_run_as_failed_and_retention_deletes_nothing(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    fake.sessions = daily(TODAY - timedelta(days=5), TODAY - timedelta(days=1))
    su.execute("INSERT INTO atc_stats_session (connection_id, cid, callsign, session_start) VALUES (1, 1, 'OLD_TWR', '2020-01-01')")
    fake.script = [500, 500, 500]
    assert run(conn, fake) == "failed"
    r = last_run(su)
    assert r["status"] == "failed" and r["error"] and r["finished_at"] is not None
    assert n_rows(su) == 1  # the old row is still there: no retention after a failed run


def test_http_429_ends_the_run_as_failed(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    fake.sessions = daily(TODAY - timedelta(days=5), TODAY - timedelta(days=1))
    fake.script = [429]
    assert run(conn, fake) == "failed"
    assert fake.n == 1
    assert last_run(su)["status"] == "failed"


def _broken(fake: FakeApi, *offsets: int) -> None:
    for n in offsets:
        fake.extra_count[TODAY - timedelta(days=n)] = 1


def test_three_failed_days_in_a_row_stop_the_run_as_failed_with_the_reason(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    fake.sessions = daily(TODAY - timedelta(days=10), TODAY - timedelta(days=1))
    _broken(fake, 1, 2, 3)
    assert run(conn, fake) == "failed"
    r = last_run(su)
    assert r["status"] == "failed" and r["error"]
    assert set(r["failed_days"]) == {TODAY - timedelta(days=n) for n in (1, 2, 3)}
    assert min(req_days(fake.log)) == TODAY - timedelta(days=3)  # nothing older was asked for
    assert n_rows(su) == 0


def test_a_success_resets_the_failed_day_streak(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    fake.sessions = daily(TODAY - timedelta(days=9), TODAY - timedelta(days=1))
    _broken(fake, 1, 2, 4, 5, 7, 8)  # F F S F F S F F
    assert run(conn, fake) == "failed"  # failed days exist, so the run ends failed, but it was not stopped early
    assert stored(su) == {TODAY - timedelta(days=n) for n in (3, 6, 9)}
    assert min(req_days(fake.log)) <= TODAY - timedelta(days=9)
    assert set(last_run(su)["failed_days"]) == {TODAY - timedelta(days=n) for n in (1, 2, 4, 5, 7, 8)}


def test_an_empty_day_resets_the_failed_day_streak(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    fake.sessions = [s for s in daily(TODAY - timedelta(days=9), TODAY - timedelta(days=1)) if s.start.date() != TODAY - timedelta(days=3)]
    _broken(fake, 1, 2, 4, 5)  # F F (empty) F F S ...
    assert run(conn, fake) == "failed"
    assert {TODAY - timedelta(days=n) for n in (6, 7, 8, 9)} <= stored(su)  # the run went on past four failures


def test_a_failed_day_older_than_the_overlap_is_found_and_loaded_by_the_next_run(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    fake.sessions = daily(TODAY - timedelta(days=12), TODAY - timedelta(days=1))
    bad = TODAY - timedelta(days=8)
    _broken(fake, 8)
    assert run(conn, fake) == "failed"
    assert bad not in stored(su) and last_run(su)["failed_days"] == [bad]
    fake.extra_count.clear()
    clock.t = 86400.0
    n = fake.n
    assert run(conn, fake) == "success"
    assert bad in stored(su)
    assert bad in req_days(fake.log[n:])
    assert last_run(su)["failed_days"] == []


def test_a_failure_in_the_middle_of_a_day_leaves_nothing_of_that_day(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    y1, y2 = TODAY - timedelta(days=1), TODAY - timedelta(days=2)
    fake.sessions = [make(1, at(y1, 3)), *[make(10 + i, at(y2, 1, i)) for i in range(5)]]  # y2 needs three pages of 2

    def hook(req: Req) -> None:
        if req.start.date() == y2 and req.offset == 2:  # the next request (page 3) gets three server errors
            fake.script = [500, 500, 500]

    fake.on_request = hook
    assert run(conn, fake, page_size=2) == "failed"
    assert stored(su) == {y1}
    assert n_rows(su) == 1


def test_a_whole_day_is_written_in_one_transaction_in_the_store(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    from qa_fakes import item

    from vatsim_stats import store
    from vatsim_stats.api import parse_session

    rows = [parse_session(item(make(i, at(date(2026, 11, 14), 3, i)))) for i in (1, 2, 3)]
    rows[2]["cid"] = None  # the third row breaks a NOT NULL
    with pytest.raises(psycopg.Error):
        store.write_day(conn, rows)
    assert n_rows(su) == 0


# ---- retention ---------------------------------------------------------------------------------------------------
def test_retention_never_runs_in_the_middle_of_a_run(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    fake.sessions = daily(TODAY - timedelta(days=5), TODAY - timedelta(days=1))
    su.execute("INSERT INTO atc_stats_session (connection_id, cid, callsign, session_start) VALUES (1, 1, 'OLD_TWR', '2020-01-01')")
    seen: list[int] = []
    fake.on_request = lambda r: seen.append(su.execute("SELECT count(*) FROM atc_stats_session WHERE session_start < '2020-06-01'").fetchone()[0])  # type: ignore[index]
    assert run(conn, fake) == "success"
    assert set(seen) == {1}  # the old row was there for every request of the run
    assert su.execute("SELECT count(*) FROM atc_stats_session WHERE connection_id = 1").fetchone() == (0,)
    assert last_run(su)["sessions_deleted"] == 1


def _sessions_two_years() -> list:
    out = daily(TODAY - timedelta(days=800), TODAY - timedelta(days=1), per_day=1)
    # exactly on the 1-year and 2-year cutoffs
    out += [make(8_000_001, at(date(2025, 11, 14), 23, 59, 59)), make(8_000_002, at(date(2025, 11, 15), 0, 0, 0)),
            make(8_000_003, at(date(2024, 11, 14), 23, 59, 59)), make(8_000_004, at(date(2024, 11, 15), 0, 0, 0))]
    return out


def test_lowering_retention_deletes_only_the_older_rows_after_a_clean_run_and_raising_it_reloads_them(
        conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    fake.sessions = _sessions_two_years()
    two = cfg(STATS_RETENTION_YEARS="2")
    one = cfg(STATS_RETENTION_YEARS="1")
    assert run(conn, fake, two) == "success"
    before = su.execute("SELECT connection_id, cid, callsign, session_start, session_end FROM atc_stats_session ORDER BY 1").fetchall()
    dates_before = stored(su)
    assert min(dates_before) == date(2024, 11, 15)
    old = [r for r in before if r[3] < datetime(2025, 11, 15, tzinfo=UTC)]
    new = [r for r in before if r[3] >= datetime(2025, 11, 15, tzinfo=UTC)]
    assert old and new

    # (a1) a failed run in the same situation deletes nothing
    fake.extra_count[TODAY - timedelta(days=1)] = 1
    clock.t = 3 * 3600.0
    assert run(conn, fake, one) == "failed"
    assert n_rows(su) == len(before)

    # (a2) a clean run deletes everything older than the cutoff date, nothing newer, with no flag
    fake.extra_count.clear()
    assert run(conn, fake, one) == "success"
    after = su.execute("SELECT connection_id, cid, callsign, session_start, session_end FROM atc_stats_session ORDER BY 1").fetchall()
    assert after == new
    assert last_run(su)["sessions_deleted"] == len(old)
    ids = {r[0] for r in after}
    assert 8_000_002 in ids and 8_000_001 not in ids  # the cutoff is the whole date 15-11-2025

    # (b) raise again: no deletion; the dropped dates are loaded again, newest first, until the table is as it was
    n = fake.n
    assert run(conn, fake, two) == "success"
    assert last_run(su)["sessions_deleted"] == 0
    assert su.execute("SELECT connection_id, cid, callsign, session_start, session_end FROM atc_stats_session ORDER BY 1").fetchall() == before
    assert stored(su) == dates_before
    reload = [d for d in req_days(fake.log[n:]) if d < date(2025, 11, 15)]
    assert reload == sorted(reload, reverse=True) and reload  # newest first


def test_retention_cutoff_is_a_whole_utc_date_not_the_time_of_day(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh(datetime(2026, 11, 15, 17, 45, 10, tzinfo=UTC))
    fake.sessions = daily(date(2025, 11, 15), TODAY - timedelta(days=1), per_day=1, hours=(3,))  # first day's session is at 03:00
    su.execute("INSERT INTO atc_stats_session (connection_id, cid, callsign, session_start) VALUES (1, 1, 'X_TWR', '2025-11-14 23:59:59+00')")
    assert run(conn, fake) == "success"
    assert su.execute("SELECT count(*) FROM atc_stats_session WHERE connection_id = 1").fetchone() == (0,)
    assert date(2025, 11, 15) in stored(su)  # 03:00 on the cutoff date is kept although it is older than 17:45 a year ago


# ---- time zones --------------------------------------------------------------------------------------------------
def test_dates_are_utc_dates_whatever_the_database_or_session_time_zone(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    from vatsim_stats import store

    su.execute("INSERT INTO atc_stats_session (connection_id, cid, callsign, session_start) VALUES (5, 1, 'X_TWR', '2026-11-13 23:30:00+00')")
    assert su.execute("SHOW timezone").fetchone() == ("Pacific/Auckland",)  # the database default is deliberately not UTC
    assert store.stored_dates(conn) == {date(2026, 11, 13)}
    conn.execute("SET timezone = 'Pacific/Auckland'")
    assert store.stored_dates(conn) == {date(2026, 11, 13)}  # Auckland would say 14-11
    conn.execute("SET timezone = 'America/Los_Angeles'")
    assert store.stored_dates(conn) == {date(2026, 11, 13)}


# ---- the run log -------------------------------------------------------------------------------------------------
def test_run_log_is_running_at_start_updated_per_day_and_success_at_the_end(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    fake.sessions = daily(TODAY - timedelta(days=4), TODAY - timedelta(days=1))
    snaps: list[tuple[str, object, int]] = []

    def hook(r: Req) -> None:
        row = su.execute("SELECT status, last_day_at, days_loaded FROM atc_stats_run_log ORDER BY id DESC LIMIT 1").fetchone()
        snaps.append(row)  # type: ignore[arg-type]

    fake.on_request = hook
    assert run(conn, fake) == "success"
    assert {s[0] for s in snaps} == {"running"}
    assert snaps[0][1] is None and snaps[0][2] == 0  # first request: nothing written yet
    loaded = [s[2] for s in snaps]
    assert loaded == sorted(loaded) and loaded[-1] >= 3  # days_loaded rises while the run is going
    assert snaps[-1][1] is not None  # last_day_at is set once a day is written
    r = last_run(su)
    assert r["status"] == "success" and r["error"] is None and r["finished_at"] >= r["started_at"]
    assert r["requests"] == fake.n and r["days_loaded"] == 4 and r["sessions_inserted"] == 8 and r["failed_days"] == []
    assert r["started_at"] == UTC_START


UTC_START = datetime(2026, 11, 15, 6, 0, 0, tzinfo=UTC)


def test_a_clean_stop_signal_abandons_the_day_in_hand_and_marks_the_run_stopped(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    stop = threading.Event()
    clock = FakeClock(stop=stop)
    fake = FakeApi(clock)
    y1, y2 = TODAY - timedelta(days=1), TODAY - timedelta(days=2)
    fake.sessions = [make(1, at(y1, 3)), *[make(10 + i, at(y2, 1, i)) for i in range(5)]]
    fake.stop_at_request = (3, stop)  # the second page of y2
    assert run(conn, fake, page_size=2) == "stopped"
    r = last_run(su)
    assert r["status"] == "stopped" and r["finished_at"] is not None
    assert stored(su) == {y1}
    assert fake.n == 3


# ---- the start rule and the schedule ------------------------------------------------------------------------------
def _serve(conn: psycopg.Connection, su: psycopg.Connection, start: datetime, preload: str | None = None,
           until: datetime | None = None, runs_after: datetime | None = None) -> tuple[FakeApi, FakeClock]:
    from vatsim_stats import app

    stop = threading.Event()
    clock = FakeClock(start, stop)
    fake = FakeApi(clock)
    d = start.date()
    fake.sessions = daily(d - timedelta(days=6), d + timedelta(days=4), per_day=1)
    if preload:
        status, finished = preload.split("@")
        su.execute("INSERT INTO atc_stats_run_log (started_at, finished_at, status) VALUES (%s, %s, %s)",
                   (datetime.fromisoformat(finished) - timedelta(hours=1), datetime.fromisoformat(finished), status))
    limit = until or start + timedelta(days=1)
    fake.on_request = lambda r: stop.set() if clock.now() >= limit else None
    stop_first = until is None
    if stop_first:
        fake.on_request = lambda r: stop.set()
    app.serve(conn, fake.api(5.0, 5000), clock, cfg())
    return fake, clock


@pytest.mark.parametrize("preload", [None, "failed@2026-11-15T03:00:00+00:00", "stopped@2026-11-15T03:00:00+00:00",
                                      "interrupted@2026-11-15T03:00:00+00:00", "success@2026-11-14T03:00:00+00:00"])
def test_at_container_start_a_run_begins_after_60_seconds_unless_one_finished_successfully_today(
        conn: psycopg.Connection, su: psycopg.Connection, preload: str | None) -> None:
    start = datetime(2026, 11, 15, 10, 0, 0, tzinfo=UTC)
    fake, clock = _serve(conn, su, start, preload)
    first = fake.log[0]
    assert 60.0 <= first.t < 70.0, preload


@pytest.mark.parametrize("start", [datetime(2026, 11, 15, 10, 0, 0, tzinfo=UTC), datetime(2026, 11, 15, 5, 30, 0, tzinfo=UTC)])
def test_a_run_that_succeeded_today_blocks_a_second_run_until_tomorrow(conn: psycopg.Connection, su: psycopg.Connection,
                                                                       start: datetime) -> None:
    """Design section 4: a restart waits for tomorrow if a run already finished successfully today (UTC)."""
    fake, clock = _serve(conn, su, start, "success@2026-11-15T03:00:00+00:00")
    first_at = datetime(2026, 11, 16, 6, 0, 0, tzinfo=UTC)
    assert start + timedelta(seconds=fake.log[0].t) >= first_at
    assert start + timedelta(seconds=fake.log[0].t) < first_at + timedelta(minutes=2)


def test_success_today_is_judged_on_the_utc_date_not_the_session_time_zone(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    conn.execute("SET timezone = 'Pacific/Auckland'")  # 22:30 UTC on 14-11 is 11:30 on 15-11 in Auckland
    start = datetime(2026, 11, 15, 10, 0, 0, tzinfo=UTC)
    fake, _ = _serve(conn, su, start, "success@2026-11-14T22:30:00+00:00")
    assert fake.log[0].t < 70.0  # not today (UTC): a run goes ahead


def test_the_daily_run_starts_at_stats_run_at_utc_each_day(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    start = datetime(2026, 11, 15, 10, 0, 0, tzinfo=UTC)
    until = datetime(2026, 11, 17, 6, 0, 1, tzinfo=UTC)
    _serve(conn, su, start, "success@2026-11-15T03:00:00+00:00", until=until)
    starts = [r[0] for r in su.execute("SELECT started_at FROM atc_stats_run_log WHERE id > 1 ORDER BY id")]
    assert len(starts) >= 2
    day2, day3 = starts[0], starts[1]
    assert datetime(2026, 11, 16, 6, 0, 0, tzinfo=UTC) <= day2 < datetime(2026, 11, 16, 6, 5, 0, tzinfo=UTC)
    assert datetime(2026, 11, 17, 6, 0, 0, tzinfo=UTC) <= day3 < datetime(2026, 11, 17, 6, 5, 0, tzinfo=UTC)


def test_sixty_seconds_pass_before_the_first_request_even_when_the_run_time_is_now(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    start = datetime(2026, 11, 14, 5, 59, 59, tzinfo=UTC)
    fake, _ = _serve(conn, su, start)
    assert fake.log[0].t >= 60.0


def test_a_run_left_running_by_a_crash_is_marked_interrupted_at_the_next_start(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    from vatsim_stats import app

    su.execute("INSERT INTO atc_stats_run_log (started_at, status) VALUES ('2026-11-15 01:00+00', 'running')")
    stop = threading.Event()
    stop.set()  # the container is stopped straight away: only the start-up step runs
    clock = FakeClock(stop=stop)
    app.serve(conn, FakeApi(clock).api(), clock, cfg())
    r = su.execute("SELECT status, finished_at, error FROM atc_stats_run_log").fetchone()
    assert r[0] == "interrupted" and r[1] is not None and r[2]  # type: ignore[index]
    assert gaps([]) == []
