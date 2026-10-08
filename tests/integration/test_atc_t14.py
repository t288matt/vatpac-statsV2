"""T14: ATC overview panels rebuilt from the edited v1 Metabase cards (63, 116, 137).

Synthetic controller sessions are inserted at fixed offsets from the start of the current UTC month, then the
panel SQL is read from grafana/dashboards/atc/atc-overview.json, the Grafana time macros are expanded and the
query is executed, so the test covers the SQL that Grafana runs.

Rules under test (v1 cards as edited on 17-10-2025 and 18-10-2025):
  card 63   per calendar month, distinct CTR/FSS controllers with a session over 60 minutes that started in the
            30 days before the end of the month
  card 137  the same for any position, session over 30 minutes
  card 116  per calendar month, distinct controllers per rating group whose sessions at that rating total over
            60 minutes (a rating change inside a month is judged separately at each rating)
Sessions are placed on day 15 of a month (offset 14 days) so the 30-day window of a 28 to 31 day month
always contains them and no neighbouring month's window does.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO

pytestmark = pytest.mark.integration

DASHBOARD = REPO / "grafana" / "dashboards" / "atc" / "atc-overview.json"

CTR_TITLE = "CTR and FSS controllers active (30 days to month end, monthly)"
ALL_TITLE = "Controllers active (30 days to month end, monthly)"
RATING_TITLE = "Controllers logged on each month, by rating group"


def month_start(months_ago: int) -> datetime:
    """00:00 UTC on the first day of the month, `months_ago` months before the current one."""
    now = datetime.now(UTC)
    index = now.year * 12 + (now.month - 1) - months_ago
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=UTC)


M2, M1 = month_start(2), month_start(1)


def panel_sql(title: str) -> str:
    panels = json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"]
    sql = next(p["targets"][0]["rawSql"] for p in panels if p.get("title") == title)
    now = datetime.now(UTC)
    frm, to = now - timedelta(days=365), now
    return (sql.replace("$__timeFrom()", f"'{frm.isoformat()}'")
               .replace("$__timeTo()", f"'{to.isoformat()}'"))


def add_session(conn, cid: int, month: datetime, minutes: int, *, callsign: str | None = None,
                rating: int = 3, day_offset: int = 14, hour: int = 0) -> None:
    start = month + timedelta(days=day_offset, hours=hour)
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (cid, callsign or f"TEST{cid}_TWR", start, f"Controller {cid}", rating, start,
         start + timedelta(minutes=minutes)),
    )


def by_month(conn, title: str) -> dict[datetime, dict]:
    rows = conn.execute(panel_sql(title)).fetchall()
    return {r["time"]: r for r in rows}


def test_ctr_fss_monthly_count(conn):
    add_session(conn, 9100001, M1, 90, callsign="TEST_CTR")                 # counts
    add_session(conn, 9100001, M1, 90, callsign="TEST_FSS", hour=5)         # same controller: still one
    add_session(conn, 9100002, M1, 45, callsign="TEST2_CTR")                # 60 minutes or less is not enough
    add_session(conn, 9100003, M1, 120, callsign="TEST3_TWR")               # not CTR or FSS
    add_session(conn, 9100001, M2, 90, callsign="TEST_CTR")
    add_session(conn, 9100004, M2, 61, callsign="TEST4_FSS")
    rows = by_month(conn, CTR_TITLE)
    assert rows[M2]["Controllers"] == 2
    assert rows[M1]["Controllers"] == 1
    assert set(rows) == {M2, M1}                 # months run from the first session to the last, no further


def test_all_positions_monthly_count_uses_30_minutes(conn):
    add_session(conn, 9100001, M1, 45)                                      # counts (over 30)
    add_session(conn, 9100002, M1, 30)                                      # exactly 30 minutes: not over 30
    add_session(conn, 9100003, M1, 90, callsign="TEST3_CTR")                # any position counts
    add_session(conn, 9100001, M2, 31)
    rows = by_month(conn, ALL_TITLE)
    assert rows[M1]["Controllers"] == 2
    assert rows[M2]["Controllers"] == 1


def test_ctr_fss_never_exceeds_all_positions(conn):
    add_session(conn, 9100001, M1, 90, callsign="TEST_CTR")
    add_session(conn, 9100002, M1, 50)
    ctr, everyone = by_month(conn, CTR_TITLE), by_month(conn, ALL_TITLE)
    assert all(ctr[m]["Controllers"] <= everyone[m]["Controllers"] for m in ctr)


def test_month_with_no_qualifying_session_is_drawn_as_zero(conn):
    add_session(conn, 9100001, M2, 90, callsign="TEST_CTR")
    add_session(conn, 9100002, M1, 20, callsign="TEST2_CTR")                # short: month M1 is in range, count 0
    rows = by_month(conn, CTR_TITLE)
    assert rows[M2]["Controllers"] == 1
    assert rows[M1]["Controllers"] == 0


def test_no_sessions_gives_no_rows(conn):
    for title in (CTR_TITLE, ALL_TITLE, RATING_TITLE):
        assert conn.execute(panel_sql(title)).fetchall() == []


def test_rating_groups_by_month(conn):
    add_session(conn, 9100010, M1, 40, rating=2, day_offset=10)             # S1: two sessions, 70 minutes in total
    add_session(conn, 9100010, M1, 30, rating=2, day_offset=11)
    add_session(conn, 9100011, M1, 50, rating=2)                            # S1 but only 50 minutes: not counted
    add_session(conn, 9100012, M1, 90, rating=5)                            # C1
    add_session(conn, 9100013, M1, 90, rating=8)                            # I1+
    add_session(conn, 9100014, M1, 120, rating=6)                           # C2 is not a column
    add_session(conn, 9100015, M2, 80, rating=3)                            # S2 in the earlier month
    rows = by_month(conn, RATING_TITLE)
    one = {k: v for k, v in rows[M1].items() if k != "time"}
    assert one == {"S1": 1, "S2": 0, "S3": 0, "C1": 1, "C3": 0, "I1+": 1}
    two = {k: v for k, v in rows[M2].items() if k != "time"}
    assert two == {"S1": 0, "S2": 1, "S3": 0, "C1": 0, "C3": 0, "I1+": 0}


def test_rating_change_inside_a_month_is_judged_per_rating(conn):
    """v1 quirk, kept on purpose: minutes are totalled per (controller, rating), not per controller."""
    add_session(conn, 9100020, M1, 40, rating=2, day_offset=10)
    add_session(conn, 9100020, M1, 40, rating=3, day_offset=12)             # 80 minutes in all, 40 at each rating
    row = by_month(conn, RATING_TITLE)[M1]
    assert row["S1"] == 0 and row["S2"] == 0


def test_a_session_counts_in_the_month_it_started(conn):
    start = M1 - timedelta(hours=1)                                        # 23:00 UTC on the last day of M2
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) "
        "VALUES (9100030, 'TEST_CTR', %s, 'Controller 9100030', 3, %s, %s)",
        (start, start, start + timedelta(minutes=120)),
    )
    add_session(conn, 9100031, M1, 90, rating=3)                            # makes M1 appear
    rows = by_month(conn, RATING_TITLE)
    assert rows[M2]["S2"] == 1 and rows[M1]["S2"] == 1


def test_every_changed_panel_query_runs(conn):
    add_session(conn, 9100001, M1, 90, callsign="TEST_CTR")
    for title in (CTR_TITLE, ALL_TITLE, RATING_TITLE):
        conn.execute(panel_sql(title)).fetchall()
