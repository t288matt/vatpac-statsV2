"""Issue 199: characterisation of every Telegram message (and log line) the watchdog's Alerter produces.

The expected texts are in tests/fixtures/watchdog_alert_golden.json, captured from the code as it was before the alert path
stopped serialising results to text and parsing them back. Each case goes through the real Alerter into a fake Telegram, with a
frozen clock, so the golden file holds exactly what the owner would have received and what the log would have said.

Only `raise_alert`, `raise_notice`, `raise_unknown_callsign`, `raise_restarted` and `raise_unrecognised` know how a case is handed to the
Alerter; the cases and the expected texts do not change when the hand-over does.

To rewrite the golden file after a deliberate wording change: WRITE_GOLDEN=1 python -m pytest tests/test_watchdog_alert_wording.py
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ops" / "watchdog"))
sys.path.insert(0, str(REPO / "ops" / "audit"))
import integrity  # noqa: E402
import messages as m  # noqa: E402

spec = importlib.util.spec_from_file_location("watchdog_under_test_wording", REPO / "ops" / "watchdog" / "watchdog.py")
assert spec and spec.loader
wd = importlib.util.module_from_spec(spec)
sys.modules["watchdog_under_test_wording"] = wd
spec.loader.exec_module(wd)

GOLDEN = REPO / "tests" / "fixtures" / "watchdog_alert_golden.json"
T0 = 1_800_000_000.0
NAMES = integrity.invariant_names((REPO / "ops" / "audit" / "invariants.sql").read_text(encoding="utf-8"))
GAPS, PARTS, QUIET, STALE = NAMES[5], NAMES[17], NAMES[12], NAMES[6]
TOKEN = "flight_quiet_hours=6.75 flight_quiet_check_grace_minutes=0"


def _sweep(*names: str, total: int | None = None, quiet: bool = False) -> tuple[str, dict[str, Any]]:
    """The detail the real sweep writes for these failing invariants, and the facts that go with it."""
    findings = [integrity.Finding(n, 1, TOKEN if (quiet and n == QUIET) else None) for n in names]
    _, detail = integrity.summarise_invariants(findings, [], total or len(NAMES))
    return detail, {}


def _integ(*names: str, quiet: bool = False) -> tuple[str, dict[str, Any]]:
    detail, _ = _sweep(*names, quiet=quiet)
    facts: dict[str, Any] = {"state": "violations", "names": list(names[:6]), "failing": len(names), "of": len(NAMES),
                             "quiet": ["6.75", "0"] if quiet else None}
    return detail, facts


_I1, _F1 = _integ(GAPS)
_I2, _F2 = _integ(PARTS, GAPS)
_I3, _F3 = _integ(GAPS, NAMES[9], NAMES[0], NAMES[1], NAMES[2], NAMES[3], NAMES[4], NAMES[7])
_I4, _F4 = _integ(QUIET, quiet=True)
_I5, _F5 = _integ(QUIET)
_I6, _F6 = _integ(NAMES[9], NAMES[0])
_PACK = "invariant pack incomplete"

# (check, level, the detail the check writes, the key numbers a check hands over alongside it)
CASES: list[tuple[str, str, str, dict[str, Any]]] = [
    ("collector_health", "crit", "unreachable (ConnectError)", {}),
    ("collector_health", "crit", "HTTP 503", {}),
    ("poll_log_ok_age", "warn", "newest ok poll is 75s old", {"age_s": 75}),
    ("poll_log_ok_age", "crit", "newest ok poll is 312s old", {"age_s": 312}),
    ("poll_log_ok_age", "crit", "newest ok poll is 7300s old", {"age_s": 7300}),
    ("poll_log_ok_age", "warn", "no newest ok poll yet", {"missing": True}),
    ("poll_log_ok_age", "warn", "something the check never writes", {}),
    ("network_snapshot_age", "warn", "newest snapshot is 90s old", {"age_s": 90}),
    ("network_snapshot_age", "crit", "newest snapshot is 400s old", {"age_s": 400}),
    ("network_snapshot_age", "crit", "no newest snapshot yet", {"missing": True}),
    ("network_snapshot_age", "warn", "gibberish", {}),
    ("grafana_health", "crit", "unreachable (ConnectError)", {}),
    ("database", "crit", "query/connect failed (OperationalError)", {}),
    ("db_size", "warn", "21.40 GB (limit 20)", {"gb": 21.4}),
    ("db_size", "warn", "25.00 GB (limit 20)", {"gb": 25.0}),
    ("db_size", "warn", "something else", {}),
    ("disk", "warn", "/backups 82% used, / 50% used", {"disks": [["/backups", 82], ["/", 50]]}),
    ("disk", "warn", "/ 82% used", {"disks": [["/", 82]]}),
    ("disk", "crit", "/ 95% used", {"disks": [["/", 95]]}),
    ("disk", "crit", "/ 95% used, /backups 99% used", {"disks": [["/", 95], ["/backups", 99]]}),
    ("disk", "warn", "/backups unreadable", {"disks": []}),
    ("disk", "warn", "/backups unreadable, / 81% used", {"disks": [["/", 81]]}),
    ("backup_age", "crit", "newest backup 27.3 h old", {"age_h": 27.3}),
    ("backup_age", "crit", "newest backup 30.0 h old", {"age_h": 30.0}),
    ("backup_age", "crit", "no backup yet (30.0 h since watchdog first ran)", {"none_yet": True}),
    ("backup_age", "crit", "odd", {}),
    ("backup_remote", "warn", "last off-box copy 30.1 h ago (limit 26)", {}),
    ("backup_remote", "warn", "no off-box copy recorded after 27.0 h since watchdog first ran", {}),
    ("telegram_outbox", "warn", "3 undelivered telegram message(s), oldest 12 min old", {"refused": False}),
    ("telegram_outbox", "warn", "Telegram refuses the messages (HTTP 400): check TELEGRAM_CHAT_ID and the bot token; 2 undelivered "
                                "telegram message(s), oldest 3 min old", {"refused": True}),
    ("restore_test", "crit", "last restore test FAILED 2.1 days ago", {"state": "failed"}),
    ("restore_test", "warn", "last restore test restored fine but the data in the dump was stale (3.0 days ago)", {"state": "stale"}),
    ("restore_test", "warn", "last restore test result unreadable", {"state": "unreadable"}),
    ("restore_test", "warn", "last successful restore test 10.0 days ago (limit 9)", {"state": "overdue"}),
    ("restore_test", "warn", "no restore test result after 10.0 days", {"state": "overdue"}),
    ("identity_flap", "warn", "1 pilot and 0 controller session pairs (same cid and callsign, logon_time within 2 s), first seen "
                              "09-10-2026 12:00:03 UTC; run ops/audit/identity_flap.sql", {"query_failed": False}),
    ("identity_flap", "warn", "identity-flap query failed (OperationalError)", {"query_failed": True}),
    ("feed_archive", "warn", "last write 601s ago (limit 600s), 4 write errors, 12 MB", {}),
    ("data_integrity", "warn", _I1, _F1),
    ("data_integrity", "warn", _I2, _F2),
    ("data_integrity", "warn", _I3, _F3),
    ("data_integrity", "warn", _I4, _F4),
    ("data_integrity", "warn", _I5, _F5),
    ("data_integrity", "warn", _I6, _F6),
    ("data_integrity", "warn", "invariant sweep could not run (InsufficientPrivilege); retrying in 1 h", {"state": "incomplete"}),
    ("data_integrity", "warn", "1 invariant(s) did not report: x. Details are in the watchdog log", {"state": "incomplete"}),
    ("data_integrity", "warn", "mystery", {}),
    ("feed_crosscheck", "warn", "1 difference(s) in 24 archived snapshots: 09-10-2026 12:00:03 UTC: pilots_in_region independent 41, stored 40",
     {"state": "differences", "n": 1}),
    ("feed_crosscheck", "warn", "3 difference(s) in 24 archived snapshots: a; b; c. Details are in the watchdog log",
     {"state": "differences", "n": 3}),
    ("feed_crosscheck", "warn", "cross-check had nothing to compare (no archive samples found); is the feed archive enabled",
     {"state": "nothing"}),
    ("feed_crosscheck", "warn", "archive cross-check could not run (OperationalError); retrying in 1 h", {"state": "could_not_run"}),
    ("feed_crosscheck", "warn", "odd", {}),
    ("container:collector", "crit", "state exited", {"missing": False}),
    ("container:collector", "crit", "unhealthy", {"missing": False}),
    ("container:db", "crit", "missing", {"missing": True}),
    ("container:migrate", "crit", "exited exit=1", {"missing": False}),
    ("container:migrate", "crit", "missing", {"missing": True}),
    ("container:grafana", "warn", "restarted by Docker (count 2)", {"missing": False}),
    ("docker_proxy", "crit", "unreachable (ConnectError)", {}),
    ("restart-cap:db", "crit", "restart cap 3/hour reached; not restarting (state exited)", {}),
    ("restart-cap:db", "warn", "x", {}),
    ("mystery_check", "warn", "something odd", {}),
    ("mystery_check", "crit", "something odd", {}),
]
RECOVERED_NAMES = sorted({c[0] for c in CASES} | {"container:other"})
UNKNOWN_CALLSIGN = "YSSY_X_TWR, YMML_N_APP"
RESTARTED = ("collector", "1/3 this hour, ok=True", "collector HTTP 503 and no failures counted")
NOTICE_DETAIL = f"new violations since the last sweep: {NAMES[9]}, {STALE}"
NOTICE_FACTS: dict[str, Any] = {"state": "new", "names": [NAMES[9], STALE], "quiet": None}
NOTICE_QUIET_DETAIL = f"new violations since the last sweep: {QUIET} [{TOKEN}]"
NOTICE_QUIET_FACTS: dict[str, Any] = {"state": "new", "names": [QUIET], "quiet": ["6.75", "0"]}


class Frozen(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> Frozen:  # type: ignore[override]
        return cls.fromtimestamp(T0, tz)


class World:
    """An Alerter wired to a fake Telegram, with the clock frozen at T0 and the log lines collected."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, state: dict[str, Any]) -> None:
        monkeypatch.setattr(wd.time, "time", lambda: T0)
        monkeypatch.setattr(m, "datetime", Frozen)
        self.sent: list[str] = []
        self.caplog = caplog

        def handler(r: httpx.Request) -> httpx.Response:
            self.sent.append(json.loads(r.content)["text"])
            return httpx.Response(200)

        cfg = type("C", (), {"confirm": 1, "tg_token": "123:T", "tg_chat": "42"})()
        self.alerter = wd.Alerter(cfg, httpx.Client(transport=httpx.MockTransport(handler)), state)  # type: ignore[arg-type]

    def result(self) -> dict[str, Any]:
        assert len(self.sent) == 1, self.sent
        lines = [r.getMessage() for r in self.caplog.records if r.name == "watchdog" and r.getMessage().startswith("ALERT ")]
        return {"log": lines, "telegram": self.sent[0]}


# ---- the only functions that know how a case reaches the Alerter ------------------------------------------------------
def raise_alert(a: Any, name: str, level: str, detail: str, facts: dict[str, Any]) -> None:
    a.update(name, level, detail, facts)


def raise_notice(a: Any, detail: str, facts: dict[str, Any]) -> None:
    a.notify("data_integrity", m.Checked("warn", detail, **facts))


def raise_unknown_callsign(a: Any, callsigns: str) -> None:
    a.notify("unknown-callsign", ("warn", callsigns))


def raise_restarted(a: Any, svc: str, counts: str, why: str) -> None:
    a.notify(f"restarted:{svc}", m.Checked("warn", f"{counts}: {why}", counts=counts, why=why))


def raise_unrecognised(a: Any, text: str) -> None:
    a.send(text)


# ---- the scenarios ----------------------------------------------------------------------------------------------------
def scenarios() -> dict[str, Any]:
    """id -> a function (World) -> None that raises exactly one alert."""
    out: dict[str, Any] = {}
    for i, (name, level, detail, facts) in enumerate(CASES):
        out[f"alert:{i:02d}:{name}:{level}"] = (
            {}, lambda w, n=name, lv=level, d=detail, f=facts: raise_alert(w.alerter, n, lv, d, f))
        if level == "warn":
            out[f"stepdown:{i:02d}:{name}"] = (
                {"alerts": {name: "crit"}, "bad_since": {name: T0 - 90}},
                lambda w, n=name, lv=level, d=detail, f=facts: raise_alert(w.alerter, n, lv, d, f))
    for name in RECOVERED_NAMES:
        out[f"recovered_with_duration:{name}"] = (
            {"alerts": {name: "crit"}, "bad_since": {name: T0 - 250}}, lambda w, n=name: raise_alert(w.alerter, n, "ok", "fine now", {}))
        out[f"recovered_without_duration:{name}"] = (
            {"alerts": {name: "warn"}}, lambda w, n=name: raise_alert(w.alerter, n, "ok", "fine now", {}))
    out["recovered_long:disk"] = (
        {"alerts": {"disk": "warn"}, "bad_since": {"disk": T0 - 3 * 86400}}, lambda w: raise_alert(w.alerter, "disk", "ok", "/ 40% used", {}))
    out["recovered_hours:data_integrity"] = (
        {"alerts": {"data_integrity": "warn"}, "bad_since": {"data_integrity": T0 - 5 * 3600}},
        lambda w: raise_alert(w.alerter, "data_integrity", "ok", f"all {len(NAMES)} invariants hold (checked 09-10-2026 12:00 UTC)", {}))
    out["unknown_callsign"] = ({}, lambda w: raise_unknown_callsign(w.alerter, UNKNOWN_CALLSIGN))
    out["unknown_callsign_single"] = ({}, lambda w: raise_unknown_callsign(w.alerter, "X_TWR"))
    out["restarted"] = ({}, lambda w: raise_restarted(w.alerter, *RESTARTED))
    out["notice_two_names"] = ({}, lambda w: raise_notice(w.alerter, NOTICE_DETAIL, NOTICE_FACTS))
    out["notice_quiet_period"] = ({}, lambda w: raise_notice(w.alerter, NOTICE_QUIET_DETAIL, NOTICE_QUIET_FACTS))
    out["unrecognised_text"] = ({}, lambda w: raise_unrecognised(w.alerter, "something nobody planned"))
    return out


SCENARIOS = scenarios()
STEPDOWN_DISK = next(k for k in SCENARIOS if k.startswith("stepdown:") and k.endswith(":disk"))


def run(sid: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    state, go = SCENARIOS[sid]
    w = World(monkeypatch, caplog, json.loads(json.dumps(state)))
    with caplog.at_level(logging.INFO, logger="watchdog"):
        go(w)
    return w.result()


def test_write_golden(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    if not os.environ.get("WRITE_GOLDEN"):
        pytest.skip("set WRITE_GOLDEN=1 to rewrite tests/fixtures/watchdog_alert_golden.json")
    got = {}
    for sid in SCENARIOS:
        caplog.clear()
        got[sid] = run(sid, monkeypatch, caplog)
    GOLDEN.write_text(json.dumps(got, indent=1, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")


EXPECTED: dict[str, Any] = json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}


def test_the_golden_file_covers_exactly_the_scenarios() -> None:
    assert sorted(EXPECTED) == sorted(SCENARIOS)


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_the_message_and_the_log_line_are_exactly_the_golden_text(sid: str, monkeypatch: pytest.MonkeyPatch,
                                                                  caplog: pytest.LogCaptureFixture) -> None:
    assert run(sid, monkeypatch, caplog) == EXPECTED[sid]


def test_a_formatting_bug_sends_the_technical_text_for_every_alert_type(monkeypatch: pytest.MonkeyPatch,
                                                                         caplog: pytest.LogCaptureFixture) -> None:
    """The fallback path: whatever breaks inside the wording code, the owner gets 'vatpac-stats: <technical text>'."""
    def boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("bug")

    monkeypatch.setattr(m, "humanise", boom)
    got = {}
    for sid in ("alert:03:poll_log_ok_age:crit", STEPDOWN_DISK, "recovered_with_duration:disk", "unknown_callsign", "restarted",
               "notice_two_names"):
        caplog.clear()
        got[sid] = run(sid, monkeypatch, caplog)["telegram"]
    assert got == {
        "alert:03:poll_log_ok_age:crit": "vatpac-stats: CRIT poll_log_ok_age: newest ok poll is 312s old",
        STEPDOWN_DISK: "vatpac-stats: WARN disk: /backups 82% used, / 50% used",
        "recovered_with_duration:disk": "vatpac-stats: RECOVERED disk (was crit): fine now",
        "unknown_callsign": f"vatpac-stats: UNKNOWN CALLSIGN not on whitelist: {UNKNOWN_CALLSIGN}",
        "restarted": "vatpac-stats: RESTARTED collector (1/3 this hour, ok=True): collector HTTP 503 and no failures counted",
        "notice_two_names": f"vatpac-stats: WARN data_integrity: {NOTICE_DETAIL}",
    }


def test_an_alert_is_not_sent_when_telegram_is_not_configured(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    w = World(monkeypatch, caplog, {})
    w.alerter.cfg = type("C", (), {"confirm": 1, "tg_token": "", "tg_chat": ""})()
    with caplog.at_level(logging.INFO, logger="watchdog"):
        raise_alert(w.alerter, "disk", "warn", "/ 85% used", {})
    assert w.sent == [] and "ALERT WARN disk: / 85% used" in caplog.text
