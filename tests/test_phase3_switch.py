"""Issue #89 phase 3 (#139, not yet deployed): static guards for the switch of the flight readers. No database.

The requirement-to-test table is docs/CONFORMANCE_89_PHASE3.md. What is checked here without PostgreSQL:
  * no panel carries an in-progress filter any more, and the eleven panels that read `flight` directly still exist;
  * no panel reads the pieces, the old merging view, flight_record or the raw positions (panels read views only);
  * the validator refuses a panel that reads flight_legacy or flight_part;
  * the migration only renames the old view, creates views and comments: it drops nothing and touches no table.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DASH = REPO / "grafana" / "dashboards"
MIGRATION = REPO / "db" / "migrations" / "20261013000200_switch_flight_views.sql"
sys.path.insert(0, str(REPO / "grafana"))
import validate_dashboards as v  # noqa: E402

DIRECT_FLIGHT = re.compile(r"\b(?:FROM|JOIN)\s+flight\b(?!_)", re.IGNORECASE)
LIVE = re.compile(r"\b(?:pilot_latest|pilot_online_now|online_now|network_history)\b")
NEVER = {"flight_legacy", "flight_part", "flight_summary", "flight_record", "pilot_position", "pilot_session"}


def _panels():
    for path in sorted(DASH.rglob("*.json")):
        for p in json.loads(path.read_text(encoding="utf-8")).get("panels", []):
            for t in p.get("targets", []):
                if t.get("rawSql"):
                    yield f"{path.name}: {p.get('title')}", t["rawSql"]


def test_no_panel_carries_an_in_progress_filter_or_a_piece_column() -> None:
    bad = [n for n, s in _panels() if re.search(r"\b(is_in_progress|gap_segment|span_part)\b", s)]
    assert bad == [], bad


def test_the_eleven_panels_that_read_flight_directly_still_exist_and_are_not_filtered_on_progress() -> None:
    direct = [(n, s) for n, s in _panels() if DIRECT_FLIGHT.search(s)]
    assert len(direct) == 11, [n for n, _ in direct]          # the panels listed in docs/CHART_DEFINITIONS.md
    assert [n for n, s in direct if "in_progress" in s] == []


def test_the_removed_filters_left_valid_sql_behind() -> None:
    """The edit removed 'WHERE NOT is_in_progress' and re-attached the next condition: no panel may be left with a dangling
    WHERE/AND or an empty WHERE."""
    for name, sql in _panels():
        flat = re.sub(r"--[^\n]*", " ", sql)
        assert not re.search(r"(?i)\bwhere\s+and\b", flat), name
        assert not re.search(r"(?i)\bwhere\s*(\)|group\b|order\b|$)", flat), name


def test_no_panel_reads_the_pieces_the_legacy_view_flight_record_or_the_raw_positions() -> None:
    reads = {name: v.relations(sql) & NEVER for name, sql in _panels()}
    assert {n: r for n, r in reads.items() if r} == {}


def test_live_panels_do_not_filter_on_in_progress() -> None:
    live = [(n, s) for n, s in _panels() if LIVE.search(s)]
    assert live
    assert [n for n, s in live if "is_in_progress" in s] == []


def test_the_validator_refuses_a_panel_that_reads_the_legacy_or_piece_views() -> None:
    assert {"flight_legacy", "flight_part"} <= set(v.NEVER_FOR_PANELS)
    assert v.relations("SELECT 1 FROM flight_legacy f") == {"flight_legacy"}
    assert v.relations("SELECT 1 FROM flight f JOIN flight_part p ON p.session_id = f.session_id") == {"flight", "flight_part"}
    # and the panels as committed pass it
    assert all(v.relations(s).isdisjoint(v.NEVER_FOR_PANELS) for _, s in _panels())


def _up() -> str:
    return MIGRATION.read_text(encoding="utf-8").split("-- migrate:down")[0]


def test_the_migration_drops_nothing_and_touches_no_table() -> None:
    sql = "\n".join(line for line in _up().splitlines() if not line.lstrip().startswith("--"))
    assert not re.search(r"(?i)\bDROP\b", sql)
    assert not re.search(r"(?i)\b(INSERT|UPDATE|DELETE|TRUNCATE)\b", sql)
    assert not re.search(r"(?i)\b(CREATE|ALTER)\s+(TABLE|FUNCTION|INDEX)\b", sql)
    renames = re.findall(r"(?i)\bALTER\s+(\w+)\s+(\w+)\s+RENAME\s+TO\s+(\w+)", sql)
    assert renames == [("VIEW", "flight", "flight_legacy")]
    created = re.findall(r"(?im)^\s*CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(\w+)", sql)
    assert created == ["flight", "flight_ext", "monday_attendance"]


def test_the_migration_is_the_newest_and_forward_only() -> None:
    names = sorted(p.name for p in (REPO / "db" / "migrations").glob("*.sql"))
    assert names.index(MIGRATION.name) > names.index("20261012000190_flight_record_writer.sql")
    assert "Forward-only" in MIGRATION.read_text(encoding="utf-8").split("-- migrate:down")[1]
    assert "SET LOCAL lock_timeout = '5s'" in _up()                 # design section 4: fail fast instead of queueing behind a panel query


def test_the_new_flight_view_reads_only_flight_record_and_the_home_prefix_setting() -> None:
    body = _up().split("CREATE VIEW flight AS", 1)[1].split("COMMENT ON VIEW flight IS", 1)[0]
    flat = re.sub(r"(?i)extract\s*\(\s*\w+\s+from", "extract(x,", re.sub(r"--[^\n]*", " ", body))   # EXTRACT(epoch FROM x) is no relation
    relations = {m.group(1).lower() for m in re.finditer(r"(?i)\b(?:from|join)\s+([a-z_]\w*)", flat)}
    assert relations - {"unnest"} == {"app_setting", "flight_record", "cfg"}
    for column in ("gap_segment", "span_part", "is_in_progress"):
        assert column not in flat


def test_flight_ext_has_no_in_progress_filter() -> None:
    body = _up().split("CREATE OR REPLACE VIEW flight_ext AS", 1)[1].split("COMMENT ON VIEW flight_ext", 1)[0]
    assert "in_progress" not in body and not re.search(r"(?im)^\s*WHERE\b", body)
