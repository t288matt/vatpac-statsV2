"""Issue 280: a look change never changes what a panel asks the database.

`tests/fixtures/dashboard_sql_baseline.json` holds, for every panel of every dashboard, a SHA-256 of its queries (`rawSql` and
`format` of each target) and its `timeFrom`, `timeShift`, `interval` and `maxDataPoints`, taken from `main` before the restyle of
each dashboard (panels nested in a collapsed row included). A restyle (type, options, fieldConfig, transformations, layout) leaves these
hashes alone. A query change needs the owner's explicit yes for that panel; the same pull request then regenerates the
baseline for it (`python tests/test_dashboards_sql_unchanged.py --write`) and says so.
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
    parts = [json.dumps([[t.get("rawSql"), t.get("format")] for t in panel.get("targets", [])])]
    parts += [str(panel.get(k)) for k in ("timeFrom", "timeShift", "interval", "maxDataPoints")]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def walk(panels: list[dict]) -> list[dict]:
    """Every panel, including those nested in a collapsed row."""
    return [q for p in panels for q in [p, *walk(p.get("panels", []))]]


def current() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for path in sorted(DASHBOARDS.glob("*/*.json")):
        panels = walk(json.loads(path.read_text(encoding="utf-8"))["panels"])
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


REMOVED = {  # owner-removed panels (title fragment, lower case): a panel with this title must not come back on any dashboard
    "departure board": "issues 305 and 313",
    "quietest home-region airports": "issue 314",
}


def test_owner_removed_panels_have_not_come_back() -> None:
    back = [f"{p['title']} ({f})" for f in sorted(DASHBOARDS.glob("*/*.json"))
            for p in walk(json.loads(f.read_text(encoding="utf-8"))["panels"])
            if any(frag in str(p.get("title", "")).lower() for frag in REMOVED)]
    assert back == [], "removed by the owner, see " + ", ".join(REMOVED.values()) + ": " + ", ".join(back)


def test_vfr_routes_panel_is_only_on_traffic_not_on_routes_and_airports() -> None:
    """Issue 317: the copy on Routes and airports was removed; the panel on Traffic and aircraft stays."""
    title = "Most popular VFR routes"

    def titles(name: str) -> list[str]:
        return [str(p.get("title")) for p in walk(json.loads((DASHBOARDS / "pilots" / name).read_text(encoding="utf-8"))["panels"])]
    assert [t for t in titles("pilots-routes-airports.json") if t.startswith(title)] == []
    assert [t for t in titles("pilots-traffic.json") if t.startswith(title)] == [title + " (last 30 days)"]


if __name__ == "__main__":
    if "--write" in sys.argv:
        BASELINE.parent.mkdir(exist_ok=True)
        BASELINE.write_text(json.dumps(current(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print("wrote", BASELINE)
