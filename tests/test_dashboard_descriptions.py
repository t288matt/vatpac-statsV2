"""Dashboard panel descriptions that must stay in step with what the collector counts (no database needed)."""
import json
from pathlib import Path

HEALTH = Path(__file__).resolve().parent.parent / "grafana" / "dashboards" / "monitoring" / "collector-health.json"


def _panel(title_start: str) -> dict:
    panels = json.loads(HEALTH.read_text(encoding="utf-8"))["panels"]
    return next(p for p in panels if p.get("title", "").startswith(title_start))


def test_rejected_values_description_covers_everything_the_counter_counts():
    # Issue #71: rejected_values counts nulled speed/altitude, unparsable or out-of-range numbers,
    # malformed entries that were skipped and text cut to its length cap, not only speed and altitude.
    desc = _panel("Rejected values").get("description", "").lower()
    for word in ("malformed", "cut", "text"):
        assert word in desc, f"description should mention '{word}': {desc}"


def test_rejected_values_threshold_is_above_one_persistent_condition_per_day():
    # One over-length name is counted every poll: 2,880 a day at 30 s polling, so an amber step at 100 would trip on it.
    steps = _panel("Rejected values")["fieldConfig"]["defaults"]["thresholds"]["steps"]
    amber = next(s["value"] for s in steps if s["color"] == "#E69F00")
    assert amber > 2880


DASHBOARDS = HEALTH.parent.parent


def _panels(title_start: str) -> list[tuple[str, dict]]:
    out = []
    for path in sorted(DASHBOARDS.glob("*/*.json")):
        for p in json.loads(path.read_text(encoding="utf-8"))["panels"]:
            if p.get("title", "").startswith(title_start):
                out.append((f"{path.parent.name}/{path.name}", p))
    return out


def test_when_are_people_online_draws_atc_on_a_right_hand_axis():
    # Issue 308: ATC is small next to pilots; the six bar charts give it its own right-hand axis. Fails if an override is removed.
    found = _panels("When are people online on week")
    assert len(found) == 6
    for where, p in found:
        over = next(o for o in p["fieldConfig"]["overrides"] if o["matcher"]["options"] == "ATC (callsigns)")
        props = {x["id"]: x["value"] for x in over["properties"]}
        assert props["custom.axisPlacement"] == "right", where
        # Grafana keys a y scale by unit: a different unit from the pilots series makes ATC a separate scale (issue 308)
        assert props["unit"] == "short" and p["fieldConfig"]["defaults"]["unit"] == "none", where
        assert props["custom.axisLabel"] == "ATC callsigns" and props["custom.axisSoftMin"] == 0, where
        # the left label sits on the Pilots series, not in the defaults: a default would also label the hour (x) axis
        assert "axisLabel" not in p["fieldConfig"]["defaults"]["custom"], where
        pil = next(o for o in p["fieldConfig"]["overrides"] if o["matcher"]["options"] == "Pilots (callsigns)")
        assert {x["id"]: x["value"] for x in pil["properties"]}["custom.axisLabel"] == "Pilot callsigns", where
        assert p["fieldConfig"]["defaults"]["custom"]["axisSoftMin"] == 0, where


def test_busiest_days_panels_cover_five_years():
    # Issue 309: 1825 days, not the old 180. Fails if the window or a title is put back.
    found = _panels("Busiest days by")
    assert len(found) == 6
    for where, p in found:
        assert p["timeFrom"] == "1825d" and p["title"].endswith("(last 5 years)"), where
        assert "180" not in p.get("description", "") and "6 months" not in p.get("description", ""), where


def test_longest_flights_panel_is_gone_and_the_layout_has_no_gap():
    # Issue 310: the A4 panel was removed; the diverse-routes panel fills its row.
    path = DASHBOARDS / "pilots" / "pilots-records.json"
    panels = json.loads(path.read_text(encoding="utf-8"))["panels"]
    assert not [p for p in panels if p.get("title", "").startswith("Longest flights")]
    routes = next(p for p in panels if p.get("title", "").startswith("Pilots with the most diverse routes"))
    assert routes["gridPos"]["w"] == 24
    rows = {}
    for p in panels:
        g = p["gridPos"]
        rows.setdefault(g["y"], []).append((g["x"], g["w"]))
    for y, cells in rows.items():
        cells.sort()
        assert cells[0][0] == 0 and sum(w for _, w in cells) == 24, (y, cells)


def test_lapsed_pilots_panel_is_gone():
    # Issue 306.
    assert not _panels("Pilots who flew last month but not this month")
