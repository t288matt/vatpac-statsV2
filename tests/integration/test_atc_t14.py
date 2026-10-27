"""T14: ATC overview panels rebuilt from the edited v1 Metabase cards (63, 116, 137).

Synthetic controller sessions are inserted at fixed offsets from the start of the current UTC month or week,
then the panel SQL is read from grafana/dashboards/atc/atc-overview.json, the Grafana time macros are
expanded and the query is executed, so the test covers the SQL that Grafana runs.

Rules under test:
  card 63   (panel 7, unchanged, still monthly) per calendar month, distinct CTR/FSS controllers with a
            session over 60 minutes that started in the 30 days before the end of the month
  card 137  (panel 8, issue #259, 27-10-2026: month -> week) per calendar WEEK, distinct controllers (any
            position) with a session over 30 minutes that started in the 30 days before the end of the week;
            the 30-day rolling window itself is unchanged, only the bucket at which it is evaluated moved
            from month end to week end
  card 116  (panel 6, issue #259, 27-10-2026: month -> week) per calendar WEEK, distinct controllers per
            rating group whose sessions at that rating total over 60 minutes that week (a rating change
            inside the bucket is judged separately at each rating, same per-bucket rule as before)

Card 63 (panel 7) sessions are placed on day 15 of a month (offset 14 days) so the 30-day window of a 28 to
31 day month always contains them and no neighbouring month's window does: the window is about as wide as
the bucket, so a session shows in exactly one bucket.

Card 137 (panel 8) is now weekly but the window is still 30 days wide, about 4 to 5 times the 7-day bucket,
so a single session is picked up by several consecutive week-rows (the one it falls in, then the next few
as the rolling window slides past it). Its test therefore checks two widely separated target weeks (1 and 6
weeks ago, a 35-day gap) rather than relying on a session appearing in exactly one bucket.

Card 116 (panel 6) buckets are not rolling (a plain per-week sum, like the old per-month sum), so a session
placed inside a given week only ever contributes to that week's row; its tests use in-week day offsets
(0-6 days from the Monday) the same way the old tests used in-month day offsets.

Panel 15 "CTR hours staffed per week" (issue #259, 27-10-2026) has no v1 card: a plain per-week sum of
`duration_minutes` on `controller_session_ranked` filtered to `facility = 6` (the VATSIM CTR code), not the
`is_ctr_fss` flag used elsewhere on this dashboard.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO

pytestmark = pytest.mark.integration

DASHBOARD = REPO / "grafana" / "dashboards" / "atc" / "atc-overview.json"

CTR_TITLE = "CTR and FSS controllers active (30 days to month end, monthly)"
ALL_TITLE = "Controllers active (30 days to week end, weekly)"
RATING_TITLE = "Controllers logged on each week, by rating group"
CTR_HOURS_TITLE = "CTR hours staffed per week"


def month_start(months_ago: int) -> datetime:
    """00:00 UTC on the first day of the month, `months_ago` months before the current one."""
    now = datetime.now(UTC)
    index = now.year * 12 + (now.month - 1) - months_ago
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=UTC)


def week_start(weeks_ago: int) -> datetime:
    """00:00 UTC on the Monday `weeks_ago` weeks before the current one.

    Matches Postgres `date_trunc('week', ts)`, which buckets to the Monday 00:00 of the ISO week
    containing `ts` (verified directly against a live Postgres 16 container, not assumed).
    """
    now = datetime.now(UTC)
    this_monday = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=now.weekday())
    return this_monday - timedelta(weeks=weeks_ago)


M2, M1 = month_start(2), month_start(1)
W1, W2 = week_start(1), week_start(2)
W_FAR = week_start(6)  # far enough from W1 (35 days) that the 30-day rolling window of panel 8 cannot bridge them


def panel_sql(title: str) -> str:
    panels = json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"]
    sql = next(p["targets"][0]["rawSql"] for p in panels if p.get("title") == title)
    now = datetime.now(UTC)
    frm, to = now - timedelta(days=365), now
    return (sql.replace("$__timeFrom()", f"'{frm.isoformat()}'")
               .replace("$__timeTo()", f"'{to.isoformat()}'"))


def add_session(conn, cid: int, period_start: datetime, minutes: int, *, callsign: str | None = None,
                rating: int = 3, facility: int | None = None, day_offset: int = 14, hour: int = 0) -> None:
    start = period_start + timedelta(days=day_offset, hours=hour)
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, facility, first_seen, last_seen) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (cid, callsign or f"TEST{cid}_TWR", start.replace(microsecond=0), f"Controller {cid}", rating, facility,
         start, start + timedelta(minutes=minutes)),
    )


def rows_by_bucket(conn, title: str) -> dict[datetime, dict]:
    rows = conn.execute(panel_sql(title)).fetchall()
    return {r["time"]: r for r in rows}


def test_ctr_fss_monthly_count(conn):
    add_session(conn, 9100001, M1, 90, callsign="TEST_CTR")                 # counts
    add_session(conn, 9100001, M1, 90, callsign="TEST_FSS", hour=5)         # same controller: still one
    add_session(conn, 9100002, M1, 45, callsign="TEST2_CTR")                # 60 minutes or less is not enough
    add_session(conn, 9100003, M1, 120, callsign="TEST3_TWR")               # not CTR or FSS
    add_session(conn, 9100001, M2, 90, callsign="TEST_CTR")
    add_session(conn, 9100004, M2, 61, callsign="TEST4_FSS")
    rows = rows_by_bucket(conn, CTR_TITLE)
    assert rows[M2]["Controllers"] == 2
    assert rows[M1]["Controllers"] == 1
    assert set(rows) == {M2, M1}                 # months run from the first session to the last, no further


def test_month_with_no_qualifying_session_is_drawn_as_zero(conn):
    add_session(conn, 9100001, M2, 90, callsign="TEST_CTR")
    add_session(conn, 9100002, M1, 20, callsign="TEST2_CTR")                # short: month M1 is in range, count 0
    rows = rows_by_bucket(conn, CTR_TITLE)
    assert rows[M2]["Controllers"] == 1
    assert rows[M1]["Controllers"] == 0


def test_ctr_fss_never_exceeds_all_positions(conn):
    """The CTR/FSS panel (7, unchanged, monthly, 60-minute threshold) never counts a controller that the
    all-positions panel (8, issue #259, now weekly, 30-minute threshold, 30-day rolling window) would not
    also count. Panel 8 no longer shares panel 7's month buckets, so this cannot be checked key-for-key any
    more: instead, for the one qualifying session, every weekly bucket of panel 8 whose 30-day window covers
    that session's timestamp must show at least one controller."""
    add_session(conn, 9100001, M1, 90, callsign="TEST_CTR")                 # qualifies for both panels
    add_session(conn, 9100002, M1, 50)                                      # qualifies for panel 8 only (not CTR/FSS)
    ctr = rows_by_bucket(conn, CTR_TITLE)
    everyone = rows_by_bucket(conn, ALL_TITLE)
    assert ctr[M1]["Controllers"] == 1                                     # sanity: panel 7 sees the CTR session
    session_time = M1 + timedelta(days=14)
    # panel 8's window for week bucket w is [w + 1 week - 30 days, w + 1 week); translated directly from the SQL.
    covering = [w for w in everyone if w - timedelta(days=23) <= session_time < w + timedelta(days=7)]
    assert covering, "no weekly bucket of panel 8 covers the test session; the fixture or the window math is wrong"
    assert all(everyone[w]["Controllers"] >= 1 for w in covering)


def test_no_sessions_gives_no_rows(conn):
    for title in (CTR_TITLE, ALL_TITLE, RATING_TITLE):
        assert conn.execute(panel_sql(title)).fetchall() == []


def test_all_positions_weekly_count_uses_30_minutes(conn):
    add_session(conn, 9100001, W1, 45, day_offset=1)                        # counts (over 30)
    add_session(conn, 9100002, W1, 30, day_offset=2)                        # exactly 30 minutes: not over 30
    add_session(conn, 9100003, W1, 90, callsign="TEST3_CTR", day_offset=3)  # any position counts
    add_session(conn, 9100001, W_FAR, 31, day_offset=1)                     # same cid, a different week: counted again
    rows = rows_by_bucket(conn, ALL_TITLE)
    assert rows[W1]["Controllers"] == 2
    assert rows[W_FAR]["Controllers"] == 1


def test_rating_groups_by_week(conn):
    add_session(conn, 9100010, W1, 40, rating=2, day_offset=1)              # S1: two sessions, 70 minutes in total
    add_session(conn, 9100010, W1, 30, rating=2, day_offset=2)
    add_session(conn, 9100011, W1, 50, rating=2, day_offset=3)              # S1 but only 50 minutes: not counted
    add_session(conn, 9100012, W1, 90, rating=5, day_offset=1)              # C1
    add_session(conn, 9100013, W1, 90, rating=8, day_offset=1)              # I1+
    add_session(conn, 9100014, W1, 120, rating=6, day_offset=1)             # C2 is not a column
    add_session(conn, 9100015, W2, 80, rating=3, day_offset=1)              # S2 in the earlier week
    rows = rows_by_bucket(conn, RATING_TITLE)
    one = {k: v for k, v in rows[W1].items() if k != "time"}
    assert one == {"S1": 1, "S2": 0, "S3": 0, "C1": 1, "C3": 0, "I1+": 1}
    two = {k: v for k, v in rows[W2].items() if k != "time"}
    assert two == {"S1": 0, "S2": 1, "S3": 0, "C1": 0, "C3": 0, "I1+": 0}


def test_rating_change_inside_a_week_is_judged_per_rating(conn):
    """v1 quirk, kept on purpose: minutes are totalled per (controller, rating), not per controller."""
    add_session(conn, 9100020, W1, 40, rating=2, day_offset=1)
    add_session(conn, 9100020, W1, 40, rating=3, day_offset=3)              # 80 minutes in all, 40 at each rating
    row = rows_by_bucket(conn, RATING_TITLE)[W1]
    assert row["S1"] == 0 and row["S2"] == 0


def test_a_session_counts_in_the_week_it_started(conn):
    start = W1 - timedelta(hours=1)                                        # 23:00 UTC on the last day of W2
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) "
        "VALUES (9100030, 'TEST_CTR', %s, 'Controller 9100030', 3, %s, %s)",
        (start.replace(microsecond=0), start, start + timedelta(minutes=120)),
    )
    add_session(conn, 9100031, W1, 90, rating=3, day_offset=1)              # makes W1 appear
    rows = rows_by_bucket(conn, RATING_TITLE)
    assert rows[W2]["S2"] == 1 and rows[W1]["S2"] == 1


def test_every_changed_panel_query_runs(conn):
    add_session(conn, 9100001, M1, 90, callsign="TEST_CTR")
    for title in (CTR_TITLE, ALL_TITLE, RATING_TITLE):
        conn.execute(panel_sql(title)).fetchall()


def test_ctr_hours_per_week_counts_only_facility_6(conn):
    """New panel (id 15, issue #259, 27-10-2026, no v1 card): facility = 6 is the VATSIM CTR code; a session
    at a different facility (4, TWR) in the same week must not add to the CTR hours total."""
    add_session(conn, 9100050, W1, 90, facility=6, day_offset=1)             # CTR: 90 minutes = 1.5 hours
    add_session(conn, 9100051, W1, 60, facility=4, day_offset=2)             # TWR: excluded
    rows = rows_by_bucket(conn, CTR_HOURS_TITLE)
    assert float(rows[W1]["CTR hours"]) == 1.5
