"""T13: the controller retention dashboard runs the recovered v1 Metabase queries over `controller_summaries`.

Sessions are inserted synthetically relative to now(), then the panel SQL is read from
grafana/dashboards/atc/atc-retention.json and executed, so the test covers the SQL that Grafana runs.
Rules under test (owner's Metabase export, 08-10-2026):
  active      = distinct CIDs with a session that started in the last 30 days
  lapsed 30   = latest session 30 to 60 days ago;  lapsed 60 = 60 to 90 days ago;  lapsed 90+ = over 90 days
  reactivated = a session in the last 30 days, none 30 to 90 days ago, and at least one session before 90 days ago
                (v1 did not require the earlier session, so a brand-new controller counted; corrected, issue 93, ADR-027)
  help        = reactivated and every session in the last 30 days under 18 minutes
  retention   = active (30 days, sessions over 60 min) / (that + lapsed 30 to 90 days with none in the last 30)
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO

pytestmark = pytest.mark.integration

DASHBOARD = REPO / "grafana" / "dashboards" / "atc" / "atc-retention.json"


def panel_sql(title: str) -> str:
    panels = json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"]
    return next(p["targets"][0]["rawSql"] for p in panels if p.get("title") == title)


def add_session(conn, cid: int, days_ago: float, minutes: int, rating: int = 3, name: str | None = None) -> None:
    # whole seconds throughout (logon_time must be, ADR-021), so a session is exactly ``minutes`` long
    start = (datetime.now(UTC) - timedelta(days=days_ago)).replace(microsecond=0)
    end = start + timedelta(minutes=minutes)
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (cid, f"TEST{cid}_TWR", start, name or f"Controller {cid}", rating, start, end),
    )


def scalar(conn, title: str):
    row = conn.execute(panel_sql(title).rstrip().rstrip(";")).fetchone()
    return list(row.values())[0]


def cids(conn, title: str) -> set[int]:
    return {r["cid"] for r in conn.execute(panel_sql(title)).fetchall()}


@pytest.fixture()
def populated(conn):
    add_session(conn, 9000001, 5, 10)      # A: a short session 5 days ago ...
    add_session(conn, 9000001, 120, 30)    # A: ... and one 120 days ago, none in between: reactivated and needs help
    add_session(conn, 9000002, 10, 90)     # B: a long recent session
    add_session(conn, 9000002, 45, 30)     # B: and one 45 days ago, so not reactivated
    add_session(conn, 9000003, 40, 30)     # C: last seen 40 days ago: lapsed 30
    add_session(conn, 9000004, 75, 30)     # D: last seen 75 days ago: lapsed 60
    add_session(conn, 9000005, 120, 30)    # E: last seen 120 days ago: lapsed 90+
    return conn


def test_view_exposes_the_v1_columns(conn):
    add_session(conn, 9000001, 1, 20, rating=5, name="Controller 9000001")
    row = conn.execute("SELECT * FROM controller_summaries").fetchone()
    assert set(row) == {"cid", "name", "rating", "session_start_time", "session_duration_minutes",
                        "callsign", "session_end_time"}
    assert row["cid"] == 9000001 and row["rating"] == 5
    assert round(float(row["session_duration_minutes"])) == 20


def test_counts_follow_the_v1_rules(populated):
    assert scalar(populated, "Active controllers") == 2
    assert scalar(populated, "Lapsed 30 days") == 1
    assert scalar(populated, "Lapsed 60 days") == 1
    assert scalar(populated, "Lapsed 90+ days") == 1        # E only: A's latest session is recent


def test_reactivated_and_help_lists(populated):
    title = "Reactivated in the last 30 days after 60+ days lapsed"
    assert scalar(populated, title) == 1                      # only A: B has a session 30 to 90 days ago
    assert cids(populated, "Who reactivated after being away for more than 60 days?") == {9000001}
    assert cids(populated, "Who might need help reactivating?") == {9000001}   # A's only session is under 18 min


def test_help_list_excludes_a_long_recent_session(conn):
    add_session(conn, 9000001, 120, 30)    # a real earlier session, so the controller counts as reactivated
    add_session(conn, 9000001, 5, 10)
    add_session(conn, 9000001, 3, 30)      # one session over 18 minutes: no longer "all short"
    assert cids(conn, "Who reactivated after being away for more than 60 days?") == {9000001}
    assert cids(conn, "Who might need help reactivating?") == set()


def test_retention_rate(populated):
    # active with a session over 60 min in the last 30 days: B (1). Lapsed 30 to 90 days with none recent: C and D (2).
    assert float(scalar(populated, "Controller retention rate")) == pytest.approx(33.3, abs=0.1)


REACTIVATED = "Reactivated in the last 30 days after 60+ days lapsed"
REACTIVATED_LISTS = ("Who reactivated after being away for more than 60 days?", "Who might need help reactivating?")


def test_a_brand_new_controller_is_not_reactivated(conn):
    """Issue 93: a controller whose first session was 10 days ago has no earlier activity, so did not return from a gap.
    The same holds for the first sessions at 2 days (short, so it would be on the help list too) and 40 days (older than
    30 days, so not active at all)."""
    add_session(conn, 9000009, 10, 45)
    add_session(conn, 9000010, 2, 10)
    add_session(conn, 9000011, 40, 45)
    assert scalar(conn, REACTIVATED) == 0
    for title in REACTIVATED_LISTS:
        assert cids(conn, title) == set()


def test_a_returning_controller_is_reactivated(conn):
    """A session 120 days ago, none 30 to 90 days ago, one in the last 30 days: reactivated, in all three panels."""
    add_session(conn, 9000020, 120, 45)
    add_session(conn, 9000020, 10, 10)
    assert scalar(conn, REACTIVATED) == 1
    for title in REACTIVATED_LISTS:
        assert cids(conn, title) == {9000020}


def test_a_session_exactly_at_the_edges_of_the_windows(conn):
    """Earlier activity must be before 90 days ago: 89 days ago is the gap window (so not reactivated), 91 days ago is real."""
    add_session(conn, 9000030, 89, 30)     # inside 30 to 90: not reactivated
    add_session(conn, 9000030, 5, 30)
    add_session(conn, 9000031, 91, 30)     # just before 90 days: real earlier activity
    add_session(conn, 9000031, 5, 30)
    assert cids(conn, REACTIVATED_LISTS[0]) == {9000031}
    assert scalar(conn, REACTIVATED) == 1


def add_session_on(conn, cid: int, day: str, minutes: int = 30) -> None:
    start = datetime.fromisoformat(day).replace(tzinfo=UTC)
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) "
        "VALUES (%s, %s, %s, %s, 3, %s, %s)",
        (cid, f"TEST{cid}_TWR", start.replace(microsecond=0), f"Controller {cid}", start, start + timedelta(minutes=minutes)),
    )


def test_activity_trends_by_month(conn):
    """Card 176: X is active in Jan and Mar; Y in Jan and Feb (fixed dates, so the month rule is exact)."""
    add_session_on(conn, 9000001, "2026-01-10")
    add_session_on(conn, 9000001, "2026-03-10")
    add_session_on(conn, 9000002, "2026-01-12")
    add_session_on(conn, 9000002, "2026-02-12")
    rows = conn.execute(panel_sql("Controller activity trends")).fetchall()
    by_month = {r["month"].strftime("%Y-%m"): (int(r["reactivations"]), int(r["total_active_controllers"]), int(r["lapsed"]))
                for r in rows}
    assert by_month == {
        "2026-01": (2, 2, 0),   # nobody the month before, so both count as reactivations (v1 rule)
        "2026-02": (0, 1, 1),   # Y continues; X lapsed
        "2026-03": (1, 1, 1),   # X returns after a gap; Y lapsed
    }


def test_every_panel_query_runs(populated):
    for p in json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"]:
        for t in p.get("targets", []):
            populated.execute(t["rawSql"]).fetchall()
