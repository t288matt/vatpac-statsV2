"""Issue 199: the Telegram wording is built from the values a check hands over, never from its detail text.

Three things are shown here (the exact old and new texts are pinned by test_watchdog_alert_wording.py):
1. the real check functions produce the same facts that table feeds the wording (so the table is not invented);
2. rewording any check's detail text changes nothing in the plain sentence;
3. the scheduled checks keep their facts in the status file and read them back, and an older status file without facts is safe.
"""
from __future__ import annotations

import json
import time as _time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from test_watchdog_alert_wording import CASES, GAPS, NAMES, TOKEN, integrity, m, wd

NOW = datetime(2026, 10, 9, 14, 5, tzinfo=UTC)
BY_DETAIL = {(n, d): (lv, f) for n, lv, d, f in CASES}


def check(name: str, result: tuple[str, str], same_level: bool = True) -> None:
    """The real check's result must be exactly a row of the wording table: same level, same detail, same facts. (The table also has
    rows at a level the check does not use for that detail, to pin the wording of both levels: same_level=False for those.)"""
    level, detail = result
    assert (name, detail) in BY_DETAIL, (name, detail)
    want_level, want_facts = BY_DETAIL[(name, detail)]
    assert (level == want_level or not same_level) and m.facts_of(result) == want_facts


class Cfg:
    page_s, warn_s = 300, 60


def test_age_checks_hand_over_the_age() -> None:
    check("poll_log_ok_age", wd.age_level(75.2, Cfg(), "newest ok poll"))  # type: ignore[arg-type]
    check("poll_log_ok_age", wd.age_level(312.0, Cfg(), "newest ok poll"))  # type: ignore[arg-type]
    check("poll_log_ok_age", wd.age_level(None, Cfg(), "newest ok poll"))  # type: ignore[arg-type]
    check("network_snapshot_age", wd.age_level(400.0, Cfg(), "newest snapshot"))  # type: ignore[arg-type]
    check("network_snapshot_age", wd.age_level(None, Cfg(), "newest snapshot"), same_level=False)  # type: ignore[arg-type]


def test_db_size_hands_over_the_size() -> None:
    check("db_size", wd.db_size_level(21.4, 20))
    check("db_size", wd.db_size_level(25.0, 20))


def test_disk_hands_over_each_readable_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Usage:
        def __init__(self, pct: int) -> None:
            self.used, self.total = pct, 100

    pcts = {"/backups": 82, "/": 50}
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    names = {str(a): "/backups", str(b): "/"}
    monkeypatch.setattr(wd.shutil, "disk_usage", lambda p: Usage(pcts[names[str(p)]]))
    devs = {str(a): 1, str(b): 2}
    monkeypatch.setattr(wd.os, "stat", lambda p: type("S", (), {"st_dev": devs[str(p)]})())
    cfg = type("C", (), {"disk_paths": [a, b], "disk_pct": 80})()
    res = wd.check_disk(cfg)  # type: ignore[arg-type]
    assert res[0] == "warn" and m.facts_of(res) == {"disks": [[str(a), 82], [str(b), 50]]}
    # the same numbers, under the names the wording table uses
    assert m.humanise("disk", "warn", res[1], facts={"disks": [["/backups", 82], ["/", 50]]}, now=NOW).splitlines()[0] == \
        m.humanise("disk", "warn", "/backups 82% used, / 50% used", facts=BY_DETAIL[("disk", "/backups 82% used, / 50% used")][1], now=NOW).splitlines()[0]


def test_backup_hands_over_its_age(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = type("C", (), {"backup_dir": tmp_path, "backup_max_h": 26})()
    now = _time.time()
    res = wd.check_backup(cfg, now - 30 * 3600)  # type: ignore[arg-type]
    assert res[0] == "crit" and m.facts_of(res) == {"none_yet": True}
    dump = tmp_path / "x.dump"
    dump.write_text("x")
    import os
    os.utime(dump, (now - 27.3 * 3600, now - 27.3 * 3600))
    res = wd.check_backup(cfg, now)  # type: ignore[arg-type]
    assert res[0] == "crit" and m.facts_of(res) == {"age_h": 27.3}


def test_restore_test_hands_over_what_went_wrong(tmp_path: Path) -> None:
    cfg = type("C", (), {"backup_dir": tmp_path, "restore_test_max_days": 9})()
    now = _time.time()
    stamp = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 3600))
    old = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(now - 10 * 86400))
    marker = tmp_path / ".last_restore_test"
    for text, state in ((f"{stamp} FAIL x", "failed"), (f"{stamp} WARN x", "stale"), ("garbage", "unreadable"), (f"{old} OK x", "overdue")):
        marker.write_text(text)
        res = wd.check_restore_test(cfg, now)  # type: ignore[arg-type]
        assert m.facts_of(res) == {"state": state}, (text, res)
    marker.unlink()
    assert m.facts_of(wd.check_restore_test(cfg, now - 10 * 86400 - 60)) == {"state": "overdue"}  # type: ignore[arg-type]


def test_outbox_says_whether_telegram_refuses() -> None:
    cfg = type("C", (), {"tg_token": "1:T", "tg_chat": "9"})()
    waiting = {"outbox": [{"at": 1000.0, "body": "x"}]}
    assert m.facts_of(wd.check_telegram_outbox(cfg, waiting, 1000.0 + 3600)) == {"refused": False}  # type: ignore[arg-type]
    refused = {**waiting, "outbox_refused": {"status": 400}}
    res = wd.check_telegram_outbox(cfg, refused, 1000.0 + 60)  # type: ignore[arg-type]
    assert res[0] == "warn" and m.facts_of(res) == {"refused": True}


def test_identity_flap_says_whether_the_query_failed() -> None:
    check("identity_flap", wd.flap_level(1, 0, datetime(2026, 10, 9, 12, 0, 3, tzinfo=UTC)))

    class Boom:
        def execute(self, *a: Any) -> None:
            raise wd.psycopg.OperationalError("x")

    check("identity_flap", wd.check_identity_flap(Boom()))


def test_a_missing_container_is_flagged() -> None:
    cfg = type("C", (), {"expected": {"db"}})()
    out = wd.check_containers(cfg, {}, {})  # type: ignore[arg-type]
    check("container:db", out["container:db"])
    assert m.facts_of(out["container:migrate"]) == {"missing": True}


def test_the_scheduled_checks_hand_over_their_facts() -> None:
    res = integrity.summarise_invariants([integrity.Finding(GAPS, 1)], [], len(NAMES))
    assert m.facts_of(res) == BY_DETAIL[("data_integrity", res[1])][1]
    assert m.facts_of(integrity.summarise_invariants([], ["1 invariant(s) did not report: x"], 20))["state"] == "incomplete"
    one = integrity.summarise_crosscheck(integrity.CrossResult(compared=24, mismatches=["a"]))
    assert (one[0], m.facts_of(one)) == ("warn", {"state": "differences", "n": 1})
    none = integrity.summarise_crosscheck(integrity.CrossResult())
    assert m.facts_of(none) == {"state": "nothing"}


# ---------------------------------------------------------------- rewording a detail cannot change the plain sentence

@pytest.mark.parametrize("name,level,detail,facts", CASES)
def test_rewording_the_detail_does_not_change_the_plain_sentence(name: str, level: str, detail: str, facts: dict[str, Any]) -> None:
    """The test that fails if the wording goes back to reading the detail text: a check's detail is free text for people and the
    log, so a different sentence there must leave the Telegram sentence alone (only the Technical line shows the detail)."""
    same = m.humanise(name, level, detail, facts=facts, now=NOW)
    worded = m.humanise(name, level, "a completely different way of saying it", facts=facts, now=NOW)
    assert worded.splitlines()[0] == same.splitlines()[0]
    assert "a completely different way of saying it" in worded.splitlines()[-1] or name == "data_integrity"  # the log text is shown as given


def test_a_reworded_check_detail_reaches_telegram_unchanged_in_meaning(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through the real age check and the Alerter: the same five-minute age under two different detail sentences."""
    texts: list[str] = []
    monkeypatch.setattr(m, "datetime", type("F", (datetime,), {"now": classmethod(lambda cls, tz=None: NOW)}))
    for what in ("newest ok poll", "last successful VATSIM fetch"):
        res = wd.age_level(312.0, Cfg(), what)  # type: ignore[arg-type]
        cfg = type("C", (), {"confirm": 1, "tg_token": "", "tg_chat": ""})()
        a = wd.Alerter(cfg, None, {})  # type: ignore[arg-type]
        a.send = lambda text, alert=None: texts.append(m.telegram_text(text, alert))  # type: ignore[method-assign]
        a.update("poll_log_ok_age", res[0], res[1], m.facts_of(res))
    assert texts[0].splitlines()[0] == texts[1].splitlines()[0] and "about 5 minutes" in texts[0]
    assert texts[0].splitlines()[1] != texts[1].splitlines()[1]


def test_a_result_without_facts_gets_the_vague_wording_not_an_error() -> None:
    t = m.humanise("poll_log_ok_age", "crit", "newest ok poll is 312s old", now=NOW)
    assert "No fresh VATSIM data for a while." in t
    assert m.humanise("data_integrity", "warn", "anything", now=NOW).splitlines()[0].endswith("for what to do.")


def test_nothing_parses_the_alert_text_back() -> None:
    assert not hasattr(m, "from_alert_text") and not hasattr(m, "_num")
    cfg = type("C", (), {"confirm": 1, "tg_token": "", "tg_chat": ""})()
    assert not hasattr(wd.Alerter(cfg, None, {}), "_context")  # type: ignore[arg-type]


# ---------------------------------------------------------------- the status file keeps the facts of the scheduled checks

def test_the_facts_survive_the_status_file_and_an_older_file_without_them_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = integrity.Plan(dsn="", sql_path=Path("x"), archive_dir=Path("x"), config_dir=Path("x"), crosscheck_interval_s=0)
    finding = integrity.Finding(NAMES[13], 1, TOKEN)
    result = integrity.summarise_invariants([finding], [], len(NAMES))
    monkeypatch.setattr(integrity, "sweep", lambda p: (result, [NAMES[13]], 6 * 3600.0))
    state: dict[str, Any] = {}
    first, _ = integrity.run_due(plan, state, now=0.0)
    saved = json.loads(json.dumps(state))                       # what the status file keeps across a restart
    again, _ = integrity.run_due(plan, saved, now=60.0)         # not due: read back from the file
    assert m.facts_of(again["data_integrity"]) == m.facts_of(first["data_integrity"]) == m.facts_of(result)
    assert m.facts_of(result)["quiet"] == ["6.75", "0"]
    del saved["integrity"]["data_integrity"]["facts"]           # a file written before this change
    old, _ = integrity.run_due(plan, saved, now=120.0)
    assert old["data_integrity"][0] == "warn" and m.facts_of(old["data_integrity"]) == {}
    text = m.humanise("data_integrity", "warn", old["data_integrity"][1], facts=m.facts_of(old["data_integrity"]), now=NOW)
    assert text.startswith("VATPAC Stats: heads up (14:05 UTC). A routine data check found a problem. See ")


def test_a_new_violation_notice_carries_the_quiet_settings_from_the_stored_result(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = integrity.Plan(dsn="", sql_path=Path("x"), archive_dir=Path("x"), config_dir=Path("x"), crosscheck_interval_s=0)
    one = integrity.summarise_invariants([integrity.Finding(GAPS, 1)], [], len(NAMES))
    two = integrity.summarise_invariants([integrity.Finding(GAPS, 1), integrity.Finding(NAMES[13], 1, TOKEN)], [], len(NAMES))
    box = {"r": (one, [GAPS])}
    monkeypatch.setattr(integrity, "sweep", lambda p: (box["r"][0], box["r"][1], 6 * 3600.0))
    state: dict[str, Any] = {}
    integrity.run_due(plan, state, now=0.0)
    box["r"] = (two, [GAPS, NAMES[13]])
    _, notices = integrity.run_due(plan, state, now=6 * 3600.0)
    assert len(notices) == 1 and m.facts_of(notices[0]) == {"state": "new", "names": [NAMES[13]], "quiet": ["6.75", "0"]}
    assert notices[0][1] == f"new violations since the last sweep: {NAMES[13]} [{TOKEN}]"
