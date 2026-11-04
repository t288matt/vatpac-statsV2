"""Group G of the database-questions catalogue (issue #267, 04-11-2026): ATC hours staffed by position type.

Two panels on their own dashboard, atc/atc-position-hours.json ("ATC: position hours", short address /atc-hours):
  "ATC hours staffed per week, by position type"  (stacked bars, one series per position type)
  "ATC hours staffed by position type (7 days, ...)" (table, eight fixed rows, four windows)

The rule under test (position-hours): time from merged controller sessions, clipped to the period shown, summed per
position type; the type is the callsign suffix, so APP and DEP are separate and CTR excludes FSS; two CTR sectors online
together for 3 hours count as 6. The expected numbers below are worked out by hand, or by a small independent
Python overlap function, never by the SQL under test.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO

pytestmark = pytest.mark.integration

DASHBOARD = REPO / "grafana" / "dashboards" / "atc" / "atc-position-hours.json"
WEEKLY = "ATC hours staffed per week, by position type"
TABLE = "ATC hours staffed by position type (7 days, 30 days, last full month, since go-live)"
TYPES = ["CTR", "TWR", "APP", "DEP", "GND", "DEL", "FSS", "FMP"]
WINDOWS = ["Last 7 days", "Last 30 days", "Last full month", "Since go-live"]


def panel(title: str) -> dict:
    return next(p for p in json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"] if p.get("title") == title)


def sql_of(title: str, frm: datetime | None = None) -> str:
    now = datetime.now(UTC)
    frm, to = frm or now - timedelta(days=365), now + timedelta(days=1)
    return (panel(title)["targets"][0]["rawSql"]
            .replace("$__timeFrom()", f"'{frm.isoformat()}'").replace("$__timeTo()", f"'{to.isoformat()}'"))


def monday(weeks_ago: int) -> datetime:
    """00:00 UTC on the Monday `weeks_ago` weeks before the current week (the same bucket as date_trunc('week'))."""
    now = datetime.now(UTC)
    this = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=now.weekday())
    return this - timedelta(weeks=weeks_ago)


def add(conn, cid: int, callsign: str, start: datetime, minutes: int) -> None:
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, facility, first_seen, last_seen) "
        "VALUES (%s, %s, %s, %s, 5, NULL, %s, %s)",
        (cid, callsign, start.replace(microsecond=0), f"Controller {cid}", start, start + timedelta(minutes=minutes)),
    )


def weekly(conn, frm: datetime | None = None) -> dict[tuple[datetime, str], float]:
    return {(r["time"], r["metric"]): float(r["hours"]) for r in conn.execute(sql_of(WEEKLY, frm)).fetchall()}


def table(conn) -> dict[str, dict]:
    return {r["Position type"]: r for r in conn.execute(sql_of(TABLE)).fetchall()}


def overlap_hours(start: datetime, minutes: int, lo: datetime, hi: datetime) -> float:
    """Independent oracle: hours of [start, start + minutes] that fall inside [lo, hi]."""
    end = start + timedelta(minutes=minutes)
    secs = (min(end, hi) - max(start, lo)).total_seconds()
    return round(max(secs, 0) / 3600.0, 1)


def test_every_whitelisted_suffix_maps_to_its_own_type_and_others_are_left_out(conn):
    w = monday(2)
    for i, t in enumerate(TYPES):
        add(conn, 9300000 + i, f"TEST{i}_{t}", w + timedelta(days=1, hours=i), 60 + 30 * i)  # 1.0, 1.5, 2.0 ... hours
    add(conn, 9300050, "TEST_ATIS", w + timedelta(days=2), 120)    # not a staffed position type
    add(conn, 9300051, "TEST_OBS", w + timedelta(days=2), 120)
    got = weekly(conn)
    assert {m for (_, m) in got} == set(TYPES)
    for i, t in enumerate(TYPES):
        assert got[(w, t)] == (60 + 30 * i) / 60.0
    assert len(got) == 8


def test_ctr_excludes_fss_and_app_is_separate_from_dep(conn):
    w = monday(2)
    add(conn, 9300101, "TEST_CTR", w + timedelta(days=1), 60)
    add(conn, 9300102, "TEST_FSS", w + timedelta(days=1), 120)
    add(conn, 9300103, "TEST_APP", w + timedelta(days=1), 90)
    add(conn, 9300104, "TEST_DEP", w + timedelta(days=1), 30)
    got = weekly(conn)
    assert got[(w, "CTR")] == 1.0 and got[(w, "FSS")] == 2.0
    assert got[(w, "APP")] == 1.5 and got[(w, "DEP")] == 0.5


def test_two_sectors_staffed_together_count_as_position_hours(conn):
    w = monday(2)
    add(conn, 9300201, "TEST_A_CTR", w + timedelta(days=2), 180)   # two CTR sectors online for the same 3 hours
    add(conn, 9300202, "TEST_B_CTR", w + timedelta(days=2), 180)
    assert weekly(conn)[(w, "CTR")] == 6.0


def test_a_session_across_the_week_boundary_is_split_between_the_weeks(conn):
    """Sunday 23:00 UTC for 3 hours: one hour in the week it started, two in the next. Crediting it all to the
    start week (as the older CTR panel does) would give 3.0 and 0.0 and fail this test."""
    w1, w2 = monday(3), monday(2)
    add(conn, 9300301, "TEST_TWR", w2 - timedelta(hours=1), 180)
    got = weekly(conn)
    assert got[(w1, "TWR")] == 1.0
    assert got[(w2, "TWR")] == 2.0


def test_a_reconnect_within_five_minutes_is_one_session(conn):
    w = monday(2)
    start = w + timedelta(days=3)
    add(conn, 9300401, "TEST_GND", start, 30)
    add(conn, 9300401, "TEST_GND", start + timedelta(minutes=32), 28)   # 2 minutes later: same merged session, 60 minutes in all
    assert weekly(conn)[(w, "GND")] == 1.0


def test_table_has_eight_fixed_rows_and_a_type_with_no_sessions_shows_zero(conn):
    now = datetime.now(UTC)
    add(conn, 9300501, "TEST_CTR", now - timedelta(days=2), 90)
    rows = table(conn)
    assert list(rows) == TYPES                       # always the eight, in this order
    assert float(rows["FMP"]["Last 7 days"]) == 0.0 and float(rows["FMP"]["Since go-live"]) == 0.0
    assert float(rows["CTR"]["Last 7 days"]) == 1.5


def test_table_windows_clip_a_session_to_each_window(conn):
    now = datetime.now(UTC)
    this_month = datetime(now.year, now.month, 1, tzinfo=UTC)
    last_month = datetime(this_month.year - (this_month.month == 1), 12 if this_month.month == 1 else this_month.month - 1, 1, tzinfo=UTC)
    cases = [  # (callsign, start, minutes)
        ("TEST1_TWR", now - timedelta(days=3), 120),                       # inside 7 days, 30 days and the month it is in
        ("TEST2_TWR", now - timedelta(days=20), 60),                       # inside 30 days only
        ("TEST3_TWR", now - timedelta(days=6, hours=23), 180),             # starts just inside 7 days
        ("TEST4_TWR", last_month + timedelta(days=9), 90),                 # inside the last full month
        ("TEST5_TWR", this_month - timedelta(hours=1), 180),               # crosses the month boundary: 1 hour last month
        ("TEST6_TWR", now - timedelta(days=40), 60),                       # outside 7 and 30 days
    ]
    for i, (cs, start, minutes) in enumerate(cases):
        add(conn, 9300600 + i, cs, start, minutes)
    row = table(conn)["TWR"]
    windows = {
        "Last 7 days": (now - timedelta(days=7), now),
        "Last 30 days": (now - timedelta(days=30), now),
        "Last full month": (last_month, this_month),
        "Since go-live": (datetime(2000, 1, 1, tzinfo=UTC), now),
    }
    for name, (lo, hi) in windows.items():
        expected = round(sum(overlap_hours(s, m, lo, hi) for _, s, m in cases), 1)
        assert abs(float(row[name]) - expected) <= 0.15, (name, float(row[name]), expected)
    # the month-boundary session is the point of the check: its first hour is last month's, the rest is not
    assert overlap_hours(this_month - timedelta(hours=1), 180, last_month, this_month) == 1.0


def test_both_panels_are_present_and_typed_as_intended():
    weekly_panel, table_panel = panel(WEEKLY), panel(TABLE)
    assert weekly_panel["type"] == "timeseries" and weekly_panel["targets"][0]["format"] == "time_series"
    assert table_panel["type"] == "table" and table_panel["targets"][0]["format"] == "table"
    assert [c for c in WINDOWS if c in table_panel["targets"][0]["rawSql"]] == WINDOWS


def test_suffix_edge_cases_and_very_short_sessions(conn):
    """The suffix is what follows the last underscore and must be upper case; a six-minute session still counts
    (all session time counts, no minimum length)."""
    w = monday(2)
    add(conn, 9300701, "A_B_TWR", w + timedelta(days=1), 60)       # an extra underscore: still TWR
    add(conn, 9300702, "TEST_twr", w + timedelta(days=1), 60)      # lower case: not a staffed position type
    add(conn, 9300703, "CTR", w + timedelta(days=1), 60)           # a suffix alone, no underscore: left out
    add(conn, 9300704, "TEST_DEL", w + timedelta(days=1), 6)       # six minutes: 0.1 hours, no minimum length
    got = weekly(conn)
    assert got[(w, "TWR")] == 1.0
    assert (w, "CTR") not in got
    assert got[(w, "DEL")] == 0.1


def test_first_week_is_whole_when_the_time_range_starts_mid_week(conn):
    """A session earlier in the first week than the range start still belongs to that week's bar."""
    w = monday(2)
    add(conn, 9300801, "TEST_TWR", w + timedelta(hours=1), 60)     # Monday 01:00, before a Wednesday range start
    got = weekly(conn, frm=w + timedelta(days=2))
    assert got[(w, "TWR")] == 1.0
