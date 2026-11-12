"""Issues 200 and 238: the airborne gap cap and the flight quiet hours each have ONE source, the app_setting row.

Pure text and pure function tests (no database): nothing in SQL, shell or watchdog code carries its own number for either setting, the
alert wording is built from the setting the check reported, and the watchdog role is granted exactly the two read-only views. The
behaviour against a database (missing row raises, the invariant follows a changed setting) is in tests/integration/test_settings_single_source.py."""
from __future__ import annotations

import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ops" / "watchdog"))
sys.path.insert(0, str(REPO / "ops" / "audit"))
import integrity  # noqa: E402
import messages as m  # noqa: E402

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
QUIET = "flights quiet longer than the quiet period with unprocessed positions"
NEW = REPO / "db" / "migrations" / "20261112000010_settings_single_source.sql"


def read(*parts: str) -> str:
    return (REPO.joinpath(*parts)).read_text(encoding="utf-8")


def code_only(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def test_the_migration_replaces_exactly_the_two_fallback_expressions_of_the_live_writer() -> None:
    """The writer is re-created from the catalogue text (pg_get_functiondef), so every other line stays as deployed; the migration holds no copy."""
    sql = NEW.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION write_finished_flights" not in sql and "pg_get_functiondef('public.write_finished_flights(integer)'" in sql
    pairs = re.findall(r"(old_[qg]) text := \$a\$(.*?)\$a\$;\s+(new_[qg]) text := \$a\$(.*?)\$a\$;", sql, re.S)
    assert [(p[1], p[3]) for p in pairs] == [
        ("COALESCE((SELECT value::numeric FROM app_setting WHERE key = 'flight_quiet_hours'), 8)", "setting_num('flight_quiet_hours')"),
        ("COALESCE((SELECT value::numeric FROM app_setting WHERE key = 'airborne_gap_cap_seconds'), 40)", "setting_num('airborne_gap_cap_seconds')"),
    ]
    previous = read("db", "migrations", "20261012000200_flight_quiet_per_flight.sql")
    for _, old, _, _ in pairs:
        assert previous.count(old) == 1  # the text that is replaced is the text of the deployed writer, once each
    assert "must occur exactly once" in sql or "exactly once" in sql


def test_setting_num_raises_on_a_missing_or_bad_row_and_is_not_executable_by_other_roles() -> None:
    sql = code_only(NEW.read_text(encoding="utf-8"))
    fn = sql[sql.index("CREATE FUNCTION setting_num"):sql.index("$f$;", sql.index("$f$\n"))]
    assert "IF NOT FOUND THEN" in fn and "RAISE EXCEPTION" in fn and "COALESCE" not in fn and "RETURN n;" in fn
    assert "REVOKE ALL ON FUNCTION setting_num(text) FROM PUBLIC;" in sql and "REVOKE ALL ON FUNCTION ops_setting_num(text) FROM PUBLIC;" in sql
    wrapper = sql[sql.index("CREATE FUNCTION ops_setting_num"):sql.index("$f$;", sql.index("CREATE FUNCTION ops_setting_num"))]
    assert "SECURITY DEFINER" in wrapper and "'flight_quiet_hours', 'airborne_gap_cap_seconds'" in wrapper and "RETURN setting_num(p_key)" in wrapper
    assert "SECURITY DEFINER" not in fn
    assert not re.search(r"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+setting_num", sql, re.I)


def test_the_audit_pack_has_no_copy_of_the_pending_flight_rule_and_no_quiet_number() -> None:
    sql = code_only(read("ops", "audit", "invariants.sql"))
    assert "ops_pending_flight" in sql and "ops_setting" in sql
    assert "9 hours" not in sql and "app_setting" not in sql
    assert not re.search(r"interval '\d+(\.\d+)? hours?'", sql)  # no hour figure at all: the quiet period is the setting
    assert "UNBOUNDED PRECEDING AND 1 PRECEDING" not in sql and "flown_upto" in sql  # the grouping lives in the view only


def test_backfill_flights_reads_the_shared_definition_and_carries_no_number() -> None:
    sh = read("ops", "backfill_flights.sh")
    assert "FROM ops_pending_flight WHERE is_quiet" in sh
    for banned in ("COALESCE", "app_setting", "flight_quiet_hours), 8", "UNBOUNDED PRECEDING", "make_interval"):
        assert banned not in sh, banned


def test_no_watchdog_code_or_alert_wording_quotes_nine_hours() -> None:
    for f in ("watchdog.py", "messages.py", "integrity.py"):
        text = read("ops", "watchdog", f)
        assert not re.search(r"nine hours|9 hours|plus 1 hour", text, re.I), f


def test_the_only_other_sql_sources_of_the_two_numbers_are_the_applied_migrations() -> None:
    """Every place outside db/migrations that reads one of the two settings goes through setting_num or the ops views."""
    for path in [REPO / "ops" / "audit" / "invariants.sql", REPO / "ops" / "backfill_flights.sh", REPO / "ops" / "soak_report.sql"]:
        text = code_only(path.read_text(encoding="utf-8")) if path.suffix == ".sql" else path.read_text(encoding="utf-8")
        assert not re.search(r"(flight_quiet_hours|airborne_gap_cap_seconds)'\)\s*,\s*\d", text), path.name


# ------------------------------------------------------------------ grants
def watchdog_section() -> str:
    """The watchdog_ro section of db/roles.sql (the last one in the file)."""
    return read("db", "roles.sql").split("\n-- watchdog_ro\n")[-1]


def test_the_watchdog_role_is_granted_the_two_views_and_nothing_wider() -> None:
    sh = watchdog_section()
    block = re.search(r"GRANT SELECT ON %I TO watchdog_ro.*?ARRAY\[(.*?)\]", sh, re.S)
    assert block
    names = set(re.findall(r"'(\w+)'", block.group(1)))
    assert {"ops_setting", "ops_pending_flight"} <= names
    assert not names & {"app_setting", "controller_rating_event"}
    assert {n for n in names if n.startswith("ops_")} == {"ops_setting", "ops_pending_flight"}
    assert re.findall(r"GRANT EXECUTE ON FUNCTION", sh) == ["GRANT EXECUTE ON FUNCTION"]  # one grant, and it is the wrapper below
    assert re.search(r"unnest\(ARRAY\['ops_setting_num\(text\)'\]\)", sh) and "'setting_num" not in sh


def test_grafana_ro_is_not_granted_the_ops_views() -> None:
    roles = read("db", "roles.sql")
    assert "left(viewname, 4) <> 'ops_'" in roles
    other_roles = roles.split("\n-- watchdog_ro\n")[0]  # the grafana_ro, collector and backup_ro sections: the ops_* views are for the watchdog only
    assert "ops_" not in re.sub(r"--[^\n]*", "", other_roles).replace("left(viewname, 4) <> 'ops_'", "")
    assert "TO grafana_ro" not in watchdog_section()


# ------------------------------------------------------------------ the alert wording follows the setting
def tok(hours: str, grace: str) -> str:
    return f"flight_quiet_hours={hours} flight_quiet_check_grace_minutes={grace}"


def _text(token: str | None) -> str:
    finding = integrity.Finding(QUIET, 2, token)
    res = integrity.summarise_invariants([finding], [], 20)
    return m.humanise("data_integrity", "warn", res[1], now=NOW, facts=integrity.facts_of(res))


@pytest.mark.parametrize("hours,grace,words", [("8", "30", "8 hours 30 minutes"), ("8", "0", "8 hours"), ("6", "30", "6 hours 30 minutes"),
                                               ("12", "90", "13 hours 30 minutes"), ("2.5", "30", "3 hours"), ("1", "30", "1 hour 30 minutes")])
def test_the_alert_wording_is_built_from_both_settings(hours: str, grace: str, words: str) -> None:
    t = _text(tok(hours, grace))
    assert f"ended over {words} ago" in t and "nine" not in t.lower() and "9 hours" not in t
    assert m.invariant_words(QUIET, (hours, grace)) in t


def test_the_alert_wording_names_no_number_when_the_settings_were_not_reported() -> None:
    t = _text(None)
    assert "longer ago than the quiet period" in t and not re.search(r"over \d", t.splitlines()[0])


def test_the_settings_travel_in_the_sweep_detail_and_in_the_new_violation_notice() -> None:
    finding = integrity.Finding(QUIET, 1, tok("8", "30"))
    _, detail = integrity.summarise_invariants([finding, integrity.Finding("daily partitions missing for today..today+3", 1)], [], 20)
    assert f"[{tok('8', '30')}]" in detail
    _, other = integrity.summarise_invariants([integrity.Finding(QUIET, 1, "not a setting")], [], 20)
    assert "flight_quiet_hours" not in other  # only a well-formed token is carried
    _, half = integrity.summarise_invariants([integrity.Finding(QUIET, 1, "flight_quiet_hours=8")], [], 20)
    assert "flight_quiet_hours" not in half
    assert integrity.facts_of(integrity.summarise_invariants([finding], [], 20))["quiet"] == ["8", "30"]
    notice = m.humanise("data_integrity", "warn", f"new violations since the last sweep: {QUIET} [{tok('6', '45')}]", now=NOW,
                        facts={"state": "new", "names": [QUIET], "quiet": ["6", "45"]})
    assert "ended over 6 hours 45 minutes ago" in notice


def test_the_grace_is_a_setting_row_seeded_by_the_migration_and_not_a_typed_number() -> None:
    sql = NEW.read_text(encoding="utf-8")
    assert "('flight_quiet_check_grace_minutes', '30') ON CONFLICT (key) DO NOTHING" in sql
    inv = code_only(read("ops", "audit", "invariants.sql"))
    assert "is_overdue" in inv and not re.search(r"interval '\d+ (minutes?|hours?)'", inv.split("flights written in the last 3 days")[0].split("'daily")[0][-900:])
