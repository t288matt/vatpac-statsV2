"""The limiter and the stop rules, with a fake clock and a fake API: nothing waits."""

from __future__ import annotations

import contextlib

import pytest

from fakes import FakeApi, FakeClock, at
from vatsim_stats.api import (
    Api,
    BadResponse,
    RateLimited,
    ServerErrors,
    Stopped,
    WindowTimeout,
    bare_connection_id,
    has_no_member_id,
    parse_session,
)

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
    fake.script = [504, "timeout", 504]
    for _ in range(3):
        with pytest.raises(WindowTimeout):  # HTTP 504 and no answer in time both mean "the window is too long"
            api.page(S, E, 0)
    fake.script = [500, 500]
    assert api.page(S, E, 0) == (0, [])  # three timeouts did not count: two server errors are still not three


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


def item(**conn: object) -> dict:
    base = {"id": 7, "vatsim_id": "100", "callsign": "AA_TWR", "start": "2026-11-10T01:00:00Z", "end": None}
    return {"connection_id": {**base, **conn}}


@pytest.mark.parametrize("value", ["", "   ", "	", None])
def test_a_blank_or_null_member_id_is_no_member(value: object) -> None:
    assert has_no_member_id(item(vatsim_id=value))


def test_a_missing_member_id_key_is_no_member() -> None:
    it = item()
    del it["connection_id"]["vatsim_id"]
    assert has_no_member_id(it)


def test_a_real_member_id_or_another_defect_is_not_no_member() -> None:
    assert not has_no_member_id(item())
    assert not has_no_member_id(item(vatsim_id="abc"))  # unreadable, not blank: still fails in parse_session
    assert not has_no_member_id({"connection_id": "x"})
    assert not has_no_member_id({})


def test_parse_session_stays_strict_for_everything_else() -> None:
    assert parse_session(item())["cid"] == 100
    for bad in (item(vatsim_id="abc"), item(start="not a time"), item(callsign=None, id="x")):
        with pytest.raises(BadResponse):
            parse_session(bad)


def test_a_skipped_item_needs_a_connection_id() -> None:
    assert bare_connection_id(item(vatsim_id="")) == 7
    for bad in (item(id=None), {"connection_id": {}}, {}):
        with pytest.raises(BadResponse):
            bare_connection_id(bad)
