"""Integration test plumbing.

Written against SPEC.md, then run against a real PostgreSQL 16.4 on 07-10-2026 (94 pass, 1 skipped:
see tests/README.md, "Known assumptions", for the skip).

Skipped unless VATPAC_TEST_DSN is set. The database must already be migrated (dbmate) and
must be a THROWAWAY database: the fixtures TRUNCATE all data tables. As a safety net the
database name must end in "_test" unless VATPAC_TEST_ALLOW_TRUNCATE=1.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "collector" / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DSN = os.environ.get("VATPAC_TEST_DSN")

DATA_TABLES = [
    "pilot_position", "pilot_session", "controller_rating_event", "controller_frequency_event",
    "controller_session", "network_snapshot", "poll_log", "controller_unlisted_sighting",
    "reference_data_change",
]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: needs PostgreSQL (VATPAC_TEST_DSN)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if DSN:
        return
    skip = pytest.mark.skip(reason="VATPAC_TEST_DSN not set (integration tests need a migrated Postgres 16)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def dsn() -> str:
    assert DSN
    return DSN


@pytest.fixture()
def conn(dsn: str) -> Iterator["psycopg.Connection"]:  # noqa: F821
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as c:
        dbname = c.execute("SELECT current_database() AS d").fetchone()["d"]
        if not (dbname.endswith("_test") or os.environ.get("VATPAC_TEST_ALLOW_TRUNCATE") == "1"):
            pytest.fail(f"refusing to TRUNCATE database {dbname!r}: name must end in _test "
                        "(or set VATPAC_TEST_ALLOW_TRUNCATE=1)")
        # drop leftover synthetic partitions from earlier runs, then empty the data tables
        for row in c.execute(
            "SELECT inhrelid::regclass::text AS part FROM pg_inherits "
            "WHERE inhparent = 'pilot_position'::regclass"
        ).fetchall():
            c.execute(f"DROP TABLE IF EXISTS {row['part']}")
        c.execute("TRUNCATE " + ", ".join(DATA_TABLES) + " RESTART IDENTITY CASCADE")
        yield c


@pytest.fixture(scope="session")
def payload() -> dict:
    return json.loads((REPO / "tests" / "fixtures" / "vatsim-data.sample.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def expected() -> dict:
    return json.loads((REPO / "tests" / "fixtures" / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def refdata():
    from vatpac.filters import load_polygon, load_whitelist

    return (
        load_polygon(REPO / "config" / "australian_airspace_polygon.json"),
        load_whitelist(REPO / "config" / "controller_callsigns_list.txt"),
    )
