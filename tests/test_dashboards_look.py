"""Issue 280: the restyled dashboards follow the shared look (grafana/look_rules.py), and the rules catch a panel that does not."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "grafana"))
import look_rules as lr  # noqa: E402

DASHBOARDS = REPO / "grafana" / "dashboards"


def test_every_restyled_dashboard_exists_and_follows_the_look() -> None:
    for rel in sorted(lr.RESTYLED):
        dash = json.loads((DASHBOARDS / rel).read_text(encoding="utf-8"))
        assert lr.check_dashboard(rel, dash) == [], rel


def test_the_rules_catch_a_stray_colour_a_bare_table_and_a_trend_panel() -> None:
    stray = {"type": "stat", "fieldConfig": {"defaults": {"color": {"mode": "fixed", "fixedColor": "#FF0000"}}}}
    assert any("not in the fixed palette" in e for e in lr.check_panel("x", stray))
    assert any("cellHeight" in e for e in lr.check_panel("x", {"type": "table", "options": {}, "fieldConfig": {}}))
    assert any("trend" in e for e in lr.check_panel("x", {"type": "trend"}))
    assert any("legend table" in e for e in lr.check_panel("x", {"type": "timeseries", "options": {}, "fieldConfig": {}}))
    no_window = {"type": "stat", "options": {"showPercentChange": True}, "fieldConfig": {}}
    assert any("change arrow" in e for e in lr.check_panel("x", no_window))


def test_named_and_short_or_alpha_colours_are_caught() -> None:
    for bad in ("red", "#f00", "#3AA0DD80"):
        panel = {"type": "stat", "fieldConfig": {"defaults": {"color": {"mode": "fixed", "fixedColor": bad}}}}
        assert any("not in the fixed palette" in e for e in lr.check_panel("x", panel)), bad
    steps = {"type": "stat", "fieldConfig": {"defaults": {"thresholds": {"steps": [{"color": "green", "value": None}]}}}}
    assert any("not in the fixed palette" in e for e in lr.check_panel("x", steps))


def test_a_dashboard_that_is_not_restyled_yet_is_not_checked() -> None:
    assert lr.check_dashboard("atc/atc-overview.json", {"panels": [{"type": "trend"}]}) == []
