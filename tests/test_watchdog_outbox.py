"""Issue 83: undelivered Telegram messages are kept in the watchdog state and retried (ops/watchdog/watchdog.py)."""
from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("watchdog_under_test_outbox", REPO / "ops" / "watchdog" / "watchdog.py")
assert spec and spec.loader
wd = importlib.util.module_from_spec(spec)
sys.modules["watchdog_under_test_outbox"] = wd
spec.loader.exec_module(wd)

NOW = 1_800_000_000.0


class Clock:
    now = NOW

    def time(self) -> float:
        return self.now


class Telegram:
    """A fake Bot API: the answer comes from `status` (an int, or an Exception to raise); delivered texts are recorded."""

    def __init__(self) -> None:
        self.status: int | Exception = 200
        self.delivered: list[str] = []
        self.attempts = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if isinstance(self.status, Exception):
            raise self.status
        if self.status == 200:
            self.delivered.append(json.loads(request.content)["text"])
        return httpx.Response(self.status)


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(wd, "time", c)
    return c


@pytest.fixture()
def tg() -> Telegram:
    return Telegram()


def make(tg: Telegram, state: dict[str, Any]) -> wd.Alerter:
    cfg = type("C", (), {"confirm": 1, "tg_token": "123:SECRET", "tg_chat": "42"})()
    return wd.Alerter(cfg, httpx.Client(transport=httpx.MockTransport(tg.handle)), state)  # type: ignore[arg-type]


def test_fail_then_succeed_delivers_once_after_the_back_off(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = httpx.ConnectError("down")
    a.send("CRIT disk: / 95% used")
    assert tg.delivered == [] and len(state["outbox"]) == 1
    tg.status = 200
    a.flush()                                           # too soon: still backing off
    assert tg.delivered == []
    clock.now += wd.OUTBOX_BACKOFF_BASE_S + 1
    a.flush()
    assert len(tg.delivered) == 1 and state["outbox"] == []
    a.flush()
    assert len(tg.delivered) == 1                       # delivered exactly once


def test_http_error_status_is_retried_too(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = 502
    a.send("WARN disk: / 85% used")
    assert len(state["outbox"]) == 1
    tg.status = 200
    clock.now += 1000
    a.flush()
    assert len(tg.delivered) == 1


def test_a_late_message_says_so_and_keeps_the_original_time(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = 500
    a.send("CRIT disk: / 95% used")
    original = state["outbox"][0]["body"]
    tg.status = 200
    clock.now += 600
    a.flush()
    sent = tg.delivered[0]
    assert "delayed message, first raised" in sent and "UTC" in sent
    assert original.split("(", 1)[1].split(")", 1)[0] in sent   # the event's own HH:MM UTC is still there
    assert sent.splitlines()[-1] == original.splitlines()[-1]


def test_a_prompt_message_is_not_marked_delayed(clock: Clock, tg: Telegram) -> None:
    a = make(tg, {})
    a.send("CRIT disk: / 95% used")
    assert "delayed" not in tg.delivered[0]


def test_outbox_survives_a_restart(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = httpx.ConnectError("down")
    a.send("CRIT disk: / 95% used")
    reloaded = json.loads(json.dumps(state))             # the status file is JSON
    tg.status = 200
    clock.now += 1000
    b = make(tg, reloaded)                               # a new process
    b.flush()
    assert len(tg.delivered) == 1 and reloaded["outbox"] == []


def test_an_old_status_file_without_an_outbox_loads(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {"alerts": {"disk": "crit"}, "bad_since": {}}
    a = make(tg, state)
    assert state["outbox"] == [] and a.confirmed == {"disk": "crit"}


def test_order_is_kept_and_a_recovery_never_overtakes_its_problem(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = httpx.ConnectError("down")
    a.send("CRIT disk: / 95% used")
    a.send("RECOVERED disk (was crit): / 40% used")      # raised while the first is still stuck
    assert tg.delivered == [] and len(state["outbox"]) == 2
    tg.status = 200
    clock.now += 1000
    a.flush()
    assert len(tg.delivered) == 2
    assert "problem" in tg.delivered[0] and "all clear" in tg.delivered[1]


def test_a_new_message_waits_behind_a_backlog_and_does_not_jump_the_queue(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = 500
    a.send("CRIT disk: / 95% used")
    tg.status = 200
    a.send("WARN db_size: 30.00 GB (limit 20)")          # still backing off: must not be sent ahead of the first
    assert tg.delivered == [] and len(state["outbox"]) == 2


def test_outbox_is_bounded_and_drops_the_oldest(clock: Clock, tg: Telegram, caplog: pytest.LogCaptureFixture) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = 500
    with caplog.at_level(logging.WARNING, logger="watchdog"):
        for i in range(wd.OUTBOX_MAX + 5):
            a.send(f"WARN check{i}: detail")
    assert len(state["outbox"]) == wd.OUTBOX_MAX
    assert "check0" not in state["outbox"][0]["body"] and f"check{wd.OUTBOX_MAX + 4}" in state["outbox"][-1]["body"]
    assert "dropped the oldest undelivered message" in caplog.text
    assert "SECRET" not in caplog.text


def test_back_off_grows_and_is_capped(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = 500
    a.send("WARN x: d")
    gaps = []
    for _ in range(9):
        clock.now = state["outbox_next_try"] + 0.1
        a.flush()
        gaps.append(state["outbox_next_try"] - clock.now)
    assert gaps[0] > wd.OUTBOX_BACKOFF_BASE_S and gaps == sorted(gaps) and max(gaps) <= wd.OUTBOX_BACKOFF_MAX_S + 0.01


def test_a_message_telegram_always_rejects_is_dropped_so_it_cannot_block_the_queue(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = 400
    a.send("WARN x: d")
    for _ in range(wd.OUTBOX_MAX_REJECTIONS):
        clock.now += 2000
        a.flush()
    assert state["outbox"] == []


def test_no_telegram_settings_queue_nothing(clock: Clock, tg: Telegram) -> None:
    cfg = type("C", (), {"confirm": 1, "tg_token": "", "tg_chat": ""})()
    state: dict[str, Any] = {}
    wd.Alerter(cfg, httpx.Client(transport=httpx.MockTransport(tg.handle)), state).send("hello")  # type: ignore[arg-type]
    assert state["outbox"] == [] and tg.attempts == 0


def test_alert_logic_is_unchanged_one_message_per_confirmed_change(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    for lv in ("crit", "crit", "crit", "ok"):
        a.update("disk", lv, f"/ 95% used {lv}")
    assert len(tg.delivered) == 2

# ---- characterisation: the alerts a fixed run of check results produces are exactly what origin/main produced (golden file
# captured from the code before issue 83 changed the send path); only a FAILED send now behaves differently.
def test_a_fixed_run_of_check_results_sends_exactly_the_golden_messages(clock: Clock, tg: Telegram) -> None:
    import re
    cfg = type("C", (), {"confirm": 2, "tg_token": "1:T", "tg_chat": "9"})()
    a = wd.Alerter(cfg, httpx.Client(transport=httpx.MockTransport(tg.handle)), {})  # type: ignore[arg-type]
    seq = [
     ("disk", "ok", "/ 40% used"), ("disk", "warn", "/ 85% used"), ("disk", "warn", "/ 85% used"), ("disk", "crit", "/ 95% used"),
     ("disk", "crit", "/ 95% used"), ("disk", "ok", "/ 40% used"), ("disk", "ok", "/ 40% used"),
     ("poll_log_ok_age", "warn", "newest ok poll is 70s old"), ("poll_log_ok_age", "crit", "newest ok poll is 400s old"),
     ("poll_log_ok_age", "crit", "newest ok poll is 400s old"), ("poll_log_ok_age", "ok", "newest ok poll 5s old"), ("poll_log_ok_age", "ok", "x"),
     ("backup_age", "crit", "newest backup 30.0 h old"), ("backup_age", "crit", "newest backup 30.0 h old"),
     ("container:collector", "crit", "unhealthy"), ("container:collector", "crit", "unhealthy"), ("container:collector", "ok", "running/healthy"), ("container:collector", "ok", "running/healthy"),
     ("restore_test", "crit", "last restore test FAILED 2.0 days ago"), ("restore_test", "crit", "last restore test FAILED 2.0 days ago"),
     ("identity_flap", "warn", "3 pilot and 0 controller session pairs"), ("identity_flap", "warn", "x"),
     ("db_size", "warn", "25.00 GB (limit 20)"), ("db_size", "warn", "25.00 GB (limit 20)"),
    ]
    for n, lv, d in seq:
        clock.now += 60
        a.update(n, lv, d)
    a.send("NEAR-MISS controller callsign(s) not on whitelist: X_TWR")
    a.send("RESTARTED collector (1/3 this hour, ok=True): stuck")
    got = [re.sub(r"\(\d\d:\d\d UTC\)", "(HH:MM UTC)", t) for t in tg.delivered]
    assert got == json.loads((REPO / "tests" / "fixtures" / "alerter_replay_golden.json").read_text())
