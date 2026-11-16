#!/usr/bin/env python3
"""Tiered CI (issue 290): decide how much of the suite a pull request needs, and check that it all reported.

Three scopes, in order of cost:
  docs        only prose changed: the secret scan and the attribution check are the whole check.
  dashboards  only dashboard JSON (and the tests and chart table that describe it) changed: validators, the dashboard
              tests, and the dashboard SQL tests against the real views.
  full        anything else, anything unknown, any mix, an empty or unreadable change list: the whole suite.
A path is classified one by one and the most expensive scope wins, so an unknown path can only ever raise the scope.
A change to .github/ or to this script is full, because those files decide what runs.

Commands (all stdlib, no network):
  git diff -z --name-status -M BASE HEAD | python3 ops/ci_scope.py          print docs, dashboards or full
  python3 ops/ci_scope.py tests unit|integration                            print the pytest targets of the dashboards scope
  NEEDS_JSON='<toJSON(needs)>' python3 ops/ci_scope.py verify               exit 1 unless every job the scope needs succeeded
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import PurePosixPath

DOCS, DASHBOARDS, FULL = "docs", "dashboards", "full"
RANK = {DOCS: 0, DASHBOARDS: 1, FULL: 2}

# Dashboard tests that only describe dashboards: editing one of them is a dashboards-scope change.
DASHBOARD_ONLY_TESTS = (
    "tests/test_dashboard_descriptions.py",
    "tests/test_dashboard_nav.py",
    "tests/test_chart_definitions_tests.py",
    "tests/test_phase3_switch.py",
    "tests/test_dashboards_look.py",
    "tests/test_dashboards_sql_unchanged.py",
    "tests/test_military_panels_only_on_military.py",
    "tests/test_no_data_history_banner.py",
)
# What the dashboards scope runs. The unit list is the nine above plus the Grafana tooling test, which walks every dashboard
# file. The integration list is every integration test that runs dashboard SQL against the views (grafana_ro); editing one is
# full, because most of them test more than dashboards. tests/test_ci_scope.py fails if a test that reads the dashboards is
# missing from these lists.
UNIT_TARGETS = (*DASHBOARD_ONLY_TESTS, "tests/test_grafana_public.py")
INTEGRATION_TARGETS = tuple(
    f"tests/integration/{n}.py"
    for n in (
        "test_atc_live_events", "test_atc_position_hours", "test_atc_sessions_t15", "test_atc_t14", "test_audit_t30",
        "test_card_65", "test_card_90", "test_controller_retention", "test_end_to_end", "test_events_t14",
        "test_flight_views_switch", "test_military_events_t15", "test_online_hours", "test_panel_rules_gaps",
        "test_phase5_remove_old", "test_pilot_latest_one_row_per_flight", "test_pilots_people_t15", "test_pilots_records",
        "test_pilots_t14", "test_routes_aircraft",
        "test_time_zones", "test_one_row_per_key_awkward_data", "test_norm_code", "test_controller_promotion",
    )
)

# Docs under docs/ that CI has always run for (tests read them, or they are procedure): not prose-only.
FULL_DOCS_DIRS = ("docs/RUNBOOKS/", "docs/reference/")
FULL_DOCS_FILES = ("docs/DATA_DICTIONARY.md", "docs/DEFINITIONS.md", "docs/CONFIG_MAP.md", "SPEC.md")
DASHBOARD_DOCS = ("docs/CHART_DEFINITIONS.md",)  # test_chart_definitions_tests.py reads it against the dashboards

# Jobs of ci.yml. The dashboards scope needs these; full needs all of them; docs needs the scope job and quick-checks only.
# quick-checks holds the small checks as steps (issue 340): the secret scan and the attribution check in every scope, config
# drift, the dashboard validators and the size budget when not docs, the compose checks in full only. Those step conditions are
# held by tests/test_ci_scope.py. Lint is a set of steps of unit-tests (full only).
DOCS_JOBS = ("scope", "quick-checks")
DASHBOARD_JOBS = (*DOCS_JOBS, "unit-tests", "integration-tests")
FULL_JOBS = (*DASHBOARD_JOBS, "image-build")
NEEDED = {DOCS: DOCS_JOBS, DASHBOARDS: DASHBOARD_JOBS, FULL: FULL_JOBS}


def classify_path(path: str) -> str:
    """Scope of one changed path. Anything not named here is full."""
    p = path.strip()
    parts = PurePosixPath(p).parts
    if not p or p.startswith("/") or "\\" in p or ".." in parts or "." in parts:
        return FULL
    if p.startswith((".github/", "ops/")) or p in ("docker-compose.yml", "Makefile"):
        return FULL  # decides what runs, or what ships
    if p.endswith(".json") and p.startswith("grafana/dashboards/") and len(parts) >= 4:
        return DASHBOARDS
    if p in DASHBOARD_ONLY_TESTS or p in DASHBOARD_DOCS:
        return DASHBOARDS
    if p.startswith(FULL_DOCS_DIRS) or p in FULL_DOCS_FILES:
        return FULL
    if p.startswith("docs/") or (len(parts) == 1 and p.endswith(".md")):
        return DOCS
    return FULL


def classify(paths: list[str]) -> str:
    """Scope of a whole change: the most expensive path wins; no paths at all is full (a failed diff must not read as docs)."""
    if not paths:
        return FULL
    return max((classify_path(p) for p in paths), key=RANK.__getitem__)


def parse_name_status_z(data: bytes) -> list[str]:
    """Paths from `git diff -z --name-status -M`: STATUS NUL path NUL, and two paths after R and C. Every path counts, so a
    rename out of collector/ into docs/ is still a change to collector/. Raises ValueError on a shape it does not know."""
    tokens = data.decode("utf-8", errors="replace").split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    paths: list[str] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status or status[0] not in "ACDMRTUXB":
            raise ValueError(f"unexpected status field {status!r}")
        n = 2 if status[0] in "RC" else 1
        if i + n >= len(tokens):
            raise ValueError("truncated change list")
        paths.extend(tokens[i + 1 : i + 1 + n])
        i += 1 + n
    return paths


def scope_of(data: bytes) -> str:
    """The scope for raw `git diff -z --name-status -M` output. Any doubt is full."""
    try:
        return classify(parse_name_status_z(data))
    except ValueError:
        return FULL


def verify(scope: str, results: dict[str, str]) -> list[str]:
    """Problems with the job results, empty when ci may report success. A job the scope needs must have succeeded; any other
    job must have succeeded or been skipped. A skipped job therefore passes only for code the scope says it did not touch,
    and an unknown or missing scope, or a missing job, fails."""
    if scope not in NEEDED:
        return [f"scope is {scope!r}, not one of docs, dashboards, full"]
    problems = []
    for job in FULL_JOBS:
        got = results.get(job)
        if job in NEEDED[scope]:
            if got != "success":
                problems.append(f"{job}: needed for scope {scope} but its result is {got!r}")
        elif got not in ("skipped", "success"):
            problems.append(f"{job}: result is {got!r}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "tests" and argv[2] in ("unit", "integration"):
        print(" ".join(UNIT_TARGETS if argv[2] == "unit" else INTEGRATION_TARGETS))
        return 0
    if len(argv) == 2 and argv[1] == "verify":
        try:
            needs = json.loads(os.environ.get("NEEDS_JSON", ""))
            results = {k: str(v.get("result")) for k, v in needs.items()}
            scope = str(needs["scope"]["outputs"]["scope"])
        except (ValueError, KeyError, AttributeError, TypeError) as exc:
            print(f"ci-result: cannot read the job results ({exc!r}); failing", file=sys.stderr)
            return 1
        problems = verify(scope, results)
        print(f"ci-result: scope {scope}; " + ", ".join(f"{k}={v}" for k, v in sorted(results.items())))
        for line in problems:
            print(f"ci-result: FAIL {line}", file=sys.stderr)
        return 1 if problems else 0
    if len(argv) == 1:
        print(scope_of(sys.stdin.buffer.read()))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
