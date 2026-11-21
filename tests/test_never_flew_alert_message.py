"""Issue 331: the wording and the settings of the never-flew alert, from the owner's decisions on the issue.

Owner's message content (10-11-2026): "cid, name location, route filed. logon time utc". Plain ASCII, British English, no emoji, dates
DD-MM-YYYY, times UTC. Dry-run is on by default. No database is needed here.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ops" / "watchdog"))
import messages as m  # noqa: E402

spec = importlib.util.spec_from_file_location("watchdog_under_test_never_flew", REPO / "ops" / "watchdog" / "watchdog.py")
assert spec and spec.loader
wd = importlib.util.module_from_spec(spec)
sys.modules["watchdog_under_test_never_flew"] = wd
spec.loader.exec_module(wd)

FIRST: dict[str, Any] = {"cid": 1000001, "name": "Test Pilot YSSY", "kind": "first_timer", "flights_90d": 0, "logon_number": 1,
                         "callsign": "QA1", "departure": "YSSY", "arrival": "YMML", "route": "DCT WOL DCT", "lat": -33.94, "lon": 151.17,
                         "logon": "16-11-2026 10:25"}
REPEAT: dict[str, Any] = {**FIRST, "cid": 1000002, "kind": "repeat", "flights_90d": 3, "logon_number": 4}


def text(facts: dict[str, Any]) -> str:
    return m.humanise("never-flew", "warn", f"cid {facts['cid']} {facts['kind']} logon", facts=m.Checked("warn", "x", **facts).facts)


@pytest.mark.parametrize("facts", [FIRST, REPEAT])
def test_message_has_the_content_the_owner_asked_for(facts: dict[str, Any]) -> None:
    t = text(facts)
    assert str(facts["cid"]) in t and "Test Pilot YSSY" in t                 # cid and name
    assert "-33.94" in t and "151.17" in t                                  # location
    assert "YSSY to YMML" in t and "DCT WOL DCT" in t                       # route filed
    assert "16-11-2026 10:25" in t and "UTC" in t                           # logon time, UTC, DD-MM-YYYY


@pytest.mark.parametrize("facts", [FIRST, REPEAT])
def test_message_is_plain_ascii_without_emoji_and_not_too_long(facts: dict[str, Any]) -> None:
    t = text(facts)
    assert t.isascii() and len(t) < 1500


def test_first_timer_and_repeat_are_told_apart_in_plain_words() -> None:
    assert "never flown" in text(FIRST) and "1st logon" in text(FIRST)
    r = text(REPEAT)
    assert "4th logon" in r and "3 flights" in r and "never flown" not in r


@pytest.mark.parametrize("n,word", [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"), (13, "13th"),
                                    (21, "21st"), (22, "22nd"), (23, "23rd"), (101, "101st"), (111, "111th")])
def test_logon_count_is_a_correct_english_ordinal(n: int, word: str) -> None:
    assert f"{word} logon" in text({**FIRST, "logon_number": n})


def test_unknown_position_and_missing_route_do_not_break_the_message() -> None:
    t = text({**FIRST, "lat": None, "lon": None, "route": None})
    assert "YSSY to YMML" in t and "None" not in t and "nan" not in t.lower()


def test_a_very_long_route_and_name_are_clipped_and_the_message_stays_sendable() -> None:
    t = text({**FIRST, "route": "DCT " + "ABCDE " * 400, "name": "N" * 500 + " YSSY"})
    assert len(t) < 3000                                                    # Telegram's limit is 4096 characters


def test_message_goes_through_the_real_alerter_to_telegram_once() -> None:
    sent: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    cfg = wd.Config()
    cfg.tg_token, cfg.tg_chat = "123:fake", "-1"
    client = httpx.Client(transport=httpx.MockTransport(handler))
    state: dict[str, Any] = {}
    a = wd.Alerter(cfg, client, state)
    a.notify("never-flew", m.Checked("warn", "cid 1000001 first_timer logon x", **FIRST))
    assert len(sent) == 1 and sent[0]["chat_id"] == "-1"
    assert "1000001" in sent[0]["text"] and "Test Pilot YSSY" in sent[0]["text"]


def test_alert_is_a_warning_not_a_page_and_the_log_line_carries_the_cid_only() -> None:
    a = m.Alert("never-flew", "warn", "cid 1000001 first_timer logon 2026-11-16T10:25:00Z YSSY-YMML", None, None, dict(FIRST))
    assert "1000001" in a.technical() and "Test Pilot" not in a.technical()   # personal data: the log keeps the CID, not the name


def test_settings_defaults_dry_run_on_and_24_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("WATCHDOG_NEVER_FLEW_DRY_RUN", "WATCHDOG_NEVER_FLEW_REMEMBER_HOURS"):
        monkeypatch.delenv(k, raising=False)
    c = wd.Config()
    assert c.never_flew_dry_run is True and c.never_flew_remember_hours == 24


@pytest.mark.parametrize("raw,dry", [("1", True), ("true", True), ("TRUE", True), (" yes ", True), ("on", True),
                                     ("0", False), ("false", False), ("no", False), ("off", False), (" OFF ", False),
                                     ("y", True), ("ture", True), ("enabled", True), ("", True)])
def test_dry_run_switch_values(monkeypatch: pytest.MonkeyPatch, raw: str, dry: bool) -> None:
    monkeypatch.setenv("WATCHDOG_NEVER_FLEW_DRY_RUN", raw)
    assert wd.Config().never_flew_dry_run is dry


def test_remember_hours_is_read_from_the_environment_and_never_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_NEVER_FLEW_REMEMBER_HOURS", "48")
    assert wd.Config().never_flew_remember_hours == 48
    monkeypatch.setenv("WATCHDOG_NEVER_FLEW_REMEMBER_HOURS", "-5")
    assert wd.Config().never_flew_remember_hours >= 0


def test_settings_reach_the_watchdog_container_and_the_compose_default_is_live() -> None:
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    env = (REPO / ".env.example").read_text(encoding="utf-8")
    assert "WATCHDOG_NEVER_FLEW_DRY_RUN: ${WATCHDOG_NEVER_FLEW_DRY_RUN:-0}" in compose      # live unless .env sets dry-run (1)
    assert "WATCHDOG_NEVER_FLEW_DRY_RUN=1" in env                                            # the example file ships with it on
    assert "WATCHDOG_NEVER_FLEW_REMEMBER_HOURS: 24" in compose
