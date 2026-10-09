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
