"""Shared pytest path set-up for the tests under tests/.

ops/watchdog/watchdog.py imports its sibling module integrity (in the image both sit in /app), and integrity imports
ops/audit/independent.py; tests load watchdog.py by file path, so put both folders on sys.path once, here."""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for sub in ("ops/watchdog", "ops/audit"):
    p = str(REPO / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

# DATABASE_URL is a required collector setting; these tests never connect with it, so any well-formed value will do.
os.environ.setdefault("DATABASE_URL", "postgresql://unit:unit@localhost:5432/unit")


def pytest_configure(config):  # type: ignore[no-untyped-def]
    config.addinivalue_line("markers", "slow_fuzz: the full fuzz grids (about 20 s); CI runs them, skip locally with -m 'not slow_fuzz'")
    config.addinivalue_line("markers", "slow_shell: many shell processes (ops/deploy.sh harness); skip locally with -m 'not slow_shell'")


# Archived tests (issue 347) are run on demand only, by naming the folder: `pytest tests/archive/v1_import`. A folder named on the
# command line is still collected; this only keeps `pytest tests` and `pytest .` from picking it up.
collect_ignore = ["archive"]
