"""Issue 301: military graphs live on the Military dashboard only (no database needed).

A panel whose title or SQL mentions RAAFv or 'military aircraft' may appear only in military/military.json. "Highest groundspeeds
(last 7 days)" is allowed elsewhere: it is not a military graph, its query only leaves military flights out.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DASHBOARDS = Path(__file__).resolve().parents[1] / "grafana" / "dashboards"
MILITARY = re.compile(r"raafv|military aircraft", re.IGNORECASE)
ALLOWED_ELSEWHERE = {"Highest groundspeeds (last 7 days)"}


def walk(panels: list[dict]) -> list[dict]:
    """Every panel, including those nested in a collapsed row."""
    return [q for p in panels for q in [p, *walk(p.get("panels", []))]]


def is_military(panel: dict) -> bool:
    text = (panel.get("title") or "") + "\n" + "\n".join(str(t.get("rawSql", "")) for t in panel.get("targets", []))
    return bool(MILITARY.search(text))


def test_military_panels_appear_only_on_the_military_dashboard() -> None:
    stray = []
    for path in sorted(DASHBOARDS.glob("*/*.json")):
        if path.parent.name == "military":
            continue
        for p in walk(json.loads(path.read_text(encoding="utf-8"))["panels"]):
            if is_military(p) and p.get("title") not in ALLOWED_ELSEWHERE:
                stray.append(f"{path.parent.name}/{path.name}: {p.get('title')}")
    assert not stray, "military panels belong on military/military.json only: " + "; ".join(stray)


def test_the_check_sees_the_military_dashboard_panels() -> None:
    panels = walk(json.loads((DASHBOARDS / "military" / "military.json").read_text(encoding="utf-8"))["panels"])
    titles = [p["title"] for p in panels if is_military(p)]
    assert "Most popular military aircraft (unique pilots, last 7 days)" in titles
    assert "Most popular RAAFv routes (last month)" in titles
    assert len(titles) >= 7
