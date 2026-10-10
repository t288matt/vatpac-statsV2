"""Shared pytest path set-up for the tests under tests/.

ops/watchdog/watchdog.py imports its sibling module integrity (in the image both sit in /app), and integrity imports
ops/audit/independent.py; tests load watchdog.py by file path, so put both folders on sys.path once, here."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for sub in ("ops/watchdog", "ops/audit"):
    p = str(REPO / sub)
    if p not in sys.path:
        sys.path.insert(0, p)

def pytest_configure(config):
    config.addinivalue_line("markers", "slow_fuzz: the full fuzz grids (about 20 s); CI runs them, skip locally with -m 'not slow_fuzz'")
