"""Issue #55: the soak report verdict (ops/soak_report.sql), run against synthetic poll_log rows.

ops/soak_report.sh feeds the file to psql with `-v hours=N`; here the same text is run through psycopg with `:hours`
replaced by the number. Since ADR-024 the limits come from app_setting 'poll_interval_seconds' (expected snapshots =
span / interval + 1; a gap is "over the limit" above 2.5 x interval; p95 cadence may be at most interval + 2 s), so each
test sets the interval it needs. Verdict order: no snapshots, INCOMPLETE (covered under 98 percent of the window),
coverage under 99 percent, p95 cadence above interval + 2 s, else PASS.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO
from helpers import set_poll_interval

pytestmark = pytest.mark.integration

SQL = (REPO / "ops" / "soak_report.sql").read_text(encoding="utf-8")


def report(conn, hours: int = 1) -> dict:
    sql = re.sub(r"(?<!:):hours\b", str(hours), SQL)
    return conn.execute(sql.rstrip().rstrip(";")).fetchone()


def seed(conn, gaps: list[float], status: str = "ok", end: datetime | None = None) -> list[datetime]:
    """Snapshots separated by ``gaps`` seconds (earliest first); the last one is ``end`` (default: now). Each is one poll_log row."""
    last = end or datetime.now(UTC)
    times = [last]
    for g in reversed(gaps):
        times.append(times[-1] - timedelta(seconds=g))
    times.reverse()
    for t in times:
        conn.execute("INSERT INTO poll_log (status, polled_at, snapshot_ts) VALUES (%s,%s,%s)", (status, t, t))
    return times


def test_steady_cadence_over_the_whole_window_passes(conn) -> None:
    set_poll_interval(conn, 30)
    seed(conn, [30] * 119)                                    # 120 snapshots, 59.5 minutes
    r = report(conn)
    assert r["verdict"] == "PASS"
    assert (r["snapshots_stored"], r["snapshots_expected"]) == (120, 120)
    assert float(r["coverage_pct"]) == 100.0 and float(r["p95_cadence_s"]) == 30.0 and r["gaps_over_limit"] == 0
    assert float(r["hours_covered"]) == 0.99 and r["hours_requested"] == 1
    assert r["first_snapshot"].endswith(" UTC") and r["last_snapshot"].endswith(" UTC")


def test_no_snapshots_is_a_fail(conn) -> None:
    set_poll_interval(conn, 30)
    assert report(conn)["verdict"] == "FAIL: no snapshots in the window"
    seed(conn, [30] * 10, status="error")                     # failed polls are not snapshots
    assert report(conn)["verdict"] == "FAIL: no snapshots in the window"


def test_a_short_run_is_incomplete_never_pass(conn) -> None:
    set_poll_interval(conn, 30)
    seed(conn, [30] * 60)                                     # half an hour of perfect cadence
    verdict = report(conn)["verdict"]
    assert verdict.startswith("INCOMPLETE: only 0.5") and "of 1 h requested" in verdict


def test_the_window_must_be_covered_to_98_percent(conn) -> None:
    """With a 36 s interval, 98 gaps are exactly 0.98 h: enough. 97 gaps are 0.97 h: incomplete."""
    set_poll_interval(conn, 36)
    seed(conn, [36] * 98)
    assert report(conn)["verdict"] == "PASS"
    conn.execute("TRUNCATE poll_log")
    seed(conn, [36] * 97)
    assert report(conn)["verdict"].startswith("INCOMPLETE")


def test_coverage_of_99_percent_passes_and_98_percent_fails(conn) -> None:
    """Interval 36 s, 99 gaps of 36 s would be 100 snapshots expected. Dropping one interior snapshot leaves 99 percent."""
    set_poll_interval(conn, 36)
    seed(conn, [36] * 48 + [72] + [36] * 49)                  # one snapshot missing: 99 of 100
    r = report(conn)
    assert (r["snapshots_stored"], r["snapshots_expected"], float(r["coverage_pct"])) == (99, 100, 99.0)
    assert r["verdict"] == "PASS"
    conn.execute("TRUNCATE poll_log")
    seed(conn, [36] * 30 + [72] + [36] * 30 + [72] + [36] * 36)   # two missing: 98 of 100
    r = report(conn)
    assert float(r["coverage_pct"]) == 98.0 and r["verdict"] == "FAIL: coverage below 99%"


def test_p95_cadence_must_stay_within_the_interval_plus_two_seconds(conn) -> None:
    set_poll_interval(conn, 30)
    seed(conn, [28, 32] * 59 + [30])                          # p95 gap exactly 32 s: allowed
    r = report(conn)
    assert float(r["p95_cadence_s"]) == 32.0 and r["verdict"] == "PASS"
    conn.execute("TRUNCATE poll_log")
    seed(conn, [25, 35] * 59 + [30])                          # steady on average but p95 is 35 s
    r = report(conn)
    assert float(r["p95_cadence_s"]) == 35.0 and r["verdict"] == "FAIL: p95 cadence above the poll interval + 2 s"


def test_the_limits_follow_the_poll_interval_setting(conn) -> None:
    """The same data is a pass at a 30 s interval and a coverage failure at 15 s (ADR-024); nothing is hard-coded."""
    seed(conn, [30] * 119)
    set_poll_interval(conn, 30)
    assert report(conn)["verdict"] == "PASS"
    set_poll_interval(conn, 15)
    r = report(conn)
    assert r["snapshots_expected"] == 239 and r["verdict"] == "FAIL: coverage below 99%"
    conn.execute("TRUNCATE poll_log")
    seed(conn, [25, 35] * 59 + [30])                          # p95 35 s
    set_poll_interval(conn, 30)
    assert report(conn)["verdict"].startswith("FAIL: p95")
    set_poll_interval(conn, 34)                               # limit 36 s: the same cadence now passes the p95 test
    r = report(conn)
    assert float(r["p95_cadence_s"]) == 35.0 and r["verdict"] != "FAIL: p95 cadence above the poll interval + 2 s"


def test_only_ok_polls_count_as_snapshots_and_duplicates_count_once(conn) -> None:
    set_poll_interval(conn, 30)
    times = seed(conn, [30] * 119)
    for t in times[:20]:                                      # a replayed snapshot logged again by a restart
        conn.execute("INSERT INTO poll_log (status, polled_at, snapshot_ts) VALUES ('ok',%s,%s)", (t + timedelta(seconds=1), t))
    for t in times[:7]:
        conn.execute("INSERT INTO poll_log (status, polled_at, snapshot_ts) VALUES ('skipped_unchanged',%s,%s)", (t + timedelta(seconds=2), t + timedelta(seconds=10)))
    for t in times[:3]:
        conn.execute("INSERT INTO poll_log (status, polled_at) VALUES ('error',%s)", (t + timedelta(seconds=3),))
    r = report(conn)
    assert r["snapshots_stored"] == 120 and r["verdict"] == "PASS"
    assert (r["error_polls"], r["skipped_polls"]) == (3, 7)


def test_polls_older_than_the_window_are_ignored(conn) -> None:
    set_poll_interval(conn, 30)
    seed(conn, [30] * 119)
    seed(conn, [30] * 5, end=datetime.now(UTC) - timedelta(hours=3))      # an earlier run, long gap before the window
    r = report(conn)
    assert r["snapshots_stored"] == 120 and r["verdict"] == "PASS"


def test_a_gap_is_over_the_limit_above_two_and_a_half_intervals(conn) -> None:
    set_poll_interval(conn, 30)
    seed(conn, [30] * 50 + [75] + [30] * 50)                  # exactly 75 s: not over
    r = report(conn)
    assert r["gaps_over_limit"] == 0 and str(r["longest_gap"]) == "0:01:15"
    conn.execute("TRUNCATE poll_log")
    seed(conn, [30] * 50 + [76] + [30] * 50)
    r = report(conn)
    assert r["gaps_over_limit"] == 1 and str(r["longest_gap"]) == "0:01:16"


def test_the_hours_argument_sets_the_window(conn) -> None:
    set_poll_interval(conn, 30)
    seed(conn, [30] * 119)
    assert report(conn, hours=1)["verdict"] == "PASS"
    verdict = report(conn, hours=2)["verdict"]
    assert verdict.startswith("INCOMPLETE") and "of 2 h requested" in verdict
