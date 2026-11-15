"""Fixtures for the independent QA tests (issue 387). A throwaway Postgres is needed for the database tests."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / "tests"), str(HERE.parent / "src")]

import qa_stats_loader_support as sup  # noqa: E402

DB = "qa_loader_test"


@pytest.fixture(scope="session")
def _built() -> str:
    sup.need_db()
    sup.build_db(DB)
    return DB


@pytest.fixture
def su(_built: str) -> Iterator[psycopg.Connection]:
    """Superuser connection (database default time zone is Pacific/Auckland on purpose). Tables emptied before each test."""
    with psycopg.connect(sup.dsn(_built), autocommit=True) as c:
        c.execute("TRUNCATE atc_stats_session, atc_stats_run_log RESTART IDENTITY")
        yield c


@pytest.fixture
def conn(su: psycopg.Connection, _built: str) -> Iterator[psycopg.Connection]:
    """The loader's own connection: role stats_loader, as the code under test uses it."""
    with psycopg.connect(sup.dsn(_built, "stats_loader"), autocommit=True) as c:
        yield c
