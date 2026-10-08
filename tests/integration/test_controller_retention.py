"""T13: the controller retention dashboard runs the recovered v1 Metabase queries over `controller_summaries`.

Sessions are inserted synthetically relative to now(), then the panel SQL is read from
grafana/dashboards/atc/atc-retention.json and executed, so the test covers the SQL that Grafana runs.
Rules under test (owner's Metabase export, 08-10-2026):
  active      = distinct CIDs with a session that started in the last 30 days
  lapsed 30   = latest session 30 to 60 days ago;  lapsed 60 = 60 to 90 days ago;  lapsed 90+ = over 90 days
  reactivated = a session in the last 30 days and none 30 to 90 days ago (v1 does not require an earlier
                session, so a brand-new controller counts)
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
    start = datetime.now(UTC) - timedelta(days=days_ago)
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
    add_session(conn, 9000001, 5, 10)      # A: only a short session, 5 days ago: reactivated and needs help
    add_session(conn, 9000002, 10, 90)     # B: a long recent session
    add_session(conn, 9000002, 45, 30)     # B: and one 45 days ago, so not reactivated
    add_session(conn, 9000003, 40, 30)     # C: last seen 40 days ago: lapsed 30
    add_session(conn, 9000004, 75, 30)     # D: last seen 75 days ago: lapsed 60
    add_session(conn, 9000005, 120, 30)    # E: last seen 120 days ago: lapsed 90+
    return conn


def test_view_exposes_the_v1_columns(conn):
    add_session(conn, 9000001, 1, 20, rating=5, name="Controller 9000001")
    row = conn.execute("SELECT * FROM controller_summaries").fetchone()
    assert set(row) == {"cid", "name", "rating", "session_start_time", "session_duration_minutes"}
    assert row["cid"] == 9000001 and row["rating"] == 5
    assert round(float(row["session_duration_minutes"])) == 20


def test_counts_follow_the_v1_rules(populated):
    assert scalar(populated, "Active controllers") == 2
    assert scalar(populated, "Lapsed 30 days") == 1
    assert scalar(populated, "Lapsed 60 days") == 1
    assert scalar(populated, "Lapsed 90+ days") == 1


def test_reactivated_and_help_lists(populated):
    title = "Reactivated in the last 30 days after 60+ days lapsed"
    assert scalar(populated, title) == 1                      # only A: B has a session 30 to 90 days ago
    assert cids(populated, "Who reactivated after being away for more than 60 days?") == {9000001}
    assert cids(populated, "Who might need help reactivating?") == {9000001}   # A's only session is under 18 min


def test_help_list_excludes_a_long_recent_session(conn):
    add_session(conn, 9000001, 5, 10)
    add_session(conn, 9000001, 3, 30)      # one session over 18 minutes: no longer "all short"
    assert cids(conn, "Who reactivated after being away for more than 60 days?") == {9000001}
    assert cids(conn, "Who might need help reactivating?") == set()


def test_retention_rate(populated):
    # active with a session over 60 min in the last 30 days: B (1). Lapsed 30 to 90 days with none recent: C and D (2).
    assert float(scalar(populated, "Controller retention rate")) == pytest.approx(33.3, abs=0.1)


def test_a_brand_new_controller_counts_as_reactivated(conn):
    """Known v1 quirk, kept on purpose: no earlier session is required."""
    add_session(conn, 9000009, 2, 45)
    assert scalar(conn, "Reactivated in the last 30 days after 60+ days lapsed") == 1


def test_every_panel_query_runs(populated):
    for p in json.loads(DASHBOARD.read_text(encoding="utf-8"))["panels"]:
        for t in p.get("targets", []):
            populated.execute(t["rawSql"]).fetchall()
