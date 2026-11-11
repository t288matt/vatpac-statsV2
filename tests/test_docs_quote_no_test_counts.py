"""Issue #53: the test documentation must not quote test counts (they go stale the day a test is added).

It says how to print the current counts instead (docs/RUNBOOKS/run-tests.md section 4)."""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COUNT = re.compile(r"\b\d{2,}\s+(passed|skipped|failed|tests?|unit tests?|integration tests?|xfailed)\b", re.I)


def test_runbook_and_tests_readme_quote_no_counts() -> None:
    for rel in ("docs/RUNBOOKS/run-tests.md", "tests/README.md"):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert COUNT.findall(text) == [], f"{rel} quotes a test count; print the current one instead"


def test_the_runbook_still_says_how_to_print_the_counts() -> None:
    text = (REPO / "docs/RUNBOOKS/run-tests.md").read_text(encoding="utf-8")
    assert "--collect-only" in text and "VATPAC_TEST_STRICT" in text and "UTC" in text
