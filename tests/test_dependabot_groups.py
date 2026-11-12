"""Issue 344: every Dependabot entry groups its version updates into one pull request; security updates are not grouped."""
from __future__ import annotations

from pathlib import Path

import yaml

UPDATES = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github" / "dependabot.yml").read_text(encoding="utf-8"))["updates"]


def test_every_entry_has_one_group_of_all_version_updates() -> None:
    assert len(UPDATES) == 8
    for u in UPDATES:
        assert len(u["groups"]) == 1, u["directory"]
        (group,) = u["groups"].values()
        assert group["applies-to"] == "version-updates" and group["patterns"] == ["*"], u["directory"]


def test_the_major_version_ignores_are_kept() -> None:
    ignored = {(u["package-ecosystem"], u["directory"]) for u in UPDATES if u.get("ignore")}
    assert ignored == {("docker", "/ops/backup"), ("docker", "/ops/migrate"), ("docker-compose", "/")}
