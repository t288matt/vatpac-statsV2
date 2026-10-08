"""T30 test audit: tests added to kill mutants that survived the existing suite (see docs/TEST_AUDIT.md).

Each test names the mutant it kills. Style follows the existing integration tests: synthetic rows, rules taken from
SPEC and docs/CHART_DEFINITIONS.md, expected values written by hand (not derived from the SQL under test).
"""
from __future__ import annotations

import calendar
import copy
import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import REPO

import test_atc_sessions_t15 as atc15
import test_atc_t14 as atc14
import test_controller_retention as ret
import test_events_t14 as ev14
import test_online_hours as hours
import test_pilots_t14 as p14
from helpers import apply, at, partition_for
from test_flight_view import T0, flights, make_session, run

pytestmark = pytest.mark.integration

T1 = datetime(2026, 10, 7, 16, 11, 19, tzinfo=timezone.utc)


@pytest.fixture()
def db(conn):
    partition_for(conn, T1)
    return conn


def test_controller_whitelist_is_an_exact_case_sensitive_match(db, payload, expected, refdata) -> None:
    """Mutants C08 (prefix match) and C09 (case-insensitive match): a callsign that merely starts with, or differs
    only in case from, a whitelisted one must not be stored."""
    p = at(payload)
    base = next(c for c in p["controllers"] if c["callsign"] == "ML_APP")
    for i, cs in enumerate(("ML_APP2", "ML_APP_X", "ml_app", "Ml_App")):
        extra = copy.deepcopy(base)
        extra.update(callsign=cs, cid=9990000 + i)
        p["controllers"].append(extra)
    apply(db, p, refdata)
    stored = sorted(r["callsign"] for r in db.execute("SELECT callsign FROM controller_session").fetchall())
    assert stored == expected["controllers_kept_callsigns"]
