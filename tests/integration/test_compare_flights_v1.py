"""Issue #89 exit gate (#138), comparison (B): the script the owner runs, exercised on synthetic data. (Comparison A, the new table
against the old merging view, was retired with that view in phase 5; it stays in git history.)

ops/compare_flights_v1_89.sql (B) flight_summary against v1's flight summaries, loaded from a CSV by psql's \\copy. The test cannot run
psql meta-commands, so it drops the \\copy line and fills the temp table with rows of its own: it proves the classification
logic, not the owner's real v1 data (the script says so).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from test_flight_writer import HOUR, MIN, mk, snap, times, write

pytestmark = pytest.mark.integration
REPO = Path(__file__).resolve().parents[2]


def metrics(conn: Any, sql: str) -> tuple[dict[str, int], list[str]]:
    """(the numbered metric rows by name, the detail lines)."""
    rows = conn.execute(sql).fetchall()
    return {r["metric"]: int(r["value"]) for r in rows if r["ord"] < 100}, [r["metric"] for r in rows if r["ord"] >= 100]


def _v1(conn: Any, rows: list[tuple]) -> None:
    for r in rows:
        conn.execute("INSERT INTO v1_summary VALUES (%s, %s, %s, %s, %s, %s)", r)


def test_comparison_b_classifies_exact_split_amended_and_v3_only(conn: Any) -> None:
    t = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    # 311: one v3 flight, one matching v1 summary                                              -> exact
    mk(conn, 311, times(t, 30), callsign="QFA11")
    # 312: two sessions 20 minutes apart joined into ONE v3 flight; v1 has one summary         -> exact
    mk(conn, 312, times(t, 30), callsign="QFA12")
    mk(conn, 312, times(t + 50 * MIN, 30), callsign="QFA12", logon=t + 50 * MIN)
    # 313: v3 has two flights (logons 4 h apart); v1 has ONE summary spanning both             -> split
    mk(conn, 313, times(t, 30), callsign="QFA13")
    mk(conn, 313, times(t + 4 * HOUR, 30), callsign="QFA13", logon=t + 4 * HOUR)
    # 314: v3 final plan YSSY-YMML; v1 also kept the plan before the amendment, YSSY-YBBN      -> exact + v1-only (amendment)
    mk(conn, 314, times(t, 30), callsign="QFA14")
    # 315: v3 only
    mk(conn, 315, times(t, 30), callsign="QFA15")
    snap(conn, t + 40 * HOUR)
    assert write(conn) == 6

    sql = (REPO / "ops" / "compare_flights_v1_89.sql").read_text(encoding="utf-8")
    sql = "\n".join(line for line in sql.splitlines() if not line.startswith("\\"))     # no psql meta-commands over a driver
    head, tail = sql.split("WITH win AS", 1)
    conn.execute(head)                                                                   # creates the temp table
    end = t + 12 * HOUR
    _v1(conn, [(311, "QFA11", "YSSY", "YMML", t, end), (312, "QFA12", "YSSY", "YMML", t, end),
               (313, "QFA13", "YSSY", "YMML", t, t + 12 * HOUR), (314, "QFA14", "YSSY", "YMML", t, end),
               (314, "QFA14", "YSSY", "YBBN", t, end)])
    m, details = metrics(conn, "WITH win AS" + tail)
    assert m["v1 summaries in the window"] == 5
    assert m["v3 flights in the window (expected 867 on the design window)"] == 6
    assert m["exact 1-to-1 matches (expected 864)"] == 3
    assert m["v1 flights split in two or more by v3 (expected 0)"] == 1
    assert m["v1-only flights (expected 23)"] == 1
    assert m["  of which an amendment candidate (same pilot, callsign and logon, other departure or arrival; expected all)"] == 1
    assert m["v1 flights classed other (to explain by hand; expected 0)"] == 0
    assert m["v3-only flights (expected 3)"] == 1
    assert any(d.startswith("v3-only: cid 315") for d in details) and any("amendment candidate" in d and "YBBN" in d for d in details)


def test_comparison_b_leaves_out_flights_outside_the_window_and_in_the_power_cut(conn: Any) -> None:
    before = datetime(2026, 10, 8, 7, 0, tzinfo=UTC)                                    # logged on before v1 started polling
    cut = datetime(2026, 10, 9, 15, 35, tzinfo=UTC)                                     # inside the joint power cut
    mk(conn, 320, times(before, 30), callsign="OLD1")
    mk(conn, 321, times(cut, 30), callsign="CUT1")
    snap(conn, cut + 40 * HOUR)
    write(conn)
    sql = "\n".join(line for line in (REPO / "ops" / "compare_flights_v1_89.sql").read_text(encoding="utf-8").splitlines()
                    if not line.startswith("\\"))
    head, tail = sql.split("WITH win AS", 1)
    conn.execute(head)
    _v1(conn, [(320, "OLD1", "YSSY", "YMML", before, before + 8 * HOUR), (321, "CUT1", "YSSY", "YMML", cut, cut + 8 * HOUR)])
    m, _ = metrics(conn, "WITH win AS" + tail)
    assert m["v1 summaries in the window"] == 0 and m["v3 flights in the window (expected 867 on the design window)"] == 0
