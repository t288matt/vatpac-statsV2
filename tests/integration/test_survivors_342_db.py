"""Issue 342: tests for database mutation survivors of the CI test review (A10, D02, D03, D04). Each one fails against its
mutant, named in the docstring, and passes on the real code."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from drop_gate_helpers import drain, process_flights
from helpers import apply, at, partition_for
from test_drop_gate import FN, TODAY, call, day, exists, mk, mins

pytestmark = pytest.mark.integration

CUTOFF = datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=timezone.utc) - timedelta(days=90)


@pytest.fixture()
def db(conn):
    partition_for(conn, datetime(2026, 10, 7, tzinfo=timezone.utc))
    return conn


def test_a_near_miss_is_reported_new_once_and_not_again(db, payload, refdata) -> None:
    """A10 (the `inserted` flag inverted): the first sighting of each unlisted callsign is reported as new, a repeat is not."""
    first = apply(db, at(payload), refdata)
    assert sorted(first.new_near_misses) == ["AD_XYZ_TWR", "ML_XYZ_CTR"]
    again = apply(db, at(payload, 15), refdata)
    assert again.new_near_misses == []


def test_the_partition_ending_exactly_at_the_cutoff_goes_and_the_next_day_stays(conn) -> None:
    """D02 (a partition dropped a day too early): with 90 days of retention the day 91 days back ends at the cutoff and is
    dropped; the day 90 days back ends a day after it and stays."""
    partition_for(conn, day(91))
    partition_for(conn, day(90))
    assert drain(conn, 90) == 1                    # one drop per call, so call until it has nothing left
    assert not exists(conn, 91) and exists(conn, 90)


def test_a_session_last_seen_exactly_at_the_cutoff_still_pins_its_partition(conn) -> None:
    """D03 (`last_seen > cutoff` at BOTH places in the function): a session seen at exactly the cutoff is inside the window
    and keeps its old partition; one microsecond earlier it no longer does. Changing only the first (pinning) check is
    near-equivalent: the re-check before the drop still keeps the partition, so no test can tell that single change apart."""
    sid = mk(conn, 9842001, mins(day(200), 10))
    process_flights(conn)
    conn.execute("UPDATE pilot_session SET last_seen = %s WHERE id = %s", (CUTOFF, sid))
    assert call(conn) == 0 and exists(conn, 200)
    conn.execute("UPDATE pilot_session SET last_seen = %s WHERE id = %s", (CUTOFF - timedelta(microseconds=1), sid))
    assert call(conn) == 1 and not exists(conn, 200)


@pytest.mark.parametrize("days", [0, -1, None])
def test_a_retention_below_one_day_is_refused(conn, days) -> None:
    """D04 (`retention_days < 0`): zero days would drop everything older than today, so 0 is refused like -1 and NULL."""
    partition_for(conn, day(200))
    with pytest.raises(psycopg.errors.RaiseException, match="retention_days must be >= 1"):
        conn.execute(f"SELECT {FN}(%s, %s)", (days, TODAY))
    conn.rollback()
    assert exists(conn, 200)
