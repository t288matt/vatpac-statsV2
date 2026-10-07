"""Shared fixtures for the collector unit tests (no network, no database).

Tests are written from SPEC.md section 7 (public interfaces), not from the implementation.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "collector" / "src"
if SRC.is_dir() and str(SRC) not in sys.path:  # allow running without an editable install
    sys.path.insert(0, str(SRC))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO


@pytest.fixture(scope="session")
def polygon_path() -> Path:
    return REPO / "config" / "australian_airspace_polygon.json"


@pytest.fixture(scope="session")
def whitelist_path() -> Path:
    return REPO / "config" / "controller_callsigns_list.txt"


@pytest.fixture(scope="session")
def fixture_payload() -> dict:
    path = REPO / "tests" / "fixtures" / "vatsim-data.sample.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def expected() -> dict:
    path = REPO / "tests" / "fixtures" / "expected.json"
    return json.loads(path.read_text(encoding="utf-8"))
