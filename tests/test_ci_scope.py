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
    assert cs.verify("full", {**ok_full, "lint": "skipped"})                 # a skipped job is not a pass for full
    assert cs.verify("full", {**ok_full, "integration-tests": "failure"})
    assert cs.verify("full", {**ok_full, "image-build": "cancelled"})
    assert cs.verify("full", {k: v for k, v in ok_full.items() if k != "lint"})   # a missing job fails
    dash = {j: "success" for j in cs.DASHBOARD_JOBS} | {j: "skipped" for j in ("lint", "compose-config", "image-build")}
    assert cs.verify("dashboards", dash) == []
    assert cs.verify("dashboards", {**dash, "integration-tests": "skipped"})   # needed in this scope
    assert cs.verify("dashboards", {**dash, "dashboards": "skipped"})
    assert cs.verify("dashboards", {**dash, "lint": "failure"})
    docs = {j: "skipped" for j in cs.FULL_JOBS} | {"scope": "success", "secret-scan": "success", "commit-attribution": "success"}
    assert cs.verify("docs", docs) == []
    assert cs.verify("docs", {**docs, "secret-scan": "skipped"})          # the scan must have run
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
    assert run_cli("verify", env={"NEEDS_JSON": needs("full", {**good, "lint": "skipped"})}).returncode == 1
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
    "tests/scale/run_panels.py": "scale tool, not a test that CI runs",
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


def test_every_job_is_gated_by_the_scope_it_needs() -> None:
    """The `if` of each job, against ci_scope.NEEDED: which scopes run a job must be exactly the scopes that need it."""
    jobs = CI["jobs"]
    for name, job in jobs.items():
        if name in ("scope", "ci-result", "secret-scan", "commit-attribution"):
            assert "if" not in job or name == "ci-result", name   # scope and the secret scan run in every scope
            continue
        assert job["needs"] == "scope" or "scope" in job["needs"], name
        cond = job["if"].replace(" ", "")
        runs_in = {s for s in cs.NEEDED if (f"=='{s}'" in cond and "!=" not in cond) or (s != "docs" and cond.endswith("!='docs'"))}
        wanted = {s for s, jobs_needed in cs.NEEDED.items() if name in jobs_needed}
        assert runs_in == wanted, f"{name}: runs in {runs_in}, ci_scope.NEEDED says {wanted}"
    assert all("if" not in jobs[j] for j in ("scope", "secret-scan", "commit-attribution"))
    assert "needs" not in jobs["secret-scan"]   # a failed scope job cannot stop the secret scan


def test_the_dashboards_scope_steps_use_the_scope_and_the_full_steps_are_untouched() -> None:
    steps = [st for j in ("unit-tests", "integration-tests") for st in CI["jobs"][j]["steps"] if "if" in st]
    by_scope: dict[str, int] = {}
    for st in steps:
        by_scope[st["if"].split("'")[1]] = by_scope.get(st["if"].split("'")[1], 0) + 1
    assert by_scope == {"full": 3, "dashboards": 3}   # unit once and integration twice (two time zones), for each scope
    full_runs = {st["run"] for st in steps if "'full'" in st["if"]}
    dash_runs = sorted(st["run"] for st in steps if "'dashboards'" in st["if"])
    assert dash_runs == sorted([UNIT_RUN, INTEG_RUN, INTEG_RUN])   # exact text: widening them to all of tests/ fails
    assert full_runs == {'pytest -q -m "not integration" tests collector/tests --ignore=tests/.venv',
                         "pytest tests/integration -m integration -q"}


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
