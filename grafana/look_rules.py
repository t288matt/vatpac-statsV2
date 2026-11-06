"""Issue 280: the shared look of the public dashboards, checked in `grafana/validate_dashboards.py`.

Only the dashboards in RESTYLED are checked; a batch adds its dashboards here when it restyles them, so the look cannot drift
afterwards. The rules are about type, options and field settings. They never look at a query (that is
`tests/test_dashboards_sql_unchanged.py`). The conventions in words: `docs/RUNBOOKS/dashboards.md`, "Look conventions".
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

# The fixed colours (colour-blind safe, legible on the dark and the light theme), taken from "Pilots: online now".
BLUE, AMBER, GREEN, PINK, GREY = "#3AA0DD", "#E69F00", "#1FAF8A", "#CC79A7", "#8A97B2"
PALETTE = {BLUE, AMBER, GREEN, PINK, GREY}

# Dashboards that have the new look (path under grafana/dashboards). Add a batch's files here in the same pull request.
RESTYLED = {
    "pilots/pilots-now.json",
    "pilots/pilots-traffic.json",
    "pilots/pilots-routes-airports.json",
    "pilots/pilots-aircraft-airlines.json",
    "pilots/pilots-records.json",
}

# Panels whose colours come from Grafana's classic palette because the number of slices or series is not known when the
# dashboard is written (a donut has one colour per slice). Every other colour must be one of PALETTE.
CLASSIC_PALETTE_TYPES = {"piechart"}


def _colours(node: Any) -> list[str]:
    """Every fixed colour (#rrggbb) used anywhere below ``node``."""
    return [s for s in _strings(node) if len(s) == 7 and s.startswith("#")]


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def check_panel(label: str, p: dict[str, Any]) -> list[str]:
    """Problems with one panel's look (empty list when it follows the conventions)."""
    out: list[str] = []
    kind = p.get("type")
    if kind == "text":
        return out
    opts, fc = p.get("options", {}), p.get("fieldConfig", {})
    defaults = fc.get("defaults", {})
    for c in _colours({"d": defaults, "o": fc.get("overrides", [])}):
        if c.upper() not in {x.upper() for x in PALETTE}:
            out.append(f"{label}: colour {c} is not in the fixed palette")
    mode = defaults.get("color", {}).get("mode")
    if kind in CLASSIC_PALETTE_TYPES:
        if mode != "palette-classic":
            out.append(f"{label}: a donut uses color mode palette-classic")
        if opts.get("pieType") != "donut" or opts.get("legend", {}).get("displayMode") != "table":
            out.append(f"{label}: a donut is a donut with a legend table")
    elif mode == "palette-classic":
        out.append(f"{label}: only donuts use the classic palette")
    if kind == "table":
        if opts.get("cellHeight") != "md":
            out.append(f"{label}: table cellHeight must be 'md'")
        for o in fc.get("overrides", []):
            for prop in o.get("properties", []):
                cell = prop.get("value") if prop.get("id") == "custom.cellOptions" else None
                if not (isinstance(cell, dict) and cell.get("type") == "gauge"):
                    continue
                if cell.get("mode") != "basic" or cell.get("valueDisplayMode") != "text":
                    out.append(f"{label}: in-cell bars use mode 'basic' with the value shown as text")
    if kind == "timeseries":
        if opts.get("legend", {}).get("displayMode") != "table":
            out.append(f"{label}: a time series has a legend table")
        if defaults.get("custom", {}).get("gradientMode") != "opacity":
            out.append(f"{label}: a time series uses the opacity gradient")
    if kind == "barchart" and opts.get("showValue") == "never" and not opts.get("xField", "").lower().startswith("hour"):
        out.append(f"{label}: ranked bars show their values")
    if kind == "stat" and opts.get("showPercentChange") and p.get("timeFrom") is None:
        out.append(f"{label}: a change arrow needs a time window to compare against")
    if kind == "trend":
        out.append(f"{label}: the 'trend' panel type is replaced by the bar chart used for hours online")
    return out


def check_dashboard(rel: str, dash: dict[str, Any]) -> list[str]:
    if rel not in RESTYLED:
        return []
    out: list[str] = []
    for p in dash.get("panels", []):
        out += check_panel(f"{rel}: panel {p.get('id')} '{p.get('title')}'", p)
    return out
