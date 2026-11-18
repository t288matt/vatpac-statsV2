# ruff: noqa: E501
"""QA: window edges, the count check, paging, timeouts, the 5-second rule. From the design; no database."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from qa_fakes import FakeApi, FakeClock, at, gaps, make

from vatsim_stats.api import Api, RateLimited, ServerErrors, WindowTimeout
from vatsim_stats.windows import DayFailed, fetch_day

D = date(2026, 11, 10)
END = at(D, 23, 59, 59)


def fx(page: int = 5000, interval: float = 5.0) -> tuple[FakeApi, Api]:
    fake = FakeApi(FakeClock())
    return fake, fake.api(interval, page)


def ids(day: Any) -> set[int]:
    return {r["connection_id"] for r in day.rows}


# ---- window edges -------------------------------------------------------------------------------------------------
def test_day_window_is_00_00_00_to_23_59_59_both_ends_inclusive() -> None:
    fake, api = fx()
    fake.sessions = [make(1, at(D, 0, 0, 0)), make(2, at(D, 23, 59, 59)), make(3, at(D, 12)),
                     make(4, at(D + timedelta(days=1), 0, 0, 0)), make(5, at(D - timedelta(days=1), 23, 59, 59))]
    day = fetch_day(api, D)
    assert ids(day) == {1, 2, 3}
    assert fake.n == 1
    assert fake.log[0].start == at(D, 0, 0, 0) and fake.log[0].end == END


def test_boundary_session_between_two_days_is_counted_once_across_both_days() -> None:
    fake, api = fx()
    fake.sessions = [make(1, at(D + timedelta(days=1), 0, 0, 0))]
    a, b = fetch_day(api, D), fetch_day(api, D + timedelta(days=1))
    assert ids(a) == set()
    assert ids(b) == {1}


# ---- the count check ----------------------------------------------------------------------------------------------
def test_stray_atis_is_skipped_counted_and_the_day_still_passes() -> None:
    fake, api = fx()
    fake.sessions = [make(i, at(D, 8, i)) for i in range(1, 5)] + [make(9, at(D, 9, 0), callsign="SY_ATIS")]
    day = fetch_day(api, D)
    assert ids(day) == {1, 2, 3, 4}
    assert fake.n == 1  # passed first time: the count (5) is balanced by 4 distinct + 1 ATIS


def test_shifting_page_order_fails_the_day_with_no_retry_and_no_hour_windows() -> None:
    fake, api = fx(page=2)
    fake.sessions = [make(i, at(D, 7, 30, 0)) for i in range(1, 5)]
    fake.shift_always = True
    with pytest.raises(DayFailed):
        fetch_day(api, D)
    assert fake.n <= 3  # one attempt at the day, no hour-window repair


def test_count_that_changes_between_pages_fails_the_check_with_no_retry() -> None:
    fake, api = fx(page=2)
    fake.sessions = [make(i, at(D, 7 + i)) for i in range(1, 5)]
    fake.drift = lambda n, c: c + 1 if n == 2 else c  # a late-closing session arrives between page 1 and page 2
    with pytest.raises(DayFailed):
        fetch_day(api, D)


def test_a_day_whose_count_is_wrong_in_every_window_fails_and_nothing_is_returned() -> None:
    fake, api = fx()
    fake.sessions = [make(1, at(D, 3))]
    fake.extra_count[D] = 1
    with pytest.raises(DayFailed):
        fetch_day(api, D)


# ---- paging ------------------------------------------------------------------------------------------------------
def test_short_pages_continue_from_what_was_actually_received() -> None:
    fake, api = fx(page=5)
    fake.cap = 2
    fake.sessions = [make(i, at(D, 1, i)) for i in range(1, 8)]
    day = fetch_day(api, D)
    assert ids(day) == set(range(1, 8))
    assert [r.offset for r in fake.log] == [0, 2, 4, 6]
    assert {r.limit for r in fake.log} == {5}


def test_a_day_above_one_page_is_complete() -> None:
    fake, api = fx(page=3)
    fake.sessions = [make(i, at(D, 1, i % 60, i // 60)) for i in range(1, 11)]
    day = fetch_day(api, D)
    assert ids(day) == set(range(1, 11))
    assert [r.offset for r in fake.log] == [0, 3, 6, 9]


def test_an_empty_page_before_count_is_reached_ends_paging_and_fails_the_check_without_looping() -> None:
    fake, api = fx(page=5)
    fake.sessions = [make(i, at(D, 1, i)) for i in range(1, 4)]
    fake.extra_count[D] = 2  # the API claims 5, only 3 exist
    with pytest.raises(DayFailed):
        fetch_day(api, D)
    assert fake.n < 10  # one day's worth of pages only, no hour-window repair


def test_a_day_with_nothing_is_trusted_from_its_single_request() -> None:
    fake, api = fx()
    day = fetch_day(api, D)
    assert day.rows == [] and day.empty
    assert fake.n == 1  # no 24-hour double-check


# ---- timeouts and errors ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["504", "none"])
def test_a_timeout_fails_the_day_with_no_retry_and_no_hour_windows(mode: str) -> None:
    fake, api = fx()
    fake.sessions = [make(i, at(D, i, 5)) for i in range(1, 9)]
    fake.timeout_before, fake.timeout_mode = date(2026, 11, 14), mode
    with pytest.raises(DayFailed, match="timed out"):
        fetch_day(api, D)
    assert fake.n == 1  # one request only; never shrunk to hour windows


@pytest.mark.parametrize("mode", ["504", "none"])
def test_a_timeout_is_not_a_server_error(mode: str) -> None:
    """A timeout, caught as a failed day, must not be counted towards the three-server-errors-in-a-row rule."""
    fake, api = fx()
    fake.sessions = [make(1, at(D, 2, 5))]
    fake.timeout_before, fake.timeout_mode = date(2026, 11, 14), mode
    with pytest.raises(DayFailed):
        fetch_day(api, D)
    assert api.requests == 1  # the timeout alone never raises ServerErrors


def test_three_server_errors_in_a_row_stop_the_run() -> None:
    fake, api = fx()
    fake.script = [500, 502, 503]
    with pytest.raises(ServerErrors):
        api.page(at(D, 0), END, 0)


def test_two_server_errors_then_a_success_is_fine_and_resets_the_streak() -> None:
    fake, api = fx()
    fake.sessions = [make(1, at(D, 3))]
    fake.script = [500, 500]
    api.page(at(D, 0), END, 0)
    fake.script = [500, 500]
    count, _ = api.page(at(D, 0), END, 0)
    assert count == 1


def test_http_429_stops_the_run_at_once() -> None:
    fake, api = fx()
    fake.script = [429]
    with pytest.raises(RateLimited):
        api.page(at(D, 0), END, 0)
    assert fake.n == 1


def test_a_connection_failure_counts_as_a_server_error() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)

    api = Api(httpx.Client(transport=httpx.MockTransport(boom)), FakeClock(), 5.0, 100)
    with pytest.raises(ServerErrors):
        api.page(at(D, 0), END, 0)


# ---- the 5-second rule --------------------------------------------------------------------------------------------
def test_five_seconds_between_every_request_whatever_the_status() -> None:
    fake, api = fx()
    fake.sessions = [make(i, at(D, 1, i)) for i in range(1, 6)]
    fake.script = [500, 500]
    fetch_day(api, D)
    assert fake.n == 3  # two server errors, then the one page that answers the whole day
    assert min(gaps(fake.log)) >= 5.0


def test_the_wait_after_a_timeout_also_applies() -> None:
    fake, api = fx()
    fake.timeout_before = date(2026, 11, 14)
    fake.sessions = [make(1, at(D, 1, 1))]
    with pytest.raises(WindowTimeout):
        api.page(at(D, 0), END, 0)
    fake.timeout_before = None  # the next request (a different one, not a retry of the same day) still waits its turn
    api.page(at(D, 0), END, 0)
    assert min(gaps(fake.log)) >= 5.0


def test_a_larger_interval_setting_is_honoured_and_a_hold_delays_the_first_request() -> None:
    fake, api = fx(page=1, interval=8.0)
    fake.sessions = [make(1, at(D, 1, 1)), make(2, at(D, 1, 2))]  # two sessions, one per page, so there is a gap to check
    api.hold(60.0)
    fetch_day(api, D)
    assert fake.log[0].t >= 60.0
    assert min(gaps(fake.log)) >= 8.0
