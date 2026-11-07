"""Issue #285: pilot_latest has one row per flight (cid and callsign), the most recent logon, not one row per logon.

The old definition (migration 20261007000010) is kept below as a temporary view so the tests compare old and new on the same rows.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterator

import pytest
from conftest import REPO

from helpers import CID, make_session, panel, session_ending, set_up_clock

pytestmark = pytest.mark.integration

BASE = CID + 9000
OLD_VIEW = """
CREATE TEMP VIEW pilot_latest_old AS
SELECT ps.id AS session_id, ps.cid, ps.callsign, ps.name, ps.home_icao, ps.server,
       upper(btrim(ps.departure)) AS departure, upper(btrim(ps.arrival)) AS arrival,
       upper(btrim(ps.aircraft_icao)) AS aircraft_icao, ps.flight_rules, ps.deptime,
       ps.logon_time, ps.last_seen,
       p.observed_at AS position_time, p.lat, p.lon, p.alt_ft, p.gs_kt,
       (ps.last_seen >= (SELECT min(s.observed_at)
                           FROM (SELECT observed_at FROM network_snapshot
                                  ORDER BY observed_at DESC LIMIT 2) s)
        AND ps.last_seen >= now() - interval '5 minutes') AS is_online
FROM pilot_session ps
CROSS JOIN LATERAL (
  SELECT pp.observed_at, pp.lat, pp.lon, pp.alt_ft, pp.gs_kt
  FROM pilot_position pp
  WHERE pp.session_id = ps.id AND pp.observed_at >= now() - interval '3 hours'
  ORDER BY pp.observed_at DESC LIMIT 1
) p
WHERE ps.last_seen >= now() - interval '3 hours'
"""


@pytest.fixture
def old(conn: Any) -> Iterator[None]:
    conn.execute(OLD_VIEW)
    yield
    conn.execute("DROP VIEW IF EXISTS pilot_latest_old")


def _ids(conn: Any, view: str) -> set[int]:
    return {r["session_id"] for r in conn.execute(f"SELECT session_id FROM {view}").fetchall()}


def _logons(conn: Any, t: datetime, cid: int, *ages_s: int, callsign: str | None = None) -> list[int]:
    """One session per age (seconds before t its last position was; 0 = online now). Logon times differ by session."""
    ids = [make_session(conn, cid, session_ending(t, age, n=20 + i)) for i, age in enumerate(ages_s)]
    if callsign:
        conn.execute("UPDATE pilot_session SET callsign = %s WHERE id = ANY(%s)", (callsign, ids))
    return ids


def test_a_relog_leaves_only_the_newest_session(conn: Any, old: None) -> None:
    t = set_up_clock(conn)
    first, second, third = _logons(conn, t, BASE + 1, 3000, 1500, 10)
    assert {first, second, third} <= _ids(conn, "pilot_latest_old")
    rows = conn.execute("SELECT session_id FROM pilot_latest WHERE cid = %s", (BASE + 1,)).fetchall()
    assert [r["session_id"] for r in rows] == [third]


def test_ties_on_last_seen_go_to_the_highest_session_id(conn: Any, old: None) -> None:
    t = set_up_clock(conn)
    a = make_session(conn, BASE + 2, session_ending(t, 10, n=20))
    b = make_session(conn, BASE + 2, session_ending(t, 10, n=30))   # logs on earlier, last seen at the same moment
    assert conn.execute("SELECT count(DISTINCT last_seen) AS n FROM pilot_session WHERE cid = %s", (BASE + 2,)).fetchone()["n"] == 1
    assert [r["session_id"] for r in conn.execute("SELECT session_id FROM pilot_latest WHERE cid = %s", (BASE + 2,)).fetchall()] == [max(a, b)]


def test_a_different_callsign_or_pilot_is_never_hidden(conn: Any, old: None) -> None:
    t = set_up_clock(conn)
    one = _logons(conn, t, BASE + 3, 600, callsign="ALPHA1")
    two = _logons(conn, t, BASE + 3, 20, callsign="BRAVO2")        # same pilot, another flight
    other = _logons(conn, t, BASE + 4, 600, callsign="ALPHA1")     # another pilot, same callsign
    assert _ids(conn, "pilot_latest") >= {*one, *two, *other}


def test_old_against_new_only_older_sessions_of_the_same_flight_disappear(conn: Any, old: None) -> None:
    t = set_up_clock(conn)
    _logons(conn, t, BASE + 5, 2000, 900, 10)                      # three logons, one flight
    _logons(conn, t, BASE + 6, 40)                                 # single logon
    _logons(conn, t, BASE + 7, 7000, 300)                          # both inside the 3 hour window
    _logons(conn, t, BASE + 8, 5 * 3600)                           # outside the window altogether
    new_ids, old_ids = _ids(conn, "pilot_latest"), _ids(conn, "pilot_latest_old")
    assert new_ids <= old_ids
    gone = old_ids - new_ids
    assert gone, "the comparison must exercise the rule"
    for sid in gone:
        row = conn.execute("SELECT * FROM pilot_latest_old WHERE session_id = %s", (sid,)).fetchone()
        winner = conn.execute("SELECT * FROM pilot_latest WHERE cid = %s AND callsign = %s", (row["cid"], row["callsign"])).fetchone()
        assert winner is not None and winner["last_seen"] >= row["last_seen"]
    # every surviving row is identical to its old row, column for column
    for sid in new_ids:
        n = conn.execute("SELECT * FROM pilot_latest WHERE session_id = %s", (sid,)).fetchone()
        o = conn.execute("SELECT * FROM pilot_latest_old WHERE session_id = %s", (sid,)).fetchone()
        assert n == o


def test_columns_order_and_types_are_unchanged(conn: Any, old: None) -> None:
    sql = ("SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS t FROM pg_attribute a "
           "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum")
    assert conn.execute(sql, ("pilot_latest",)).fetchall() == conn.execute(sql, ("pilot_latest_old",)).fetchall()


def test_online_now_counts_one_pilot_once_and_other_counts_do_not_move(conn: Any, old: None) -> None:
    t = set_up_clock(conn)
    _logons(conn, t, BASE + 10, 0, 0)                              # both logons online: two rows before, one now
    _logons(conn, t, BASE + 11, 0)
    _logons(conn, t, BASE + 12, 7200)                              # logged off, still on the board
    count = "SELECT count(*) AS n FROM {} WHERE is_online"
    assert conn.execute(count.format("pilot_latest_old")).fetchone()["n"] == 3
    assert conn.execute(count.format("pilot_latest")).fetchone()["n"] == 2
    assert conn.execute("SELECT count(DISTINCT cid) AS n FROM pilot_online_now").fetchone()["n"] == 2
    assert conn.execute("SELECT count(DISTINCT cid) AS n FROM pilot_latest_old WHERE is_online").fetchone()["n"] == 2


def test_the_departure_board_lists_a_flight_once(conn: Any) -> None:
    """The exact panel SQL, unchanged: a relog no longer gives ON GROUND and LOGGED OFF for the same flight."""
    t = set_up_clock(conn)
    _logons(conn, t, BASE + 20, 3000, 0)
    sql = panel(REPO / "grafana" / "dashboards" / "pilots" / "pilots-now.json", "Departure board")["targets"][0]["rawSql"]
    rows = [r for r in conn.execute(sql).fetchall() if r["Flight"] == f"TEST{BASE + 20}"]
    assert len(rows) == 1 and rows[0]["Status"] == "AIRBORNE"
