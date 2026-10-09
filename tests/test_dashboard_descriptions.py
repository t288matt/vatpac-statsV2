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
    # One over-length name is counted every poll: 2,880 a day at 30 s polling, so an orange step at 100 would trip on it.
    steps = _panel("Rejected values")["fieldConfig"]["defaults"]["thresholds"]["steps"]
    orange = next(s["value"] for s in steps if s["color"] == "orange")
    assert orange > 2880
