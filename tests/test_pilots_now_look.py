"""Issue 280: the redesigned "Pilots: online now" dashboard keeps its queries; only the look changed.

Static checks on the dashboard JSON (no database): the two hour charts, the ATC panel and the busiest-hours table all run the
same SQL as before the redesign, the 24 hour and 7 day charts share one query, the stat cards read a one-hour window and no
panel carries a generated sentence.
"""
from __future__ import annotations

import json
from pathlib import Path

DASH = Path(__file__).resolve().parents[1] / "grafana" / "dashboards" / "pilots" / "pilots-now.json"
PANELS = {p["title"]: p for p in json.loads(DASH.read_text(encoding="utf-8"))["panels"] if p.get("title")}
WEEKDAYS = "When are people online on weekdays (last 30 days)"
WEEKENDS = "When are people online on weekends (last 30 days)"


def sqls(title: str) -> list[str]:
    return [t["rawSql"] for t in PANELS[title]["targets"]]


def test_pilots_now_panels_reuse_the_unchanged_queries() -> None:
    weekday, weekend = sqls(WEEKDAYS)[0], sqls(WEEKENDS)[0]
    assert "-- v1 card 94" in weekday and "-- v1 card 95" in weekend
    assert sqls("ATC online by hour (weekdays and weekends, last 30 days)") == [weekday, weekend]
    assert sqls("Busiest hours on weekdays (last 30 days)") == [weekday]
    assert sqls("Pilots online over the last 7 days") == sqls("Pilots online over the last 24 hours")


def test_pilots_now_time_windows_are_set_on_the_panels() -> None:
    assert PANELS["Pilots online over the last 24 hours"]["timeFrom"] == "24h"
    assert PANELS["Pilots online over the last 7 days"]["timeFrom"] == "7d"
    for title in ("Pilots in the region now", "Pilots on the whole network now", "Region share of the network"):
        assert PANELS[title]["timeFrom"] == "1h" and "$__timeFilter(observed_at)" in sqls(title)[0]
    assert PANELS["Pilots in the region now"]["options"]["showPercentChange"] is True      # change against one hour ago
    assert PANELS["Region share of the network"]["options"]["showPercentChange"] is False  # a percentage of a percentage: no arrow


def test_pilots_now_hour_charts_show_one_series_each() -> None:
    for title, col in ((WEEKDAYS, "Pilots (callsigns)"), (WEEKENDS, "Pilots (callsigns)")):
        names = PANELS[title]["transformations"][0]["options"]["include"]["names"]
        assert names == ["hour_utc", col] and PANELS[title]["type"] == "barchart"


def test_pilots_now_has_no_generated_sentence_and_a_status_pill() -> None:
    text = [p for p in PANELS.values() if p["type"] == "text"]
    assert len(text) == 0 or all("${" in json.dumps(p["options"]) for p in text)
    status = next(o for o in PANELS["Departure board"]["fieldConfig"]["overrides"] if o["matcher"]["options"] == "Status")
    assert {"id": "custom.cellOptions", "value": {"type": "pill"}} in status["properties"]
    assert "LIMIT 25" in sqls("Departure board")[0]
