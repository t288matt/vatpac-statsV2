"""Issue #55: the watchdog's decisions (ops/watchdog/watchdog.py) without a network, a database or Docker.

The watchdog sends the alerts and restarts containers, so its thresholds and its confirm / restart-cap logic are tested here
with stubs: httpx.MockTransport for HTTP, monkeypatched disk and clock, fake Docker objects. The flap and archive checks
have their own file (test_watchdog_checks.py); the database queries are in tests/integration/test_watchdog_db_checks.py.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("watchdog_under_test_decisions", REPO / "ops" / "watchdog" / "watchdog.py")
assert spec and spec.loader
wd = importlib.util.module_from_spec(spec)
sys.modules["watchdog_under_test_decisions"] = wd
spec.loader.exec_module(wd)

OK, WARN, CRIT = wd.OK, wd.WARN, wd.CRIT
NOW = 1_800_000_000.0  # a fixed "time.time()"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.startswith(("WATCHDOG_", "TELEGRAM_", "UPTIME_", "DOCKER_", "COMPOSE_", "COLLECTOR_", "GRAFANA_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("STATUS_FILE", str(tmp_path / "status" / "watchdog_status.json"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))


class Clock:
    """Stands in for the `time` module inside the watchdog only (the real time.time stays untouched for pytest)."""

    now = NOW

    def time(self) -> float:
        return self.now


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(wd, "time", c)
    return c


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ------------------------------------------------------------------ configuration
def test_defaults() -> None:
    c = wd.Config()
    assert (c.interval, c.warn_s, c.page_s, c.confirm) == (60, 60, 300, 2)
    assert (c.disk_pct, c.db_max_gb, c.backup_max_h, c.restore_test_max_days) == (80, 5, 26, 9)
    assert (c.restart_cap, c.restart_after) == (3, 3)
    assert c.restartable == c.expected == {"db", "collector", "grafana", "backup"}
    assert c.disk_paths == ["/backups", "/"] and c.docker == "" and c.project == "vatpac"


def test_overrides_and_bad_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_DISK_PCT", "70")
    monkeypatch.setenv("WATCHDOG_WARN_SECONDS", "not a number")      # falls back to the default
    monkeypatch.setenv("WATCHDOG_CONFIRM_CYCLES", "0")               # never below 1
    monkeypatch.setenv("WATCHDOG_RESTART_AFTER_CYCLES", "-4")
    monkeypatch.setenv("WATCHDOG_RESTARTABLE", " db , grafana ,,")
    monkeypatch.setenv("WATCHDOG_DISK_PATHS", "/data, ,/")
    c = wd.Config()
    assert c.disk_pct == 70 and c.warn_s == 60 and c.confirm == 1 and c.restart_after == 1
    assert c.restartable == {"db", "grafana"} and c.disk_paths == ["/data", "/"]


# ------------------------------------------------------------------ age_level
@pytest.mark.parametrize("age,level", [(None, WARN), (0, OK), (59.9, OK), (60, WARN), (299.9, WARN), (300, CRIT), (9999, CRIT)])
def test_age_level_thresholds(age: float | None, level: str) -> None:
    got, detail = wd.age_level(age, wd.Config(), "newest snapshot")
    assert got == level
    assert ("no newest snapshot yet" in detail) if age is None else (f"{age:.0f}s" in detail)


def test_age_level_uses_the_configured_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_WARN_SECONDS", "10")
    monkeypatch.setenv("WATCHDOG_PAGE_SECONDS", "20")
    c = wd.Config()
    assert [wd.age_level(a, c, "x")[0] for a in (9, 10, 19, 20)] == [OK, WARN, WARN, CRIT]


# The watchdog's automatic restart code is commented out (21-10-2026, owner decision; issue #232). The tests that exercise it
# are skipped, not deleted, so they are ready if the restart code comes back.
restart_disabled = pytest.mark.skip(reason="watchdog auto-restart code is commented out (issue #232)")


# ------------------------------------------------------------------ check_http and collector_is_stuck
def test_check_http_healthy_returns_the_body() -> None:
    res, body = wd.check_http(client_for(lambda r: httpx.Response(200, json={"status": "ok"})), "http://x/health")
    assert res == (OK, "healthy") and body == {"status": "ok"}


def test_check_http_unhealthy_is_critical_and_keeps_the_body() -> None:
    res, body = wd.check_http(client_for(lambda r: httpx.Response(503, json={"consecutive_failures": 4})), "http://x/health")
    assert res == (CRIT, "HTTP 503") and body == {"consecutive_failures": 4}


@pytest.mark.parametrize("response", [httpx.Response(200, text="not json"), httpx.Response(200, json=[1, 2])])
def test_check_http_ignores_a_body_that_is_not_an_object(response: httpx.Response) -> None:
    res, body = wd.check_http(client_for(lambda r: response), "http://x/health")
    assert res[0] == OK and body is None


def test_check_http_unreachable_is_critical() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    res, body = wd.check_http(client_for(boom), "http://x/health")
    assert res == (CRIT, "unreachable (ConnectError)") and body is None


@pytest.mark.parametrize("res,body,stuck", [
    ((OK, "healthy"), None, False),
    ((OK, "healthy"), {"consecutive_failures": 9}, False),
    ((CRIT, "unreachable"), None, True),                              # not answering: stuck
    ((CRIT, "HTTP 503"), {"consecutive_failures": 0}, True),          # 503 with no failures counted: stuck
    ((CRIT, "HTTP 503"), {}, True),
    ((CRIT, "HTTP 503"), {"consecutive_failures": None}, True),
    ((CRIT, "HTTP 503"), {"consecutive_failures": 1}, False),         # upstream outage: restarting would not help
    ((CRIT, "HTTP 503"), {"consecutive_failures": "3"}, False),
])
@restart_disabled
def test_collector_is_stuck(res: tuple[str, str], body: dict | None, stuck: bool) -> None:
    assert wd.collector_is_stuck(res, body) is stuck


# ------------------------------------------------------------------ check_disk
Usage = namedtuple("Usage", "total used free")


def fake_disk(monkeypatch: pytest.MonkeyPatch, table: dict[str, tuple[int, float]]) -> None:
    """table: path -> (device number, percent used)."""
    def stat(path):
        if path not in table:
            raise FileNotFoundError(path)
        return type("S", (), {"st_dev": table[path][0]})()

    def usage(path):
        return Usage(1000, int(table[path][1] * 10), 0)

    monkeypatch.setattr(wd.os, "stat", stat)
    monkeypatch.setattr(wd.shutil, "disk_usage", usage)


@pytest.mark.parametrize("pct,level", [(0, OK), (79.9, OK), (80, WARN), (89.9, WARN), (90, CRIT), (99, CRIT)])
def test_check_disk_thresholds(monkeypatch: pytest.MonkeyPatch, pct: float, level: str) -> None:
    fake_disk(monkeypatch, {"/backups": (1, pct), "/": (1, pct)})
    got, detail = wd.check_disk(wd.Config())
    assert got == level and f"{pct:.0f}% used" in detail


def test_check_disk_reports_the_worst_path_and_checks_a_shared_device_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_disk(monkeypatch, {"/backups": (1, 85), "/": (2, 50), "/var": (1, 99)})
    monkeypatch.setenv("WATCHDOG_DISK_PATHS", "/backups,/,/var")
    got, detail = wd.check_disk(wd.Config())
    assert got == WARN and "/backups 85% used" in detail and "/ 50% used" in detail and "/var" not in detail


def test_check_disk_unreadable_path_is_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_disk(monkeypatch, {"/": (2, 10)})
    got, detail = wd.check_disk(wd.Config())
    assert got == WARN and "/backups unreadable" in detail and "/ 10% used" in detail


def test_check_disk_critical_beats_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_disk(monkeypatch, {"/": (2, 95)})
    assert wd.check_disk(wd.Config())[0] == CRIT


def test_check_disk_with_no_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_DISK_PATHS", " , ")
    wd.Config().disk_paths  # noqa: B018  (the default applies when the variable is blank)
    c = wd.Config()
    c.disk_paths = []
    assert wd.check_disk(c) == (OK, "no path to check")


# ------------------------------------------------------------------ backups and the restore test
def make_backup(directory: Path, name: str, age_h: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    f = directory / name
    f.write_text("x")
    os.utime(f, (NOW - age_h * 3600, NOW - age_h * 3600))


@pytest.mark.parametrize("age_h,level", [(1, OK), (25.9, OK), (26.0, OK), (26.1, CRIT), (100, CRIT)])
def test_check_backup_age(clock, tmp_path: Path, age_h: float, level: str) -> None:
    make_backup(tmp_path / "backups", "old.dump", age_h + 50)
    make_backup(tmp_path / "backups", "new.dump", age_h)                  # the newest one counts
    got, detail = wd.check_backup(wd.Config(), first_run=NOW)
    assert got == level and f"{age_h:.1f} h old" in detail


def test_check_backup_ignores_files_that_are_not_dumps(clock, tmp_path: Path) -> None:
    make_backup(tmp_path / "backups", "notes.txt", 1)
    assert wd.check_backup(wd.Config(), first_run=NOW - 30 * 3600)[0] == CRIT


@pytest.mark.parametrize("since_h,level", [(5, OK), (25, OK), (27, CRIT)])
def test_check_backup_without_any_backup_counts_from_the_first_run(clock, since_h: float, level: str) -> None:
    got, detail = wd.check_backup(wd.Config(), first_run=NOW - since_h * 3600)
    assert got == level and "no backup yet" in detail


def test_check_backup_limit_is_configurable(clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_backup(tmp_path / "backups", "a.dump", 10)
    monkeypatch.setenv("WATCHDOG_BACKUP_MAX_HOURS", "8")
    assert wd.check_backup(wd.Config(), NOW)[0] == CRIT


@pytest.mark.parametrize("text,when,verdict", [
    ("2026-10-08T03:00:00Z OK restored 12 tables", datetime(2026, 10, 8, 3, 0, tzinfo=UTC), "OK"),
    ("2026-10-08T03:00:00Z FAIL pg_restore", datetime(2026, 10, 8, 3, 0, tzinfo=UTC), "FAIL"),
    ("2026-10-08T03:00:00Z MAYBE", datetime(2026, 10, 8, 3, 0, tzinfo=UTC), "?"),
    ("yesterday OK", None, "OK"),
    ("2026-10-08T03:00:00Z", None, "?"),
    ("", None, "?"),
])
def test_parse_restore_result(text: str, when: datetime | None, verdict: str) -> None:
    assert wd.parse_restore_result(text) == (when, verdict)


def stamp(age_days: float) -> str:
    return datetime.fromtimestamp(NOW - age_days * 86400, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_restore(tmp_path: Path, text: str, mtime_age_days: float = 0) -> None:
    d = tmp_path / "backups"
    d.mkdir(exist_ok=True)
    f = d / ".last_restore_test"
    f.write_text(text)
    os.utime(f, (NOW - mtime_age_days * 86400, NOW - mtime_age_days * 86400))


@pytest.mark.parametrize("age_d,level", [(1, OK), (8.9, OK), (9.0, OK), (9.1, WARN)])
def test_restore_test_age(clock, tmp_path: Path, age_d: float, level: str) -> None:
    write_restore(tmp_path, f"{stamp(age_d)} OK")
    got, detail = wd.check_restore_test(wd.Config(), first_run=NOW)
    assert got == level and f"{age_d:.1f} days ago" in detail


def test_restore_test_failure_is_critical_whatever_its_age(clock, tmp_path: Path) -> None:
    write_restore(tmp_path, f"{stamp(0.1)} FAIL")
    got, detail = wd.check_restore_test(wd.Config(), NOW)
    assert got == CRIT and "FAILED" in detail


def test_restore_test_unreadable_verdict_is_a_warning(clock, tmp_path: Path) -> None:
    write_restore(tmp_path, f"{stamp(0.1)} ???")
    assert wd.check_restore_test(wd.Config(), NOW)[0] == WARN


def test_restore_test_uses_the_timestamp_in_the_file_not_the_file_time(clock, tmp_path: Path) -> None:
    write_restore(tmp_path, f"{stamp(20)} OK", mtime_age_days=0)        # fresh file, old result
    assert wd.check_restore_test(wd.Config(), NOW)[0] == WARN
    write_restore(tmp_path, "garbage OK", mtime_age_days=20)            # no readable timestamp: the file time is used
    assert wd.check_restore_test(wd.Config(), NOW)[0] == WARN
    write_restore(tmp_path, "garbage OK", mtime_age_days=1)
    assert wd.check_restore_test(wd.Config(), NOW)[0] == OK


@pytest.mark.parametrize("since_d,level", [(2, OK), (10, WARN)])
def test_restore_test_missing_result(clock, since_d: float, level: str) -> None:
    got, detail = wd.check_restore_test(wd.Config(), first_run=NOW - since_d * 86400)
    assert got == level and ("no restore test yet" in detail or "no restore test result" in detail)


def test_restore_test_check_can_be_disabled(clock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_restore(tmp_path, f"{stamp(0.1)} FAIL")
    monkeypatch.setenv("WATCHDOG_RESTORE_TEST_MAX_DAYS", "0")
    assert wd.check_restore_test(wd.Config(), NOW) == (OK, "restore-test age check disabled")


# ------------------------------------------------------------------ check_containers
def ins(status="running", health="healthy", restarts=0, exit_code=0) -> dict[str, Any]:
    return {"Id": "id", "RestartCount": restarts, "State": {"Status": status, "Health": {"Status": health}, "ExitCode": exit_code}}


def healthy_set(**over: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out = {s: ins() for s in ("db", "collector", "grafana", "backup")}
    out["migrate"] = ins("exited", "none", exit_code=0)
    out.update(over)
    return out


def test_containers_all_fine() -> None:
    got = wd.check_containers(wd.Config(), healthy_set(), {})
    assert set(got) == {f"container:{s}" for s in ("db", "collector", "grafana", "backup", "migrate")}
    assert all(lv == OK for lv, _ in got.values())


def test_containers_missing_is_critical() -> None:
    c = healthy_set()
    del c["grafana"]
    assert wd.check_containers(wd.Config(), c, {})["container:grafana"] == (CRIT, "missing")


@pytest.mark.parametrize("i,level,word", [
    (ins("exited", "none"), CRIT, "state exited"), (ins("restarting"), CRIT, "restarting"),
    (ins(health="unhealthy"), CRIT, "unhealthy"), (ins(health="starting"), OK, "running/starting"),
    (ins(health="none"), OK, "running/none"),
])
def test_container_states(i: dict[str, Any], level: str, word: str) -> None:
    got = wd.check_containers(wd.Config(), healthy_set(db=i), {})["container:db"]
    assert got[0] == level and word in got[1]


@pytest.mark.parametrize("i,level", [
    (ins("exited", "none", exit_code=0), OK), (ins("exited", "none", exit_code=1), CRIT),
    (ins("running", "none"), OK), (ins("dead", "none", exit_code=0), CRIT),
])
def test_migrate_must_have_exited_cleanly_or_be_running(i: dict[str, Any], level: str) -> None:
    assert wd.check_containers(wd.Config(), healthy_set(migrate=i), {})["container:migrate"][0] == level


def test_a_docker_restart_warns_for_ten_minutes(clock: Clock) -> None:
    state: dict[str, Any] = {}
    wd.check_containers(wd.Config(), healthy_set(), state)                       # first sight: no baseline to compare
    got = wd.check_containers(wd.Config(), healthy_set(collector=ins(restarts=1)), state)
    assert got["container:collector"] == (WARN, "restarted by Docker (count 1)")
    assert state["recent_restart_until"]["collector"] == NOW + 600
    clock.now = NOW + 599
    assert wd.check_containers(wd.Config(), healthy_set(collector=ins(restarts=1)), state)["container:collector"][0] == WARN
    clock.now = NOW + 601
    assert wd.check_containers(wd.Config(), healthy_set(collector=ins(restarts=1)), state)["container:collector"][0] == OK


def test_first_sight_of_a_nonzero_restart_count_is_not_a_new_restart(clock) -> None:
    got = wd.check_containers(wd.Config(), healthy_set(collector=ins(restarts=5)), {})
    assert got["container:collector"][0] == OK


# ------------------------------------------------------------------ Alerter
class Recorder:
    """An Alerter whose messages are collected instead of sent."""

    def __init__(self, confirm: int = 2) -> None:
        self.sent: list[str] = []
        sent = self.sent

        class A(wd.Alerter):
            def send(self, text: str, alert: object = None) -> None:
                sent.append(text)

        self.alerter = A(type("C", (), {"confirm": confirm, "tg_token": "", "tg_chat": ""})(), None, {})  # type: ignore[arg-type]

    def feed(self, *levels: str, name: str = "x") -> None:
        for lv in levels:
            self.alerter.update(name, lv, f"detail {lv}")


def test_a_problem_alerts_only_once_it_is_confirmed() -> None:
    r = Recorder(confirm=2)
    r.feed(CRIT)
    assert r.sent == []
    r.feed(CRIT)
    assert r.sent == ["CRIT x: detail crit"]
    r.feed(CRIT, CRIT, CRIT)
    assert len(r.sent) == 1                                                  # no repeat while it stays bad


def test_a_flap_that_does_not_repeat_never_alerts() -> None:
    r = Recorder(confirm=2)
    r.feed(CRIT, OK, CRIT, OK, CRIT, OK)
    assert r.sent == []


def test_recovery_is_announced_once_with_the_old_level() -> None:
    r = Recorder(confirm=2)
    r.feed(WARN, WARN, OK)
    assert r.sent == ["WARN x: detail warn"]
    r.feed(OK)
    assert r.sent[-1] == "RECOVERED x (was warn): detail ok" and len(r.sent) == 2
    r.feed(OK, OK)
    assert len(r.sent) == 2


def test_a_change_of_level_must_itself_be_confirmed() -> None:
    r = Recorder(confirm=2)
    r.feed(WARN, WARN, CRIT)
    assert len(r.sent) == 1
    r.feed(CRIT)
    assert r.sent[-1] == "CRIT x: detail crit"


def test_a_different_level_restarts_the_confirmation_count() -> None:
    r = Recorder(confirm=2)
    r.feed(CRIT, WARN)
    assert r.sent == []                                                      # one sighting of each: neither is confirmed
    r.feed(WARN)
    assert r.sent == ["WARN x: detail warn"]


def test_checks_are_confirmed_independently() -> None:
    r = Recorder(confirm=2)
    r.feed(CRIT, name="a")
    r.feed(CRIT, name="b")
    r.feed(CRIT, name="a")
    assert r.sent == ["CRIT a: detail crit"]


def test_confirm_of_one_alerts_at_once() -> None:
    r = Recorder(confirm=1)
    r.feed(CRIT)
    assert r.sent == ["CRIT x: detail crit"]


def telegram_alerter(handler, token="123:SECRET", chat="42") -> wd.Alerter:
    cfg = type("C", (), {"confirm": 1, "tg_token": token, "tg_chat": chat})()
    return wd.Alerter(cfg, client_for(handler), {})  # type: ignore[arg-type]


def test_telegram_message_goes_to_the_bot_api_in_plain_text() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    telegram_alerter(handler).update("disk", "crit", "/ 95% used", {"disks": [["/", 95]]})
    assert len(seen) == 1 and seen[0].url.host == "api.telegram.org" and seen[0].url.path == "/bot123:SECRET/sendMessage"
    sent = json.loads(seen[0].content)
    assert sent["chat_id"] == "42" and set(sent) == {"chat_id", "text"}
    assert sent["text"].startswith("VATPAC Stats: problem") and "95% full" in sent["text"]
    assert sent["text"].splitlines()[-1] == "Technical: disk, / 95% used"


@pytest.mark.parametrize("token,chat", [("", "42"), ("123:SECRET", "")])
def test_without_telegram_settings_nothing_is_sent(token: str, chat: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not send")

    telegram_alerter(handler, token, chat).send("hello")


def test_a_failed_telegram_send_never_raises_and_never_logs_the_token(caplog: pytest.LogCaptureFixture) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to https://api.telegram.org/bot123:SECRET/sendMessage")

    with caplog.at_level(logging.DEBUG, logger="watchdog"):
        telegram_alerter(boom).send("hello")
        telegram_alerter(lambda r: httpx.Response(500)).send("hello")
    assert "SECRET" not in caplog.text
    assert "telegram send failed (ConnectError)" in caplog.text and "telegram returned HTTP 500" in caplog.text


# ------------------------------------------------------------------ try_restart
class FakeDocker:
    def __init__(self, result: bool | Exception = True) -> None:
        self.restarted: list[str] = []
        self.result = result
        self.data: dict[str, dict[str, Any]] = {}

    def restart(self, container_id: str) -> bool:
        if isinstance(self.result, Exception):
            raise self.result
        self.restarted.append(container_id)
        return self.result

    def containers(self) -> dict[str, dict[str, Any]]:
        return self.data


def restart_setup(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WATCHDOG_CONFIRM_CYCLES", "1")
    cfg = wd.Config()
    rec = Recorder(confirm=1)
    rec.alerter.cfg = cfg
    return cfg, rec


@restart_disabled
def test_try_restart_restarts_and_says_so(clock, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, rec = restart_setup(monkeypatch)
    docker, state = FakeDocker(), {}
    wd.try_restart(cfg, docker, rec.alerter, state, "collector", {"Id": "abc"}, "collector stuck")  # type: ignore[arg-type]
    assert docker.restarted == ["abc"] and state["restarts"]["collector"] == [NOW]
    assert rec.sent == ["RESTARTED collector (1/3 this hour, ok=True): collector stuck"]


@restart_disabled
def test_try_restart_does_nothing_for_a_service_that_is_not_restartable(clock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_RESTARTABLE", "db")
    cfg, rec = restart_setup(monkeypatch)
    docker = FakeDocker()
    wd.try_restart(cfg, docker, rec.alerter, {}, "collector", {"Id": "abc"}, "stuck")  # type: ignore[arg-type]
    assert docker.restarted == [] and rec.sent == []


@restart_disabled
def test_try_restart_stops_at_the_hourly_cap_and_alerts_once(clock, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, rec = restart_setup(monkeypatch)
    docker, state = FakeDocker(), {"restarts": {"collector": [NOW - 100, NOW - 200, NOW - 300]}}
    for _ in range(2):
        wd.try_restart(cfg, docker, rec.alerter, state, "collector", {"Id": "abc"}, "stuck")  # type: ignore[arg-type]
    assert docker.restarted == []
    assert rec.sent == ["CRIT restart-cap:collector: restart cap 3/hour reached; not restarting (stuck)"]


@restart_disabled
def test_try_restart_forgets_restarts_older_than_an_hour(clock, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, rec = restart_setup(monkeypatch)
    docker = FakeDocker()
    state = {"restarts": {"collector": [NOW - 3599, NOW - 3600, NOW - 3601, NOW - 9000]}}
    wd.try_restart(cfg, docker, rec.alerter, state, "collector", {"Id": "abc"}, "stuck")  # type: ignore[arg-type]
    assert docker.restarted == ["abc"]                                       # one stamp left (3599 s), so below the cap of 3
    assert state["restarts"]["collector"] == [NOW - 3599, NOW]


@restart_disabled
def test_the_cap_alert_clears_when_restarts_age_out(clock, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, rec = restart_setup(monkeypatch)
    docker, state = FakeDocker(), {"restarts": {"db": [NOW - 10, NOW - 20, NOW - 30]}}
    wd.try_restart(cfg, docker, rec.alerter, state, "db", {"Id": "d"}, "down")  # type: ignore[arg-type]
    state["restarts"]["db"] = []
    wd.try_restart(cfg, docker, rec.alerter, state, "db", {"Id": "d"}, "down")  # type: ignore[arg-type]
    assert rec.sent[0].startswith("CRIT restart-cap:db") and rec.sent[1] == "RECOVERED restart-cap:db (was crit): below restart cap"
    assert docker.restarted == ["d"]


@restart_disabled
def test_a_docker_error_during_restart_is_logged_not_raised(clock, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, rec = restart_setup(monkeypatch)
    state: dict[str, Any] = {}
    wd.try_restart(cfg, FakeDocker(httpx.ConnectError("x")), rec.alerter, state, "db", {"Id": "d"}, "down")  # type: ignore[arg-type]
    assert rec.sent == [] and state["restarts"]["db"] == []                  # a failed call does not use up the cap


# ------------------------------------------------------------------ status file and state
def test_write_status_overall_is_the_worst_level_and_the_file_is_complete(tmp_path: Path) -> None:
    cfg = wd.Config()
    overall = wd.write_status(cfg, {"a": (OK, "fine"), "b": (CRIT, "bad"), "c": (WARN, "meh")}, {"k": 1})
    data = json.loads(cfg.status_file.read_text())
    assert overall == CRIT == data["overall"]
    assert data["checks"] == {"a": {"level": "ok", "detail": "fine"}, "b": {"level": "crit", "detail": "bad"},
                              "c": {"level": "warn", "detail": "meh"}}
    assert data["state"] == {"k": 1} and data["updated"].endswith("Z")
    assert not cfg.status_file.with_suffix(".tmp").exists()
    assert wd.write_status(cfg, {}, {}) == OK
    assert wd.write_status(cfg, {"a": (OK, ""), "w": (WARN, "")}, {}) == WARN


def test_write_status_survives_an_unwritable_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("STATUS_FILE", str(blocker / "sub" / "status.json"))
    assert wd.write_status(wd.Config(), {"a": (CRIT, "")}, {}) == CRIT


def test_load_state(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    assert wd.load_state(p) == {}
    p.write_text("{not json")
    assert wd.load_state(p) == {}
    p.write_text("[1, 2]")
    assert wd.load_state(p) == {}
    p.write_text(json.dumps({"state": {"known_unlisted_callsign": ["X_TWR"]}}))
    assert wd.load_state(p) == {"known_unlisted_callsign": ["X_TWR"]}


# ------------------------------------------------------------------ cycle: the restart decisions
class World:
    """Everything one watchdog cycle talks to, controllable per test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, docker: bool = True, **env: str) -> None:
        monkeypatch.setenv("WATCHDOG_CONFIRM_CYCLES", "2")
        if docker:
            monkeypatch.setenv("DOCKER_HOST_URL", "http://docker")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        self.cfg = wd.Config()
        self.collector: httpx.Response | Exception = httpx.Response(200, json={})
        self.grafana: httpx.Response | Exception = httpx.Response(200, json={})
        self.kuma_calls = 0
        self.telegram: list[dict] = []
        self.db_result: dict[str, wd.Result] = {"database": (OK, "reachable")}
        self.unlisted: list[str] = []
        self.docker = FakeDocker()
        self.docker.data = {s: {**ins(), "Id": f"id-{s}"} for s in ("db", "collector", "grafana", "backup")}
        self.docker.data["migrate"] = {**ins("exited", "none"), "Id": "id-migrate"}
        monkeypatch.setattr(wd, "check_db", lambda cfg: dict(self.db_result))
        monkeypatch.setattr(wd, "unlisted_callsigns", lambda cfg: list(self.unlisted))
        monkeypatch.setattr(wd, "check_disk", lambda cfg: (OK, "disk fine"))
        monkeypatch.setattr(wd, "check_backup", lambda cfg, first: (OK, "backup fine"))
        monkeypatch.setattr(wd, "check_restore_test", lambda cfg, first: (OK, "restore fine"))
        # the scheduled integrity checks (issue 20) need a database and the archive: the tests of their own stub them here
        self.integrity: tuple[dict[str, wd.Result], list[str]] = ({}, [])
        monkeypatch.setattr(wd.integrity, "run_due", lambda plan, state: self.integrity)
        self.state: dict[str, Any] = {}
        self.sent: list[str] = []
        sent = self.sent

        class A(wd.Alerter):
            def send(self, text: str, alert: object = None) -> None:
                sent.append(text)

        self.client = client_for(self.handle)
        self.alerter = A(self.cfg, self.client, self.state)

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "kuma":
            self.kuma_calls += 1
            return httpx.Response(200)
        answer = self.collector if host == "collector" else self.grafana
        if isinstance(answer, Exception):
            raise answer
        return answer

    def cycle(self, n: int = 1) -> str:
        out = ""
        for _ in range(n):
            out = wd.cycle(self.cfg, self.client, self.docker, self.alerter, self.state)  # type: ignore[arg-type]
        return out


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    return World(monkeypatch, tmp_path)


def test_cycle_all_healthy_alerts_nothing_and_restarts_nothing(world: World) -> None:
    assert world.cycle(4) == OK
    assert world.sent == [] and world.docker.restarted == []
    data = json.loads(world.cfg.status_file.read_text())
    assert data["overall"] == "ok" and data["checks"]["collector_health"]["level"] == "ok"


def test_cycle_scheduled_integrity_result_is_in_the_status_alerted_once_and_recovered(world: World) -> None:
    world.integrity = ({"data_integrity": (wd.WARN, "1 of 18 invariants report violations: x (2) (checked now)")}, [])
    world.cycle(6)
    assert [s for s in world.sent if "data_integrity" in s] == [
        "WARN data_integrity: 1 of 18 invariants report violations: x (2) (checked now)"]
    data = json.loads(world.cfg.status_file.read_text())
    assert data["overall"] == "warn" and data["checks"]["data_integrity"]["level"] == "warn"
    world.integrity = ({"data_integrity": (OK, "all 18 invariants hold (checked later)")}, [])
    world.cycle(4)
    assert world.sent[-1].startswith("RECOVERED data_integrity (was warn): all 18 invariants hold") and len(world.sent) == 2


def test_cycle_sends_the_follow_up_notices_from_a_sweep(world: World) -> None:
    world.integrity = ({}, [("warn", "new violations since the last sweep: y")])
    world.cycle(1)
    assert world.sent == ["WARN data_integrity: new violations since the last sweep: y"]
    world.integrity = ({}, [])
    world.cycle(2)
    assert len(world.sent) == 1


@restart_disabled
def test_cycle_restarts_a_collector_that_does_not_answer_after_three_cycles(world: World) -> None:
    world.collector = httpx.ConnectError("down")
    world.cycle(2)
    assert world.docker.restarted == []                                      # restart_after is 3
    world.cycle(1)
    assert world.docker.restarted == ["id-collector"]
    assert any(s.startswith("RESTARTED collector") for s in world.sent)
    assert any(s.startswith("CRIT collector_health") for s in world.sent)


@restart_disabled
def test_cycle_does_not_restart_a_collector_that_reports_an_upstream_outage(world: World) -> None:
    world.collector = httpx.Response(503, json={"consecutive_failures": 7})
    world.cycle(6)
    assert world.docker.restarted == []
    assert any(s.startswith("CRIT collector_health: HTTP 503") for s in world.sent)


@restart_disabled
def test_cycle_does_not_restart_a_collector_that_is_still_starting(world: World) -> None:
    world.collector = httpx.ConnectError("not up yet")
    world.docker.data["collector"] = {**ins(health="starting"), "Id": "id-collector"}
    world.cycle(6)
    assert world.docker.restarted == []


@restart_disabled
def test_cycle_recovery_resets_the_restart_countdown(world: World) -> None:
    world.collector = httpx.ConnectError("down")
    world.cycle(2)
    world.collector = httpx.Response(200, json={})
    world.cycle(1)
    world.collector = httpx.ConnectError("down")
    world.cycle(2)
    assert world.docker.restarted == []                                      # never three bad cycles in a row


@restart_disabled
def test_cycle_restarts_grafana_after_three_failed_health_checks(world: World) -> None:
    world.grafana = httpx.Response(500)
    world.cycle(3)
    assert world.docker.restarted == ["id-grafana"]


@restart_disabled
def test_cycle_restarts_a_stopped_container_but_never_migrate(world: World) -> None:
    world.docker.data["db"] = {**ins("exited", "none"), "Id": "id-db"}
    world.docker.data["migrate"] = {**ins("exited", "none", exit_code=1), "Id": "id-migrate"}
    world.cycle(3)
    assert world.docker.restarted == ["id-db"]
    assert any(s.startswith("CRIT container:migrate") for s in world.sent)   # alerted, not restarted


def test_cycle_never_restarts_without_docker_access(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    w = World(monkeypatch, tmp_path, docker=False)
    w.collector = httpx.ConnectError("down")
    w.cycle(6)
    assert w.docker.restarted == []
    assert not any("container:" in k for k in json.loads(w.cfg.status_file.read_text())["checks"])


@restart_disabled
def test_cycle_restarts_once_per_countdown_not_every_cycle(world: World) -> None:
    world.collector = httpx.ConnectError("down")
    world.cycle(5)
    assert world.docker.restarted == ["id-collector"]                        # the countdown starts again after a restart
    world.cycle(1)
    assert world.docker.restarted == ["id-collector"] * 2


@restart_disabled
def test_cycle_never_restarts_migrate_even_if_it_were_listed_as_restartable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    w = World(monkeypatch, tmp_path, WATCHDOG_RESTARTABLE="db,migrate")
    w.docker.data["migrate"] = {**ins("exited", "none", exit_code=1), "Id": "id-migrate"}
    w.cycle(4)
    assert w.docker.restarted == []


@restart_disabled
def test_cycle_respects_the_restart_cap(world: World) -> None:
    world.collector = httpx.ConnectError("down")
    world.cycle(3 * 5)                                                       # five restart decisions, cap is 3 per hour
    assert world.docker.restarted == ["id-collector"] * 3
    assert any(s.startswith("CRIT restart-cap:collector") for s in world.sent)


def test_cycle_docker_proxy_failure_is_reported(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> dict:
        raise httpx.ConnectError("proxy down")

    world.docker.containers = boom                                           # type: ignore[method-assign]
    world.cycle(2)
    assert "CRIT docker_proxy: unreachable (ConnectError)" in world.sent


def test_cycle_alerts_each_new_unknown_callsign_once(world: World) -> None:
    world.unlisted = ["ML_XYZ_CTR", "AD_XYZ_TWR"]
    world.cycle(3)
    assert world.sent.count("UNKNOWN CALLSIGN not on whitelist: ML_XYZ_CTR, AD_XYZ_TWR") == 1
    world.unlisted = ["ML_XYZ_CTR", "AD_XYZ_TWR", "BN_ZZZ_TWR"]
    world.cycle(1)
    assert world.sent[-1] == "UNKNOWN CALLSIGN not on whitelist: BN_ZZZ_TWR"
    assert world.state["known_unlisted_callsign"] == ["AD_XYZ_TWR", "BN_ZZZ_TWR", "ML_XYZ_CTR"]


def test_a_callsign_already_alerted_under_the_old_state_key_is_not_alerted_again(world: World) -> None:
    """Issue 209: the state key known_near_miss became known_unlisted_callsign. A callsign recorded under the old key must stay quiet
    after the upgrade, a new one alerts exactly once, and the old key is read once and then gone."""
    world.state["known_near_miss"] = ["ML_XYZ_CTR", "AD_XYZ_TWR"]
    world.unlisted = ["ML_XYZ_CTR", "AD_XYZ_TWR"]
    world.cycle(3)
    assert [s for s in world.sent if "UNKNOWN CALLSIGN" in s] == []
    assert "known_near_miss" not in world.state
    assert world.state["known_unlisted_callsign"] == ["AD_XYZ_TWR", "ML_XYZ_CTR"]
    world.unlisted = ["ML_XYZ_CTR", "AD_XYZ_TWR", "BN_ZZZ_TWR"]
    world.cycle(3)
    assert [s for s in world.sent if "UNKNOWN CALLSIGN" in s] == ["UNKNOWN CALLSIGN not on whitelist: BN_ZZZ_TWR"]



def test_cycle_the_uptime_push_is_skipped_only_when_the_overall_state_is_critical(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    w = World(monkeypatch, tmp_path, UPTIME_KUMA_PUSH_URL="http://kuma/push")
    assert w.cycle() == OK and w.kuma_calls == 1
    w.db_result["identity_flap"] = (WARN, "pairs")
    assert w.cycle() == WARN and w.kuma_calls == 2                           # a warning still pushes
    w.grafana = httpx.Response(500)
    assert w.cycle() == CRIT and w.kuma_calls == 2                           # critical: no push, so Kuma notices


def test_cycle_reports_database_checks_through_the_alerter(world: World) -> None:
    world.db_result["network_snapshot_age"] = (CRIT, "newest snapshot is 400s old")
    world.cycle(2)
    assert "CRIT network_snapshot_age: newest snapshot is 400s old" in world.sent
