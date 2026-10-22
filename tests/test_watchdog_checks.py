"""Watchdog checks added for issue 20: the identity-flap alert and the raw-feed archive age (ops/watchdog/watchdog.py).

No database or network: the cursor is a stub and the alerts go to a recording client."""
from __future__ import annotations

import importlib.util
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("watchdog_under_test", REPO / "ops" / "watchdog" / "watchdog.py")
assert spec and spec.loader
wd = importlib.util.module_from_spec(spec)
sys.modules["watchdog_under_test"] = wd
spec.loader.exec_module(wd)

T = datetime(2026, 10, 8, 4, 8, 27, tzinfo=UTC)


class Cur:
    """Answers the watchdog's two statements: set_config for the window, then ops/audit/identity_flap.sql (one row per table)."""

    def __init__(self, pilot: int = 0, ctrl: int = 0, fail: Exception | None = None) -> None:
        self.rows = [("pilot session pairs, same cid+callsign, logon_time within 2 s (identity flap)", pilot, T if pilot else None),
                     ("controller session pairs, same cid+callsign, logon_time within 2 s (identity flap)", ctrl, T if ctrl else None)]
        self.sql: list[str] = []
        self.params: list[Any] = []
        self.fail = fail

    def execute(self, sql: str, params: Any = None) -> None:
        if self.fail:
            raise self.fail
        self.sql.append(sql)
        self.params.append(params)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


def test_flap_ok_when_nothing_is_found() -> None:
    level, detail = wd.check_identity_flap(Cur())
    assert level == wd.OK and "24 h" in detail


@pytest.mark.parametrize("cur,words", [
    (Cur(pilot=31, ctrl=3), ["31 pilot", "3 controller", "08-10-2026 04:08:27 UTC"]),
    (Cur(ctrl=1), ["0 pilot", "1 controller"]),
])
def test_flap_is_a_warning_with_the_counts(cur: Cur, words: list[str]) -> None:
    level, detail = wd.check_identity_flap(cur)
    assert level == wd.WARN
    for w in words:
        assert w in detail
    assert "audit/identity_flap.sql" in detail


def test_flap_query_failure_is_a_warning_not_a_crash() -> None:
    import psycopg

    level, detail = wd.check_identity_flap(Cur(fail=psycopg.errors.InsufficientPrivilege("denied")))
    assert level == wd.WARN and "InsufficientPrivilege" in detail and "denied" not in detail


def test_flap_runs_the_shared_file_with_the_window_set_first() -> None:
    """One definition of the rule: the watchdog sets the window, then runs ops/audit/identity_flap.sql unchanged."""
    cur = Cur()
    wd.check_identity_flap(cur)
    shared = (REPO / "ops" / "audit" / "identity_flap.sql").read_text(encoding="utf-8")
    assert "vatpac.flap_window_hours" in cur.sql[0] and cur.params[0] == (str(wd.FLAP_WINDOW_HOURS),)
    assert cur.sql[1] == shared and "a.cid = b.cid AND a.callsign = b.callsign" in shared and "< 2" in shared


def test_flap_missing_file_is_a_warning_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wd, "_FLAP_FILE", "no_such_file.sql")
    level, detail = wd.check_identity_flap(Cur())
    assert level == wd.WARN and "FileNotFoundError" in detail and "query failed" in detail


def test_flap_alerts_once_and_recovers() -> None:
    """Same dedupe as every other check: one alert when it confirms, one RECOVERED when it clears, nothing between."""
    sent: list[str] = []

    class Rec(wd.Alerter):
        def __init__(self) -> None:
            self.confirmed, self.pending = {}, {}
            self.cfg = type("C", (), {"confirm": 2})()

        def send(self, text: str) -> None:
            sent.append(text)

    a = Rec()
    bad, good = wd.check_identity_flap(Cur(pilot=2)), wd.check_identity_flap(Cur())
    for res in (bad, bad, bad, bad, good, good, good):
        a.update("identity_flap", *res)
    assert len(sent) == 2
    assert sent[0].startswith("WARN identity_flap:") and sent[1].startswith("RECOVERED identity_flap")


def body(**kw: Any) -> dict[str, Any]:
    arc = {"enabled": True, "last_write_age_seconds": 100, "compact_every_s": 300, "errors": 0, "size_bytes": 5 * 1048576}
    return {"archive": {**arc, **kw}}


def test_archive_ok_within_twice_the_interval() -> None:
    assert wd.check_archive(body(last_write_age_seconds=599), True)[0] == wd.OK


def test_archive_warns_after_twice_the_interval() -> None:
    level, detail = wd.check_archive(body(last_write_age_seconds=601, errors=4), True)
    assert level == wd.WARN and "601s" in detail and "4 write errors" in detail


def test_archive_uses_the_collectors_own_interval() -> None:
    assert wd.check_archive(body(last_write_age_seconds=601, compact_every_s=600), True)[0] == wd.OK
    assert wd.check_archive(body(last_write_age_seconds=1201, compact_every_s=600), True)[0] == wd.WARN


@pytest.mark.parametrize("b", [None, {}, {"archive": None}, {"archive": {"enabled": False}}, body(enabled=False)])
def test_archive_not_enabled_or_not_reported_is_ok(b: Any) -> None:
    assert wd.check_archive(b, True)[0] == wd.OK


def test_archive_check_is_skipped_while_the_collector_is_unhealthy() -> None:
    assert wd.check_archive(body(last_write_age_seconds=99999), False)[0] == wd.OK


def test_migrate_grants_the_watchdog_role_the_session_tables() -> None:
    sh = (REPO / "ops" / "migrate" / "migrate.sh").read_text(encoding="utf-8")
    tables = re.search(r"GRANT SELECT ON %I TO watchdog_ro.*?ARRAY\[(.*?)\]", sh, re.S)
    assert tables
    names = set(re.findall(r"'(\w+)'", tables.group(1)))
    assert {"pilot_session", "controller_session"} <= names
    assert {"pilot_position", "flight_summary", "reference_data_change"} <= names  # added for the invariant sweep (issue 20; flight_summary is the finished-flights table since issue 89)
    assert not names & {"app_setting", "controller_rating_event"}  # nothing wider than the checks need


def test_check_backup_skips_a_dump_deleted_between_listing_and_stat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue 97: rotation can delete a dump after the watchdog listed it; that must not crash the check."""
    import os
    import time as _t
    good, gone = tmp_path / "a.dump", tmp_path / "b.dump"
    good.write_text("x")
    gone.write_text("x")
    real_stat = Path.stat

    def flaky(self: Path, *a: Any, **k: Any) -> Any:
        if self.name == "b.dump":
            raise FileNotFoundError(self.name)
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", flaky)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    level, detail = wd.check_backup(wd.Config(), _t.time())
    assert level == wd.OK and "newest backup" in detail
    assert os.path.exists(good)


# ---------------------------------------------------------------- backup_remote (issue 91)
def _remote_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enabled: bool = True) -> Any:
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    if enabled:
        monkeypatch.setenv("BACKUP_REMOTE_ENABLED", "1")
    else:
        monkeypatch.delenv("BACKUP_REMOTE_ENABLED", raising=False)
    return wd.Config()


def _stamp(hours_ago: float) -> str:
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_t.time() - hours_ago * 3600))


def test_backup_remote_is_inactive_without_the_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import time as _t
    level, detail = wd.check_backup_remote(_remote_cfg(monkeypatch, tmp_path, enabled=False), _t.time() - 99 * 3600)
    assert level == wd.OK and "not configured" in detail


@pytest.mark.parametrize("hours,level", [(1, "ok"), (25.5, "ok"), (27, "warn"), (200, "warn")])
def test_backup_remote_marker_age(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hours: float, level: str) -> None:
    import time as _t
    cfg = _remote_cfg(monkeypatch, tmp_path)
    (tmp_path / ".last_remote_success").write_text(_stamp(hours) + "\n")
    assert wd.check_backup_remote(cfg, _t.time())[0] == level


def test_backup_remote_missing_marker_waits_then_warns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import time as _t
    cfg = _remote_cfg(monkeypatch, tmp_path)
    assert wd.check_backup_remote(cfg, _t.time() - 3600)[0] == wd.OK
    assert wd.check_backup_remote(cfg, _t.time() - 30 * 3600)[0] == wd.WARN


def test_backup_remote_unreadable_content_falls_back_to_the_file_time(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import time as _t
    cfg = _remote_cfg(monkeypatch, tmp_path)
    (tmp_path / ".last_remote_success").write_text("garbage")
    assert wd.check_backup_remote(cfg, _t.time())[0] == wd.OK


def test_compose_passes_only_a_flag_never_the_remote_command() -> None:
    text = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    wd_block = text[text.index("  watchdog:"):]
    assert "BACKUP_REMOTE_ENABLED: ${BACKUP_REMOTE:+1}" in wd_block
    assert "BACKUP_REMOTE: " not in wd_block


def test_a_warn_line_from_the_weekly_restore_test_is_a_warning_not_critical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time as _t
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    cfg = wd.Config()
    stamp = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_t.time() - 3600))
    (tmp_path / ".last_restore_test").write_text(f"{stamp} WARN /backups/x.dump STALE poll_log=3 pilot_session=2: old\n")
    level, detail = wd.check_restore_test(cfg, _t.time())
    assert level == wd.WARN and "stale" in detail
    from importlib import import_module  # the plain wording for it
    sys.path.insert(0, str(REPO / "ops" / "watchdog"))
    messages = import_module("messages")
    assert "worked, but the newest data in the backup was old" in messages.humanise("restore_test", "warn", detail)
    (tmp_path / ".last_restore_test").write_text(f"{stamp} FAIL x\n")
    assert wd.check_restore_test(cfg, _t.time())[0] == wd.CRIT                    # a real failure is still critical


# --- marker-file characterisation (issue 216): the two checks that read a "<UTC timestamp> ..." marker must behave the same
# --- for every marker content before and after they share one reader and one timestamp parser.

def _marker(path: Path, content: str | None, mtime_ago_s: float) -> None:
    import os
    import time as _t
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.write_text(content)
    os.utime(path, (_t.time() - mtime_ago_s, _t.time() - mtime_ago_s))


def _ts(seconds_ago: float) -> str:
    import time as _t
    return _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_t.time() - seconds_ago))


H, D = 3600.0, 86400.0
REMOTE_CASES = [  # (content, marker mtime age s, expected level, expected detail)
    ("{t:2h} extra words\n", 0, "ok", "last off-box copy 2.0 h ago"),
    ("{t:100h}\n", 0, "warn", "last off-box copy 100.0 h ago (limit 26)"),
    ("{t:2h}", 50 * H, "ok", "last off-box copy 2.0 h ago"),            # timestamp alone is enough; the file time is ignored
    ("", 5 * H, "ok", "last off-box copy 5.0 h ago"),                    # empty: file time
    ("   \n", 40 * H, "warn", "last off-box copy 40.0 h ago (limit 26)"),
    ("garbage here", 7 * H, "ok", "last off-box copy 7.0 h ago"),        # malformed: file time
    ("2026-13-45T99:99:99Z ok", 8 * H, "ok", "last off-box copy 8.0 h ago"),
    ("{t:-3h} ok\n", 0, "ok", "last off-box copy -3.0 h ago"),           # future timestamp: negative age, not an alert
    (None, 0, "ok", "no off-box copy yet"),                              # missing, watchdog first ran 1 h ago
]


def _expand(content: str | None) -> str | None:
    if content is None:
        return None
    for tag, secs in (("{t:2h}", 2 * H), ("{t:100h}", 100 * H), ("{t:-3h}", -3 * H)):
        content = content.replace(tag, _ts(secs))
    return content


@pytest.mark.parametrize("content,mtime_ago,level,detail", REMOTE_CASES)
def test_backup_remote_marker_behaviour_is_pinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str | None,
                                                  mtime_ago: float, level: str, detail: str) -> None:
    import time as _t
    cfg = _remote_cfg(monkeypatch, tmp_path)
    _marker(tmp_path / ".last_remote_success", _expand(content), mtime_ago)
    assert wd.check_backup_remote(cfg, _t.time() - H) == (level, detail)


RESTORE_CASES = [
    ("{t:2d} OK /b/x.dump\n", 0, "ok", "last restore test OK 2.0 days ago"),
    ("{t:12d} OK x\n", 0, "warn", "last successful restore test 12.0 days ago (limit 9)"),
    ("{t:2d} FAIL x\n", 0, "crit", "last restore test FAILED 2.0 days ago"),
    ("{t:2d} WARN x\n", 0, "warn", "last restore test restored fine but the data in the dump was stale (2.0 days ago)"),
    ("{t:2d} BOGUS x\n", 0, "warn", "last restore test result unreadable"),
    ("{t:2d}\n", 0, "warn", "last restore test result unreadable"),     # one token: no verdict, so unreadable
    ("", 3 * D, "warn", "last restore test result unreadable"),
    ("garbage OK", 3 * D, "ok", "last restore test OK 3.0 days ago"),   # bad timestamp, valid verdict: file time
    ("garbage FAIL", 4 * D, "crit", "last restore test FAILED 4.0 days ago"),
    ("{t:-2d} OK x\n", 0, "ok", "last restore test OK -2.0 days ago"),  # future timestamp
    (None, 0, "ok", "no restore test yet"),
]


@pytest.mark.parametrize("content,mtime_ago,level,detail", RESTORE_CASES)
def test_restore_test_marker_behaviour_is_pinned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str | None,
                                                 mtime_ago: float, level: str, detail: str) -> None:
    import time as _t
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    cfg = wd.Config()
    if content is not None:
        for tag, secs in (("{t:2d}", 2 * D), ("{t:12d}", 12 * D), ("{t:-2d}", -2 * D)):
            content = content.replace(tag, _ts(secs))
    _marker(tmp_path / ".last_restore_test", content, mtime_ago)
    assert wd.check_restore_test(cfg, _t.time() - H) == (level, detail)


def test_restore_test_with_the_age_check_disabled_ignores_the_marker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import time as _t
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("WATCHDOG_RESTORE_TEST_MAX_DAYS", "0")
    (tmp_path / ".last_restore_test").write_text(f"{_ts(2 * D)} FAIL x\n")
    assert wd.check_restore_test(wd.Config(), _t.time()) == ("ok", "restore-test age check disabled")


@pytest.mark.parametrize("text,expected", [
    ("2026-10-08T04:08:27Z OK x", (datetime(2026, 10, 8, 4, 8, 27, tzinfo=UTC), "OK")),
    ("2026-10-08T04:08:27Z FAIL", (datetime(2026, 10, 8, 4, 8, 27, tzinfo=UTC), "FAIL")),
    ("2026-10-08T04:08:27Z WARN x", (datetime(2026, 10, 8, 4, 8, 27, tzinfo=UTC), "WARN")),
    ("2026-10-08T04:08:27Z nope", (datetime(2026, 10, 8, 4, 8, 27, tzinfo=UTC), "?")),
    ("2026-10-08T04:08:27Z", (None, "?")),
    ("bad OK", (None, "OK")),
    ("", (None, "?")),
])
def test_parse_restore_result_is_pinned(text: str, expected: tuple[datetime | None, str]) -> None:
    assert wd.parse_restore_result(text) == expected


def test_read_marker_returns_text_and_mtime_or_none(tmp_path: Path) -> None:
    """Fail-if-reverted (issue 216): the one reader both marker checks use."""
    import os
    f = tmp_path / ".marker"
    assert wd.read_marker(f) is None                                      # missing
    assert wd.read_marker(tmp_path) is None                               # unreadable (a directory): OSError, not a crash
    f.write_text("")
    os.utime(f, (1_700_000_000, 1_700_000_000))
    assert wd.read_marker(f) == ("", 1_700_000_000.0)                     # empty is a marker, not a miss
    f.write_text("2026-10-08T04:08:27Z OK\n")
    assert wd.read_marker(f) is not None and wd.read_marker(f)[0] == "2026-10-08T04:08:27Z OK\n"


@pytest.mark.parametrize("text,ok", [("2026-10-08T04:08:27Z", True), ("  2026-10-08T04:08:27Z OK x\n", True),
                                     ("", False), ("  \n", False), ("garbage", False), ("2026-13-45T99:99:99Z", False)])
def test_parse_marker_time(text: str, ok: bool) -> None:
    assert (wd.parse_marker_time(text) == datetime(2026, 10, 8, 4, 8, 27, tzinfo=UTC)) is ok
    assert ok or wd.parse_marker_time(text) is None
