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


PUBLIC = re.compile(r"/public-dashboards/([0-9a-f]{32})")


def test_strip_links_only_to_public_dashboard_addresses_without_a_query_string() -> None:
    """Short paths such as /traffic give Page not found when clicked inside Grafana (issue 284); only the long address works."""
    known = {nav.stable_token(uid) for uid in nav.SHORT_NAMES}
    for f in FILES:
        d = json.loads(f.read_text(encoding="utf-8"))
        links = LINK.findall(nav.split(top_panel(f)["options"]["content"])[0])
        assert len(links) == 16, f.name  # every other dashboard, not itself
        for target in links:
            m = PUBLIC.fullmatch(target)
            assert m and m.group(1) in known, (f.name, target)
            assert m.group(1) != nav.stable_token(d["uid"]), (f.name, "links to itself")


def test_each_token_belongs_to_the_dashboard_its_label_names() -> None:
    by_label = {name: nav.stable_token(uid) for uid, short in nav.SHORT_NAMES.items()
                for items in nav.GROUPS.values() for path, name in items if path == short}
    for f in FILES:
        for label, target in re.findall(r"\[([^\]]+)\]\(([^)]*)\)", nav.split(top_panel(f)["options"]["content"])[0]):
            assert target == f"/public-dashboards/{by_label[label]}", (f.name, label)


def test_check_catches_a_short_path_a_wrong_token_and_an_unknown_dashboard(tmp_path: Path, monkeypatch) -> None:
    for f in FILES:
        (tmp_path / f.parent.name).mkdir(exist_ok=True)
        (tmp_path / f.parent.name / f.name).write_bytes(f.read_bytes())
    monkeypatch.setattr(nav, "DASHBOARDS", tmp_path)
    victim = tmp_path / "atc" / "atc-overview.json"
    d = json.loads(victim.read_text(encoding="utf-8"))
    good = d["panels"][0]["options"]["content"]
    live = nav.stable_token("vatpac-pilots-now")
    traffic = nav.stable_token("vatpac-pilots-traffic")
    for bad in (good.replace(f"/public-dashboards/{live}", "/live"),
                good.replace(f"/public-dashboards/{live}", f"/public-dashboards/{traffic}"),
                good.replace(f"/public-dashboards/{live}", "/public-dashboards/" + "0" * 32)):
        assert bad != good
        d["panels"][0]["options"]["content"] = bad
        victim.write_text(json.dumps(d), encoding="utf-8")
        assert any("atc-overview.json" in p for p in nav.problems()), bad[:80]


def test_current_page_is_bold_and_not_linked() -> None:
    for f in FILES:
        d = json.loads(f.read_text(encoding="utf-8"))
        first = nav.split(d["panels"][0]["options"]["content"])[0]
        assert f"/public-dashboards/{nav.stable_token(d['uid'])}" not in first and first.count("**") == 2, f.name


def test_top_panel_is_tall_enough_and_no_panel_overlaps_it() -> None:
    for f in FILES:
        d = json.loads(f.read_text(encoding="utf-8"))
        top = d["panels"][0]["gridPos"]
        assert top["h"] >= 5, f.name
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
    for bad in (nav.split(good)[1], good.replace(f"/public-dashboards/{nav.stable_token('vatpac-pilots-now')})", f"/public-dashboards/{nav.stable_token('vatpac-pilots-now')}?x=1)")):
        d["panels"][0]["options"]["content"] = bad
        victim.write_text(json.dumps(d), encoding="utf-8")
        assert any("atc-overview.json" in p for p in nav.problems())
