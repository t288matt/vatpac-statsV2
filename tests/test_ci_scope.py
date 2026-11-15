"""Issue 290: the tiered CI filter (ops/ci_scope.py) and the way ci.yml uses it.

The filter decides how much of the suite a pull request runs, so a wrong answer hides a test. The rule these tests hold:
unknown, mixed, empty and unreadable all come out as full; only prose is docs; only dashboard JSON (and its own tests and
chart table) is dashboards."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ops"))
import ci_scope as cs  # noqa: E402

DASH = "grafana/dashboards/pilots/pilots-traffic.json"
UNIT_RUN = 'read -r -a targets <<< "$(python ops/ci_scope.py tests unit)"\n[ "${#targets[@]}" -gt 0 ]   # an empty list would run more than intended; stop instead\npytest -q -m "not integration" "${targets[@]}"\n'  # noqa: E501
INTEG_RUN = 'read -r -a targets <<< "$(python ops/ci_scope.py tests integration)"\n[ "${#targets[@]}" -gt 0 ]   # an empty list would run more than intended; stop instead\npytest "${targets[@]}" -m integration -q\n'  # noqa: E501


def z(*entries: tuple[str, ...]) -> bytes:
    """git diff -z --name-status -M output: each entry is (status, path) or (status, old, new)."""
    return b"".join(("\0".join(e) + "\0").encode() for e in entries)


@pytest.mark.parametrize("path,want", [
    (DASH, "dashboards"),
    ("grafana/dashboards/atc/atc-hours.json", "dashboards"),
    ("docs/CHART_DEFINITIONS.md", "dashboards"),
    ("tests/test_dashboard_nav.py", "dashboards"),
    ("tests/test_phase3_switch.py", "dashboards"),
    ("README.md", "docs"),
    ("CHANGELOG.md", "docs"),
    ("docs/DECISIONS.md", "docs"),
    ("docs/INSTALL.md", "docs"),
    ("docs/SQL_DEFINITIONS_REFERENCE.sql", "docs"),
    ("docs/RUNBOOKS/run-tests.md", "full"),       # tests read the runbooks
    ("docs/reference/v1_metabase_cards.json", "full"),
    ("docs/DATA_DICTIONARY.md", "full"),
    ("docs/DEFINITIONS.md", "full"),
    ("docs/CONFIG_MAP.md", "full"),               # tests read it
    ("SPEC.md", "full"),                          # tests read it, so it is not prose-only
    ("collector/src/vatpac/store.py", "full"),
    ("db/migrations/20261101000000_x.sql", "full"),
    ("ops/check_size_budget.py", "full"),
    ("ops/ci_scope.py", "full"),                  # the filter itself
    (".github/workflows/ci.yml", "full"),
    ("docker-compose.yml", "full"),
    ("Makefile", "full"),
    ("tests/test_ci_scope.py", "full"),
    ("tests/integration/test_time_zones.py", "full"),   # runs in the dashboards scope but tests more than dashboards
    ("tests/integration/helpers.py", "full"),
    ("tests/README.md", "full"),
    ("grafana/validate_dashboards.py", "full"),
    ("grafana/provisioning/dashboards/x.yml", "full"),
    ("grafana/dashboards/README.md", "full"),     # not a dashboard file
    ("grafana/dashboards/x.json", "full"),        # not in a group folder: unknown shape
    ("config/australian_airspace_polygon.json", "full"),
    ("something/new/we/never/heard/of.txt", "full"),
    ("sub/README.md", "full"),                    # only root-level markdown is prose
    ("docs/../collector/x.py", "full"),
    ("/etc/passwd", "full"),
    ("docs\\x.md", "full"),
    ("", "full"),
])
def test_one_path(path: str, want: str) -> None:
    assert cs.classify_path(path) == want


def test_mixed_changes_take_the_most_expensive_scope() -> None:
    assert cs.classify([DASH, "README.md"]) == "dashboards"
    assert cs.classify(["README.md", "docs/DECISIONS.md"]) == "docs"
    assert cs.classify([DASH, "docs/CHART_DEFINITIONS.md", "CHANGELOG.md"]) == "dashboards"
    assert cs.classify([DASH, "collector/src/vatpac/store.py"]) == "full"
    assert cs.classify(["README.md", "ops/deploy.sh"]) == "full"
    assert cs.classify([DASH, "brand-new-top-level-file"]) == "full"   # one unknown path is enough
    assert cs.classify([DASH, ".github/workflows/ci.yml"]) == "full"


def test_no_paths_is_full() -> None:
    assert cs.classify([]) == "full"
    assert cs.scope_of(b"") == "full"   # an empty diff may be a failed one


def test_parse_name_status_modify_add_delete() -> None:
    data = z(("M", DASH), ("A", "docs/x.md"), ("D", "collector/src/vatpac/old.py"))
    assert cs.parse_name_status_z(data) == [DASH, "docs/x.md", "collector/src/vatpac/old.py"]
    assert cs.scope_of(data) == "full"   # a deleted collector file is a collector change


def test_delete_of_a_dashboard_is_dashboards_and_of_prose_is_docs() -> None:
    assert cs.scope_of(z(("D", DASH))) == "dashboards"
    assert cs.scope_of(z(("D", "docs/OLD.md"))) == "docs"


def test_rename_counts_both_sides() -> None:
    # moving collector code into docs/ still changes collector/; moving a dashboard into collector/ too
    assert cs.parse_name_status_z(z(("R100", "collector/a.py", "docs/a.md"))) == ["collector/a.py", "docs/a.md"]
    assert cs.scope_of(z(("R100", "collector/a.py", "docs/a.md"))) == "full"
    assert cs.scope_of(z(("R090", DASH, "collector/x.json"))) == "full"
    assert cs.scope_of(z(("R100", "docs/a.md", "collector/a.md"))) == "full"
    assert cs.scope_of(z(("R100", "grafana/dashboards/pilots/a.json", "grafana/dashboards/atc/a.json"))) == "dashboards"
    assert cs.scope_of(z(("R100", "docs/a.md", "docs/b.md"))) == "docs"
    assert cs.scope_of(z(("C075", "docs/a.md", "ops/a.sh"))) == "full"   # a copy lists two paths as well


def test_paths_with_spaces_and_unicode_and_a_path_called_like_a_status() -> None:
    data = z(("M", "docs/a file with spaces.md"), ("M", "docs/café.md"), ("M", "M"))
    assert cs.parse_name_status_z(data) == ["docs/a file with spaces.md", "docs/café.md", "M"]
    assert cs.scope_of(data) == "full"   # the root file "M" is not markdown


@pytest.mark.parametrize("data", [b"M\0", b"R100\0only-one-path\0", b"Z\0docs/x.md\0", b"docs/x.md\0", b"\0\0"])
def test_unreadable_change_lists_are_full(data: bytes) -> None:
    assert cs.scope_of(data) == "full"


def test_verify_requires_everything_the_scope_needs() -> None:
    ok_full = {j: "success" for j in cs.FULL_JOBS}
    assert cs.verify("full", ok_full) == []
    assert cs.verify("full", {**ok_full, "unit-tests": "skipped"})           # a skipped job is not a pass for full
    assert cs.verify("full", {**ok_full, "integration-tests": "failure"})
    assert cs.verify("full", {**ok_full, "quick-checks": "failure"})
    assert cs.verify("full", {**ok_full, "image-build": "cancelled"})
    assert cs.verify("full", {k: v for k, v in ok_full.items() if k != "quick-checks"})   # a missing job fails
    dash = {j: "success" for j in cs.DASHBOARD_JOBS} | {"image-build": "skipped"}
    assert cs.verify("dashboards", dash) == []
    assert cs.verify("dashboards", {**dash, "integration-tests": "skipped"})   # needed in this scope
    assert cs.verify("dashboards", {**dash, "quick-checks": "skipped"})
    assert cs.verify("dashboards", {**dash, "image-build": "failure"})
    docs = {j: "skipped" for j in cs.FULL_JOBS} | {"scope": "success", "quick-checks": "success"}
    assert cs.verify("docs", docs) == []
    assert cs.verify("docs", {**docs, "quick-checks": "skipped"})         # the secret scan and attribution check must have run
    assert cs.verify("docs", {**docs, "unit-tests": "failure"})
    assert cs.verify("", ok_full) and cs.verify("None", ok_full) and cs.verify("everything", ok_full)


def run_cli(*args: str, stdin: bytes = b"", env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    import os
    return subprocess.run([sys.executable, str(REPO / "ops" / "ci_scope.py"), *args], input=stdin, capture_output=True,
                          env={**os.environ, **(env or {})}, check=False)


def test_cli_prints_the_scope_from_stdin() -> None:
    assert run_cli(stdin=z(("M", DASH))).stdout.decode().strip() == "dashboards"
    assert run_cli(stdin=z(("M", DASH), ("M", "ops/x.sh"))).stdout.decode().strip() == "full"
    assert run_cli(stdin=b"garbage").stdout.decode().strip() == "full"


def test_cli_verify_reads_the_needs_json_and_fails_closed() -> None:
    def needs(scope: str, results: dict[str, str]) -> str:
        n = {j: {"result": r, "outputs": {}} for j, r in results.items()}
        n["scope"] = {"result": "success", "outputs": {"scope": scope}}
        return json.dumps(n)
    good = {j: "success" for j in cs.FULL_JOBS}
    assert run_cli("verify", env={"NEEDS_JSON": needs("full", good)}).returncode == 0
    assert run_cli("verify", env={"NEEDS_JSON": needs("full", {**good, "quick-checks": "skipped"})}).returncode == 1
    assert run_cli("verify", env={"NEEDS_JSON": "not json"}).returncode == 1
    assert run_cli("verify", env={"NEEDS_JSON": "{}"}).returncode == 1
    # the scope job itself failed: it has no output, so nothing can pass
    broken = {j: {"result": "success", "outputs": {}} for j in cs.FULL_JOBS}
    broken["scope"] = {"result": "failure", "outputs": {}}
    assert run_cli("verify", env={"NEEDS_JSON": json.dumps(broken)}).returncode == 1


# ---- the lists of tests, held against the real tree -------------------------------------------------------------

READS_DASHBOARDS = re.compile(r"grafana[\"' /]+dashboards|\bDASH\b|run_panel|CHART_DEFINITIONS|validate_dashboards")
# shared helpers and files that mention dashboards without running dashboard queries; each reason is the reason it is exempt
EXEMPT = {
    "tests/integration/helpers.py": "shared helpers: any change to it is a full run (it is not classified as dashboards)",
    "tests/test_ci_scope.py": "this file",
    "tests/test_run_checks_parity.py": "names the validator command only to check that CI does not run it outside ops/run_checks.sh",
    "tests/scale/run_panels.py": "scale tool, not a test that CI runs",
    "tests/test_norm_code_single_rule.py": "names the dashboards folder only to scan it for copies of the ICAO text rule",
}


def test_every_listed_test_exists() -> None:
    for rel in (*cs.UNIT_TARGETS, *cs.INTEGRATION_TARGETS):
        assert (REPO / rel).is_file(), rel


def test_dashboard_only_tests_run_in_the_dashboards_scope() -> None:
    # a change to one of them is classified dashboards, so the scope must run it
    assert set(cs.DASHBOARD_ONLY_TESTS) <= set(cs.UNIT_TARGETS)


def test_no_test_that_reads_the_dashboards_is_left_out_of_the_dashboards_scope() -> None:
    listed = set(cs.UNIT_TARGETS) | set(cs.INTEGRATION_TARGETS)
    missing = []
    for f in sorted((REPO / "tests").rglob("test_*.py")) + sorted((REPO / "collector" / "tests").rglob("test_*.py")):
        rel = f.relative_to(REPO).as_posix()
        if rel in listed or rel in EXEMPT or "/.venv/" in rel:
            continue
        if READS_DASHBOARDS.search(f.read_text(encoding="utf-8")):
            missing.append(rel)
    assert not missing, f"these tests read the dashboards: add them to ops/ci_scope.py or EXEMPT: {missing}"


def test_unit_targets_are_unit_and_integration_targets_are_integration_tests() -> None:
    for rel in cs.UNIT_TARGETS:
        assert "integration" not in rel
    for rel in cs.INTEGRATION_TARGETS:
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "pytestmark" in text or "@pytest.mark.integration" in text, rel


# ---- the workflow, held against the script ----------------------------------------------------------------------

CI = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
TRIGGERS = CI.get("on") or CI.get(True)   # PyYAML reads the bare key `on` as True


def test_the_pull_request_trigger_has_no_path_filter_so_the_required_check_always_reports() -> None:
    assert TRIGGERS["pull_request"] is None or "paths" not in TRIGGERS["pull_request"]
    assert "paths" in TRIGGERS["push"]   # main keeps today's behaviour: prose-only pushes skip ci (docs-secret-scan covers them)


def test_ci_result_always_runs_and_waits_for_every_job() -> None:
    jobs = CI["jobs"]
    res = jobs["ci-result"]
    assert res["if"].replace(" ", "") in ("${{always()}}", "always()")
    assert set(res["needs"]) == set(jobs) - {"ci-result"}
    assert set(cs.FULL_JOBS) == set(jobs) - {"ci-result"}
    assert set(jobs) == {"scope", "quick-checks", "unit-tests", "integration-tests", "image-build", "ci-result"}   # issue 340


def runs_in(cond: str | None, scope: str) -> bool:
    """Evaluate a job or step `if` of ci.yml for a scope, as GitHub would when nothing has failed or been cancelled
    (`success()` is true, `!cancelled()` is true). Only the pieces ci.yml uses are understood; anything else fails the test."""
    if cond is None:
        return True
    expr = cond.strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2]
    expr = expr.replace("!cancelled()", "True").replace("needs.scope.outputs.scope", repr(scope))
    expr = expr.replace("&&", " and ").replace("||", " or ")
    assert not re.search(r"[A-Za-z_]+\(|needs\.|github\.", expr), cond
    return bool(eval(expr, {"__builtins__": {}}, {"True": True}))  # noqa: S307  (a literal expression built above)


SCOPES = ("docs", "dashboards", "full", "", "garbage")   # the last two: the scope job failed and left no (or a wrong) output


def test_every_job_is_gated_by_the_scope_it_needs() -> None:
    """The `if` of each job, run for every scope, against ci_scope.NEEDED. quick-checks has no scope condition at job level on
    purpose (its secret scan and attribution steps run in every scope, even when the scope job failed); its steps are held below."""
    jobs = CI["jobs"]
    assert "if" not in jobs["scope"] and "needs" not in jobs["scope"]
    for name, job in jobs.items():
        if name in ("scope", "ci-result"):
            continue
        assert job["needs"] == "scope" or "scope" in job["needs"], name
        if name == "quick-checks":
            assert all(runs_in(job["if"], s) for s in SCOPES), "quick-checks must start in every scope"
            continue
        for s in cs.NEEDED:
            assert runs_in(job["if"], s) == (name in cs.NEEDED[s]), f"{name} in scope {s}"


# Which scopes run each step, by the start of the step name. Every step of these two jobs must be in a table.
ALL, NOT_DOCS, FULL_ONLY, DASH_ONLY = {"docs", "dashboards", "full"}, {"dashboards", "full"}, {"full"}, {"dashboards"}
QUICK_STEPS = {
    "secret-scan": ALL, "commit-attribution": ALL,
    "actions/setup-python": NOT_DOCS, "Install": NOT_DOCS, "config-drift": NOT_DOCS, "dashboards": NOT_DOCS,
    "size-budget": NOT_DOCS, "actionlint": NOT_DOCS, "compose-config": FULL_ONLY,
}
UNIT_STEPS = {
    "Install lint tools": FULL_ONLY, "ruff": FULL_ONLY, "mypy": FULL_ONLY, "shell syntax": FULL_ONLY,
    "pytest (unit; ": FULL_ONLY, "pytest (stats loader unit)": FULL_ONLY, "unit skips": FULL_ONLY, "pytest (unit, dashboards scope)": DASH_ONLY,
}


def steps_of(job: str) -> list[dict[str, Any]]:
    return list(CI["jobs"][job]["steps"])


def test_each_quick_check_step_runs_in_exactly_the_scopes_that_need_it() -> None:
    seen = set()
    for st in steps_of("quick-checks"):
        if str(st.get("uses", "")).startswith("actions/checkout"):
            continue
        name = str(st.get("name") or str(st["uses"]).split("@")[0])
        key = next((k for k in sorted(QUICK_STEPS, key=len, reverse=True) if name.startswith(k)), None)
        assert key, f"quick-checks step {name!r} is not in the table above: say which scopes run it"
        seen.add(key)
        got = {s for s in ("docs", "dashboards", "full") if runs_in(st.get("if"), s)}
        assert got == QUICK_STEPS[key], f"{name}: runs in {got}, expected {QUICK_STEPS[key]}"
        assert "!cancelled()" in str(st.get("if")), f"{name}: must run even when an earlier step failed"
        if key in ("secret-scan", "commit-attribution"):
            assert runs_in(st["if"], "") and runs_in(st["if"], "garbage"), f"{name} must run even if the scope job failed"
    assert seen == set(QUICK_STEPS)   # every check that was a job is still a step


def test_each_unit_test_job_step_runs_in_exactly_the_scopes_that_need_it() -> None:
    seen = set()
    for st in steps_of("unit-tests")[2:]:   # the checkout and setup-python steps run whenever the job runs
        name = str(st["name"])
        if name == "Install":
            continue
        key = next((k for k in sorted(UNIT_STEPS, key=len, reverse=True) if name.startswith(k)), None)
        assert key, f"unit-tests step {name!r} is not in the table above"
        seen.add(key)
        got = {s for s in ("docs", "dashboards", "full") if runs_in(st.get("if"), s)}
        assert got == UNIT_STEPS[key], f"{name}: runs in {got}, expected {UNIT_STEPS[key]}"
        assert "!cancelled()" in str(st["if"]), f"{name}: must run even when an earlier step failed"
        assert not runs_in(st["if"], ""), f"{name} must not run on an unknown scope"
    assert seen == set(UNIT_STEPS)   # lint (ruff, mypy, shell syntax) is still checked, in the same job as the unit tests


def test_no_check_can_pass_by_being_allowed_to_fail() -> None:
    text = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "continue-on-error" not in text and "|| true" not in text


def test_the_checks_that_used_to_be_jobs_still_run_these_commands() -> None:
    """The old jobs, by the command each ran; each must still be run by a step of quick-checks or unit-tests (issue 340).
    Since issue 241 the steps call ops/run_checks.sh, so the commands are read from the steps plus that script."""
    runs = "\n".join(str(st.get("run", "")) for j in ("quick-checks", "unit-tests") for st in steps_of(j))
    runs += "\n" + (REPO / "ops" / "run_checks.sh").read_text(encoding="utf-8")
    for cmd in ("ops/check_config_drift.py", "grafana/validate_dashboards.py", "ops/dashboard_nav.py --check",
                "ops/check_size_budget.py", "ops/check_no_claude_attribution.py", "sh ops/gen_env.sh",
                "sh ops/check_env.sh", 'docker compose --env-file "$f" config -q', "config --images",
                "docker compose --env-file .env.example config -q",
                "(cd collector && $PY -m ruff check .)", "(cd collector && $PY -m mypy src)", "ruff check --config ops/ruff.toml ops",
                "mypy --strict --ignore-missing-imports", "sh -n", "bash -n", "actionlint -shellcheck="):
        assert cmd in runs, cmd
    assert any(str(st.get("uses", "")).startswith("gitleaks/gitleaks-action@") for st in steps_of("quick-checks"))


def loader_integration_steps() -> list[dict[str, Any]]:
    return [st for st in steps_of("integration-tests") if str(st.get("name", "")).startswith("pytest (stats loader")]


def test_the_stats_loader_integration_pass_is_strict_utc_and_full_scope_only() -> None:
    """Issue 387: the loader's own integration tests run in the full scope only, strict, on the migrated CI database."""
    (st,) = loader_integration_steps()
    assert str(st["if"]) == "needs.scope.outputs.scope == 'full'"
    assert st["env"]["VATPAC_TEST_STRICT"] == "1" and st["env"]["PYTHONPATH"] == "stats_loader/src"
    assert st["env"]["STATS_TEST_DSN"].endswith("/vatpac_test?options=-c%20TimeZone%3DUTC")
    assert st["run"] == "cd stats_loader && pytest tests -m integration -q"


def test_integration_runs_once_in_utc_and_no_pass_uses_another_zone() -> None:
    steps = [st for st in steps_of("integration-tests")
             if str(st.get("name", "")).startswith("pytest") and not str(st["name"]).startswith("pytest (stats loader")]
    assert sorted(str(st["if"]).split("'")[1] for st in steps) == ["dashboards", "full"]   # one pass per scope
    for st in steps:
        env = st["env"]
        assert env["VATPAC_TEST_EXPECT_TZ"] == "UTC", st["name"]
        assert env["VATPAC_TEST_DSN"].endswith("?options=-c%20TimeZone%3DUTC"), st["name"]   # explicit, not the runner's zone
        assert env["VATPAC_TEST_STRICT"] == "1"
    text = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "Sydney" not in text   # issue 339: no second time zone pass


def test_the_scope_steps_run_the_exact_commands_and_the_full_steps_are_untouched() -> None:
    unit = [st for st in steps_of("unit-tests") if str(st.get("name", "")).startswith("pytest")]
    unit = [st for st in unit if "stats loader" not in str(st["name"])]   # the loader's own passes are tested above and in run_checks
    integ = [st for st in steps_of("integration-tests")
             if str(st.get("name", "")).startswith("pytest") and "stats loader" not in str(st["name"])]
    dash_runs = sorted(str(st["run"]) for st in unit + integ if "'dashboards'" in str(st["if"]))
    assert dash_runs == sorted([UNIT_RUN, INTEG_RUN])   # exact text: widening them to all of tests/ fails
    full_runs = {str(st["run"]) for st in unit + integ if "'full'" in str(st["if"])}
    assert full_runs == {"sh ops/run_checks.sh pytest-unit", "pytest tests/integration -m integration -q"}
    script = (REPO / "ops" / "run_checks.sh").read_text(encoding="utf-8")   # the unit command itself lives in the shared script
    assert '-m pytest -q -m "not integration" tests collector/tests --ignore=tests/.venv' in script


def test_no_pytest_command_deselects_or_skips_anything() -> None:
    for name, job in CI["jobs"].items():
        for st in job.get("steps", []):
            run = st.get("run", "")
            if "pytest" in run:
                assert not re.search(r"--deselect|--ignore=tests/integration|\s-k\s|--co|-p no:|skip", run), (name, run)


def test_the_scope_step_cannot_be_loosened_by_the_pull_request_itself() -> None:
    step = next(st for st in CI["jobs"]["scope"]["steps"] if st.get("id") == "scope")["run"]
    assert 'git show "$BASE_SHA:ops/ci_scope.py"' in step   # the base branch's script classifies
    assert "python3 ops/ci_scope.py" not in step            # never the pull request's copy
    assert "grep -zqE" in step and ".github/" in step and "ci_scope" in step   # shell-side force to full
    assert "cat -v" in step   # changed file names are printed escaped
    pat = r"^(\.github/|ops/ci_scope\.py$)"   # the shell-side guard, run for real on a NUL list

    def forced(data: bytes) -> bool:
        return subprocess.run(["grep", "-zqE", pat], input=data, check=False).returncode == 0

    assert forced(z(("M", "ops/ci_scope.py"))) and forced(z(("M", ".github/workflows/ci.yml")))
    assert forced(z(("R100", "docs/a.md", ".github/x"))) and not forced(z(("M", DASH), ("M", "docs/x.md")))


def test_a_run_on_main_is_never_cancelled_and_the_secret_scan_sees_all_history() -> None:
    """Issue 342, mutants W03 and W05, and W04 for the strict flag (the Sydney part of W04 went with issue 339)."""
    assert CI["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    checkout = next(st for st in steps_of("quick-checks") if str(st.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0   # gitleaks reads the history; a shallow clone would scan almost nothing
    for st in steps_of("integration-tests"):
        if str(st.get("name", "")).startswith("pytest"):
            assert st["env"]["VATPAC_TEST_STRICT"] == "1", st["name"]


def test_a_checking_command_piped_into_tee_runs_under_pipefail() -> None:
    """GitHub's default shell is `bash -e` without pipefail, so `check | tee file` takes tee's exit status and a failing check
    leaves the step green. Every step that pipes into tee must say `set -o pipefail` first (issue 343 review)."""
    seen = 0
    for name, job in CI["jobs"].items():
        for st in job.get("steps", []):
            run = str(st.get("run", ""))
            if re.search(r"\|\s*tee\b", run) and "scope" != name:   # the scope job's tee only prints a summary line
                seen += 1
                assert "set -o pipefail" in run.split("|")[0] or run.lstrip().startswith("set -o pipefail"), (name, st.get("name"))
    # The validator's output used to go through tee. Since issue 241 it lives in ops/run_checks.sh, which writes to a file and
    # tests the validator's own exit status, so no pipe can hide it.
    script = (REPO / "ops" / "run_checks.sh").read_text(encoding="utf-8")
    assert seen == 0 and "validate_dashboards.py > " in script and '[ "$rc" -eq 0 ] || return 1' in script


def test_image_build_does_not_wait_for_quick_checks_and_ci_result_still_needs_it() -> None:
    assert CI["jobs"]["image-build"]["needs"] == "scope"
    assert {"quick-checks", "image-build"} <= set(CI["jobs"]["ci-result"]["needs"])
