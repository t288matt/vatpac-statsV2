"""Issue #50: the Test column of docs/CHART_DEFINITIONS.md must not claim tests that do not exist.

Every query panel in grafana/dashboards has a row; every row says which tests assert it ("Asserted by"); every test
function named in the document exists in the test tree. Renaming or deleting a test without updating the document fails here.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = (REPO / "docs" / "CHART_DEFINITIONS.md").read_text(encoding="utf-8")
PANEL_CELL = re.compile(r"(?:^|\| )([a-z\-]+/[a-z\-]+\.json): (.+?) \|")


def all_panels() -> set[tuple[str, str]]:
    def walk(panels):
        for p in panels:
            yield p
            yield from walk(p.get("panels", []))

    out = set()
    for path in sorted((REPO / "grafana" / "dashboards").rglob("*.json")):
        for p in walk(json.loads(path.read_text(encoding="utf-8"))["panels"]):
            if p.get("targets") and "rawSql" in p["targets"][0]:
                out.add((path.relative_to(REPO / "grafana" / "dashboards").as_posix(), p["title"]))
    return out


def test_every_panel_has_a_row_with_named_tests() -> None:
    rows = {}
    for line in DOC.split("\n"):
        m = PANEL_CELL.search(line) if line.startswith("|") else None
        if m:
            rows[(m.group(1), m.group(2).strip())] = line
    panels = all_panels()
    assert panels - set(rows) == set(), f"panels with no row in CHART_DEFINITIONS.md: {sorted(panels - set(rows))}"
    without = [k for k, line in rows.items() if "Asserted by" not in line.rsplit(" | ", 1)[-1]]
    assert without == [], f"rows whose Test column names no test: {without}"


def test_every_test_named_in_the_document_exists() -> None:
    have: set[str] = set()
    for root in ("tests", "collector/tests"):
        for f in (REPO / root).rglob("test_*.py"):
            have |= {n.name for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))) if isinstance(n, ast.FunctionDef)}
    named = set(re.findall(r"`(test_[a-z0-9_]+)`", DOC))
    assert len(named) > 80
    assert sorted(named - have) == []
