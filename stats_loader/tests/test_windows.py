"""Windows, paging and the count check, against the fake API and fake clock (no database)."""

from __future__ import annotations

from datetime import date

import pytest

from fakes import FakeApi, FakeClock, at
from vatsim_stats.api import Api, BadResponse
from vatsim_stats.windows import (
    DayFailed,
    day_window,
    fetch_day,
    fetch_window,
    hour_windows,
    oldest_allowed,
    plan,
)

D = date(2026, 11, 10)


def make(fake: FakeApi, clock: FakeClock, page: int = 10) -> Api:
    return Api(fake.client(), clock, 5.0, page)


def fill(fake: FakeApi, n: int, day: int = 10) -> None:
    for i in range(n):
        fake.add(100 + i, "AA_TWR", at(2026, 11, day, i // 60, i % 60, i % 7))


def ids(rows: list[dict]) -> set[int]:
    return {r["connection_id"] for r in rows}


def test_windows_end_one_second_before_the_next_begins() -> None:
    assert day_window(D) == (at(2026, 11, 10), at(2026, 11, 10, 23, 59, 59))
    hours = hour_windows(D)
    assert len(hours) == 24 and hours[0][0] == at(2026, 11, 10) and hours[23][1] == at(2026, 11, 10, 23, 59, 59)
    one_second = at(2026, 11, 10, 0, 0, 1) - at(2026, 11, 10)
    assert all(b[0] - a[1] == one_second for a, b in zip(hours, hours[1:], strict=False))


def test_oldest_allowed_is_a_whole_utc_date_and_survives_29_february() -> None:
    assert oldest_allowed(at(2026, 11, 15, 23, 59), 3) == date(2023, 11, 15)
    assert oldest_allowed(at(2028, 2, 29, 5), 1) == date(2027, 2, 28)


def test_paging_asks_again_from_where_it_got_to(fake: FakeApi, clock: FakeClock) -> None:
    fill(fake, 25)
    count, rows = fetch_window(make(fake, clock), *day_window(D))
    assert count == 25 and len(rows) == 25
    assert [c.offset for c in fake.calls] == [0, 10, 20]


def test_short_pages_are_not_a_problem(fake: FakeApi, clock: FakeClock) -> None:
    fill(fake, 25)
    fake.max_page = 7  # the server returns fewer than the 10 asked for
    count, rows = fetch_window(make(fake, clock), *day_window(D))
    assert count == 25 and len(rows) == 25
    assert [c.offset for c in fake.calls] == [0, 7, 14, 21]


def test_a_day_above_one_page_comes_back_whole(fake: FakeApi, clock: FakeClock) -> None:
    fill(fake, 120)
    assert len(fetch_day(make(fake, clock, page=50), D, hourly=False).rows) == 120


def test_a_stray_atis_session_is_skipped_counted_and_the_day_passes(fake: FakeApi, clock: FakeClock) -> None:
    fill(fake, 5)
    fake.add(1, "ML_ATIS", at(2026, 11, 10, 3))
    day = fetch_day(make(fake, clock), D, hourly=False)
    assert len(day.rows) == 5 and not any(r["callsign"].endswith("_ATIS") for r in day.rows)
    assert len(fake.calls) == 1  # no refetch: distinct ids plus the skipped ATIS equal the count


def test_a_page_order_that_shifts_is_caught_and_hour_windows_fix_it(fake: FakeApi, clock: FakeClock) -> None:
    for i in range(4):
        fake.add(100 + i, "AA_TWR", at(2026, 11, 10, 10, 0, 0))  # all in one second: their order is not fixed
    fake.flip_ties_on = {2}  # the second request lists them the other way: a duplicate and a missing session
    day = fetch_day(make(fake, clock, page=2), D, hourly=False)
    assert len(day.rows) == 4 and len({r["connection_id"] for r in day.rows}) == 4
    assert len({c.start for c in fake.calls if c.hours < 2}) == 24  # the retry was the 24 one-hour windows
    assert not day.timed_out


def test_a_session_exactly_on_a_window_boundary_is_stored_once(fake: FakeApi, clock: FakeClock) -> None:
    a = fake.add(1, "AA_TWR", at(2026, 11, 10, 23, 59, 59))
    b = fake.add(2, "AA_TWR", at(2026, 11, 11, 0, 0, 0))
    c = fake.add(3, "AA_TWR", at(2026, 11, 10, 0, 0, 0))
    api = make(fake, clock)
    today, tomorrow = fetch_day(api, D, False).rows, fetch_day(api, date(2026, 11, 11), False).rows
    assert ids(today) == {a, c} and ids(tomorrow) == {b}
    # the same through the hour windows: 00:59:59 and 01:00:00 belong to different hours, each counted once
    x = fake.add(4, "AA_TWR", at(2026, 11, 10, 0, 59, 59))
    y = fake.add(5, "AA_TWR", at(2026, 11, 10, 1, 0, 0))
    hourly = fetch_day(api, D, True).rows
    assert len(hourly) == 4 and {x, y} <= ids(hourly)  # a, c, x, y; b belongs to the next day


def test_a_late_closing_count_change_is_caught_then_the_day_loads_from_hours(fake: FakeApi, clock: FakeClock) -> None:
    fill(fake, 15)
    late = []

    def grow(n: int) -> None:
        if n == 1 and not late:
            late.append(fake.add(900, "AA_TWR", at(2026, 11, 10, 22)))

    fake.on_call = grow  # a session arrives after the first page: the next page reports 16
    day = fetch_day(make(fake, clock), D, hourly=False)
    assert len(day.rows) == 16 and late[0] in ids(day.rows)


def test_a_count_that_keeps_changing_writes_nothing(fake: FakeApi, clock: FakeClock) -> None:
    for i in range(5):
        fake.add(100 + i, "AA_TWR", at(2026, 11, 10, 22, 0, i))
    fake.on_call = lambda n: fake.add(1000 + n, "AA_TWR", at(2026, 11, 10, 22, 30))
    with pytest.raises(DayFailed):  # the day window fails its check, and so does the 22:00 hour window
        fetch_day(make(fake, clock, page=2), D, hourly=False)


def test_a_timeout_shrinks_the_window_to_hours(fake: FakeApi, clock: FakeClock) -> None:
    fill(fake, 30)
    fake.slow_before = at(2026, 12, 1)
    day = fetch_day(make(fake, clock), D, hourly=False)
    assert day.timed_out and len(day.rows) == 30
    assert sum(c.hours > 1 for c in fake.calls) == 1  # one timeout, then only hour windows
    # a day that is already known to need hours goes straight to them
    fake.calls.clear()
    assert not fetch_day(make(fake, clock), D, hourly=True).timed_out
    assert all(c.hours < 1 for c in fake.calls)


def test_an_hour_window_that_still_times_out_fails_the_day(fake: FakeApi, clock: FakeClock) -> None:
    fake.script = ["timeout"] * 2
    with pytest.raises(DayFailed):
        fetch_day(make(fake, clock), D, hourly=False)


def test_an_empty_day_is_confirmed_on_all_24_hours(fake: FakeApi, clock: FakeClock) -> None:
    day = fetch_day(make(fake, clock), D, hourly=False)
    assert day.empty and not day.rows
    assert len(fake.calls) == 25  # the day window, then 24 hours


def test_plan_recent_days_then_missing_newest_first() -> None:
    today = date(2026, 11, 15)
    stored = {date(2026, 11, 8), date(2026, 11, 12), date(2026, 11, 11), date(2026, 11, 6)}
    recent, missing = plan(stored, today, date(2026, 11, 1), 2, set())
    assert recent == [date(2026, 11, d) for d in (14, 13, 12, 11, 10)]  # from 2 days before the newest, to yesterday
    assert missing == [date(2026, 11, d) for d in (9, 7, 5, 4, 3, 2, 1)]


def test_plan_first_run_is_three_days_then_the_rest() -> None:
    recent, missing = plan(set(), date(2026, 11, 15), date(2026, 11, 8), 2, set())
    assert recent == [date(2026, 11, 14), date(2026, 11, 13), date(2026, 11, 12)]
    assert missing == [date(2026, 11, d) for d in (11, 10, 9, 8)]


def test_plan_does_not_ask_again_for_empty_dates_or_before_the_start_of_history() -> None:
    stored = {date(2026, 11, 10)}
    empty = {date(2026, 11, 5), date(2026, 11, 3)}  # 3 and 5 are older than the oldest stored date
    recent, missing = plan(stored, date(2026, 11, 11), date(2026, 10, 1), 0, empty)
    assert recent == [date(2026, 11, 10)]
    assert missing == [date(2026, 11, d) for d in (9, 8, 7, 6)]  # nothing at or before 5 November (the history start)


def test_blank_member_sessions_are_skipped_counted_and_the_good_ones_load(fake: FakeApi, clock: FakeClock,
                                                                         caplog: pytest.LogCaptureFixture) -> None:
    fill(fake, 12)
    for i in range(5):
        fake.add_blank(at(2026, 11, 10, 4, i))
    caplog.set_level("INFO", logger="vatsim_stats.windows")
    count, rows = fetch_window(make(fake, clock, page=4), *day_window(D))  # paged: 17 items over 5 pages
    assert count == 17 and len(rows) == 12
    assert any("skipped 5 sessions with no member ID" in r.message for r in caplog.records)
    caplog.clear()
    fetch_window(make(fake, clock), at(2026, 11, 10, 6), at(2026, 11, 10, 6, 59, 59))  # none here: no line
    assert not caplog.records


def test_every_blank_form_and_a_server_name_are_skipped(fake: FakeApi, clock: FakeClock) -> None:
    fill(fake, 3)
    fake.add_blank(at(2026, 11, 10, 5, 0), member="")
    fake.add_blank(at(2026, 11, 10, 5, 1), member="  ")
    fake.add_blank(at(2026, 11, 10, 5, 2), missing=True)
    fake.add_blank(at(2026, 11, 10, 5, 3), member=None)
    fake.add_blank(at(2026, 11, 10, 5, 4), server="SYD")  # a blank ID with a server is skipped too
    day = fetch_day(make(fake, clock), D, hourly=False)
    assert len(day.rows) == 3 and len(fake.calls) == 1


def test_a_day_of_only_blank_sessions_is_not_empty_but_stores_nothing(fake: FakeApi, clock: FakeClock) -> None:
    fake.add_blank(at(2026, 11, 10, 5))
    day = fetch_day(make(fake, clock), D, hourly=False)
    assert day.rows == [] and len(fake.calls) == 1  # the API reported 1 session, so the date is not empty
    assert not day.empty


def test_blank_member_sessions_load_through_hour_windows(fake: FakeApi, clock: FakeClock) -> None:
    fill(fake, 6)
    fake.add_blank(at(2026, 11, 10, 2, 5))
    fake.add_blank(at(2026, 11, 10, 14, 5))
    assert len(fetch_day(make(fake, clock), D, hourly=True).rows) == 6


def test_another_defect_still_fails_the_day(fake: FakeApi, clock: FakeClock) -> None:
    fill(fake, 3)
    fake.add_blank(at(2026, 11, 10, 5))
    fake.add(500, "AA_TWR", at(2026, 11, 10, 6))
    fake.items[-1]["aircrafttracked"] = "many"  # unreadable, and not a blank member ID
    with pytest.raises(DayFailed):
        fetch_day(make(fake, clock), D, hourly=False)
    with pytest.raises(BadResponse):
        fetch_window(make(fake, clock), *day_window(D))


def test_a_blank_member_session_without_a_connection_id_still_fails(fake: FakeApi, clock: FakeClock) -> None:
    fake.add_blank(at(2026, 11, 10, 5))
    fake.items[-1]["connection_id"]["id"] = None
    with pytest.raises(BadResponse):
        fetch_window(make(fake, clock), *day_window(D))


def test_the_count_check_includes_skipped_and_still_catches_a_short_answer(fake: FakeApi, clock: FakeClock) -> None:
    fill(fake, 4)
    fake.add_blank(at(2026, 11, 10, 5))
    fake.add(1, "ML_ATIS", at(2026, 11, 10, 3))
    assert fetch_window(make(fake, clock), *day_window(D))[0] == 6  # 4 stored + 1 ATIS + 1 no member
    # two sessions share a connection id, so stored + ATIS + skipped (5) is below the count (6)
    fake.items[0]["connection_id"]["id"] = fake.items[1]["connection_id"]["id"]
    with pytest.raises(DayFailed, match="1 with no member ID"):
        fetch_window(make(fake, clock), *day_window(D))
