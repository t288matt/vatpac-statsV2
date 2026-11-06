"""Issue 280: a look change never changes what a panel asks the database.

`tests/fixtures/dashboard_sql_baseline.json` holds, for every panel of every dashboard, a SHA-256 of its queries (`rawSql` and
`format` of each target) and its `timeFrom`, taken from `main` before the restyle of each dashboard. A restyle (type, options,
fieldConfig, transformations, layout) leaves these hashes alone. A query change needs the owner's explicit yes for that panel;
the same pull request then regenerates the baseline for it (`python tests/test_dashboards_sql_unchanged.py --write`) and says so.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DASHBOARDS = REPO / "grafana" / "dashboards"
BASELINE = REPO / "tests" / "fixtures" / "dashboard_sql_baseline.json"


def digest(panel: dict) -> str:
    parts = [json.dumps([[t.get("rawSql"), t.get("format")] for t in panel.get("targets", [])]), str(panel.get("timeFrom"))]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def current() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for path in sorted(DASHBOARDS.glob("*/*.json")):
        panels = json.loads(path.read_text(encoding="utf-8"))["panels"]
        out[f"{path.parent.name}/{path.name}"] = {str(p["id"]): digest(p) for p in panels if p.get("targets")}
    return out


def test_every_panel_still_runs_its_baseline_query_and_window() -> None:
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    now = current()
    changed = [f"{f} panel {i}" for f, panels in base.items() for i, h in panels.items() if now.get(f, {}).get(i) != h]
    assert not changed, "query, format or timeFrom changed (needs the owner's yes, then regenerate the baseline): " + ", ".join(changed)


def test_no_panel_without_a_baseline_entry() -> None:
    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    now = current()
    new = [f"{f} panel {i}" for f, panels in now.items() for i in panels if i not in base.get(f, {})]
    assert not new, "panel has no baseline entry (add it with --write in the same pull request): " + ", ".join(new)


if __name__ == "__main__":
    if "--write" in sys.argv:
        BASELINE.parent.mkdir(exist_ok=True)
        BASELINE.write_text(json.dumps(current(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print("wrote", BASELINE)
