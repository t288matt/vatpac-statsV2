"""docker-compose.yml: the four built services take their image from two optional variables (issue 173, P2).

Unset, the result must be the local image `vatpac-stats-<name>:local` (what `make up` and `up -d --build` build).
Set to the release workflow's names, it must be `ghcr.io/t288matt/vatpac-stats-<name>:sha-<7>`. No Docker needed.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
BUILT = ("migrate", "collector", "backup", "watchdog")
VAR_RE = re.compile(r"^\$\{VATPAC_IMAGE_PREFIX:-\}(vatpac-stats-[a-z]+):\$\{VATPAC_IMAGE_TAG:-local\}$")


def _services() -> dict:
    return yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"]


def _resolve(image: str, prefix: str, tag: str) -> str:
    return image.replace("${VATPAC_IMAGE_PREFIX:-}", prefix).replace("${VATPAC_IMAGE_TAG:-local}", tag or "local")


@pytest.mark.parametrize("name", BUILT)
def test_built_service_uses_the_variable_form_and_keeps_its_build(name: str) -> None:
    svc = _services()[name]
    m = VAR_RE.match(svc["image"])
    assert m and m.group(1) == f"vatpac-stats-{name}"
    assert "build" in svc  # local builds still work
    assert "pull_policy" not in svc  # the default (missing) is what `up --no-build` needs


@pytest.mark.parametrize("name", BUILT)
def test_unset_falls_back_to_the_local_image(name: str) -> None:
    assert _resolve(_services()[name]["image"], "", "") == f"vatpac-stats-{name}:local"


@pytest.mark.parametrize("name", BUILT)
def test_registry_names_match_the_release_workflow(name: str) -> None:
    release = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert re.search(rf"name: {name}\b", release)
    assert "ghcr.io/t288matt/vatpac-stats-${{ matrix.name }}" in release and "sha-${{ needs.check.outputs.short }}" in release
    assert _resolve(_services()[name]["image"], "ghcr.io/t288matt/", "sha-abc1234") == f"ghcr.io/t288matt/vatpac-stats-{name}:sha-abc1234"


def test_third_party_images_are_untouched_and_the_variables_are_documented_but_not_set() -> None:
    for name, svc in _services().items():
        if name not in BUILT:
            assert "VATPAC_IMAGE" not in svc["image"]
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^#VATPAC_IMAGE_PREFIX=ghcr\.io/t288matt/$", example, re.M)
    assert re.search(r"^#VATPAC_IMAGE_TAG=sha-", example, re.M)
    assert not re.search(r"^VATPAC_IMAGE_(PREFIX|TAG)=", example, re.M)
    cfg = (REPO / "docs" / "CONFIG_MAP.md").read_text(encoding="utf-8")
    assert "`VATPAC_IMAGE_PREFIX`" in cfg and "`VATPAC_IMAGE_TAG`" in cfg
