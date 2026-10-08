"""T15 (group c): ATC session cards 144, 146, 153, 154 and 186 on the ATC overview dashboard.

Synthetic controller sessions are inserted relative to now(), then the panel SQL is read from
grafana/dashboards/atc/atc-overview.json, the Grafana time macros are expanded and the query is executed, so the
test covers the SQL that Grafana runs. All times UTC. Card 148 (pilot/ATC interaction by weekday) is not built
(ADR-002: it needs controller-to-flight matching), so it has no test.

Rules under test (owner's Metabase export, 08-10-2026):
  144 / 146  S1 (rating 2) / S2 (rating 3) sessions started in the last 30 days, hours online per CID, top 50
  153        median session length in hours, last 90 days, at least 3 sessions, top 15 longest
  154        median session length in minutes, last 90 days, at least 3 sessions, named, top 15 shortest
  186        more than 240 minutes (strictly) of wall-clock time inside some 8-hour window, ATIS excluded,
             overlapping sessions of one CID not counted twice; one summary row then one detail row per session
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO

pytestmark = pytest.mark.integration

DASHBOARD = REPO / "grafana" / "dashboards" / "atc" / "atc-overview.json"

S1_TITLE = "Top S1 service providers (last 30 days)"
S2_TITLE = "Top S2 service providers (last 30 days)"
LONGEST_TITLE = "Controllers with the longest median session (last 90 days)"
SHORTEST_TITLE = "Controllers with the shortest median session (last 90 days)"
EIGHT_HOUR_TITLE = "Controllers on for more than 4 hours in any 8-hour window"
NEW_TITLES = [S1_TITLE, S2_TITLE, LONGEST_TITLE, SHORTEST_TITLE, EIGHT_HOUR_TITLE]

# A fixed anchor a few days back, on the hour, so the 30-day and 90-day windows are far away.
ANCHOR = (datetime.now(UTC) - timedelta(days=5)).replace(minute=0, second=0, microsecond=0)


def panel_sql(title: str) -> str:
    panels = json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"]
    sql = next(p["targets"][0]["rawSql"] for p in panels if p.get("title") == title)
    now = datetime.now(UTC)
    return (sql.replace("$__timeFrom()", f"'{(now - timedelta(days=30)).isoformat()}'")
               .replace("$__timeTo()", f"'{now.isoformat()}'"))


def run(conn, title: str) -> list[dict]:
    return conn.execute(panel_sql(title)).fetchall()


def add(conn, cid: int, start: datetime, minutes: float, *, callsign: str | None = None,
        rating: int = 3, name: str | None = "default") -> None:
    if name == "default":
        name = f"Controller {cid}"
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (cid, callsign or f"TEST{cid}_TWR", start, name, rating, start, start + timedelta(minutes=minutes)),
    )


def days_ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


# --- cards 144 and 146 -------------------------------------------------------------------------------

@pytest.mark.parametrize("title,rating,other", [(S1_TITLE, 2, 3), (S2_TITLE, 3, 2)])
def test_service_provider_rules(conn, title, rating, other):
    add(conn, 9100001, days_ago(10), 60, rating=rating)
    add(conn, 9100001, days_ago(9), 60, rating=rating)       # 2.00 hours in total
    add(conn, 9100002, days_ago(8), 90, rating=rating)       # 1.50 hours
    add(conn, 9100003, days_ago(7), 600, rating=other)       # other rating group: not listed
    add(conn, 9100004, days_ago(31), 600, rating=rating)     # started over 30 days ago: not listed
    add(conn, 9100005, days_ago(6), 0, rating=rating)        # zero length: not listed
    rows = run(conn, title)
    assert [(r["controller"], float(r["Hours online"])) for r in rows] == [
        ("Controller 9100001 (9100001)", 2.0),
        ("Controller 9100002 (9100002)", 1.5),
    ]


def test_provider_counts_only_sessions_at_the_stated_rating(conn):
    add(conn, 9100001, days_ago(10), 60, rating=2)
    add(conn, 9100001, days_ago(9), 120, rating=3)           # v1 filters the session's rating, not the person's
    assert [float(r["Hours online"]) for r in run(conn, S1_TITLE)] == [1.0]
    assert [float(r["Hours online"]) for r in run(conn, S2_TITLE)] == [2.0]


def test_provider_window_edges_and_limit(conn):
    add(conn, 9100001, days_ago(29.9), 60, rating=2)         # inside the 30 days
    add(conn, 9100002, days_ago(30.1), 60, rating=2)         # outside
    assert [r["controller"] for r in run(conn, S1_TITLE)] == ["Controller 9100001 (9100001)"]
    conn.execute("TRUNCATE controller_session RESTART IDENTITY CASCADE")
    for i in range(52):
        add(conn, 9101000 + i, days_ago(3), 30 + i, rating=2)
    rows = run(conn, S1_TITLE)
    assert len(rows) == 50
    assert float(rows[0]["Hours online"]) >= float(rows[-1]["Hours online"])


# --- cards 153 and 154 -------------------------------------------------------------------------------

def add_three(conn, cid: int, minutes: tuple[float, float, float], *, ago: float = 10, rating: int = 3,
              name: str | None = "default") -> None:
    for i, m in enumerate(minutes):
        add(conn, cid, days_ago(ago + i), m, rating=rating, name=name)


def test_longest_uses_the_median_not_the_mean(conn):
    add_three(conn, 9200001, (60, 60, 600))                   # median 60 min = 1.00 h (mean would be 4.00 h)
    add_three(conn, 9200002, (120, 150, 180), rating=5)       # median 150 min = 2.50 h, C1
    add_three(conn, 9200003, (300, 300, 300), rating=6)       # rating 6 (C2) shows as Unknown, as v1
    add(conn, 9200004, days_ago(4), 900)                      # only 1 session: excluded
    rows = run(conn, LONGEST_TITLE)
    assert [(r["controller"], float(r["Median session (hours)"])) for r in rows] == [
        ("Controller 9200003 (9200003, Unknown)", 5.0),
        ("Controller 9200002 (9200002, C1)", 2.5),
        ("Controller 9200001 (9200001, S2)", 1.0),
    ]


def test_longest_needs_three_sessions_in_ninety_days_and_a_name(conn):
    add_three(conn, 9200001, (60, 60, 60), ago=100)           # all older than 90 days
    add(conn, 9200002, days_ago(10), 60)
    add(conn, 9200002, days_ago(11), 60)                      # only two
    add_three(conn, 9200003, (60, 60, 60), name=None)         # no name: excluded
    assert run(conn, LONGEST_TITLE) == []


def test_longest_and_shortest_return_at_most_fifteen(conn):
    for i in range(17):
        add_three(conn, 9210000 + i, (30 + i, 30 + i, 30 + i))
    assert len(run(conn, LONGEST_TITLE)) == 15
    rows = run(conn, SHORTEST_TITLE)
    assert len(rows) == 15
    assert float(rows[0]["Median session (minutes)"]) == 30.0
    assert float(rows[-1]["Median session (minutes)"]) == 44.0   # the two longest (45, 46) are cut


def test_shortest_uses_the_median_and_ascending_order(conn):
    add_three(conn, 9200001, (10, 10, 100))                   # median 10 (mean would be 40)
    add_three(conn, 9200002, (5, 20, 25), rating=5)           # median 20
    add_three(conn, 9200003, (1, 1, 1), name="")              # empty name: excluded by card 154
    add_three(conn, 9200004, (1, 1, 1), name=None)            # no name: excluded
    add_three(conn, 9200005, (1, 1, 1), ago=100)              # outside 90 days
    rows = run(conn, SHORTEST_TITLE)
    assert [(r["controller"], float(r["Median session (minutes)"])) for r in rows] == [
        ("Controller 9200001 (9200001, rating 3)", 10.0),
        ("Controller 9200002 (9200002, rating 5)", 20.0),
    ]


# --- card 186: more than 4 hours in any 8-hour window -----------------------------------------------

def summary(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["row_type"] == "summary"]


def detail(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["row_type"] == "detail"]


def test_exactly_four_hours_is_not_enough_but_one_minute_more_is(conn):
    add(conn, 9300001, ANCHOR, 240)
    assert run(conn, EIGHT_HOUR_TITLE) == []                  # strictly more than 240 minutes
    add(conn, 9300002, ANCHOR, 241)
    rows = run(conn, EIGHT_HOUR_TITLE)
    assert [r["row_type"] for r in rows] == ["summary", "detail"]   # summary first, then its sessions
    assert rows[0]["cid"] == 9300002 and float(rows[0]["hours_controlled_in_8_hr_period"]) == 4.02
    span = rows[0]["period_end_utc"].astimezone(UTC) - rows[0]["period_start_utc"].astimezone(UTC)
    assert span == timedelta(hours=8)   # compared in UTC: same-zone subtraction in Python is wall-clock
    assert rows[1]["hours_controlled_in_8_hr_period"] is None and rows[1]["period_start_utc"] is None
    assert float(rows[1]["session_hours"]) == 4.02
    assert rows[1]["session_start_utc"] == ANCHOR and rows[1]["session_callsign"] == "TEST9300002_TWR"


def test_two_sessions_inside_eight_hours_are_added_up(conn):
    add(conn, 9300001, ANCHOR, 125, callsign="AAA_TWR")
    add(conn, 9300001, ANCHOR + timedelta(minutes=245), 125, callsign="BBB_APP")   # 2 h gap: 250 minutes in 370
    rows = run(conn, EIGHT_HOUR_TITLE)
    assert float(summary(rows)[0]["hours_controlled_in_8_hr_period"]) == 4.17
    assert sorted(r["session_callsign"] for r in detail(rows)) == ["AAA_TWR", "BBB_APP"]


def test_sessions_too_far_apart_do_not_count(conn):
    add(conn, 9300001, ANCHOR, 120, callsign="AAA_TWR")
    add(conn, 9300001, ANCHOR + timedelta(minutes=390), 120, callsign="BBB_TWR")   # best 8 h window holds 3.5 h
    assert run(conn, EIGHT_HOUR_TITLE) == []


def test_busiest_window_is_chosen_per_controller(conn):
    add(conn, 9300001, ANCHOR - timedelta(days=1), 270)       # 4.5 h
    add(conn, 9300001, ANCHOR, 300)                           # 5 h
    rows = run(conn, EIGHT_HOUR_TITLE)
    assert len(summary(rows)) == 1                            # one summary row per controller
    assert float(summary(rows)[0]["hours_controlled_in_8_hr_period"]) == 5.0


def test_overlapping_sessions_are_not_double_counted(conn):
    # 3 h on one position and a second position opened 2 h in that runs 2.5 h: wall clock 4.5 h
    add(conn, 9300001, ANCHOR, 180, callsign="AAA_TWR")
    add(conn, 9300001, ANCHOR + timedelta(minutes=120), 150, callsign="BBB_APP")
    rows = run(conn, EIGHT_HOUR_TITLE)
    assert float(summary(rows)[0]["hours_controlled_in_8_hr_period"]) == 4.5
    by_callsign = {r["session_callsign"]: float(r["session_hours"]) for r in detail(rows)}
    assert by_callsign == {"AAA_TWR": 2.0, "BBB_APP": 2.5}    # the first is cut off when the second starts


def test_a_session_inside_another_adds_nothing(conn):
    add(conn, 9300001, ANCHOR, 180, callsign="AAA_TWR")
    add(conn, 9300001, ANCHOR + timedelta(minutes=60), 120, callsign="BBB_APP")    # ends together: 3 h of wall clock
    assert run(conn, EIGHT_HOUR_TITLE) == []                  # counting both in full would give 5 h


def test_atis_callsigns_are_ignored(conn):
    add(conn, 9300001, ANCHOR, 300, callsign="YSSY_ATIS")
    add(conn, 9300002, ANCHOR, 300, callsign="ymml_atis")
    add(conn, 9300003, ANCHOR, 150, callsign="AAA_TWR")
    add(conn, 9300003, ANCHOR, 150, callsign="YSSY_ATIS")
    assert run(conn, EIGHT_HOUR_TITLE) == []


def test_different_controllers_are_never_pooled(conn):
    add(conn, 9300001, ANCHOR, 180)
    add(conn, 9300002, ANCHOR, 180)
    assert run(conn, EIGHT_HOUR_TITLE) == []


def test_reconnect_within_five_minutes_is_one_session(conn):
    """v3 merges a reconnect of the same position inside 5 minutes, so the gap counts as time on (v1 did not)."""
    add(conn, 9300001, ANCHOR, 120)
    add(conn, 9300001, ANCHOR + timedelta(minutes=123), 120)           # 3 minute gap: merged, 243 minutes
    add(conn, 9300002, ANCHOR, 120)
    add(conn, 9300002, ANCHOR + timedelta(minutes=126), 120)           # 6 minute gap: two sessions, 240 minutes
    rows = summary(run(conn, EIGHT_HOUR_TITLE))
    assert [(r["cid"], float(r["hours_controlled_in_8_hr_period"])) for r in rows] == [(9300001, 4.05)]


def test_only_sessions_in_the_dashboard_range_are_examined(conn):
    add(conn, 9300001, days_ago(60), 300)
    assert run(conn, EIGHT_HOUR_TITLE) == []
    add(conn, 9300002, days_ago(2), 300)
    assert [r["cid"] for r in summary(run(conn, EIGHT_HOUR_TITLE))] == [9300002]


def test_at_most_one_hundred_controllers_most_recent_first(conn):
    for k in range(101):
        add(conn, 9310000 + k, ANCHOR - timedelta(hours=k), 300)
    rows = summary(run(conn, EIGHT_HOUR_TITLE))
    assert len(rows) == 100
    assert rows[0]["cid"] == 9310000                                   # newest window first
    assert 9310100 not in {r["cid"] for r in rows}                     # the oldest is cut


def test_every_new_panel_query_runs_on_empty_data(conn):
    for title in NEW_TITLES:
        assert run(conn, title) == []
