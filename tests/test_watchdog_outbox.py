"""Issue 83: undelivered Telegram messages are kept in the watchdog state and retried (ops/watchdog/watchdog.py)."""
from __future__ import annotations

import importlib.util
import json
import logging
import re
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


def send(a: Any, text: str) -> None:
    """Raise an alert written as its technical text (how these tests describe it), through the structured path the watchdog uses."""
    if (m := re.match(r"(WARN|CRIT) (\S+): (.*)$", text, re.S)):
        a.notify(m.group(2), (m.group(1).lower(), m.group(3)))
    elif (m := re.match(r"RECOVERED (\S+) \(was (\w+)\): (.*)$", text, re.S)):
        a.notify(m.group(1), ("ok", m.group(3)), previous=m.group(2))
    elif (m := re.match(r"NEAR-MISS .*?: (.*)$", text)):
        a.notify("near-miss", ("warn", m.group(1)))
    elif (m := re.match(r"RESTARTED (\S+) \((.*?)\): (.*)$", text)):
        a.notify(f"restarted:{m.group(1)}", wd.Checked("warn", f"{m.group(2)}: {m.group(3)}", counts=m.group(2), why=m.group(3)))
    else:
        send(a, text)


class Clock:
    now = NOW

    def time(self) -> float:
        return self.now


class Telegram:
    """A fake Bot API: the answer comes from `status` (an int, or an Exception to raise); delivered texts are recorded."""

    def __init__(self) -> None:
        self.status: int | Exception = 200
        self.description = ""
        self.delivered: list[str] = []
        self.attempts = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.attempts += 1
        if isinstance(self.status, Exception):
            raise self.status
        if self.status == 200:
            self.delivered.append(json.loads(request.content)["text"])
        if self.description:
            return httpx.Response(self.status, json={"ok": False, "description": self.description})
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
    send(a, "CRIT disk: / 95% used")
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
    send(a, "WARN disk: / 85% used")
    assert len(state["outbox"]) == 1
    tg.status = 200
    clock.now += 1000
    a.flush()
    assert len(tg.delivered) == 1


def test_a_late_message_says_so_and_keeps_the_original_time(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = 500
    send(a, "CRIT disk: / 95% used")
    original = state["outbox"][0]["body"]
    tg.status = 200
    clock.now += 600
    a.flush()
    sent = tg.delivered[0]
    assert "delivered late (first raised" in sent and "UTC" in sent
    assert original.split("(", 1)[1].split(")", 1)[0] in sent   # the event's own HH:MM UTC is still there
    assert sent.splitlines()[-1] == original.splitlines()[-1]
    assert "). A disk" in sent and "delayed message" not in sent  # a capital after the full stop, no lowercase word


def test_a_prompt_message_is_not_marked_delayed(clock: Clock, tg: Telegram) -> None:
    a = make(tg, {})
    send(a, "CRIT disk: / 95% used")
    assert "delayed" not in tg.delivered[0]


def test_outbox_survives_a_restart(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = httpx.ConnectError("down")
    send(a, "CRIT disk: / 95% used")
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
    send(a, "CRIT disk: / 95% used")
    send(a, "RECOVERED disk (was crit): / 40% used")      # raised while the first is still stuck
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
    send(a, "CRIT disk: / 95% used")
    tg.status = 200
    send(a, "WARN db_size: 30.00 GB (limit 20)")          # still backing off: must not be sent ahead of the first
    assert tg.delivered == [] and len(state["outbox"]) == 2


def test_outbox_is_bounded_and_drops_the_oldest(clock: Clock, tg: Telegram, caplog: pytest.LogCaptureFixture) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = 500
    with caplog.at_level(logging.WARNING, logger="watchdog"):
        for i in range(wd.OUTBOX_MAX + 5):
            send(a, f"WARN check{i}: detail")
    assert len(state["outbox"]) == wd.OUTBOX_MAX
    assert "check0" not in state["outbox"][0]["body"] and f"check{wd.OUTBOX_MAX + 4}" in state["outbox"][-1]["body"]
    assert "dropped the oldest undelivered message" in caplog.text
    assert "SECRET" not in caplog.text


def test_back_off_grows_and_is_capped(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = 500
    send(a, "WARN x: d")
    gaps = []
    for _ in range(9):
        clock.now = state["outbox_next_try"] + 0.1
        a.flush()
        gaps.append(state["outbox_next_try"] - clock.now)
    assert gaps[0] > wd.OUTBOX_BACKOFF_BASE_S and gaps == sorted(gaps) and max(gaps) <= wd.OUTBOX_BACKOFF_MAX_S + 0.01


def test_a_poison_message_is_dropped_and_the_next_alert_goes_out_at_once(clock: Clock, tg: Telegram) -> None:
    """HTTP 400 five times = the message itself is bad: drop it, and do not make the next alert wait for it."""
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = 400
    send(a, "WARN x: d")
    for _ in range(wd.OUTBOX_MAX_REJECTIONS - 2):
        clock.now += 2000
        a.flush()
    assert len(state["outbox"]) == 1
    # the fifth refusal drops it; the CRIT queued behind it must be tried in the same pass, not after a back-off
    a.outbox.append({"body": "VATPAC Stats: problem (10:00 UTC). CRIT y", "at": clock.now, "tries": 0})
    clock.now += 2000
    tg.status = 400
    real = tg.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if "CRIT y" in json.loads(request.content)["text"]:
            tg.status = 200
        return real(request)

    a.client = httpx.Client(transport=httpx.MockTransport(handle))
    a.flush()
    assert state["outbox"] == [] and any("CRIT y" in t for t in tg.delivered)
    assert "outbox_next_try" not in state


@pytest.mark.parametrize("code", [401, 403, 404])
def test_a_configuration_fault_never_drops_a_message(clock: Clock, tg: Telegram, code: int) -> None:
    """A rotated token, a blocked bot or a wrong chat id must not eat a CRIT: keep it, retry at the maximum back-off."""
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = code
    send(a, "CRIT disk: / 95% used")
    assert state["outbox_next_try"] - clock.now >= wd.OUTBOX_BACKOFF_MAX_S - 1
    for _ in range(6):
        clock.now += wd.OUTBOX_BACKOFF_MAX_S + 1
        a.flush()
    assert len(state["outbox"]) == 1 and "rejected" not in state["outbox"][0]
    tg.status = 200
    clock.now += wd.OUTBOX_BACKOFF_MAX_S + 1
    a.flush()
    assert state["outbox"] == [] and len(tg.delivered) == 1


def test_at_the_bound_all_clear_messages_go_first_and_a_notice_counts_the_loss(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = 500
    send(a, "CRIT first: d")
    for i in range(wd.OUTBOX_MAX - 2):
        send(a, f"RECOVERED r{i} (was warn): d") if i % 2 else send(a, f"WARN w{i}: d")
    send(a, "RECOVERED late (was crit): d")
    send(a, "WARN newest: d")                      # over the bound
    bodies = [m["body"] for m in state["outbox"]]
    assert len(bodies) <= wd.OUTBOX_MAX
    assert any("CRIT first" in b or "first" in b for b in bodies), "the oldest CRIT must survive while all-clears exist"
    assert state["outbox"][0].get("notice") and "dropped" in state["outbox"][0]["body"]
    assert any("newest" in b for b in bodies)
    tg.status = 200
    clock.now += 2000
    a.flush()
    assert any("dropped while Telegram" in t for t in tg.delivered)


def test_a_long_backlog_is_sent_a_few_at_a_time(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = 500
    for i in range(12):
        send(a, f"WARN c{i}: d")
    tg.status = 200
    clock.now += 2000
    a.flush()
    assert len(tg.delivered) == wd.OUTBOX_MAX_SENDS_PER_CYCLE and len(state["outbox"]) == 7
    a.flush()
    a.flush()
    assert len(tg.delivered) == 12 and [t.split("WARN")[0] for t in tg.delivered][0] != ""  # delivered in order, all of them
    assert [("c%d" % i) in tg.delivered[i] for i in range(12)] == [True] * 12


def test_telegram_outbox_check(clock: Clock, tg: Telegram) -> None:
    cfg = type("C", (), {"confirm": 1, "tg_token": "1:T", "tg_chat": "9"})()
    assert wd.check_telegram_outbox(cfg, {})[0] == wd.OK                                   # type: ignore[arg-type]
    young = {"outbox": [{"body": "x", "at": NOW - 60}]}
    assert wd.check_telegram_outbox(cfg, young, NOW)[0] == wd.OK                           # type: ignore[arg-type]
    old = {"outbox": [{"body": "x", "at": NOW - 901}, {"body": "y", "at": NOW}]}
    level, detail = wd.check_telegram_outbox(cfg, old, NOW)                                # type: ignore[arg-type]
    assert level == wd.WARN and "2 undelivered" in detail
    off = type("C", (), {"tg_token": "", "tg_chat": ""})()
    assert wd.check_telegram_outbox(off, old, NOW)[0] == wd.OK                             # type: ignore[arg-type]


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
    d40, d85, d95 = {"disks": [["/", 40]]}, {"disks": [["/", 85]]}, {"disks": [["/", 95]]}
    seq: list[tuple[str, str, str, dict[str, Any]]] = [
     ("disk", "ok", "/ 40% used", d40), ("disk", "warn", "/ 85% used", d85), ("disk", "warn", "/ 85% used", d85),
     ("disk", "crit", "/ 95% used", d95), ("disk", "crit", "/ 95% used", d95), ("disk", "ok", "/ 40% used", d40), ("disk", "ok", "/ 40% used", d40),
     ("poll_log_ok_age", "warn", "newest ok poll is 70s old", {"age_s": 70}), ("poll_log_ok_age", "crit", "newest ok poll is 400s old", {"age_s": 400}),
     ("poll_log_ok_age", "crit", "newest ok poll is 400s old", {"age_s": 400}), ("poll_log_ok_age", "ok", "newest ok poll 5s old", {"age_s": 5}),
     ("poll_log_ok_age", "ok", "x", {}),
     ("backup_age", "crit", "newest backup 30.0 h old", {"age_h": 30.0}), ("backup_age", "crit", "newest backup 30.0 h old", {"age_h": 30.0}),
     ("container:collector", "crit", "unhealthy", {}), ("container:collector", "crit", "unhealthy", {}),
     ("container:collector", "ok", "running/healthy", {}), ("container:collector", "ok", "running/healthy", {}),
     ("restore_test", "crit", "last restore test FAILED 2.0 days ago", {"state": "failed"}),
     ("restore_test", "crit", "last restore test FAILED 2.0 days ago", {"state": "failed"}),
     ("identity_flap", "warn", "3 pilot and 0 controller session pairs", {"query_failed": False}), ("identity_flap", "warn", "x", {}),
     ("db_size", "warn", "25.00 GB (limit 20)", {"gb": 25.0}), ("db_size", "warn", "25.00 GB (limit 20)", {"gb": 25.0}),
    ]
    for n, lv, d, f in seq:
        clock.now += 60
        a.update(n, lv, d, f)
    send(a, "NEAR-MISS controller callsign(s) not on whitelist: X_TWR")
    send(a, "RESTARTED collector (1/3 this hour, ok=True): stuck")
    got = [re.sub(r"\(\d\d:\d\d UTC\)", "(HH:MM UTC)", t) for t in tg.delivered]
    assert got == json.loads((REPO / "tests" / "fixtures" / "alerter_replay_golden.json").read_text())


def test_a_wrong_chat_id_answered_with_400_never_drops_the_crit(clock: Clock, tg: Telegram) -> None:
    """Telegram says HTTP 400 'Bad Request: chat not found' for a wrong TELEGRAM_CHAT_ID: a configuration fault, not poison."""
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status, tg.description = 400, "Bad Request: chat not found"
    send(a, "CRIT disk: / 95% used")
    for _ in range(8):
        clock.now += wd.OUTBOX_BACKOFF_MAX_S + 1
        a.flush()
    assert len(state["outbox"]) == 1 and "rejected" not in state["outbox"][0]
    cfg = type("C", (), {"tg_token": "123:SECRET", "tg_chat": "42"})()
    level, detail = wd.check_telegram_outbox(cfg, state, clock.now)                          # type: ignore[arg-type]
    assert level == wd.WARN and "Telegram refuses the messages" in detail and "TELEGRAM_CHAT_ID" in detail
    tg.status, tg.description = 200, ""
    clock.now += wd.OUTBOX_BACKOFF_MAX_S + 1
    a.flush()
    assert len(tg.delivered) == 1 and "outbox_refused" not in state
    assert wd.check_telegram_outbox(cfg, state, clock.now)[0] == wd.OK                       # type: ignore[arg-type]


@pytest.mark.parametrize("desc", ["Bad Request: bot was blocked by the user", "Forbidden: bot was kicked from the group chat",
                                  "Bad Request: chat_id is empty", "Unauthorized", "Bad Request: not enough rights to send text messages"])
def test_other_configuration_descriptions_are_never_poison(clock: Clock, tg: Telegram, desc: str) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status, tg.description = 400, desc
    send(a, "CRIT x: d")
    for _ in range(wd.OUTBOX_MAX_REJECTIONS + 2):
        clock.now += wd.OUTBOX_BACKOFF_MAX_S + 1
        a.flush()
    assert len(state["outbox"]) == 1


def test_a_400_about_the_text_is_poison_and_the_next_alert_goes_at_once(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status, tg.description = 400, "Bad Request: can't parse entities: Can't find end of the entity"
    send(a, "WARN bad: d")
    for _ in range(wd.OUTBOX_MAX_REJECTIONS - 2):
        clock.now += 2000
        a.flush()
    a.outbox.append({"body": "VATPAC Stats: problem (10:00 UTC). CRIT good", "at": clock.now, "tries": 0})
    real = tg.handle

    def handle(request: httpx.Request) -> httpx.Response:
        if "CRIT good" in json.loads(request.content)["text"]:
            tg.status, tg.description = 200, ""
        return real(request)

    a.client = httpx.Client(transport=httpx.MockTransport(handle))
    clock.now += 2000
    a.flush()
    assert state["outbox"] == [] and any("CRIT good" in t for t in tg.delivered)
    assert "outbox_refused" not in state


def test_the_log_shows_the_description_but_never_the_token(clock: Clock, tg: Telegram, caplog: pytest.LogCaptureFixture) -> None:
    a = make(tg, {})
    tg.status, tg.description = 401, "Unauthorized 123:SECRET"
    with caplog.at_level(logging.WARNING, logger="watchdog"):
        send(a, "CRIT x: d")
    assert "Unauthorized" in caplog.text and "SECRET" not in caplog.text


def test_the_send_cap_is_per_cycle_not_per_flush(clock: Clock, tg: Telegram) -> None:
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = 500
    for i in range(12):
        send(a, f"WARN c{i}: d")
    tg.status = 200
    clock.now += 2000
    a.begin_cycle()
    a.flush()
    a.flush()
    a.flush()                                      # three flushes in ONE cycle share a single budget
    assert len(tg.delivered) == wd.OUTBOX_MAX_SENDS_PER_CYCLE
    a.end_cycle()
    a.begin_cycle()                                # the next cycle gets a fresh budget
    a.flush()
    assert len(tg.delivered) == 2 * wd.OUTBOX_MAX_SENDS_PER_CYCLE


@pytest.mark.parametrize("code", [400, 413])
def test_a_poison_message_is_dropped_on_exactly_the_fifth_refusal(clock: Clock, tg: Telegram, code: int) -> None:
    """Issue 342, mutants C11 (OUTBOX_MAX_REJECTIONS = 500) and C12 (413 no longer poison): the limit is pinned as the number
    5, not as the constant, and both statuses Telegram uses for a bad message count."""
    state: dict[str, Any] = {}
    a = make(tg, state)
    tg.status = code
    send(a, "WARN x: d")                            # send() tries once at once: refusal number 1
    assert tg.attempts == 1 and len(state["outbox"]) == 1
    for attempt in range(2, 6):
        clock.now += 100_000                       # beyond any back-off: every flush really tries
        a.flush()
        assert tg.attempts == attempt
        assert len(state["outbox"]) == (1 if attempt < 5 else 0), (code, attempt)
