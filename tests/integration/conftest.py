"""Integration test plumbing.

Written against SPEC.md and run against a real PostgreSQL 16.4 (how to run: docs/RUNBOOKS/run-tests.md).

Skipped unless VATPAC_TEST_DSN is set, EXCEPT in strict mode (VATPAC_TEST_STRICT=1, which CI sets): there a missing
VATPAC_TEST_DSN aborts the run, and any skipped test (a missing role, a missing module) fails the run, so a green
result can never hide tests that did not run. The database must already be migrated (dbmate) and
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
STRICT = os.environ.get("VATPAC_TEST_STRICT") == "1"
_skipped: list[str] = []

DATA_TABLES = [
    "pilot_position", "pilot_session", "controller_rating_event", "controller_frequency_event",
    "controller_session", "network_snapshot", "poll_log", "controller_unlisted_sighting",
    "reference_data_change", "flight_record",
]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: needs PostgreSQL (VATPAC_TEST_DSN)")


_strict_xfails: set[str] = set()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:  # only a strict xfail that runs is a pinned defect; run=False or a non-strict xfail may hide a test that never ran
        if any(m.kwargs.get("strict") is True and m.kwargs.get("run", True) is not False for m in item.iter_markers("xfail")):
            _strict_xfails.add(item.nodeid)
    if DSN:
        return
    if STRICT:
        raise pytest.UsageError("VATPAC_TEST_STRICT=1 but VATPAC_TEST_DSN is not set: refusing to skip every integration test")
    skip = pytest.mark.skip(reason="VATPAC_TEST_DSN not set (integration tests need a migrated Postgres 16)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if STRICT and report.skipped and not (hasattr(report, "wasxfail") and report.nodeid in _strict_xfails):  # a pinned defect is not a skip
        _skipped.append(report.nodeid)


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if STRICT and report.skipped:
        _skipped.append(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if STRICT and _skipped and session.exitstatus == pytest.ExitCode.OK:
        print(f"\nVATPAC_TEST_STRICT=1: {len(_skipped)} skipped test(s) turn the run red: " + ", ".join(_skipped))
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(scope="session")
def dsn() -> str:
    assert DSN
    return DSN


_settings_baseline: dict[str, str] = {}


def _restore_app_settings(c) -> None:
    """app_setting is not a data table (the migrations seed it, the collector publishes into it), so it is not truncated.
    Tests that change a setting would leak it into the next test, so put back exactly what the first test of the
    session found: delete rows that were added, restore values that were changed."""
    if not _settings_baseline:
        _settings_baseline.update({r["key"]: r["value"] for r in c.execute("SELECT key, value FROM app_setting").fetchall()})
    c.execute("DELETE FROM app_setting WHERE key <> ALL(%s)", (list(_settings_baseline),))
    for key, value in _settings_baseline.items():
        c.execute("INSERT INTO app_setting (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value "
                  "WHERE app_setting.value IS DISTINCT FROM EXCLUDED.value", (key, value))


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
        _restore_app_settings(c)
        try:
            yield c
        finally:
            c.execute("RESET ROLE")
            _restore_app_settings(c)


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
