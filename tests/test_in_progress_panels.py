"""ADR-025 guard (no database): every panel that reads `flight` directly excludes in-progress flights, live panels do not.

Panels over flight_ext, movements and cohorts inherit the exclusion from the view (migration 20261009000140, tested in
tests/integration/test_exclude_in_progress.py); the classification table is in docs/CHART_DEFINITIONS.md.
"""
import json
import re
from pathlib import Path

DASH = Path(__file__).resolve().parent.parent / "grafana" / "dashboards"
DIRECT_FLIGHT = re.compile(r"\b(?:FROM|JOIN)\s+flight\b(?!_)", re.IGNORECASE)
LIVE = re.compile(r"\b(?:pilot_latest|pilot_online_now|online_now|network_history)\b")


def _panels():
    for path in sorted(DASH.rglob("*.json")):
        for p in json.loads(path.read_text(encoding="utf-8")).get("panels", []):
            for t in p.get("targets", []):
                if t.get("rawSql"):
                    yield f"{path.name}: {p.get('title')}", t["rawSql"]


def test_panels_reading_flight_directly_exclude_in_progress_flights():
    direct = [(n, s) for n, s in _panels() if DIRECT_FLIGHT.search(s)]
    assert len(direct) == 11, [n for n, _ in direct]          # the panels listed in docs/CHART_DEFINITIONS.md
    missing = [n for n, s in direct if "is_in_progress" not in s]
    assert not missing, missing


def test_live_panels_do_not_filter_on_in_progress():
    live = [(n, s) for n, s in _panels() if LIVE.search(s)]
    assert live
    assert [n for n, s in live if "is_in_progress" in s] == []
