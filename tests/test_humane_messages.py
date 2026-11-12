"""Issue 79: the watchdog's Telegram messages are plain language, with one last 'Technical:' line (ops/watchdog/messages.py).

Pure text tests: no network, no database. The alert logic itself is tested in test_watchdog_decisions.py."""
from __future__ import annotations

import ast
import importlib.util
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ops" / "watchdog"))
sys.path.insert(0, str(REPO / "ops" / "audit"))
import integrity  # noqa: E402
import messages as m  # noqa: E402

spec = importlib.util.spec_from_file_location("watchdog_under_test_humane", REPO / "ops" / "watchdog" / "watchdog.py")
assert spec and spec.loader
wd = importlib.util.module_from_spec(spec)
sys.modules["watchdog_under_test_humane"] = wd
spec.loader.exec_module(wd)

NOW = datetime(2026, 10, 9, 14, 5, tzinfo=UTC)
SQL = (REPO / "ops" / "audit" / "invariants.sql").read_text(encoding="utf-8")
NAMES = integrity.invariant_names(SQL)

# (check, level, a realistic technical detail, text the plain sentence must contain)
CASES: list[tuple[str, str, str, str]] = [
    ("collector_health", "crit", "unreachable (ConnectError)", "collector is not answering"),
    ("collector_health", "crit", "HTTP 503", "collector is not answering"),
    ("poll_log_ok_age", "warn", "newest ok poll is 75s old", "about 1 minute"),
    ("poll_log_ok_age", "crit", "newest ok poll is 312s old", "about 5 minutes"),
    ("poll_log_ok_age", "warn", "no newest ok poll yet", "No successful"),
    ("network_snapshot_age", "warn", "newest snapshot is 90s old", "stored picture"),
    ("network_snapshot_age", "crit", "newest snapshot is 400s old", "feed may have frozen"),
    ("grafana_health", "crit", "unreachable (ConnectError)", "dashboards"),
    ("database", "crit", "query/connect failed (OperationalError)", "cannot read the database"),
    ("db_size", "warn", "21.40 GB (limit 20)", "21.4 GB"),
    ("disk", "warn", "/backups 82% used, / 50% used", "82% full"),
    ("disk", "warn", "/backups 82% used, / 50% used", "(/backups)"),
    ("disk", "crit", "/ 95% used", "95% full"),
    ("disk", "warn", "/backups unreadable", "could not be read"),
    ("backup_age", "crit", "newest backup 27.3 h old", "27 hours old"),
    ("backup_age", "crit", "no backup yet (30.0 h since watchdog first ran)", "No database backup"),
    ("backup_remote", "warn", "last off-box copy 30.1 h ago (limit 26)", "second location"),
    ("backup_remote", "warn", "no off-box copy recorded after 27.0 h since watchdog first ran", "second location"),
    ("restore_test", "crit", "last restore test FAILED 2.1 days ago", "has failed"),
    ("restore_test", "warn", "last successful restore test 10.0 days ago (limit 9)", "overdue"),
    ("restore_test", "warn", "no restore test result after 10.0 days", "overdue"),
    ("restore_test", "warn", "last restore test result unreadable", "cannot be read"),
    ("identity_flap", "warn", "1 pilot and 0 controller session pairs (same cid and callsign)", "logon times"),
    ("identity_flap", "warn", "identity-flap query failed (OperationalError)", "could not run"),
    ("feed_archive", "warn", "last write 601s ago (limit 600s), 4 write errors, 12 MB", "safety copy"),
    ("data_integrity", "warn",
     "1 of 18 invariants report violations: snapshot gaps over 5 minutes (last 3 days) (1). Details are in the watchdog log",
     "more than 5 minutes"),
    ("data_integrity", "warn", "invariant sweep could not run (InsufficientPrivilege); retrying in 1 h", "could not run properly"),
    ("data_integrity", "warn", "new violations since the last sweep: identity duplicates (cid, callsign, logon_time)", "stored twice"),
    ("feed_crosscheck", "warn", "1 difference(s) in 24 archived snapshots: 09-10-2026 12:00:03 UTC: pilots_in_region independent 41, stored 40",
     "1 difference"),
    ("feed_crosscheck", "warn", "cross-check had nothing to compare (no archive samples found); is the feed archive enabled", "nothing to compare"),
    ("container:collector", "crit", "state exited", "collector container"),
    ("container:db", "crit", "missing", "missing"),
    ("container:migrate", "crit", "state exited", "one-off job"),
    ("container:grafana", "warn", "restarted by Docker (count 2)", "restarted the grafana container by itself"),
    ("docker_proxy", "crit", "unreachable (ConnectError)", "reach Docker"),
    ("restart-cap:db", "crit", "restart cap 3/hour reached; not restarting (state exited)", "hourly limit"),
    ("mystery_check", "warn", "something odd", "mystery_check"),
    ("mystery_check", "crit", "something odd", "mystery_check"),
]
OK_CHECKS = sorted({c for c, *_ in CASES})


def check_shape(text: str, name: str) -> None:
    assert text.isascii(), text
    assert text.startswith(m.PREFIX + " "), text
    assert len(text) <= 520, (len(text), text)
    last = text.splitlines()[-1]
    assert last.startswith("Technical: ") and name in last, text
    assert len(text.splitlines()) == 2, text


@pytest.mark.parametrize("name,level,detail,plain", CASES)
def test_every_check_and_level_has_a_plain_message(name: str, level: str, detail: str, plain: str) -> None:
    text = m.humanise(name, level, detail, now=NOW)
    check_shape(text, name)
    assert text.splitlines()[0].startswith(f"{m.PREFIX} {m.LABELS[level]} (14:05 UTC). ")
    assert plain in text.splitlines()[0], text


@pytest.mark.parametrize("name", OK_CHECKS)
def test_recovered_with_and_without_a_duration(name: str) -> None:
    without = m.humanise(name, "ok", "fine now", "warn", None, now=NOW)
    check_shape(without, name)
    assert without.startswith(f"{m.PREFIX} all clear") and "active for" not in without  # no duration is ever invented
    with_dur = m.humanise(name, "ok", "fine now", "crit", 4 * 60 + 10, now=NOW)
    check_shape(with_dur, name)
    assert "The alert was active for about 4 minutes." in with_dur and "problem lasted" not in with_dur and "(was crit)" in with_dur


@pytest.mark.parametrize("secs,words", [(5, "under a minute"), (60, "about 1 minute"), (250, "about 4 minutes"),
                                        (3600, "about 60 minutes"), (2 * 3600, "about 2 hours"), (3 * 86400, "about 3 days")])
def test_duration_words(secs: float, words: str) -> None:
    assert m.duration_words(secs) == words


def test_the_examples_in_the_brief() -> None:
    t = m.humanise("poll_log_ok_age", "crit", "newest ok poll is 312s old", now=NOW)
    assert t.splitlines()[0] == ("VATPAC Stats: problem (14:05 UTC). No fresh VATSIM data for about 5 minutes. The system keeps "
                                 "retrying; if this lasts, the collector may be stuck (see docs/RUNBOOKS/stuck-collector.md on the VM).")
    assert t.splitlines()[1] == "Technical: poll_log_ok_age, newest ok poll is 312s old"
    d = m.humanise("disk", "warn", "/ 82% used", now=NOW)
    assert "A disk on the VM (/) is 82% full. Nothing breaks yet, but it needs clearing within days" in d


def test_a_step_down_from_crit_to_warn_says_so() -> None:
    t = m.humanise("disk", "warn", "/ 85% used", previous_level="crit", now=NOW)
    assert "eased from a problem to a warning" in t


def test_near_miss_and_restarted() -> None:
    t = m.from_alert_text("NEAR-MISS controller callsign(s) not on whitelist: YSSY_X_TWR, YMML_N_APP", now=NOW)
    assert "not on the whitelist" in t and "add it to the whitelist" in t
    assert t.splitlines()[-1] == "Technical: near-miss YSSY_X_TWR, YMML_N_APP"
    assert t.isascii() and t.startswith(m.PREFIX)
    r = m.from_alert_text("RESTARTED collector (1/3 this hour, ok=True): collector HTTP 503 and no failures counted", now=NOW)
    assert "restarted the collector container" in r and r.splitlines()[-1].startswith("Technical: restarted collector")


def test_an_unknown_check_name_gets_a_generic_message_with_the_technical_line() -> None:
    t = m.humanise("brand_new_check", "crit", "x is 5", now=NOW)
    assert "brand_new_check" in t.splitlines()[0] and t.splitlines()[-1] == "Technical: brand_new_check, x is 5"
    assert m.from_alert_text("something nobody planned", now=NOW).splitlines()[-1] == "Technical: something nobody planned"


def test_a_long_or_non_ascii_detail_is_clipped_and_stays_ascii() -> None:
    t = m.humanise("disk", "warn", "café — " + "x" * 1000, now=NOW)
    assert t.isascii() and len(t) < 420 and t.endswith("...")


def test_no_banned_jargon_in_any_plain_sentence() -> None:
    banned = ("invariant", "snapshot age", "logon_time", "partition", "poll_log", "cid ", "gs_kt")
    for name, level, detail, _ in CASES:
        first = m.humanise(name, level, detail, now=NOW).splitlines()[0].lower()
        assert not any(b in first for b in banned), first
    for words in m.INVARIANT_WORDS.values():
        assert words.isascii() and not any(b in words.lower() for b in banned), words


# ---------------------------------------------------------------- the 20 invariants


def test_every_invariant_has_a_plain_wording_and_none_is_stale() -> None:
    """Fails when invariants.sql gains, loses or renames an invariant and messages.INVARIANT_WORDS does not follow."""
    assert len(NAMES) == 20
    assert sorted(m.INVARIANT_WORDS) == sorted(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_each_invariant_produces_a_sentence_from_the_real_summary_text(name: str) -> None:
    findings = [integrity.Finding(name, 3)]
    _, detail = integrity.summarise_invariants(findings, [], len(NAMES))
    t = m.humanise("data_integrity", "warn", detail, now=NOW)
    check_shape(t, "data_integrity")
    assert m.invariant_words(name, detail) in t
    assert "data_integrity" in t.splitlines()[-1]


def test_several_failing_invariants_are_counted_in_words() -> None:
    # NAMES[0], [1] and [4]: none is a same-day invariant, so the first listed still leads (the same-day order has its own tests below)
    findings = [integrity.Finding(NAMES[i], 1) for i in (0, 1, 4)]
    _, detail = integrity.summarise_invariants(findings, [], len(NAMES))
    t = m.humanise("data_integrity", "warn", detail, now=NOW)
    assert "(plus 2 others)" in t and m.INVARIANT_WORDS[NAMES[0]] in t
    check_shape(t, "data_integrity")


def test_more_than_six_failing_are_counted_from_the_n_of_20_figure() -> None:
    findings = [integrity.Finding(n, 1) for n in NAMES[:10]]
    _, detail = integrity.summarise_invariants(findings, [], len(NAMES))
    assert "10 of 20 invariants" in detail and "and 4 more" in detail
    t = m.humanise("data_integrity", "warn", detail, now=NOW)
    assert "(plus 9 others)" in t
    last = t.splitlines()[-1]
    assert "10 of 20 checks failing" in last and "(+" in last and "more)" in last  # the Technical line now lists names, bounded, with the rest counted
    check_shape(t, "data_integrity")


def test_every_invariant_has_its_own_action_hint() -> None:
    assert sorted(m.INVARIANT_ACTIONS) == sorted(NAMES)
    for name, hint in m.INVARIANT_ACTIONS.items():
        assert hint.isascii() and len(hint) > 20 and hint.endswith("."), name
        assert "docs/RUNBOOKS/" in hint or "Nothing to do" in hint, name


@pytest.mark.parametrize("name", NAMES)
def test_each_invariant_message_carries_its_own_hint_not_a_blanket_reassurance(name: str) -> None:
    _, detail = integrity.summarise_invariants([integrity.Finding(name, 1)], [], len(NAMES))
    first = m.humanise("data_integrity", "warn", detail, now=NOW).splitlines()[0]
    assert m.INVARIANT_ACTIONS[name] in first
    if name != "snapshot gaps over 5 minutes (last 3 days)":
        assert "Nothing to do unless it keeps happening" not in first


def test_a_missing_partition_is_not_told_to_wait() -> None:
    _, detail = integrity.summarise_invariants([integrity.Finding("daily partitions missing for today..today+3", 1)], [], len(NAMES))
    first = m.humanise("data_integrity", "warn", detail, now=NOW).splitlines()[0]
    assert "Writes will start to fail" in first and "maintenance log today" in first and "keeps happening" not in first


def test_the_new_violations_notice_from_integrity_is_humanised() -> None:
    notice = f"WARN data_integrity: new violations since the last sweep: {NAMES[9]}, {NAMES[6]}"
    t = m.from_alert_text(notice, now=NOW)
    # issue 79 re-review: the sentence now follows the MOST URGENT failing invariant (NAMES[6], the stale newest snapshot), not the first listed
    assert "new problem" in t and m.INVARIANT_WORDS[NAMES[6]] in t and "(and 1 more)" in t and "Some need action today" in t
    check_shape(t, "data_integrity")


def test_the_pack_incomplete_problem_is_not_called_a_data_problem() -> None:
    _, detail = integrity.summarise_invariants([], ["1 invariant(s) did not report: x"], 18)
    assert "could not run properly" in m.humanise("data_integrity", "warn", detail, now=NOW)


# ---------------------------------------------------------------- every check the watchdog produces has wording


def names_the_watchdog_can_alert_on() -> set[str]:
    """Read from the watchdog source itself: every literal key put into the results dicts (`out["x"] = ...`, `results["x"] = ...`,
    and the literal keys of `out.update({...})` / `results.update({...})`) in ops/watchdog/watchdog.py, plus the two slow checks
    named in integrity.py. Keys starting with an underscore are internal.

    LIMIT OF THIS GUARD: it is a static reading of the source. It cannot see a check name built from an f-string or a variable
    (`results[f"container:{svc}"]`, `results[name]`), nor keys added by `results.update(some_function())` (that is why the
    container, restart-cap and integrity names are covered by their own tests and prefixes). A check added in such a way needs its
    wording added and tested by hand."""
    return result_keys_in((REPO / "ops" / "watchdog" / "watchdog.py").read_text(encoding="utf-8")) | {integrity.INVARIANTS_KEY, integrity.CROSSCHECK_KEY}


def result_keys_in(source: str) -> set[str]:
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id in {"out", "results"}
                        and isinstance(t.slice, ast.Constant) and isinstance(t.slice.value, str) and not t.slice.value.startswith("_")):
                    found.add(t.slice.value)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update"
                and isinstance(node.func.value, ast.Name) and node.func.value.id in {"out", "results"}):
            for arg in node.args:
                if isinstance(arg, ast.Dict):
                    found |= {k.value for k in arg.keys
                              if isinstance(k, ast.Constant) and isinstance(k.value, str) and not k.value.startswith("_")}
    return found


def test_every_check_name_the_watchdog_can_alert_on_has_its_own_wording() -> None:
    """The names are read from watchdog.py, so a new check added there without wording in messages.py fails here."""
    produced = names_the_watchdog_can_alert_on()
    assert len(produced) >= 14, produced                      # the parser itself must not silently find nothing
    assert produced >= {"collector_health", "disk", "docker_proxy", "database", "data_integrity"}
    missing = produced - set(m.known_checks())
    assert not missing, f"check(s) with no wording in messages.py: {sorted(missing)}"
    assert set(m.known_checks()) <= produced, sorted(set(m.known_checks()) - produced)
    runbook = (REPO / "docs" / "RUNBOOKS" / "alerts.md").read_text(encoding="utf-8")
    for name in produced:
        assert f"`{name}`" in runbook, name
    for name in ("container:db", "restart-cap:db"):
        first = m.humanise(name, "crit", "x", now=NOW).splitlines()[0]
        assert "monitoring check" not in first  # own wording, not the generic one


# ---------------------------------------------------------------- the safe fallback and the Alerter wiring


def test_a_formatting_bug_falls_back_to_the_old_technical_text(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    def boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("bug")

    monkeypatch.setattr(m, "humanise", boom)
    with caplog.at_level(logging.ERROR, logger="watchdog.messages"):
        assert m.telegram_text("CRIT disk: 95%") == "vatpac-stats: CRIT disk: 95%"
    assert "could not format" in caplog.text and "RuntimeError" in caplog.text


def alerter(handler: Any, state: dict[str, Any] | None = None, confirm: int = 1) -> Any:
    cfg = type("C", (), {"confirm": confirm, "tg_token": "123:SECRET", "tg_chat": "42"})()
    return wd.Alerter(cfg, httpx.Client(transport=httpx.MockTransport(handler)), {} if state is None else state)


def test_the_alert_is_still_sent_when_formatting_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, str]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(json.loads(r.content))
        return httpx.Response(200)

    monkeypatch.setattr(m, "from_alert_text", lambda *a, **k: 1 / 0)
    alerter(handler).send("WARN x: y")
    assert seen == [{"chat_id": "42", "text": "vatpac-stats: WARN x: y"}]


def test_the_log_line_stays_technical(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="watchdog"):
        alerter(lambda r: httpx.Response(200)).send("CRIT disk: / 95% used")
    assert "ALERT CRIT disk: / 95% used" in caplog.text


def test_recovered_reports_how_long_the_problem_lasted_and_survives_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(json.loads(r.content)["text"])
        return httpx.Response(200)

    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(wd.time, "time", lambda: clock["t"])
    state: dict[str, Any] = {}
    a = alerter(handler, state)
    a.update("poll_log_ok_age", "crit", "newest ok poll is 400s old")
    clock["t"] += 250
    state = json.loads(json.dumps(state))                       # what the status file keeps across a restart
    b = alerter(handler, state)
    b.update("poll_log_ok_age", "ok", "newest ok poll 5s old")
    assert "The alert was active for about 4 minutes." in seen[-1] and seen[-1].startswith("VATPAC Stats: all clear")
    assert "poll_log_ok_age" not in state["bad_since"]


def test_recovered_without_a_stored_start_omits_the_duration() -> None:
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(json.loads(r.content)["text"])
        return httpx.Response(200)

    state: dict[str, Any] = {"alerts": {"disk": "warn"}}         # an older status file: no bad_since
    alerter(handler, state).update("disk", "ok", "/ 40% used")
    assert seen and "active for" not in seen[0] and "all clear" in seen[0]


def test_a_context_never_leaks_into_the_next_message() -> None:
    seen: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(json.loads(r.content)["text"])
        return httpx.Response(200)

    a = alerter(handler, {"alerts": {"disk": "warn"}, "bad_since": {"disk": 1.0}})
    a.update("disk", "ok", "/ 40% used")
    a.send("NEAR-MISS controller callsign(s) not on whitelist: X_TWR")
    assert "active for" in seen[0] and "active for" not in seen[1]


# ---------------------------------------------------------------- several failing invariants (issue 79 re-review)
PARTITIONS = "daily partitions missing for today..today+3"
GAPS = "snapshot gaps over 5 minutes (last 3 days)"


def _detail_for(first_names: list[str]) -> str:
    return integrity.summarise_invariants([integrity.Finding(n, 1) for n in first_names], [], len(NAMES))[1]


def test_a_same_day_failure_listed_second_is_not_hidden() -> None:
    detail = _detail_for([GAPS, PARTITIONS])                 # the poll-record style failure is listed first
    t = m.humanise("data_integrity", "warn", detail, now=NOW)
    first = t.splitlines()[0]
    assert m.INVARIANT_WORDS[PARTITIONS] in first and "Writes will start to fail" in first
    assert "Some need action today" in first and "(plus 1 other)" in first
    assert "Several checks failed" not in first
    check_shape(t, "data_integrity")


def test_several_failures_none_urgent_do_not_claim_action_today() -> None:
    detail = _detail_for([NAMES[0], NAMES[1]])
    first = m.humanise("data_integrity", "warn", detail, now=NOW).splitlines()[0]
    assert m.INVARIANT_WORDS[NAMES[0]] in first and "action today" not in first


def test_urgency_order_is_explicit_and_every_entry_is_a_real_invariant() -> None:
    assert set(m.SAME_DAY_INVARIANTS) <= set(NAMES) and len(set(m.SAME_DAY_INVARIANTS)) == len(m.SAME_DAY_INVARIANTS)
    detail = _detail_for([NAMES[0], m.SAME_DAY_INVARIANTS[3], m.SAME_DAY_INVARIANTS[0]])
    first = m.humanise("data_integrity", "warn", detail, now=NOW).splitlines()[0]
    assert m.INVARIANT_WORDS[m.SAME_DAY_INVARIANTS[0]] in first


def test_the_technical_line_names_every_failing_check_it_has_room_for_and_counts_the_rest() -> None:
    detail = _detail_for([GAPS, PARTITIONS])
    last = m.humanise("data_integrity", "warn", detail, now=NOW).splitlines()[-1]
    assert PARTITIONS in last and GAPS in last and "more)" not in last and len(last) <= len("Technical: data_integrity, ") + m.MAX_TECH
    many = m.humanise("data_integrity", "warn", _detail_for(NAMES[:10]), now=NOW).splitlines()[-1]
    assert "10 of 20 checks failing" in many and "more)" in many and len(many) <= len("Technical: data_integrity, ") + m.MAX_TECH


def test_the_guard_reads_update_dict_literals_too() -> None:
    src = chr(10).join(["results.update({'a_check': 1, '_hidden': 2})", "out.update({'b_check': 3})",
                        "results['c_check'] = 1", "other.update({'x': 1})"])
    assert result_keys_in(src) == {"a_check", "b_check", "c_check"}


def test_a_stopped_flight_writer_is_a_same_day_invariant() -> None:
    """Issue 89 phase 5: the old same-day check for missing flight summaries went with the old table; this one replaces it."""
    name = "flights quiet longer than the quiet period with unprocessed positions"
    assert name in m.SAME_DAY_INVARIANTS and name in NAMES
    assert m._by_urgency([NAMES[0], name])[0] == name
