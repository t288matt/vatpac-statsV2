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
    """Answers the three flap queries in order: pilot pairs, controller pairs, fractional count."""

    def __init__(self, pilot: int = 0, ctrl: int = 0, frac: int = 0, fail: Exception | None = None) -> None:
        self.rows = [(pilot, T if pilot else None), (ctrl, T if ctrl else None), (frac,)]
        self.sql: list[str] = []
        self.fail = fail

    def execute(self, sql: str, params: Any = None) -> None:
        if self.fail:
            raise self.fail
        self.sql.append(sql)

    def fetchone(self) -> tuple[Any, ...]:
        return self.rows[len(self.sql) - 1]


def test_flap_ok_when_nothing_is_found() -> None:
    level, detail = wd.check_identity_flap(Cur())
    assert level == wd.OK and "24 h" in detail


@pytest.mark.parametrize("cur,words", [
    (Cur(pilot=31, ctrl=3), ["31 pilot", "3 controller", "08-10-2026 04:08:27 UTC"]),
    (Cur(ctrl=1), ["0 pilot", "1 controller"]),
    (Cur(frac=2), ["2 fractional"]),
])
def test_flap_is_a_warning_with_the_counts(cur: Cur, words: list[str]) -> None:
    level, detail = wd.check_identity_flap(cur)
    assert level == wd.WARN
    for w in words:
        assert w in detail
    assert "check_identity_flap.sql" in detail


def test_flap_query_failure_is_a_warning_not_a_crash() -> None:
    import psycopg

    level, detail = wd.check_identity_flap(Cur(fail=psycopg.errors.InsufficientPrivilege("denied")))
    assert level == wd.WARN and "InsufficientPrivilege" in detail and "denied" not in detail


def test_flap_sql_matches_the_ops_script_logic() -> None:
    """The watchdog query and ops/check_identity_flap.sql must apply the same rule (cid, callsign, 2 s, fraction)."""
    script = (REPO / "ops" / "check_identity_flap.sql").read_text(encoding="utf-8")
    assert "a.cid = b.cid AND a.callsign = b.callsign" in script and "< 2" in script
    pairs = wd._PAIRS_SQL.format(t="pilot_session")
    assert "a.cid = b.cid AND a.callsign = b.callsign" in pairs and "2 seconds" in pairs
    assert "logon_time <> date_trunc('second', logon_time)" in script
    assert "logon_time <> date_trunc('second', logon_time)" in wd._FRACTION_SQL


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
    assert {"pilot_position", "flight_summary", "reference_data_change"} <= names  # added for the invariant sweep (issue 20)
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
