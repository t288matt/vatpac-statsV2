"""Issue #57: the test plumbing itself. Settings changed by one test must not reach the next one (a leftover
`home_icao_prefixes` row would change every flight view result), and the retention helper must build exact durations."""
from __future__ import annotations

import pytest
from test_controller_retention import add_session

pytestmark = pytest.mark.integration

SEEDED = {"home_icao_prefixes": "Y", "airborne_gap_cap_seconds": "40"}  # the migration seed for the prefixes; the gap cap row is written by the test setup (conftest TEST_GAP_CAP)


def test_a_test_may_change_and_add_settings(conn) -> None:
    """Pollute on purpose; the next test proves the conn fixture put the table back (the pair runs in file order)."""
    conn.execute("UPDATE app_setting SET value = 'Z' WHERE key = 'home_icao_prefixes'")
    conn.execute("DELETE FROM app_setting WHERE key = 'airborne_gap_cap_seconds'")
    conn.execute("INSERT INTO app_setting (key, value) VALUES ('leak_probe', '1')")


def test_the_next_test_sees_the_settings_the_test_setup_started_with(conn) -> None:
    got = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_setting").fetchall()}
    assert "leak_probe" not in got
    assert {k: got.get(k) for k in SEEDED} == SEEDED


def test_the_retention_helper_builds_sessions_of_exactly_the_requested_length(conn) -> None:
    add_session(conn, 9890001, 3, 60)
    row = conn.execute("SELECT first_seen, last_seen, logon_time, last_seen - first_seen AS length "
                       "FROM controller_session WHERE cid = 9890001").fetchone()
    assert row["length"].total_seconds() == 3600
    assert row["first_seen"].microsecond == 0 and row["first_seen"] == row["logon_time"]
