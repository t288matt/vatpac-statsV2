"""Issue 379: the `controller_promotion` view and the "ATC: promotions" dashboard, tested from the rule in the issue.

The rule (owner decisions 15-11-2026, with the 16-11-2026 change that history is complete, so the "seen at a lower rank in the
previous 60 days" test is gone and a promotion after a long gap counts):
  ladder 2, 3, 4, 5, 7 only; a session qualifies only if duration_seconds > 1200 (an open session has none and never counts);
  a promotion is the first qualifying session above every earlier qualifying session of the CID; a first ever qualifying session
  counts only at S1 (is_new, from_rating NULL); a skipped rank is one promotion; each rank once; promoted_at is the start of that
  session; previous_seen_at the end of the latest earlier qualifying session; name from controller_summaries, else the CID.
Synthetic sessions go into atc_stats_session (the table the stats loader fills). Dashboard queries run as grafana_ro.
"""
from __future__ import annotations

import itertools
import json
import os
import re
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO

pytestmark = pytest.mark.integration

DASHBOARD = REPO / "grafana" / "dashboards" / "atc" / "atc-promotions.json"
S1, S2, S3, C1, C3 = 2, 3, 4, 5, 7
_ids = itertools.count(9_000_000_000)


def d(y: int, m: int, day: int, h: int = 10, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, m, day, h, mi, s, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _empty_stats(conn):
    conn.execute("TRUNCATE atc_stats_session")
    yield
    conn.execute("TRUNCATE atc_stats_session")


def add(conn, cid: int, rating: int | None, start: datetime, minutes: float | None = 30, *, seconds: int | None = None,
        callsign: str = "ML_TWR", connection_id: int | None = None) -> None:
    """One session. ``minutes`` None gives an open session (no end); ``seconds`` sets the length exactly."""
    length = None if (minutes is None and seconds is None) else timedelta(seconds=seconds if seconds is not None else minutes * 60)
    conn.execute(
        "INSERT INTO atc_stats_session (connection_id, cid, callsign, rating, session_start, session_end) VALUES (%s,%s,%s,%s,%s,%s)",
        (connection_id if connection_id is not None else next(_ids), cid, callsign, rating, start,
         None if length is None else start + length))


def promos(conn, cid: int | None = None) -> list[dict]:
    sql = "SELECT * FROM controller_promotion" + ("" if cid is None else " WHERE cid = %s") + " ORDER BY promoted_at, to_rating_id, cid"
    return conn.execute(sql, () if cid is None else (cid,)).fetchall()


def path(conn, cid: int) -> list[tuple[str | None, str]]:
    return [(r["from_rating"], r["to_rating"]) for r in promos(conn, cid)]


# ------------------------------------------------------------------ the 20 minute test
def test_19_minutes_does_not_count_21_does(conn) -> None:
    add(conn, 1, S1, d(2026, 1, 5), 19)
    add(conn, 2, S1, d(2026, 1, 5), 21)
    assert [r["cid"] for r in promos(conn)] == [2]


def test_exactly_1200_seconds_does_not_count_1201_does(conn) -> None:
    add(conn, 1, S1, d(2026, 1, 5), seconds=1200)
    add(conn, 2, S1, d(2026, 1, 5), seconds=1201)
    assert [r["cid"] for r in promos(conn)] == [2]


def test_short_sessions_are_ignored_everywhere_not_just_as_a_promotion(conn) -> None:
    """A short session at a higher rank is not "held": the later real promotion comes from S1, and the short S2 first
    session cannot hide or date the promotion."""
    add(conn, 1, S1, d(2026, 1, 5), 30)
    add(conn, 1, S3, d(2026, 1, 6), 19)      # accidental logon at the wrong rank
    add(conn, 1, S2, d(2026, 1, 7), 5)       # short S2: nothing
    add(conn, 1, S2, d(2026, 1, 8), 25)      # the first qualifying S2
    rows = promos(conn, 1)
    assert [(r["from_rating"], r["to_rating"]) for r in rows] == [(None, "S1"), ("S1", "S2")]
    assert rows[1]["promoted_at"] == d(2026, 1, 8)
    assert rows[1]["previous_seen_at"] == d(2026, 1, 5) + timedelta(minutes=30)


def test_a_short_session_is_not_a_previous_session_for_previous_seen_at(conn) -> None:
    add(conn, 1, S1, d(2026, 1, 5), 30)
    add(conn, 1, S1, d(2026, 1, 9), 10)      # short: must not become previous_seen_at
    add(conn, 1, S2, d(2026, 1, 20), 30)
    assert promos(conn, 1)[1]["previous_seen_at"] == d(2026, 1, 5, 10, 30)


# ------------------------------------------------------------------ first ever session
def test_first_ever_s1_session_is_counted_as_new(conn) -> None:
    add(conn, 1, S1, d(2026, 3, 4, 8, 15), 45)
    (row,) = promos(conn, 1)
    assert row["is_new"] is True and row["from_rating"] is None and row["to_rating"] == "S1" and row["to_rating_id"] == S1
    assert row["promoted_at"] == d(2026, 3, 4, 8, 15) and row["previous_seen_at"] is None


@pytest.mark.parametrize("rating", [S2, S3, C1, C3])
def test_first_ever_session_above_s1_is_not_counted(conn, rating: int) -> None:
    add(conn, 1, rating, d(2026, 3, 4), 45)
    assert promos(conn, 1) == []


def test_first_session_above_s1_then_higher_is_counted_from_that_rank(conn) -> None:
    """A visitor's first S2 session is not a promotion, but it is a qualifying session: the later S3 is one, from S2."""
    add(conn, 1, S2, d(2026, 3, 4), 45)
    add(conn, 1, S3, d(2026, 3, 20), 45)
    assert path(conn, 1) == [("S2", "S3")]


def test_short_first_s1_then_long_s1_counts_at_the_long_one(conn) -> None:
    add(conn, 1, S1, d(2026, 3, 4), 5)
    add(conn, 1, S1, d(2026, 3, 6), 25)
    (row,) = promos(conn, 1)
    assert row["is_new"] and row["promoted_at"] == d(2026, 3, 6)


# ------------------------------------------------------------------ ladder moves
def test_a_skipped_rank_is_one_promotion_to_the_new_rank(conn) -> None:
    add(conn, 1, S1, d(2026, 1, 5), 30)
    add(conn, 1, S3, d(2026, 2, 5), 30)
    assert path(conn, 1) == [(None, "S1"), ("S1", "S3")]


def test_drop_and_return_is_not_a_promotion(conn) -> None:
    add(conn, 1, S1, d(2026, 1, 1), 30)
    add(conn, 1, C1, d(2026, 1, 2), 30)
    add(conn, 1, S3, d(2026, 1, 3), seconds=1260)   # a drop for 21 minutes
    add(conn, 1, C1, d(2026, 1, 4), 30)
    assert path(conn, 1) == [(None, "S1"), ("S1", "C1")]


def test_each_rank_counts_once(conn) -> None:
    for i, rating in enumerate([S1, S2, S3, C1, C3]):
        add(conn, 1, rating, d(2026, 1, 1) + timedelta(days=10 * i), 30)
        add(conn, 1, rating, d(2026, 1, 2) + timedelta(days=10 * i), 30)   # same rank again
    assert path(conn, 1) == [(None, "S1"), ("S1", "S2"), ("S2", "S3"), ("S3", "C1"), ("C1", "C3")]
    assert [r["to_rating_id"] for r in promos(conn, 1)] == [2, 3, 4, 5, 7]


@pytest.mark.parametrize("gap_days", [59, 61, 400])
def test_promotion_after_a_gap_counts(conn, gap_days: int) -> None:
    """The 60 day test is gone: history is complete, so a long gap is still a promotion."""
    add(conn, 1, S1, d(2025, 1, 1), 30)
    add(conn, 1, S2, d(2025, 1, 1) + timedelta(days=gap_days), 30)
    rows = promos(conn, 1)
    assert [(r["from_rating"], r["to_rating"]) for r in rows] == [(None, "S1"), ("S1", "S2")]
    assert rows[1]["promoted_at"] == d(2025, 1, 1) + timedelta(days=gap_days)


def test_c3_then_instructor_rating_is_not_a_promotion(conn) -> None:
    """Rating 7 then 8 (I1), 10 (I3): the instructor ratings are not on the ladder."""
    add(conn, 1, S1, d(2026, 1, 1), 30)
    add(conn, 1, C3, d(2026, 1, 2), 30)
    add(conn, 1, 8, d(2026, 1, 3), 30)
    add(conn, 1, 10, d(2026, 1, 4), 30)
    add(conn, 1, C3, d(2026, 1, 5), 30)
    assert path(conn, 1) == [(None, "S1"), ("S1", "C3")]


@pytest.mark.parametrize("rating", [-1, 0, 1, 6, 8, 9, 10, 11, 12, None])
def test_ratings_off_the_ladder_are_ignored(conn, rating: int | None) -> None:
    """INAC, SUS, OBS, C2, I1, I2, I3, SUP, ADM and an unknown rating never create or block a promotion."""
    add(conn, 1, rating, d(2026, 1, 1), 60)
    add(conn, 2, S1, d(2026, 1, 1), 30)
    add(conn, 2, rating, d(2026, 1, 2), 60)
    add(conn, 2, S2, d(2026, 1, 3), 30)
    assert promos(conn, 1) == []
    assert path(conn, 2) == [(None, "S1"), ("S1", "S2")]


def test_off_ladder_rating_does_not_count_as_a_previous_session(conn) -> None:
    """A supervisor session before the first S1 does not stop the first S1 being "new"."""
    add(conn, 1, 11, d(2026, 1, 1), 60)
    add(conn, 1, S1, d(2026, 1, 2), 30)
    assert path(conn, 1) == [(None, "S1")]


def test_open_session_is_ignored(conn) -> None:
    add(conn, 1, S1, d(2026, 1, 1), None)          # still open: no end, no duration
    add(conn, 2, S1, d(2026, 1, 1), 30)
    add(conn, 2, S2, d(2026, 1, 5), None)          # an open S2 is not a promotion yet
    assert promos(conn, 1) == []
    assert path(conn, 2) == [(None, "S1")]


def test_open_session_does_not_become_previous_seen_at(conn) -> None:
    add(conn, 1, S1, d(2026, 1, 1), 30)
    add(conn, 1, S1, d(2026, 1, 3), None)
    add(conn, 1, S2, d(2026, 1, 9), 30)
    assert promos(conn, 1)[1]["previous_seen_at"] == d(2026, 1, 1, 10, 30)


def test_promoted_at_and_previous_seen_at(conn) -> None:
    add(conn, 1, S1, d(2026, 1, 1, 9), 30)
    add(conn, 1, S1, d(2026, 1, 3, 9), 60)         # the latest qualifying S1 session before the promotion
    add(conn, 1, S2, d(2026, 1, 10, 12, 30), 22)   # first S2
    add(conn, 1, S2, d(2026, 1, 12, 12), 90)       # later S2: nothing
    s1, s2 = promos(conn, 1)
    assert s1["promoted_at"] == d(2026, 1, 1, 9) and s1["previous_seen_at"] is None
    assert s2["promoted_at"] == d(2026, 1, 10, 12, 30) and s2["previous_seen_at"] == d(2026, 1, 3, 10)
    assert s2["from_rating"] == "S1" and s2["to_rating"] == "S2" and s2["is_new"] is False


def test_previous_seen_at_after_a_drop_is_the_latest_earlier_qualifying_session(conn) -> None:
    add(conn, 1, S1, d(2026, 1, 1), 30)
    add(conn, 1, S2, d(2026, 1, 5), 30)            # promotion to S2
    add(conn, 1, S1, d(2026, 1, 8), 30)            # drop
    add(conn, 1, S3, d(2026, 1, 20), 30)           # promotion: latest earlier session is the S1 drop
    assert promos(conn, 1)[2]["previous_seen_at"] == d(2026, 1, 8, 10, 30)
    assert promos(conn, 1)[2]["from_rating"] == "S2"   # from the highest held, not the last session


# ------------------------------------------------------------------ ordering and independence
def test_two_sessions_starting_in_the_same_second_give_one_row_per_rank_whatever_the_ids(conn) -> None:
    """Same CID, same start second, same rating: one promotion. The result must not depend on the connection ids."""
    for ids in ((1, 2), (2, 1)):
        conn.execute("TRUNCATE atc_stats_session")
        add(conn, 1, S1, d(2026, 1, 1), 30, connection_id=ids[0])
        add(conn, 1, S1, d(2026, 1, 1), 40, connection_id=ids[1])
        assert path(conn, 1) == [(None, "S1")]


def test_same_second_different_ratings_is_deterministic_and_once_per_rank(conn) -> None:
    """The rule is silent on the order of two sessions that start together at different ratings. The view orders them by rating,
    so S1 is the new session and S2 the promotion; swapping the connection ids must not change that, and no rank repeats."""
    results = []
    for ids, s2_first in (((10, 11), False), ((11, 10), False), ((10, 11), True), ((11, 10), True)):
        conn.execute("TRUNCATE atc_stats_session")
        rows = [(S1, ids[0]), (S2, ids[1])]
        for rating, cid_ in (rows[::-1] if s2_first else rows):   # also swap the physical insertion order
            add(conn, 1, rating, d(2026, 1, 1), 30, connection_id=cid_)
        results.append(path(conn, 1))
    assert all(r == [(None, "S1"), ("S1", "S2")] for r in results), results


def test_two_controllers_are_independent(conn) -> None:
    add(conn, 1, S1, d(2026, 1, 1), 30)
    add(conn, 1, S2, d(2026, 1, 10), 30)
    add(conn, 2, S2, d(2026, 1, 5), 30)            # a first S2: not counted, and it must not block cid 1
    add(conn, 2, S3, d(2026, 1, 6), 30)
    add(conn, 3, C1, d(2026, 1, 7), 30)            # first C1: nothing
    assert path(conn, 1) == [(None, "S1"), ("S1", "S2")]
    assert path(conn, 2) == [("S2", "S3")]
    assert promos(conn, 3) == []
    assert len(promos(conn)) == 3


# ------------------------------------------------------------------ names
def _summary(conn, cid: int, name: str | None, start: datetime) -> None:
    conn.execute("INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) "
                 "VALUES (%s, %s, %s, %s, %s, %s, %s)", (cid, f"TEST{cid}_TWR", start, name, S1, start, start + timedelta(minutes=30)))


def test_name_comes_from_controller_summaries_latest_non_blank_else_the_cid(conn) -> None:
    for cid in (1, 2, 3, 4):
        add(conn, cid, S1, d(2026, 1, 1), 30)
    _summary(conn, 1, "Old Name", d(2025, 12, 1))
    _summary(conn, 1, "New Name", d(2026, 1, 20))
    _summary(conn, 1, "   ", d(2026, 2, 20))      # a blank latest name must not hide the real one
    _summary(conn, 2, "  Padded Name  ", d(2026, 1, 2))
    _summary(conn, 3, "", d(2026, 1, 2))           # only a blank name: the CID
    # cid 4 has no controller_summaries row at all
    names = {r["cid"]: r["name"] for r in promos(conn)}
    assert names == {1: "New Name", 2: "Padded Name", 3: "3", 4: "4"}


def test_a_controller_with_two_summary_rows_gives_one_promotion_row(conn) -> None:
    add(conn, 1, S1, d(2026, 1, 1), 30)
    _summary(conn, 1, "A", d(2026, 1, 1))
    _summary(conn, 1, "B", d(2026, 1, 2))
    assert len(promos(conn, 1)) == 1


# ------------------------------------------------------------------ the read-only role
def _ro(conn):
    import psycopg
    from psycopg.rows import dict_row

    dsn = os.environ.get("VATPAC_TEST_GRAFANA_RO_DSN")
    if dsn:
        return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    c = psycopg.connect(os.environ["VATPAC_TEST_DSN"], autocommit=True, row_factory=dict_row)
    c.execute("SET ROLE grafana_ro")
    return c


def test_grafana_ro_reads_the_view_but_not_the_session_table(conn) -> None:
    add(conn, 1, S1, d(2026, 1, 1), 30)
    priv = conn.execute("SELECT has_table_privilege('grafana_ro','controller_promotion','SELECT') AS v, "
                        "has_table_privilege('grafana_ro','atc_stats_session','SELECT') AS t").fetchone()
    assert priv == {"v": True, "t": False}
    with _ro(conn) as ro:
        assert ro.execute("SELECT count(*) AS n FROM controller_promotion").fetchone()["n"] == 1
        with pytest.raises(Exception, match="permission denied"):
            ro.execute("SELECT count(*) FROM atc_stats_session")


# ------------------------------------------------------------------ the dashboard
def _panels() -> dict[str, str]:
    dash = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    return {p["title"]: p["targets"][0]["rawSql"] for p in dash["panels"] if p.get("targets") and "rawSql" in p["targets"][0]}


def _run(ro, title: str, lo: str = "2026-06-10 00:00+00", hi: str = "2026-10-20 00:00+00", interval_ms: int = 86_400_000) -> list[dict]:
    """Run a panel's query the way Grafana would: the time range and the bar size (the browser's interval) are filled in."""
    sql = _panels()[title].replace("$__timeFrom()", f"'{lo}'::timestamptz").replace("$__timeTo()", f"'{hi}'::timestamptz")
    sql = re.sub(r"\$__timeFilter\(([^)]*)\)", lambda m: f"{m.group(1)} BETWEEN '{lo}'::timestamptz AND '{hi}'::timestamptz", sql)
    sql = sql.replace("$__interval_ms", str(interval_ms))
    assert "$__" not in sql
    return ro.execute(sql).fetchall()


def _month_start() -> datetime:
    return datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@pytest.fixture()
def dashboard_data(conn):
    """Range 10-06-2026 to 20-10-2026 (months June to October; August has none). Every session lasts 30 minutes unless stated.
    cid 1: S1 new 03-06 (before the range), S2 12-07, S3 05-09
    cid 2: S1 new 15-06, C1 20-07 (a skipped rank: one promotion, S1 to C1)
    cid 3 (named "Cee Three"): S1 new 28-06, S3 01-10, C1 19-10, C3 21-10 (after the range)
    cid 4: S1 19 minutes on 05-07 (ignored), S1 21 minutes on 06-07 (the new S1)
    cid 5: S1 new 30-09 23:59:59, cid 6: S1 new 01-10 00:00:00 (the month edges)
    cid 11: a first ever C1 on 20-07 (a visitor: no row)"""
    add(conn, 1, S1, d(2026, 6, 3))
    add(conn, 1, S2, d(2026, 7, 12))
    add(conn, 1, S3, d(2026, 9, 5))
    add(conn, 2, S1, d(2026, 6, 15))
    add(conn, 2, C1, d(2026, 7, 20))
    add(conn, 3, S1, d(2026, 6, 28))
    add(conn, 3, S3, d(2026, 10, 1))
    add(conn, 3, C1, d(2026, 10, 19))
    add(conn, 3, C3, d(2026, 10, 21))
    add(conn, 4, S1, d(2026, 7, 5), 19)
    add(conn, 4, S1, d(2026, 7, 6), 21)
    add(conn, 5, S1, d(2026, 9, 30, 23, 59, 59))
    add(conn, 6, S1, d(2026, 10, 1, 0, 0, 0))
    add(conn, 11, C1, d(2026, 7, 20))
    _summary(conn, 3, "Cee Three", d(2026, 6, 28))
    return conn


CHARTS = ("All promotions", "Promotions to S1", "Promotions to S2", "Promotions to S3", "Promotions to C1", "Promotions to C3")


def _points(ro, title: str, **kw) -> list[tuple[datetime, int]]:
    return [(r["time"], r[next(k for k in r if k != "time")]) for r in _run(ro, title, **kw)]


def test_dashboard_charts_add_up_to_the_hand_count_for_the_range(dashboard_data) -> None:
    """Range 10-06 to 20-10: 10 promotions. S1: cid 2, 3, 4, 5, 6. S2: cid 1. S3: cid 1, 3. C1: cid 2, 3. C3: none (cid 3 is 21-10,
    after the range). Out of range: cid 1 S1 (03-06). Every chart has one point per step, so the rank charts line up with the total."""
    with _ro(dashboard_data) as ro:
        total = _points(ro, "All promotions")
        assert sum(v for _, v in total) == 10 == _run(ro, "Promotions in the selected period")[0]["promotions"]
        by_rank = {r: _points(ro, f"Promotions to {r}") for r in ("S1", "S2", "S3", "C1", "C3")}
        assert {r: sum(v for _, v in pts) for r, pts in by_rank.items()} == {"S1": 5, "S2": 1, "S3": 2, "C1": 2, "C3": 0}
        assert all([t for t, _ in pts] == [t for t, _ in total] for pts in by_rank.values())
        assert [sum(pts[i][1] for pts in by_rank.values()) for i in range(len(total))] == [v for _, v in total]


def test_dashboard_short_range_has_a_point_for_every_day_with_zeros_inside_the_window(dashboard_data) -> None:
    """Range 06-07 to 13-07: cid 4 S1 on 06-07 and cid 1 S2 on 12-07. Quiet days are zero, nothing falls outside the window."""
    lo, hi = "2026-07-06 00:00+00", "2026-07-13 00:00+00"
    with _ro(dashboard_data) as ro:
        pts = _points(ro, "All promotions", lo=lo, hi=hi)
        assert [(t.strftime("%d"), v) for t, v in pts] == [("06", 1), ("07", 0), ("08", 0), ("09", 0), ("10", 0), ("11", 0),
                                                           ("12", 1), ("13", 0)]
        assert all(datetime(2026, 7, 6, tzinfo=UTC) <= t <= datetime(2026, 7, 13, tzinfo=UTC) for t, _ in pts)
        assert [v for _, v in _points(ro, "Promotions to S3", lo=lo, hi=hi)] == [0] * 8   # an empty rank is a flat zero line


def test_dashboard_week_sized_bars_put_every_promotion_in_its_own_week(dashboard_data) -> None:
    """The browser can ask for 7 day bars. Over 10-06 to 20-10 the bars start on UTC midnights that are whole multiples of 7 days
    from 01-01-1970 (Thursdays), and each of the 10 promotions is counted in the week it happened (30-09 23:59:59 in the week of
    24-09, 01-10 00:00:00 in the week of 01-10)."""
    with _ro(dashboard_data) as ro:
        pts = _points(ro, "All promotions", interval_ms=604_800_000)
    assert all(int(t.timestamp()) % 604_800 == 0 for t, _ in pts)
    assert {t.strftime("%d-%m"): v for t, v in pts if v} == {"11-06": 1, "25-06": 1, "02-07": 1, "09-07": 1, "16-07": 1,
                                                             "03-09": 1, "24-09": 1, "01-10": 2, "15-10": 1}
    assert sum(v for _, v in pts) == 10


def test_dashboard_chart_points_stay_capped_whatever_range_and_bar_size_the_browser_sends(conn) -> None:
    """The public endpoint uses the browser's interval, so a hostile request (one second bars over thousands of years)
    must not make the query build millions of points."""
    with _ro(conn) as ro:
        for title in CHARTS:
            assert len(_run(ro, title, lo="0001-01-01 00:00+00", hi="9999-12-31 00:00+00", interval_ms=1000)) <= 402
            assert len(_run(ro, title, lo="2006-11-16 00:00+00", hi="2026-11-16 00:00+00", interval_ms=1)) <= 402


def test_dashboard_empty_database_shows_zero_lines_and_no_rows(conn) -> None:
    with _ro(conn) as ro:
        assert {v for _, v in _points(ro, "All promotions")} == {0}
        assert _run(ro, "Promotions in the selected period") == [{"promotions": 0}]
        assert _run(ro, "Recent promotions") == []
        assert _run(ro, "Date of the last promotion") == [{"last_promotion": None}]


def test_dashboard_cards_and_table_match_the_hand_count(dashboard_data) -> None:
    conn = dashboard_data
    ms = _month_start()
    add(conn, 7, S1, ms)                              # this month
    add(conn, 8, S1, ms)                              # this month
    add(conn, 9, S1, ms - timedelta(days=3))          # last month
    with _ro(conn) as ro:
        assert _run(ro, "Promotions this month") == [{"promotions": 2}]
        assert _run(ro, "Promotions in the selected period") == [{"promotions": 10}]
        assert _run(ro, "Date of the last promotion") == [{"last_promotion": f"{ms:%d-%m-%Y}"}]
        table = [tuple(r.values()) for r in _run(ro, "Recent promotions")]
        assert list(_run(ro, "Recent promotions")[0]) == ["Date", "Name", "CID", "From", "To"]
    assert table == [
        ("19-10-2026", "Cee Three", "3", "S3", "C1"),
        ("01-10-2026", "Cee Three", "3", "S1", "S3"),
        ("01-10-2026", "6", "6", "new", "S1"),
        ("30-09-2026", "5", "5", "new", "S1"),
        ("05-09-2026", "1", "1", "S2", "S3"),
        ("20-07-2026", "2", "2", "S1", "C1"),
        ("12-07-2026", "1", "1", "S1", "S2"),
        ("06-07-2026", "4", "4", "new", "S1"),
        ("28-06-2026", "Cee Three", "3", "new", "S1"),
        ("15-06-2026", "2", "2", "new", "S1"),
    ]


def test_dashboard_last_promotion_card_ignores_a_future_dated_row(conn) -> None:
    add(conn, 1, S1, d(2026, 7, 1))
    add(conn, 2, S1, d(2099, 1, 1))
    with _ro(conn) as ro:
        assert _run(ro, "Date of the last promotion") == [{"last_promotion": "01-07-2026"}]


def test_dashboard_table_stops_at_500_rows(conn) -> None:
    for cid in range(1, 506):
        add(conn, cid, S1, d(2026, 7, 1) + timedelta(minutes=cid), 30)
    with _ro(conn) as ro:
        assert len(_run(ro, "Recent promotions")) == 500
        assert _run(ro, "Promotions in the selected period") == [{"promotions": 505}]


def test_every_dashboard_query_runs_as_grafana_ro_on_data(dashboard_data) -> None:
    with _ro(dashboard_data) as ro:
        for title in _panels():
            _run(ro, title)
