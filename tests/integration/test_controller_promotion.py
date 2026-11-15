"""Issue 379: the `controller_promotion` view and the panels of atc/atc-promotions.json, on synthetic rows.

Rules under test (owner decisions 15-11-2026 in issue 379): the first session per rating comes from each member's VATSIM
history (controller_rating_first_seen) or from our own sessions, whichever is earlier; only sessions over 20 minutes count; a
promotion is the first session above every earlier rating; a first ever S1 is a promotion from "new"; a first ever session
above S1 is not; a skipped rank is one promotion; a drop and return is not; C3/I3 flips are not; only CIDs with a backfill
marker are included; a missing name falls back to the CID in the table.
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
OBS, S1, S2, S3, C1, C3, I3 = 1, 2, 3, 4, 5, 7, 10


def logon(conn: Any, cid: int, day: float, minutes: float, rating: int, name: str = "Test Name") -> None:
    start = (BASE + timedelta(days=day)).replace(microsecond=0)
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (cid, f"TEST{cid}_TWR", start, name, rating, start, start + timedelta(minutes=minutes)),
    )


def mark(conn: Any, cid: int) -> None:
    conn.execute("INSERT INTO controller_rating_backfill (cid) VALUES (%s) ON CONFLICT DO NOTHING", (cid,))


def hist(conn: Any, cid: int, day: float, rating: int, marker: bool = True) -> None:
    """A backfilled row: the first long session at this rating. The marker row is added unless told otherwise."""
    conn.execute("INSERT INTO controller_rating_first_seen (cid, rating, first_session_start) VALUES (%s, %s, %s)",
                 (cid, rating, BASE + timedelta(days=day)))
    if marker:
        mark(conn, cid)


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
    hist(conn, 9790001, 0, S1)
    assert list(conn.execute("SELECT * FROM controller_promotion").fetchone()) == ["cid", "name", "promoted_at", "from_rank", "to_rank"]


def test_history_rows_give_promotions_dated_at_the_first_session(conn):
    hist(conn, 9790001, 0, S1)
    hist(conn, 9790001, 5, S2, marker=False)
    assert promotions(conn) == [(9790001, "new", "S1", BASE), (9790001, "S1", "S2", BASE + timedelta(days=5))]


def test_our_sessions_19_minutes_ignored_21_counts_20_does_not(conn):
    for cid in (9790001, 9790002, 9790003):
        hist(conn, cid, -50, S1)
    logon(conn, 9790001, 0, 19, S2)           # too short
    logon(conn, 9790002, 0, 21, S2)           # long enough
    logon(conn, 9790003, 0, 20, S2)           # exactly 20 is not "longer than 20"
    assert [c for c, f, t in simple(conn) if t == "S2"] == [9790002]


def test_first_ever_s1_is_a_promotion_from_new_dated_at_the_session(conn):
    hist(conn, 9790001, 3, S1)
    logon(conn, 9790001, 4, 45, S1)           # a later S1 session is not another promotion
    assert promotions(conn) == [(9790001, "new", "S1", BASE + timedelta(days=3))]


def test_returning_s1_with_an_earlier_s1_row_is_not_counted_as_new_in_our_data(conn):
    hist(conn, 9790001, -400, S1)             # S1 long ago in the history; our data first sees them now
    logon(conn, 9790001, 0, 60, S1)
    assert promotions(conn) == [(9790001, "new", "S1", BASE - timedelta(days=400))]


def test_first_ever_session_above_s1_is_not_a_promotion(conn):
    hist(conn, 9790001, 0, S2)
    hist(conn, 9790002, 0, C3)
    assert promotions(conn) == []


def test_observer_history_gives_from_obs_and_a_lone_observer_is_nothing(conn):
    hist(conn, 9790001, 0, OBS)
    hist(conn, 9790001, 10, S1, marker=False)
    hist(conn, 9790002, 0, OBS)
    assert simple(conn) == [(9790001, "OBS", "S1")]


def test_skipped_rank_is_one_promotion_at_the_new_rank(conn):
    hist(conn, 9790001, 0, S1)
    hist(conn, 9790001, 30, S3, marker=False)
    assert simple(conn) == [(9790001, "new", "S1"), (9790001, "S1", "S3")]


def test_drop_and_return_is_not_a_promotion(conn):
    hist(conn, 9790001, 0, S1)
    hist(conn, 9790001, 10, S2, marker=False)
    for day, rating in [(20, S1), (30, S2), (40, S1)]:    # our sessions repeat ranks already held
        logon(conn, 9790001, day, 60, rating)
    assert simple(conn) == [(9790001, "new", "S1"), (9790001, "S1", "S2")]


def test_c3_i3_flip_is_not_a_promotion(conn):
    hist(conn, 9790001, 0, C1)
    hist(conn, 9790001, 5, C3, marker=False)
    for day, rating in [(10, I3), (15, C3), (20, I3)]:
        logon(conn, 9790001, day, 60, rating)
    hist(conn, 9790002, 0, I3)                 # an instructor rating alone never promotes
    hist(conn, 9790002, 5, C3, marker=False)
    assert simple(conn) == [(9790001, "C1", "C3")]


def test_cid_without_a_backfill_marker_is_not_counted(conn):
    conn.execute("INSERT INTO controller_rating_first_seen (cid, rating, first_session_start) VALUES (9790001, %s, %s)", (S1, BASE))
    logon(conn, 9790001, 0, 60, S1)
    logon(conn, 9790001, 5, 60, S2)
    assert promotions(conn) == []


def test_our_own_later_session_supplies_a_new_rank_after_the_backfill(conn):
    hist(conn, 9790001, -100, S1)
    logon(conn, 9790001, 20, 60, S2)
    logon(conn, 9790001, 25, 60, S2)
    assert promotions(conn)[-1] == (9790001, "S1", "S2", BASE + timedelta(days=20))


def test_first_session_is_the_earlier_of_table_and_our_data(conn):
    hist(conn, 9790001, 0, S1)
    hist(conn, 9790001, 50, S2, marker=False)          # the table says S2 on day 50 ...
    logon(conn, 9790001, 40, 60, S2)                   # ... but our data saw it on day 40
    assert promotions(conn)[-1] == (9790001, "S1", "S2", BASE + timedelta(days=40))


def test_name_is_the_latest_non_blank_name_or_null(conn):
    hist(conn, 9790001, 0, S1)
    hist(conn, 9790002, 0, S1)
    logon(conn, 9790001, 0, 60, S1, name="Old Name")
    logon(conn, 9790001, 5, 60, S1, name="  ")
    logon(conn, 9790002, 0, 60, S1, name="")
    rows = {r["cid"]: r["name"] for r in conn.execute("SELECT cid, name FROM controller_promotion").fetchall()}
    assert rows == {9790001: "Old Name", 9790002: None}


def test_table_panel_newest_first_with_cid_where_no_name(conn):
    hist(conn, 9790001, 0, S1)
    hist(conn, 9790001, 20, S2, marker=False)
    hist(conn, 9790002, 30, S1)
    logon(conn, 9790001, 0, 60, S1, name="Alex Example")
    rows = run_panel(conn, "Recent promotions")
    assert [(r["Date"], r["Name"], r["CID"], r["From"], r["To"]) for r in rows] == [
        ("01-04-2026", "9790002", "9790002", "new", "S1"),
        ("22-03-2026", "Alex Example", "9790001", "S1", "S2"),
        ("02-03-2026", "Alex Example", "9790001", "new", "S1"),
    ]


def test_line_panels_show_zero_for_empty_months_and_sum_to_the_all_line(conn):
    hist(conn, 9790001, 0, S1)                 # March
    hist(conn, 9790002, 0, S1)
    hist(conn, 9790002, 50, S2, marker=False)  # April 21
    to = "2026-05-15T00:00:00Z"
    s1 = run_panel(conn, "Promotions to S1 per month", to=to)
    assert [(r["time"].strftime("%Y-%m"), r["S1"]) for r in s1] == [("2026-01", 0), ("2026-02", 0), ("2026-03", 2), ("2026-04", 0), ("2026-05", 0)]
    assert [r["S2"] for r in run_panel(conn, "Promotions to S2 per month", to=to)] == [0, 0, 0, 1, 0]
    for rank in ("S3", "C1", "C3"):
        assert {r[rank] for r in run_panel(conn, f"Promotions to {rank} per month")} == {0}
    assert [r["All promotions"] for r in run_panel(conn, "All promotions per month", to=to)] == [0, 0, 2, 1, 0]


def test_month_series_covers_a_fourteen_month_range(conn):
    hist(conn, 9790001, 0, S1)
    rows = run_panel(conn, "All promotions per month", frm="2025-01-10T00:00:00Z", to="2026-03-05T00:00:00Z")
    assert len(rows) == 15 and rows[0]["time"].strftime("%Y-%m") == "2025-01" and rows[-1]["All promotions"] == 1


def test_cards(conn):
    hist(conn, 9790001, 0, S1)
    hist(conn, 9790001, 20, S2, marker=False)
    assert run_panel(conn, "Promotions in the selected period")[0]["promotions"] == 2
    assert run_panel(conn, "Promotions in the selected period", to="2026-03-10T00:00:00Z")[0]["promotions"] == 1
    assert run_panel(conn, "Date of the last promotion")[0]["last_promotion"] == "22-03-2026"
    assert run_panel(conn, "Promotions this month")[0]["promotions"] == 0     # synthetic data is in March 2026


def test_card_with_no_promotions(conn):
    assert run_panel(conn, "Date of the last promotion")[0]["last_promotion"] == "none"
    assert run_panel(conn, "Promotions in the selected period")[0]["promotions"] == 0


def test_grafana_ro_reads_the_view_but_not_the_tables(conn):
    hist(conn, 9790001, 0, S1)
    conn.execute("SET ROLE grafana_ro")
    try:
        assert conn.execute("SELECT count(*) AS n FROM controller_promotion").fetchone()["n"] == 1
    finally:
        conn.execute("RESET ROLE")
    for table in ("controller_rating_first_seen", "controller_rating_backfill"):
        conn.execute("SET ROLE grafana_ro")
        try:
            with pytest.raises(Exception, match="permission denied"):
                conn.execute(f"SELECT * FROM {table}")
        finally:
            conn.rollback()
            conn.execute("RESET ROLE")
