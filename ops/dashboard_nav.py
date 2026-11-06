#!/usr/bin/env python3
"""The navigation strip at the top of every provisioned Grafana dashboard (issue 280).

Public dashboards cannot use variables or a menu, so the first line of each dashboard's top text panel (panel id 1) is a
line of Markdown links to the other dashboards, with the current page in bold and not linked. The dashboards and their
short addresses come from SHORT_NAMES in ops/grafana_public.py (the one list); the grouping and the display names are in
GROUPS below. To add a dashboard: add it to SHORT_NAMES, add it to a group here, then run `python ops/dashboard_nav.py`.

    python ops/dashboard_nav.py            rewrite the strip in every dashboard file (idempotent)
    python ops/dashboard_nav.py --check    change nothing; exit 1 if any strip is missing or out of step (run in CI)

Links are bare short paths such as /live with NO query string: the Traefik redirect rule does not match one. Standard library only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # grafana_public imports env_file from this folder
from grafana_public import SHORT_NAMES  # noqa: E402

DASHBOARDS = Path(__file__).resolve().parents[1] / "grafana" / "dashboards"
NAV_ROWS = 3  # rows the strip adds to the top text panel (a banner-only panel is 2 or 3 rows tall, so 5 or 6 with the strip)

# group label -> [(short address, display name)]; the short address must be a value in SHORT_NAMES
GROUPS: dict[str, list[tuple[str, str]]] = {
    "Pilots": [("live", "Live now"), ("traffic", "Traffic"), ("new-pilots", "New pilots"), ("origins", "Origins"),
               ("pilots", "Records"), ("routes", "Routes"), ("aircraft", "Aircraft"), ("military", "Military")],
    "ATC": [("atc", "Overview"), ("atc-retention", "Retention"), ("atc-hours", "Hours"), ("atc-positions", "Positions"),
            ("atc-ranks", "Ranks")],
    "Events": [("live-events", "Live events"), ("monday", "Monday events")],
    "Management": [("monthly", "Monthly"), ("health", "Health")],
}


def strip(uid: str) -> str:
    """The strip for one dashboard: group labels in plain text, links to the others, the current page bold."""
    here = SHORT_NAMES[uid]
    parts = []
    for group, items in GROUPS.items():
        links = [f"**{name}**" if path == here else f"[{name}](/{path})" for path, name in items]
        parts.append(f"{group}: " + " · ".join(links))
    return " | ".join(parts)


def split(content: str) -> tuple[str, str]:
    """(first paragraph, the rest) of a text panel's Markdown."""
    head, _, rest = content.partition("\n\n")
    return head, rest


def problems() -> list[str]:
    """Every way the strips differ from what strip() builds, one message per dashboard file."""
    out = []
    listed = [path for items in GROUPS.values() for path, _ in items]
    if sorted(listed) != sorted(SHORT_NAMES.values()) or len(set(listed)) != len(listed):
        out.append("GROUPS and SHORT_NAMES do not list the same short addresses (each once)")
    for f in sorted(DASHBOARDS.glob("*/*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        panel = next((p for p in d["panels"] if p.get("id") == 1 and p.get("type") == "text"), None)
        if panel is None or d["uid"] not in SHORT_NAMES:
            out.append(f"{f.name}: no top text panel (id 1) or no short name")
        elif split(panel["options"]["content"])[0] != strip(d["uid"]):
            out.append(f"{f.name}: navigation strip missing or out of step; run python ops/dashboard_nav.py")
        elif panel["gridPos"]["h"] < 2 + NAV_ROWS:
            out.append(f"{f.name}: top text panel too short for the strip (scrolls)")
    return out


def apply() -> int:
    """Put the strip in every dashboard. A file that has none gets it plus NAV_ROWS rows of height, and the panels below move down."""
    for f in sorted(DASHBOARDS.glob("*/*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        panel = next(p for p in d["panels"] if p.get("id") == 1 and p.get("type") == "text")
        content = panel["options"]["content"]
        head, rest = split(content)
        if head.startswith("Pilots: "):
            panel["options"]["content"] = strip(d["uid"]) + "\n\n" + rest
        else:
            panel["options"]["content"] = strip(d["uid"]) + "\n\n" + content
            for p in d["panels"]:
                p["gridPos"]["y" if p is not panel else "h"] += NAV_ROWS
        f.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return 0


def main(argv: list[str]) -> int:
    if argv == ["--check"]:
        found = problems()
        print("\n".join(found) if found else "navigation strips ok")
        return 1 if found else 0
    return apply() if not argv else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
