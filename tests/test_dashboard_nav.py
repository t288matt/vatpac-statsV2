"""The navigation strip on every provisioned dashboard (issue 280): present, in step with SHORT_NAMES, safe links."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ops"))
spec = importlib.util.spec_from_file_location("dashboard_nav", REPO / "ops" / "dashboard_nav.py")
nav = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nav)

FILES = sorted((REPO / "grafana" / "dashboards").glob("*/*.json"))
LINK = re.compile(r"\[[^\]]+\]\(([^)]*)\)")


def top_panel(f: Path) -> dict:
    return next(p for p in json.loads(f.read_text(encoding="utf-8"))["panels"] if p["id"] == 1)


def test_there_are_seventeen_dashboards_in_four_groups() -> None:
    assert len(FILES) == 17 and {g: len(i) for g, i in nav.GROUPS.items()} == {"Pilots": 8, "ATC": 5, "Events": 2, "Management": 2}


def test_every_dashboard_has_the_current_strip_and_the_files_are_in_step() -> None:
    assert nav.problems() == []


def test_strip_links_only_to_known_short_paths_without_a_query_string() -> None:
    known = {f"/{n}" for n in nav.SHORT_NAMES.values()}
    for f in FILES:
        first = nav.split(top_panel(f)["options"]["content"])[0]
        links = LINK.findall(first)
        assert len(links) == 16, f.name  # every other dashboard, not itself
        for target in links:
            assert target in known and "?" not in target and "#" not in target, (f.name, target)


def test_current_page_is_bold_and_not_linked() -> None:
    for f in FILES:
        d = json.loads(f.read_text(encoding="utf-8"))
        first = nav.split(d["panels"][0]["options"]["content"])[0]
        here = nav.SHORT_NAMES[d["uid"]]
        assert f"(/{here})" not in first and first.count("**") == 2, f.name


def test_top_panel_is_tall_enough_and_no_panel_overlaps_it() -> None:
    for f in FILES:
        d = json.loads(f.read_text(encoding="utf-8"))
        top = d["panels"][0]["gridPos"]
        assert top["h"] >= 4, f.name
        assert all(p["gridPos"]["y"] >= top["h"] for p in d["panels"][1:]), f.name


def test_check_catches_a_missing_stale_or_query_string_strip(tmp_path: Path, monkeypatch) -> None:
    for f in FILES:
        (tmp_path / f.parent.name).mkdir(exist_ok=True)
        (tmp_path / f.parent.name / f.name).write_bytes(f.read_bytes())
    monkeypatch.setattr(nav, "DASHBOARDS", tmp_path)
    assert nav.problems() == []
    victim = tmp_path / "atc" / "atc-overview.json"
    d = json.loads(victim.read_text(encoding="utf-8"))
    good = d["panels"][0]["options"]["content"]
    for bad in (nav.split(good)[1], good.replace("(/live)", "(/live?x=1)"), good.replace("(/live)", "(/gone)")):
        d["panels"][0]["options"]["content"] = bad
        victim.write_text(json.dumps(d), encoding="utf-8")
        assert any("atc-overview.json" in p for p in nav.problems())
