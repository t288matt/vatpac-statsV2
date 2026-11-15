"""The limiter and the stop rules, with a fake clock and a fake API: nothing waits."""

from __future__ import annotations

import contextlib

import pytest

from fakes import FakeApi, FakeClock, at
from vatsim_stats.api import Api, RateLimited, ServerErrors, Stopped, WindowTimeout

S, E = at(2026, 11, 10), at(2026, 11, 10, 23, 59, 59)


def make(fake: FakeApi, clock: FakeClock, interval: float = 5.0, page: int = 100) -> Api:
    return Api(fake.client(), clock, interval, page)


def test_five_seconds_after_every_response_even_a_failed_one(fake: FakeApi, clock: FakeClock) -> None:
    api = make(fake, clock)
    fake.script = [500, "timeout", "timeout", 500]
    fake.add(1, "AA_TWR", at(2026, 11, 10, 1))
    for _ in range(3):
        with contextlib.suppress(WindowTimeout):
            api.page(S, E, 0)
    assert len(fake.calls) == 5
    for before, after in zip(fake.calls, fake.calls[1:], strict=False):
        assert after.started - before.ended >= 5.0


def test_a_larger_interval_setting_is_kept_and_one_request_at_a_time(fake: FakeApi, clock: FakeClock) -> None:
    api = make(fake, clock, interval=12.0)
    api.page(S, E, 0)
    api.page(S, E, 0)
    assert fake.calls[1].started - fake.calls[0].ended >= 12.0


def test_sixty_second_wait_after_a_start(fake: FakeApi, clock: FakeClock) -> None:
    api = make(fake, clock)
    api.hold(60)
    api.page(S, E, 0)
    assert fake.calls[0].started >= 60.0


def test_429_stops_at_once_with_no_retry(fake: FakeApi, clock: FakeClock) -> None:
    api = make(fake, clock)
    fake.script = [429]
    with pytest.raises(RateLimited):
        api.page(S, E, 0)
    assert len(fake.calls) == 1


def test_three_server_errors_in_a_row_stop_the_run(fake: FakeApi, clock: FakeClock) -> None:
    api = make(fake, clock)
    fake.script = [500, 502, 503]
    with pytest.raises(ServerErrors):
        api.page(S, E, 0)
    assert len(fake.calls) == 3


def test_server_error_count_is_reset_by_a_good_answer(fake: FakeApi, clock: FakeClock) -> None:
    api = make(fake, clock)
    fake.script = [500, 500]
    api.page(S, E, 0)  # two errors, then a good answer from the fake
    fake.script = [500, 500]
    api.page(S, E, 0)  # two more: still not three in a row
    assert len(fake.calls) == 6


def test_a_timeout_is_not_a_server_error(fake: FakeApi, clock: FakeClock) -> None:
    api = make(fake, clock)
    fake.script = [500, 500, "timeout", 504]
    with pytest.raises(WindowTimeout):
        api.page(S, E, 0)
    with pytest.raises(WindowTimeout):
        api.page(S, E, 0)  # HTTP 504 is a timeout too
    assert api.page(S, E, 0) == (0, [])  # the two server errors were never three in a row


def test_a_stop_signal_ends_the_wait_and_sends_no_request(fake: FakeApi, clock: FakeClock) -> None:
    api = make(fake, clock)
    api.page(S, E, 0)
    clock.stop.set()
    with pytest.raises(Stopped):
        api.page(S, E, 0)
    assert len(fake.calls) == 1


def test_gzip_is_asked_for_and_read(fake: FakeApi, clock: FakeClock) -> None:
    fake.add(1, "AA_TWR", at(2026, 11, 10, 1))
    count, items = make(fake, clock).page(S, E, 0)
    assert fake.calls[0].headers["accept-encoding"] == "gzip"
    assert count == 1 and items[0]["connection_id"]["callsign"] == "AA_TWR"
