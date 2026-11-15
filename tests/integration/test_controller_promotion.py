"""Issue 379: the `controller_promotion` view and the panels of atc/atc-promotions.json, on synthetic sessions.

Rules under test (owner decisions 15-11-2026 in issue 379): only logons over 20 minutes count; a promotion is the first
qualifying logon above every earlier one, with a lower-rated qualifying logon in the 60 days before; a first S1 is a
promotion from "new"; a first logon above S1 is not; a skipped rank is one promotion at the new rank; a drop and return is
not a promotion; C3/I3 flips are not promotions; a missing name falls back to the CID in the table.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import REPO

pytestmark = pytest.mark.integration

DASH = json.loads((REPO / "grafana" / "dashboards" / "atc" / "atc-promotions.json").read_text(encoding="utf-8"))
BASE = datetime(2026, 3, 2, 10, 0, 0, tzinfo=UTC)
S1, S2, S3, C1, C3, I3 = 2, 3, 4, 5, 7, 10


def logon(conn: Any, cid: int, day: float, minutes: float, rating: int, name: str = "Test Name") -> None:
    start = (BASE + timedelta(days=day)).replace(microsecond=0)
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (cid, f"TEST{cid}_TWR", start, name, rating, start, start + timedelta(minutes=minutes)),
    )


def promotions(conn: Any) -> list[tuple[int, str, str, datetime]]:
    rows = conn.execute("SELECT cid, from_rank, to_rank, promoted_at FROM controller_promotion ORDER BY cid, promoted_at").fetchall()
    return [(r["cid"], r["from_rank"], r["to_rank"], r["promoted_at"]) for r in rows]


def simple(conn: Any) -> list[tuple[int, str, str]]:
    return [(c, f, t) for c, f, t, _ in promotions(conn)]


def panel(title: str) -> dict[str, Any]:
    return next(p for p in DASH["panels"] if p.get("title") == title)


def run_panel(conn: Any, title: str, frm: str = "2026-01-01T00:00:00Z", to: str = "2026-06-01T00:00:00Z") -> list[dict[str, Any]]:
    sql = panel(title)["targets"][0]["rawSql"].replace("$__timeFrom()", f"'{frm}'").replace("$__timeTo()", f"'{to}'")
    conn.execute("SET ROLE grafana_ro")
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.execute("RESET ROLE")


def test_view_has_the_documented_columns(conn):
    logon(conn, 9790001, 0, 30, S1)
    assert set(conn.execute("SELECT * FROM controller_promotion").fetchone()) == {"cid", "name", "promoted_at", "from_rank", "to_rank"}


def test_nineteen_minute_logon_is_ignored_and_twenty_one_counts(conn):
    logon(conn, 9790001, 0, 19, S1)          # too short: no first S1
    logon(conn, 9790002, 0, 21, S1)          # long enough: a first S1
    logon(conn, 9790003, 0, 30, S1)
    logon(conn, 9790003, 5, 19, S2)          # a short logon at S2 is ignored everywhere
    logon(conn, 9790004, 0, 30, S1)
    logon(conn, 9790004, 5, 21, S2)
    logon(conn, 9790005, 0, 20, S1)          # exactly 20 minutes is not "longer than 20"
    assert simple(conn) == [(9790002, "new", "S1"), (9790003, "new", "S1"), (9790004, "new", "S1"), (9790004, "S1", "S2")]


def test_first_s1_is_a_promotion_from_new_dated_at_the_logon(conn):
    logon(conn, 9790001, 3, 45, S1)
    logon(conn, 9790001, 4, 45, S1)           # a second S1 logon is not another promotion
    assert promotions(conn) == [(9790001, "new", "S1", BASE + timedelta(days=3))]


def test_first_logon_above_s1_is_not_a_promotion(conn):
    logon(conn, 9790001, 0, 60, S2)
    logon(conn, 9790001, 5, 60, S2)
    logon(conn, 9790002, 0, 60, C3)
    assert promotions(conn) == []


def test_skipped_rank_is_one_promotion_at_the_new_rank(conn):
    logon(conn, 9790001, 0, 60, S1)
    logon(conn, 9790001, 30, 60, S3)
    logon(conn, 9790001, 40, 60, S3)
    assert simple(conn) == [(9790001, "new", "S1"), (9790001, "S1", "S3")]


def test_date_is_the_first_qualifying_logon_at_the_new_rating(conn):
    logon(conn, 9790001, 0, 60, S1)
    logon(conn, 9790001, 10, 15, S2)          # too short, so not the date
    logon(conn, 9790001, 12, 60, S2)
    logon(conn, 9790001, 14, 60, S2)
    assert promotions(conn)[1] == (9790001, "S1", "S2", BASE + timedelta(days=12))


def test_drop_and_return_is_not_a_promotion(conn):
    for day, rating in [(0, S1), (10, S2), (20, S1), (30, S2), (40, S1)]:
        logon(conn, 9790001, day, 60, rating)
    assert simple(conn) == [(9790001, "new", "S1"), (9790001, "S1", "S2")]


def test_gap_of_59_days_counts_and_61_days_does_not(conn):
    logon(conn, 9790001, 0, 60, S1)
    logon(conn, 9790001, 59, 60, S2)
    logon(conn, 9790002, 0, 60, S1)
    logon(conn, 9790002, 61, 60, S2)
    assert simple(conn) == [(9790001, "new", "S1"), (9790001, "S1", "S2"), (9790002, "new", "S1")]


def test_gap_is_measured_from_a_lower_logon_not_only_the_latest(conn):
    logon(conn, 9790001, 0, 60, S1)
    logon(conn, 9790001, 40, 60, S1)          # another S1 logon 21 days before the S2 logon: inside 60 days
    logon(conn, 9790001, 61, 60, S2)
    assert simple(conn) == [(9790001, "new", "S1"), (9790001, "S1", "S2")]


def test_c3_i3_flip_is_not_a_promotion(conn):
    logon(conn, 9790001, 0, 60, C1)
    logon(conn, 9790001, 5, 60, C3)           # the one real promotion
    logon(conn, 9790001, 10, 60, I3)
    logon(conn, 9790001, 15, 60, C3)
    logon(conn, 9790001, 20, 60, I3)
    logon(conn, 9790002, 0, 60, I3)           # instructor ratings alone never promote
    logon(conn, 9790002, 5, 60, C3)
    assert simple(conn) == [(9790001, "C1", "C3")]


def test_name_is_the_latest_non_blank_name_or_null(conn):
    logon(conn, 9790001, 0, 60, S1, name="Old Name")
    logon(conn, 9790001, 5, 60, S1, name="  ")
    logon(conn, 9790002, 0, 60, S1, name="")
    rows = {r["cid"]: r["name"] for r in conn.execute("SELECT cid, name FROM controller_promotion").fetchall()}
    assert rows == {9790001: "Old Name", 9790002: None}


def test_table_panel_newest_first_with_cid_where_no_name(conn):
    logon(conn, 9790001, 0, 60, S1, name="Alex Example")
    logon(conn, 9790001, 20, 60, S2, name="Alex Example")
    logon(conn, 9790002, 30, 60, S1, name="")
    rows = run_panel(conn, "Recent promotions")
    assert [(r["Date"], r["Name"], r["CID"], r["From"], r["To"]) for r in rows] == [
        ("01-04-2026", "9790002", "9790002", "new", "S1"),
        ("22-03-2026", "Alex Example", "9790001", "S1", "S2"),
        ("02-03-2026", "Alex Example", "9790001", "new", "S1"),
    ]


def test_bar_panels_show_zero_for_empty_months_and_sum_to_the_line(conn):
    logon(conn, 9790001, 0, 60, S1)           # March
    logon(conn, 9790002, 0, 60, S1)
    logon(conn, 9790002, 50, 60, S2)          # April 21
    bars = run_panel(conn, "Promotions to S1 per month", to="2026-05-15T00:00:00Z")
    assert [(r["month"], r["promotions"]) for r in bars] == [("2026-01", 0), ("2026-02", 0), ("2026-03", 2), ("2026-04", 0), ("2026-05", 0)]
    s2 = run_panel(conn, "Promotions to S2 per month", to="2026-05-15T00:00:00Z")
    assert [r["promotions"] for r in s2] == [0, 0, 0, 1, 0]
    for rank in ("S3", "C1", "C3"):
        assert {r["promotions"] for r in run_panel(conn, f"Promotions to {rank} per month")} == {0}
    line = run_panel(conn, "All promotions per month", to="2026-05-15T00:00:00Z")
    assert [r["All promotions"] for r in line] == [0, 0, 2, 1, 0]


def test_cards(conn):
    logon(conn, 9790001, 0, 60, S1)
    logon(conn, 9790001, 20, 60, S2)
    assert run_panel(conn, "Promotions in the selected period")[0]["promotions"] == 2
    assert run_panel(conn, "Promotions in the selected period", to="2026-03-10T00:00:00Z")[0]["promotions"] == 1
    assert run_panel(conn, "Date of the last promotion")[0]["last_promotion"] == "22-03-2026"
    assert run_panel(conn, "Promotions this month")[0]["promotions"] == 0     # synthetic data is in March 2026


def test_card_with_no_promotions(conn):
    assert run_panel(conn, "Date of the last promotion")[0]["last_promotion"] == "none"
    assert run_panel(conn, "Promotions in the selected period")[0]["promotions"] == 0
