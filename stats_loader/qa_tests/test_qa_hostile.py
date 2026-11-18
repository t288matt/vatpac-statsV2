# ruff: noqa: E501
"""QA: a hostile or buggy API count, a bad day-window response, and an impossible retention. Written from the manager's specification."""

from __future__ import annotations

from datetime import date, timedelta

import psycopg
import pytest
from qa_fakes import FakeApi, FakeClock, at, make
from qa_helpers import TODAY, fresh, last_run, n_rows, run

from vatsim_stats import app, settings
from vatsim_stats.windows import DayFailed, fetch_day

D = date(2026, 11, 10)
ENV = {"DATABASE_URL": "postgresql://x", "STATS_RUN_AT": "06:00", "STATS_MIN_INTERVAL_SECONDS": "5", "STATS_PAGE_SIZE": "5000",
       "STATS_OVERLAP_DAYS": "2", "STATS_RETENTION_YEARS": "1"}


# ---- (1) a count that cannot be reached ------------------------------------------------------------------------
@pytest.mark.parametrize("count", [10**12, 10**6, 50])
def test_a_count_that_pages_never_reach_stops_the_window_and_fails_the_day(count: int) -> None:
    fake = FakeApi(FakeClock())
    fake.sessions = [make(i, at(D, 1, i)) for i in range(1, 4)]
    fake.repeat_pages = True  # non-empty pages for ever, always the same sessions
    fake.drift = lambda n, c: count
    api = fake.api(5.0, 5000)
    with pytest.raises(DayFailed):
        fetch_day(api, D)
    assert fake.n <= 5  # nothing like count / 3; no hour-window retry either


def test_a_large_count_with_honest_empty_pages_ends_on_the_empty_page() -> None:
    fake = FakeApi(FakeClock())
    fake.sessions = [make(i, at(D, 1, i)) for i in range(1, 4)]
    fake.drift = lambda n, c: 10**12
    with pytest.raises(DayFailed):
        fetch_day(fake.api(5.0, 5000), D)
    assert fake.n < 20


def test_the_request_bound_follows_the_count_and_the_page_size_received() -> None:
    """A real day of 30 sessions answered 10 at a time needs 3 pages; it must not be cut short by the new bound."""
    fake = FakeApi(FakeClock())
    fake.sessions = [make(i, at(D, 1, i % 60, i // 60)) for i in range(1, 31)]
    fake.cap = 10
    day = fetch_day(fake.api(5.0, 5000), D)
    assert len(day.rows) == 30 and fake.n == 3


def test_pages_that_shrink_to_one_item_are_cut_off_by_the_request_bound_and_fail_the_day() -> None:
    """count 96, first page 10 items, then one item a page: 10 + 86 more requests would be needed; the bound (about
    ceil(96/10) + 2) catches it and fails the day, with no hour-window rescue."""
    fake = FakeApi(FakeClock())
    fake.sessions = [make(i + 1, at(D, i // 4, i % 4 * 10)) for i in range(96)]
    fake.cap, fake.cap_after_first = 10, 1
    with pytest.raises(DayFailed):
        fetch_day(fake.api(5.0, 5000), D)
    assert fake.n <= 13


def test_pages_made_only_of_atis_do_not_fail_an_honest_day() -> None:
    fake = FakeApi(FakeClock())
    fake.sessions = [make(1, at(D, 1, 0), callsign="SY_ATIS"), make(2, at(D, 1, 1), callsign="ML_ATIS"),
                     make(3, at(D, 2, 0)), make(4, at(D, 2, 1))]
    day = fetch_day(fake.api(5.0, 2), D)  # the first page holds two ATIS sessions and nothing else
    assert {r["connection_id"] for r in day.rows} == {3, 4}


def test_a_hostile_count_in_a_run_fails_that_day_writes_nothing_and_the_run_is_failed(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    y1 = TODAY - timedelta(days=1)
    fake.sessions = [make(1, at(y1, 3)), make(2, at(y1, 4))]
    fake.repeat_pages = True
    fake.drift = lambda n, c: 10**12
    assert run(conn, fake) == "failed"
    assert n_rows(su) == 0
    r = last_run(su)
    assert r["status"] == "failed" and y1 in r["failed_days"]
    assert fake.n < 400


# ---- (2) shifted order and bad day-window responses simply fail the day, with no hour-window repair ---------------
def test_shifted_page_order_in_a_run_fails_that_day_with_no_hour_window_repair(conn: psycopg.Connection, su: psycopg.Connection) -> None:
    clock, fake = fresh()
    y1 = TODAY - timedelta(days=1)
    fake.sessions = [make(i, at(y1, 7, 30, 0)) for i in range(1, 5)] + [make(9, at(y1, 20, 0, 0))]
    fake.shift_always = True
    assert run(conn, fake, page_size=2) == "failed"
    assert last_run(su)["failed_days"] == [y1]
    assert n_rows(su) == 0  # nothing for the shifted day; no hour windows were ever tried to repair it


def test_a_bad_response_to_the_day_window_fails_the_day_with_no_hour_window_fallback() -> None:
    fake = FakeApi(FakeClock())
    fake.sessions = [make(i, at(D, i, 5)) for i in range(1, 6)]
    fake.garbage = 1  # the one request gets a body that is not the API's JSON
    with pytest.raises(DayFailed):  # a failed day, not an escaping BadResponse
        fetch_day(fake.api(5.0, 5000), D)
    assert fake.n == 1  # one request; never retried as hour windows


def test_a_malformed_session_in_the_day_window_fails_the_day() -> None:
    fake = FakeApi(FakeClock())
    fake.sessions = [make(1, at(D, 1, 5)), make(2, at(D, 2, 5))]
    fake.sessions[1].cid = "abc"  # type: ignore[assignment]  # not blank, and not a controller id (a null ID is skipped since #387)
    with pytest.raises(DayFailed):
        fetch_day(fake.api(5.0, 5000), D)


# ---- (3) retention too large for the calendar ---------------------------------------------------------------------
@pytest.mark.parametrize("years", ["99999", "10000", "3000", str(10**9), "2026"])
def test_a_retention_that_goes_back_before_year_1_is_refused_with_a_clear_message(years: str) -> None:
    with pytest.raises(settings.SettingsError) as e:
        settings.load({**ENV, "STATS_RETENTION_YEARS": years})
    assert "STATS_RETENTION_YEARS" in str(e.value)


@pytest.mark.parametrize("years", ["1", "3", "15", "100"])
def test_ordinary_retentions_start(years: str) -> None:
    assert settings.load({**ENV, "STATS_RETENTION_YEARS": years}).retention_years == int(years)


def test_main_refuses_to_start_on_99999_and_says_why(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    for k, v in {**ENV, "STATS_RETENTION_YEARS": "99999"}.items():
        monkeypatch.setenv(k, v)
    assert app.main(["x"]) != 0
    assert "STATS_RETENTION_YEARS" in caplog.text and "refusing" in caplog.text.lower()


