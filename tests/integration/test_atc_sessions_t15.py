"""T15 (group c): ATC session cards 144, 146, 153 and 154 on the ATC overview dashboard.

Synthetic controller sessions are inserted relative to now(), then the panel SQL is read from
grafana/dashboards/atc/atc-overview.json, the Grafana time macros are expanded and the query is executed, so the
test covers the SQL that Grafana runs. All times UTC. Card 148 (pilot/ATC interaction by weekday) is not built
(ADR-002: it needs controller-to-flight matching), so it has no test.

Rules under test (owner's Metabase export, 08-10-2026):
  144 / 146  S1 (rating 2) / S2 (rating 3) sessions started in the last 30 days, hours online per CID, top 30 (issue 302; it was 50)
  153        median session length in hours, last 90 days, at least 3 sessions, top 15 longest
  154        median session length in minutes, last 90 days, at least 3 sessions, named, top 15 shortest
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
NEW_TITLES = [S1_TITLE, S2_TITLE, LONGEST_TITLE, SHORTEST_TITLE]

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
    start = start.replace(microsecond=0)  # logon_time is kept to whole seconds (T36)
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
    for i in range(32):
        add(conn, 9101000 + i, days_ago(3), 30 + i, rating=2)
        add(conn, 9102000 + i, days_ago(3), 30 + i, rating=3)
    rows = run(conn, S1_TITLE)
    assert len(rows) == 30                                    # issue 302: 30 bars so the labels do not overlap
    assert len(run(conn, S2_TITLE)) == 30
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


def test_longest_needs_three_sessions_in_ninety_days(conn):
    add_three(conn, 9200001, (60, 60, 60), ago=100)           # all older than 90 days
    add(conn, 9200002, days_ago(10), 60)
    add(conn, 9200002, days_ago(11), 60)                      # only two
    assert run(conn, LONGEST_TITLE) == []


def test_unnamed_sessions_count_and_the_label_falls_back_to_the_cid(conn):
    """Issue 300: unnamed sessions are no longer dropped; a member with no name at all is labelled by CID."""
    add_three(conn, 9200003, (60, 60, 60), name=None, rating=5)
    add_three(conn, 9200004, (30, 30, 30), name="", rating=3)
    assert [r["controller"] for r in run(conn, LONGEST_TITLE)] == ["9200003 (9200003, C1)", "9200004 (9200004, S2)"]
    assert [r["controller"] for r in run(conn, SHORTEST_TITLE)] == ["9200004 (9200004, S2)", "9200003 (9200003, C1)"]


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
    add_three(conn, 9200005, (1, 1, 1), ago=100)              # outside 90 days
    rows = run(conn, SHORTEST_TITLE)
    assert [(r["controller"], float(r["Median session (minutes)"])) for r in rows] == [
        ("Controller 9200001 (9200001, S2)", 10.0),
        ("Controller 9200002 (9200002, C1)", 20.0),
    ]


@pytest.mark.parametrize("rating,label", [(2, "S1"), (3, "S2"), (4, "S3"), (5, "C1"), (7, "C3"), (8, "I1+"), (6, "Unknown"), (None, "Unknown")])
def test_shortest_shows_the_rating_label_not_the_number(conn, rating, label):
    add_three(conn, 9200001, (10, 10, 10), rating=rating)
    [row] = run(conn, SHORTEST_TITLE)
    assert row["controller"] == f"Controller 9200001 (9200001, {label})"


@pytest.mark.parametrize("title,column,expected", [(LONGEST_TITLE, "Median session (hours)", 1.0),
                                                    (SHORTEST_TITLE, "Median session (minutes)", 60.0)])
def test_one_row_per_cid_when_the_name_changes(conn, title, column, expected):
    """Issue 300: one CID under two names is one row; the 3-session minimum counts the person, the name is the latest."""
    add(conn, 9200001, days_ago(10), 60, name="Matt")
    add(conn, 9200001, days_ago(9), 60, name="Matt D")        # two sessions per name: neither name alone reaches 3
    add(conn, 9200001, days_ago(8), 60, name="Matt D")
    add(conn, 9200001, days_ago(7), 60, name="Matt")
    add(conn, 9200001, days_ago(1), 60, name=None)            # the newest session has no name: the latest name still wins
    rows = run(conn, title)
    assert [(r["controller"], float(r[column])) for r in rows] == [("Matt (9200001, S2)", expected)]


def test_every_new_panel_query_runs_on_empty_data(conn):
    for title in NEW_TITLES:
        assert run(conn, title) == []
