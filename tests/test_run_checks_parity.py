"""Issue 241: CI, the Makefile and a plain Git Bash run the same checks, because all of them call ops/run_checks.sh.

This fails when they drift apart: a CI step that runs a check command itself again, a check in the script that no CI step runs,
a CI step that names a check the script does not have, or a Makefile target that stops calling the script.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = (REPO / "ops" / "run_checks.sh").read_text(encoding="utf-8")
CI = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
MAKEFILE = (REPO / "Makefile").read_text(encoding="utf-8")

LOCAL_ONLY = {"tool-versions"}   # compares the installed tools with ci.yml's pins; CI installs those pins itself
CALL = re.compile(r"^sh ops/run_checks\.sh ([a-z-]+)$")
# Commands the shared script owns. A CI step that runs one of these itself would be a second definition of the check.
OWNED = ("ruff", "mypy", "sh -n", "bash -n", "pytest -q -m \"not integration\" tests", "check_skips", "check_config_drift",
         "validate_dashboards", "dashboard_nav", "check_size_budget", "actionlint", "check_no_claude_attribution",
         "docker compose --env-file", "gen_env.sh", "check_env.sh")


def script_checks() -> set[str]:
    m = re.search(r"^    ([a-z|-]+)\) echo \"\$1\" ;;$", SCRIPT, re.M)
    assert m, "the single-check line of expand() in ops/run_checks.sh changed shape"
    return set(m.group(1).split("|"))


def script_groups() -> dict[str, set[str]]:
    groups: dict[str, set[str]] = {}
    for m in re.finditer(r'^    ([a-z-]+)\) echo "([a-z -]+)" ;;$', SCRIPT, re.M):
        groups[m.group(1)] = set(m.group(2).split())
    return groups


def ci_runs() -> list[tuple[str, str, str]]:
    """(job, step name, run text) for every step of the jobs that run the shared checks."""
    return [(j, str(st.get("name", "")), str(st["run"])) for j in ("unit-tests", "quick-checks")
            for st in CI["jobs"][j]["steps"] if "run" in st]


def ci_called() -> list[str]:
    return [m.group(1) for _, _, run in ci_runs() if (m := CALL.match(run.strip()))]


def test_every_check_has_a_function_and_every_ci_call_names_a_real_check() -> None:
    checks = script_checks()
    for c in checks:
        assert f"ck_{c.replace('-', '_')}()" in SCRIPT, c
    assert set(ci_called()) <= checks, set(ci_called()) - checks


def test_ci_runs_every_check_in_the_script_exactly_once_except_the_local_only_one() -> None:
    called = ci_called()
    assert sorted(called) == sorted(set(called)), "a check is called by two CI steps"
    assert set(called) == script_checks() - LOCAL_ONLY, (set(called) ^ (script_checks() - LOCAL_ONLY))


def test_the_groups_cover_all_checks_and_all_runs_everything() -> None:
    groups = script_groups()
    assert {"lint", "tests", "repo-checks", "config"} <= set(groups)
    # `all` is written as tool-versions plus the other groups: expand it
    all_text = re.search(r'^    all\) echo "(.+)" ;;$', SCRIPT, re.M)
    assert all_text
    parts = re.findall(r"\$\(expand ([a-z-]+)\)|\b(tool-versions)\b", all_text.group(1))
    in_all = set()
    for grp, single in parts:
        in_all |= groups[grp] if grp else {single}
    assert in_all == script_checks()
    assert set().union(groups["lint"], groups["tests"], groups["repo-checks"], groups["config"]) == script_checks() - LOCAL_ONLY


def test_no_ci_step_runs_a_check_command_itself() -> None:
    for job, name, run in ci_runs():
        if CALL.match(run.strip()) or name in ("Install", "Install lint tools"):
            continue
        for owned in OWNED:
            if owned in run:
                # the dashboards-scope pytest run is the one deliberate exception: it runs a subset, not the full check
                assert name == "pytest (unit, dashboards scope)" and owned.startswith("pytest"), f"{job}/{name} runs {owned!r} itself"


def test_the_ci_step_names_and_scopes_still_exist_for_the_checks_that_moved() -> None:
    names = [n for _, n, _ in ci_runs()]
    for prefix in ("ruff (collector)", "mypy (collector)", "ruff (ops python)", "mypy (ops python)", "shell syntax", "pytest (unit;",
                   "unit skips", "config-drift", "dashboards (validator", "dashboards (navigation", "size-budget", "actionlint",
                   "commit-attribution", "compose-config (compose files", "compose-config (image names", "compose-config (blank"):
        assert any(n.startswith(prefix) for n in names), prefix


def test_the_makefile_targets_call_the_script() -> None:
    for target, check in (("config", "config"), ("lint", "lint"), ("drift", "config-drift"), ("test", "tests"), ("size", "size-budget"),
                          ("checks", "all")):
        m = re.search(rf"^{target}:.*\n((?:\t.*\n)+)", MAKEFILE, re.M)
        assert m, target
        assert m.group(1).strip() == f"sh ops/run_checks.sh {check}", target


def test_the_script_is_posix_sh_and_is_syntax_checked_by_ci() -> None:
    assert SCRIPT.startswith("#!/bin/sh\n")
    code = "\n".join(ln for ln in SCRIPT.splitlines() if not ln.lstrip().startswith("#"))
    assert "[[" not in code and not re.search(r"^\s*local ", code, re.M) and "| tee" not in code and "pipefail" not in code
    shell_step = next(run for _, n, run in ci_runs() if n.startswith("shell syntax"))
    assert shell_step == "sh ops/run_checks.sh shell-syntax"
    assert 'for f in ops/*.sh ops/migrate/*.sh; do [ "$f" = ops/deploy.sh ] || [ "$f" = ops/stale_issues.sh ] || sh -n "$f"' in SCRIPT   # ops/run_checks.sh matches ops/*.sh


def test_the_ops_python_files_checked_strictly_include_the_new_ones() -> None:
    m = re.search(r"--ignore-missing-imports \\\n(.*?)\n\}", SCRIPT, re.S)
    assert m
    listed = set(re.findall(r"ops/[\w/]+\.py", m.group(1)))
    assert {"ops/check_tool_versions.py", "ops/check_skips.py", "ops/ci_scope.py"} <= listed
    assert all((REPO / f).is_file() for f in listed)
