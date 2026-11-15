"""Shared set-up for the stats loader's tests: paths, the fake clock and API, and the Postgres plumbing.

Unit tests need nothing else. Integration tests (marker `integration`) need STATS_TEST_DSN (or VATPAC_TEST_DSN): a
THROWAWAY Postgres 16 database, as the superuser, whose name ends in _test. They are skipped without it, except with
VATPAC_TEST_STRICT=1 (CI), where a missing database stops the run so a skip can never hide a test that did not run.
No test calls the public VATSIM API or reads a copy of production.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for p in (HERE, HERE.parent / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from fakes import FakeApi, FakeClock, at  # noqa: E402

DSN = os.environ.get("STATS_TEST_DSN") or os.environ.get("VATPAC_TEST_DSN")
STRICT = os.environ.get("VATPAC_TEST_STRICT") == "1"
TABLES = "atc_stats_session, atc_stats_run_log"
# Test-only passwords (the same kind CI uses for its throwaway container); never real values.
ROLE_VARS = {"grafana_ro_password": "t-grafana", "collector_password": "t-collector", "backup_password": "t-backup",
             "watchdog_password": "t-watchdog", "stats_loader_password": "t-stats"}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: needs PostgreSQL (STATS_TEST_DSN)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if DSN:
        return
    if STRICT:
        raise pytest.UsageError("VATPAC_TEST_STRICT=1 but STATS_TEST_DSN is not set: refusing to skip the tests")
    skip = pytest.mark.skip(reason="STATS_TEST_DSN not set (integration tests need a migrated throwaway Postgres 16)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


def _apply_migrations(c: Any) -> None:
    """A blank database: apply the real migrations, in order, the 'migrate:up' part of each file."""
    for f in sorted((REPO / "db" / "migrations").glob("*.sql")):
        up = re.split(r"^-- migrate:down", f.read_text(encoding="utf-8"), flags=re.M)[0]
        c.execute(up)


def _apply_roles() -> None:
    psql = shutil.which("psql")
    if not psql:
        pytest.fail("psql is needed to apply db/roles.sql for the role tests")
    args = [psql, DSN or "", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(REPO / "db" / "roles.sql")]
    for k, v in ROLE_VARS.items():
        args[4:4] = ["-v", f"{k}={v}"]
    subprocess.run(args, check=True, capture_output=True, text=True)


@pytest.fixture(scope="session")
def dsn() -> str:
    import psycopg

    assert DSN
    with psycopg.connect(DSN, autocommit=True) as c:
        name = c.execute("SELECT current_database()").fetchone()[0]  # type: ignore[index]
        if not name.endswith("_test"):
            pytest.fail(f"refusing to use database {name!r}: the name must end in _test")
        if c.execute("SELECT to_regclass('public.atc_stats_session')").fetchone()[0] is None:  # type: ignore[index]
            _apply_migrations(c)
        if c.execute("SELECT 1 FROM pg_roles WHERE rolname = 'stats_loader'").fetchone() is None:
            _apply_roles()
    return DSN


@pytest.fixture()
def conn(dsn: str) -> Iterator[Any]:
    """The superuser, with the two tables emptied."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f"TRUNCATE {TABLES} RESTART IDENTITY")
        yield c


@pytest.fixture()
def loader_conn(conn: Any) -> Iterator[Any]:
    """The same connection acting as the stats_loader role: tests pass only with the rights the role really has."""
    conn.execute("SET ROLE stats_loader")
    try:
        yield conn
    finally:
        conn.execute("RESET ROLE")


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock(at(2026, 11, 15, 6))


@pytest.fixture()
def fake(clock: FakeClock) -> FakeApi:
    return FakeApi(clock)
