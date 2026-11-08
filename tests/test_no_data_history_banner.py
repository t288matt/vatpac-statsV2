"""Issue 307: the data-history banner ("Live data since ...; earlier history was imported") must not return on any dashboard.

Every dashboard except Collector health carries the one customer sentence instead. No database needed.
"""
from __future__ import annotations

import json
from pathlib import Path

DASHBOARDS = Path(__file__).resolve().parents[1] / "grafana" / "dashboards"
OLD = ("Live data since", "earlier history was imported", "come from the previous system", "show current data only")
NEW = "All times are UTC. Figures cover VATPAC airspace."


def text_of(path: Path) -> list[str]:
    return [p.get("options", {}).get("content", "") for p in json.loads(path.read_text(encoding="utf-8"))["panels"] if p.get("type") == "text"]


def test_the_old_banner_is_on_no_dashboard() -> None:
    bad = [f"{p.parent.name}/{p.name}" for p in sorted(DASHBOARDS.glob("*/*.json")) if any(o in t for t in text_of(p) for o in OLD)]
    assert not bad, "old data-history banner is back on: " + ", ".join(bad)


def test_sixteen_dashboards_carry_the_new_sentence_and_the_first_panel_is_the_navigation() -> None:
    paths = [p for p in sorted(DASHBOARDS.glob("*/*.json")) if p.parent.name != "monitoring"]
    assert len(paths) == 16
    for p in paths:
        first = json.loads(p.read_text(encoding="utf-8"))["panels"][0]
        assert first["type"] == "text" and NEW in first["options"]["content"] and 'target="_top"' in first["options"]["content"], p.name
