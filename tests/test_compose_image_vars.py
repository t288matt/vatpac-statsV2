"""docker-compose.yml: the four built services take their image from two optional variables (issue 173, P2).

Unset, the result must be the local image `vatpac-stats-<name>:local` (what `make up` and `up -d --build` build).
Set to the release workflow's names, it must be `ghcr.io/t288matt/vatpac-stats-<name>:sha-<7>`. No Docker needed.
"""
from __future__ import annotations

import re
import shutil
import subprocess
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


def _check_env(tmp_path: Path, extra: str) -> "subprocess.CompletedProcess[str]":
    env = tmp_path / "ci.env"
    subprocess.run(["sh", str(REPO / "ops" / "gen_env.sh"), str(env)], check=True, capture_output=True)
    env.write_text(env.read_text(encoding="utf-8") + extra, encoding="utf-8")
    return subprocess.run(["sh", str(REPO / "ops" / "check_env.sh"), str(env)], capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.parametrize("extra,ok", [
    ("", True),
    ("VATPAC_IMAGE_PREFIX=\nVATPAC_IMAGE_TAG=\n", True),
    ("VATPAC_IMAGE_PREFIX=ghcr.io/t288matt/\nVATPAC_IMAGE_TAG=sha-abc1234\n", True),
    ("VATPAC_IMAGE_TAG=sha-abc1234\n", False),
    ("VATPAC_IMAGE_PREFIX=ghcr.io/t288matt/\n", False),
])
def test_check_env_wants_both_image_variables_or_neither(tmp_path: Path, extra: str, ok: bool) -> None:
    r = _check_env(tmp_path, extra)
    assert (r.returncode == 0) == ok
    assert "sha-abc1234" not in r.stderr + r.stdout and "ghcr.io" not in r.stderr + r.stdout  # values never printed
    if not ok:
        assert "set both or neither" in r.stderr
