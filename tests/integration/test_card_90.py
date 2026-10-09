"""Card 90 (most popular aircraft by flights): v1 uses a 6-month window, flights with an aircraft type and
airborne time only, and counts flights per category. Owner decision 08-10-2026: categories come from the
aircraft_type lookup, not v1's LIKE ladder. Helpers come from helpers.py."""
from __future__ import annotations

import pytest
from helpers import TRAFFIC, add_flight, run_panel

pytestmark = pytest.mark.integration

TITLE = "Most popular aircraft by flights (last 6 months)"


def counts(conn) -> dict[str, tuple[int, int]]:
    return {r["category"]: (r["flights"], r["pilots"]) for r in run_panel(conn, TRAFFIC, TITLE)}


def test_flights_counted_per_category_within_six_months(conn):
    add_flight(conn, 9600001, aircraft="B738", days_ago=10)
    add_flight(conn, 9600001, aircraft="B738", days_ago=20)        # same pilot, second flight
    add_flight(conn, 9600002, aircraft="B738", days_ago=30)
    got = counts(conn)
    assert sum(f for f, _ in got.values()) == 3
    (flights, pilots), = got.values()
    assert (flights, pilots) == (3, 2)


def test_old_flights_are_outside_the_window(conn):
    add_flight(conn, 9600003, aircraft="B738", days_ago=100)       # inside 6 months
    add_flight(conn, 9600004, aircraft="B738", days_ago=200)       # outside
    assert sum(f for f, _ in counts(conn).values()) == 1


def test_parked_connections_and_missing_types_are_excluded(conn):
    add_flight(conn, 9600005, aircraft="B738", days_ago=5, airborne=0)      # no airborne time
    add_flight(conn, 9600006, aircraft="", days_ago=5)                      # no aircraft type
    add_flight(conn, 9600007, aircraft="B738", days_ago=5)
    assert sum(f for f, _ in counts(conn).values()) == 1


def test_unlisted_codes_show_as_other(conn):
    add_flight(conn, 9600008, aircraft="ZZZZ", days_ago=5)
    assert counts(conn) == {"Other / uncategorised": (1, 1)}
