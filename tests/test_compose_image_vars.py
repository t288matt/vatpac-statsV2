"""docker-compose.yml: the four built services take their image from two optional variables (issue 173, P2).

Unset, the result must be the local image `vatpac-stats-<name>:latest` (what `make up` and `up -d --build` build).
With the prefix alone it is `ghcr.io/t288matt/vatpac-stats-<name>:latest`; with a tag override, `...:sha-<7>`. No Docker needed.
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
VAR_RE = re.compile(r"^\$\{VATPAC_IMAGE_PREFIX:-\}(vatpac-stats-[a-z]+):\$\{VATPAC_IMAGE_TAG:-latest\}$")


def _services() -> dict:
    return yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"]


def _resolve(image: str, prefix: str, tag: str) -> str:
    return image.replace("${VATPAC_IMAGE_PREFIX:-}", prefix).replace("${VATPAC_IMAGE_TAG:-latest}", tag or "latest")


@pytest.mark.parametrize("name", BUILT)
def test_built_service_uses_the_variable_form_and_keeps_its_build(name: str) -> None:
    svc = _services()[name]
    m = VAR_RE.match(svc["image"])
    assert m and m.group(1) == f"vatpac-stats-{name}"
    assert "build" in svc  # local builds still work
    assert "pull_policy" not in svc  # the default (missing) is what `up --no-build` needs


@pytest.mark.parametrize("name", BUILT)
def test_unset_falls_back_to_the_local_image(name: str) -> None:
    assert _resolve(_services()[name]["image"], "", "") == f"vatpac-stats-{name}:latest"


@pytest.mark.parametrize("name", BUILT)
def test_registry_names_match_the_release_workflow(name: str) -> None:
    release = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert re.search(rf"name: {name}\b", release)
    assert "ghcr.io/t288matt/vatpac-stats-${{ matrix.name }}" in release and "sha-${{ needs.check.outputs.short }}" in release
    assert "type=raw,value=latest" not in release  # latest is moved by the promote job, after all four images are pushed
    promote = yaml.safe_load(release)["jobs"]["promote"]
    assert set(promote["needs"]) == {"check", "images"}
    run = " ".join(str(st.get("run", "")) for st in promote["steps"])
    assert "for n in collector watchdog backup migrate" in run and '--tag "$img:latest" "$img:sha-$SHORT"' in run
    assert _resolve(_services()[name]["image"], "ghcr.io/t288matt/", "") == f"ghcr.io/t288matt/vatpac-stats-{name}:latest"
    assert _resolve(_services()[name]["image"], "ghcr.io/t288matt/", "sha-abc1234") == f"ghcr.io/t288matt/vatpac-stats-{name}:sha-abc1234"


def test_third_party_images_are_untouched_and_the_variables_are_documented_but_not_set() -> None:
    for name, svc in _services().items():
        if name not in BUILT:
            assert "VATPAC_IMAGE" not in svc["image"]
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    assert re.search(r"^#VATPAC_IMAGE_PREFIX=ghcr\.io/t288matt/$", example, re.M)
    assert re.search(r"^#VATPAC_IMAGE_TAG=$", example, re.M)
    assert not re.search(r"^VATPAC_IMAGE_(PREFIX|TAG)=", example, re.M)
    cfg = (REPO / "docs" / "CONFIG_MAP.md").read_text(encoding="utf-8")
    assert "`VATPAC_IMAGE_PREFIX`" in cfg and "`VATPAC_IMAGE_TAG`" in cfg


def _check_env(tmp_path: Path, extra: str) -> "subprocess.CompletedProcess[str]":
    env = tmp_path / "ci.env"
    subprocess.run(["sh", str(REPO / "ops" / "gen_env.sh"), str(env)], check=True, capture_output=True)
    env.write_text(env.read_text(encoding="utf-8") + extra, encoding="utf-8")
    return subprocess.run(["sh", str(REPO / "ops" / "check_env.sh"), str(env)], capture_output=True, text=True)


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.parametrize("extra,ok,warns", [
    ("", True, False),
    ("VATPAC_IMAGE_PREFIX=\nVATPAC_IMAGE_TAG=\n", True, False),
    ("VATPAC_IMAGE_PREFIX=ghcr.io/t288matt/\n", True, False),  # the prefix alone is registry mode
    ("VATPAC_IMAGE_PREFIX=ghcr.io/t288matt/\nVATPAC_IMAGE_TAG=sha-abc1234\n", True, True),  # a pin passes but warns loudly
    ("VATPAC_IMAGE_TAG=sha-abc1234\n", True, True),
])
def test_check_env_accepts_the_prefix_alone_and_warns_about_a_pin(tmp_path: Path, extra: str, ok: bool, warns: bool) -> None:
    r = _check_env(tmp_path, extra)
    assert (r.returncode == 0) == ok
    assert "ghcr.io" not in r.stderr + r.stdout and "abc1234" not in r.stderr + r.stdout  # values never printed
    assert ("WARNING" in r.stderr) == warns


def test_release_matrix_names_equal_the_compose_services_using_vatpac_stats_images() -> None:
    release = yaml.safe_load((REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    matrix = {row["name"] for row in release["jobs"]["images"]["strategy"]["matrix"]["include"]}
    pattern = re.compile(r"^\$\{VATPAC_IMAGE_PREFIX:-\}vatpac-stats-([a-z]+):")
    compose = {m.group(1) for svc in _services().values() if (m := pattern.match(svc.get("image", "")))}
    assert matrix == compose == set(BUILT)


# Third-party images float on the loosest tag their registry actually publishes (ADR-032, issue 176; amended
# 18-10-2026): never `latest`, never an exact patch. Postgres floats on the bare major; Grafana has no such tag on
# Docker Hub (confirmed 18-10-2026: "manifest unknown" for grafana/grafana:12), so it floats on MAJOR.MINOR instead.
# Metabase (issue 288) is the one exact pin: it publishes no bare-major tag and an upgrade migrates its application database
# irreversibly, so the version changes only on purpose (docs/RUNBOOKS/metabase.md). Its own database is the db image.
EXPECTED_THIRD_PARTY = {"db": "postgres:16-bookworm", "grafana": "grafana/grafana:12.4",
                        "metabase-db": "postgres:16-bookworm", "metabase": "metabase/metabase:v0.64.5.4"}


def _from_lines(dockerfile: str) -> list[str]:
    text = (REPO / dockerfile).read_text(encoding="utf-8")
    return [m.group(1) for m in re.finditer(r"^FROM\s+(\S+)", text, re.MULTILINE)]


def test_compose_third_party_images_match_the_expected_tags() -> None:
    svcs = _services()
    third = {n: s["image"] for n, s in svcs.items() if n not in BUILT}
    assert third == EXPECTED_THIRD_PARTY


def test_dockerfile_third_party_bases_float_on_the_major() -> None:
    assert _from_lines("ops/backup/Dockerfile") == ["postgres:16-bookworm"]
    assert _from_lines("ops/migrate/Dockerfile") == ["amacneil/dbmate:2"]


def test_db_and_backup_use_the_same_postgres_tag() -> None:
    assert _services()["db"]["image"] == _from_lines("ops/backup/Dockerfile")[0]


def test_no_third_party_image_is_latest_or_more_precise_than_it_should_be() -> None:
    """Postgres and dbmate float on the bare major version; Grafana has no bare-major tag on Docker Hub (confirmed
    18-10-2026), so it floats on MAJOR.MINOR instead. Nothing floats past that: no exact patch, never `latest`."""
    images = [s["image"] for n, s in _services().items() if n not in BUILT]
    images += _from_lines("ops/backup/Dockerfile") + _from_lines("ops/migrate/Dockerfile")
    ci = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    for job in ci["jobs"].values():
        images += [s["image"] for s in (job.get("services") or {}).values()]
    assert "postgres:16" in images  # the CI service container
    for image in images:
        tag = image.rsplit(":", 1)[-1]
        assert tag != "latest", image
        if image.startswith("metabase/metabase:"):
            assert re.fullmatch(r"v\d+\.\d+\.\d+\.\d+", tag), image  # an exact version, never a floating .x tag
        elif image.startswith("grafana/grafana:"):
            assert re.fullmatch(r"\d+\.\d+", tag), image  # MAJOR.MINOR only, e.g. 12.4
        else:
            assert not re.match(r"^\d+\.\d+", tag), image  # 16.15, 12.4.12, 2.36.0 and so on


def test_ci_postgres_service_matches_production_major() -> None:
    ci = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    imgs = [s["image"] for j in ci["jobs"].values() for s in (j.get("services") or {}).values() if s["image"].startswith("postgres")]
    assert imgs and all(i == "postgres:16" for i in imgs)


def test_release_runs_after_green_ci_on_main_and_never_publishes_a_stale_commit() -> None:
    """Issue 268: automatic release after ci, but only for main's tip, and the manual run stays."""
    text = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    triggers = wf.get("on", wf.get(True))
    assert triggers["workflow_run"] == {"workflows": ["ci"], "types": ["completed"], "branches": ["main"]}
    assert "workflow_dispatch" in triggers  # rollbacks and docs-only tips are still published by hand
    check = wf["jobs"]["check"]
    assert "workflow_run.conclusion == 'success'" in check["if"] and "workflow_run.event == 'push'" in check["if"]
    resolve = next(st for st in check["steps"] if st.get("id") == "resolve")
    # the automatic path reads the commit from the triggering ci run, and the event name from the event itself
    assert resolve["env"]["AUTO_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    assert resolve["env"]["EVENT"] == "${{ github.event_name }}"
    run = resolve["run"]
    auto, manual, ci_ok, publish = (run.index(x) for x in ('[ "$EVENT" = "workflow_run" ]', '[ -z "$INPUT" ]', "--workflow ci.yml", 'publish=true'))
    assert auto < manual  # the automatic branch is taken first, so an empty commit input never means "main's tip" for it
    stale = run[auto:manual]
    assert 'tip="$(gh api' in stale and '[ "$sha" != "$tip" ]' in stale  # a commit that is not main's tip is not published
    assert "publish=false" in stale and "exit 0" in stale  # it stops green, without publishing
    assert ci_ok < publish  # the successful-ci-run check still happens before anything is published
    assert run.count("publish=true") == 1 and run.count("publish=false") == 1
    assert wf["jobs"]["images"]["if"] == "needs.check.outputs.publish == 'true'"
    assert set(wf["jobs"]["promote"]["needs"]) == {"check", "images"}  # latest still moves only after all four images
