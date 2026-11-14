"""Issue #50: rule tests for the panels that no other test named (the test audit's "still none" list, and smoke only).

Online-now panels, the "over time" series, the home-region pie charts and the joining cohorts, the quietest airports, the
four retention panels that were only run, and the monitoring dashboard. The SQL is read from the dashboard JSON with
Grafana's macros replaced by literals (`run`), data is inserted relative to now(), and each test asserts the figures a
reader of the panel would see. docs/CHART_DEFINITIONS.md names these tests in its Test column.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO
from helpers import partition_for, record_flight, set_poll_interval

pytestmark = pytest.mark.integration

DASHBOARDS = REPO / "grafana" / "dashboards"
_serial = {"n": 0}


def sql_of(title: str) -> str:
    def walk(panels):
        for p in panels:
            yield p
            yield from walk(p.get("panels", []))

    for path in DASHBOARDS.rglob("*.json"):
        for p in walk(json.loads(path.read_text(encoding="utf-8"))["panels"]):
            if p.get("title") == title and p.get("targets") and "rawSql" in p["targets"][0]:
                return p["targets"][0]["rawSql"]
    raise AssertionError(f"no panel titled {title!r}")


def run(conn, title: str, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
    """Run a panel's SQL. Macros: $__timeFrom/$__timeTo, $__timeFilter(col), $__timeGroupAlias(col, $__interval) (5 minute bins)."""
    now = datetime.now(UTC)
    lo, hi = f"'{(start or now - timedelta(days=30)).isoformat()}'::timestamptz", f"'{(end or now + timedelta(minutes=1)).isoformat()}'::timestamptz"
    sql = sql_of(title).replace("$__timeFrom()", lo).replace("$__timeTo()", hi)
    sql = re.sub(r"\$__timeFilter\((\w+)\)", rf"\1 BETWEEN {lo} AND {hi}", sql)
    sql = re.sub(r"\$__timeGroupAlias\((\w+), \$__interval\)", r"date_bin(interval '5 minutes', \1, timestamptz '1970-01-01') AS time", sql)
    return conn.execute(sql.rstrip().rstrip(";")).fetchall()


def db_now(conn) -> datetime:
    """The database's clock. Panels compare stored times with the database's now(), so a test that asserts an exact figure
    or a narrow range must build its times from the same clock; the Python clock can differ by a fraction of a second (or
    more, on Docker Desktop) and that flips a rounding boundary."""
    return conn.execute("SELECT now() AS t").fetchone()["t"]


def snapshot(conn, at: datetime, pilots=1, controllers=1, pilots_global=1, controllers_global=1) -> None:
    conn.execute("INSERT INTO network_snapshot VALUES (%s,%s,%s,%s,%s)", (at, pilots, controllers, pilots_global, controllers_global))


def flight(conn, cid: int, *, home: str | None, dep="YSSY", arr="YMML", days_ago: float = 1, airborne: float = 60.0) -> None:
    _serial["n"] += 1
    end = datetime.now(UTC) - timedelta(days=days_ago)
    start = end - timedelta(hours=2)
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, name, first_seen, last_seen, departure, arrival, home_icao) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (cid, f"GAP{_serial['n']:04d}", start.replace(microsecond=0), "Pilot", start, end, dep, arr, home)).fetchone()["id"]
    record_flight(conn, sid, start, end, airborne=airborne)


def ctrl(conn, cid: int, days_ago: float, minutes: int = 60, rating: int = 3, callsign: str | None = None,
         last_seen: datetime | None = None, frequency_hz: int | None = None) -> None:
    _serial["n"] += 1
    if last_seen is not None:                               # the session ends at last_seen and is ``minutes`` long
        start = (last_seen - timedelta(minutes=minutes)).replace(microsecond=0)
    else:
        start = (datetime.now(UTC) - timedelta(days=days_ago)).replace(microsecond=0)
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen, frequency_hz) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (cid, callsign or f"GP{_serial['n']:03d}_TWR", start, f"Controller {cid}", rating, start,
         last_seen or start + timedelta(minutes=minutes), frequency_hz))


# ------------------------------------------------------------------ online now and over time
def test_where_pilots_online_come_from(conn) -> None:
    set_poll_interval(conn, 30)
    now = datetime.now(UTC)
    partition_for(conn, now)
    snapshot(conn, now - timedelta(seconds=40))
    snapshot(conn, now - timedelta(seconds=10))
    for cid, home, seen in [(9961001, "YSSY", 10), (9961002, "YMML", 10), (9961003, "EGLL", 10), (9961004, "KLAX", 600), (9961005, None, 10)]:
        t = now - timedelta(seconds=seen)
        sid = conn.execute(
            "INSERT INTO pilot_session (cid, callsign, logon_time, name, first_seen, last_seen, home_icao) "
            "VALUES (%s,%s,%s,'Pilot',%s,%s,%s) RETURNING id", (cid, f"WH{cid}", (t - timedelta(hours=1)).replace(microsecond=0), t - timedelta(hours=1), t, home)).fetchone()["id"]
        conn.execute("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt) VALUES (%s,%s,-33,151,5000,250)", (t, sid))
    rows = run(conn, "Where pilots currently online come from")
    assert (rows[0]["region"], rows[0]["pilots"]) == ("Australia", 2)
    assert sorted((r["region"], r["pilots"]) for r in rows) == [("Australia", 2), ("Europe", 1), ("Unknown", 1)]   # KLAX seen 10 minutes ago: not online


# ------------------------------------------------------------------ pie charts, cohorts and airports
def test_flights_and_airborne_hours_by_home_region(conn) -> None:
    for cid, home, airborne in [(9962001, "YSSY", 90), (9962002, "YMML", 30), (9962003, "EGLL", 120), (9962004, "EGLL", 0), (9962005, None, 60)]:
        flight(conn, cid, home=home, airborne=airborne)
    flight(conn, 9962006, home="KLAX", days_ago=40)                                  # outside the 30 days
    flight(conn, 9962007, home="KLAX", airborne=0)                                   # parked connection: a flight, but no airborne hours
    flights = run(conn, "Who flies most in Australian airspace: flights by home region (last 30 days)")
    assert sorted((r["region"], r["flights"]) for r in flights) == [("Australia", 2), ("Europe", 2), ("United States", 1), ("Unknown", 1)]
    assert flights[0]["flights"] == 2 and flights[-1]["flights"] == 1                  # most flights first (ties have no set order)
    hours = run(conn, "Who spends most time in Australian airspace: airborne hours by home region (last 30 days)")
    assert sorted((r["region"], float(r["hours"])) for r in hours) == [("Australia", 2.0), ("Europe", 2.0), ("Unknown", 1.0)]
    assert hours[-1]["region"] == "Unknown"          # EGLL's parked flight adds nothing and a region with only parked flights is not listed


def test_flights_by_joining_cohort(conn) -> None:
    flight(conn, 150000, home="YSSY")
    flight(conn, 150001, home="YSSY")
    flight(conn, 150001, home="YSSY")                       # same pilot, second flight
    flight(conn, 1234567, home="YSSY")
    flight(conn, 99999, home="YSSY")                        # CID below 100000: cohort 0 is left out (as in v1)
    flight(conn, 150002, home="YSSY", days_ago=45)          # outside the window
    rows = run(conn, "Flights by joining cohort (CID range, last 30 days)")
    assert [(r["cohort"], r["flights"], r["pilots"]) for r in rows] == [("10-", 3, 2), ("120-", 1, 1)]  # only the start of the range is shown, in units of 10,000


# ------------------------------------------------------------------ retention panels that were only run
def test_activity_levels_of_controllers(conn) -> None:
    for d in (1, 5, 9, 14):
        ctrl(conn, 9964001, d)                              # four sessions in 30 days: Active Regular
    for d in (3, 20):
        ctrl(conn, 9964002, d)                              # two: Active Casual
    ctrl(conn, 9964003, 45)                                 # Lapsing
    ctrl(conn, 9964004, 75)                                 # At Risk
    ctrl(conn, 9964005, 120)                                # Lost
    rows = run(conn, "Activity levels of controllers")
    assert [(r["stage"], r["controller_count"], float(r["percentage"])) for r in rows] == [
        ("Active Regular", 1, 20.0), ("Active Casual", 1, 20.0), ("Lapsing", 1, 20.0), ("At Risk", 1, 20.0), ("Lost", 1, 20.0)]


def test_active_regular_needs_four_sessions_and_casual_one_to_three(conn) -> None:
    for d in (1, 5, 9):
        ctrl(conn, 9964011, d)                              # three: casual, not regular
    rows = run(conn, "Activity levels of controllers")
    assert [(r["stage"], r["controller_count"]) for r in rows] == [("Active Casual", 1)]


def test_lapsing_controllers_lists_thirty_days_or_more_most_lapsed_first(conn) -> None:
    ctrl(conn, 9965001, 29.5, rating=2)                     # recent: not listed
    ctrl(conn, 9965002, 31, rating=3, minutes=90)           # Lapsing (30-59 days)
    ctrl(conn, 9965005, 30, rating=3)                       # exactly 30 days: listed
    ctrl(conn, 9965003, 61, rating=4)                       # At Risk
    ctrl(conn, 9965004, 100, rating=5)                      # Lost
    ctrl(conn, 9965004, 130, rating=5, minutes=120)
    rows = run(conn, "Lapsing controllers (30+ days inactive)")
    assert [(r["cid"], r["rank_group"], r["days_since_last_session"], r["lapse_category"]) for r in rows] == [
        (9965004, "C1", 100, "Lost (90+ days)"), (9965003, "S3", 61, "At Risk (60-89 days)"),
        (9965002, "S2", 31, "Lapsing (30-59 days)"), (9965005, "S2", 30, "Lapsing (30-59 days)")]
    assert (rows[0]["total_sessions"], float(rows[0]["total_hours_ever"])) == (2, 3.0)


def test_engagement_index_quadrants_and_risk(conn) -> None:
    for d in range(12):
        ctrl(conn, 9966001, 2 + d, minutes=90)              # 12 sessions of 1.5 h: Super Engaged
    for d in (3, 9):
        ctrl(conn, 9966002, d, minutes=60)                  # 2 short sessions: At Risk, high risk
    for d in (4, 11, 18, 25, 32):
        ctrl(conn, 9966003, d, minutes=120)                 # 5 long sessions: Casual but Committed
    ctrl(conn, 9966004, 100, minutes=240)                   # older than 90 days: not in the matrix
    rows = {r["cid"]: r for r in run(conn, "Controller engagement index (last 90 days)")}
    assert set(rows) == {9966001, 9966002, 9966003}
    assert (rows[9966001]["engagement_quadrant"], rows[9966001]["risk_of_lapsing"], rows[9966001]["color_value"]) == ("Super Engaged", "Low Risk", 4)
    assert (rows[9966002]["engagement_quadrant"], rows[9966002]["risk_of_lapsing"], rows[9966002]["color_value"]) == ("At Risk", "High Risk", 1)
    assert (rows[9966003]["engagement_quadrant"], rows[9966003]["risk_of_lapsing"], rows[9966003]["color_value"]) == ("Casual but Committed", "Low Risk", 3)
    assert float(rows[9966001]["total_hours"]) == 18.0 and float(rows[9966003]["avg_session_duration_hours"]) == 2.0
    assert [r["cid"] for r in run(conn, "Controller engagement index (last 90 days)")][0] == 9966001   # most hours first


def test_s1_cohort_retention(conn) -> None:
    label = lambda days: conn.execute("SELECT to_char(date_trunc('month', now() - make_interval(days => %s)), 'MM-YYYY') AS m", (days,)).fetchone()["m"]  # noqa: E731
    ctrl(conn, 9967001, 100, rating=2)                      # S1 joiner, active again around day 30, 60 and 90
    for extra in (30, 60, 90):
        ctrl(conn, 9967001, 100 - extra, rating=3)
    ctrl(conn, 9967002, 100, rating=2)                      # S1 joiner who never came back
    ctrl(conn, 9967003, 100, rating=3)                      # first session as S2: not an S1 cohort member
    ctrl(conn, 9967004, 40, rating=2)                       # younger cohort: only the day 30 figure exists yet
    ctrl(conn, 9967004, 10, rating=3)
    rows = {r["cohort"]: r for r in run(conn, "S1 new controller retention by cohort")}
    assert list(rows) == [label(100), label(40)]            # labelled MM-YYYY, oldest month first (sorted by the month, not by the text)
    old, young = rows[label(100)], rows[label(40)]
    assert old["cohort_size"] == 2
    assert [float(old[c]) for c in ("Day 30 (%)", "Day 60 (%)", "Day 90 (%)")] == [50.0, 50.0, 50.0]
    assert young["cohort_size"] == 1 and float(young["Day 30 (%)"]) == 100.0 and young["Day 60 (%)"] is None


# ------------------------------------------------------------------ monitoring dashboard
def scalar(conn, title: str):
    (value,) = run(conn, title)[0].values()
    return value


def test_age_panels_read_the_newest_record_of_their_own_source(conn) -> None:
    now = datetime.now(UTC)
    snapshot(conn, now - timedelta(seconds=100))
    snapshot(conn, now - timedelta(seconds=400))
    conn.execute("INSERT INTO poll_log (status, polled_at) VALUES ('ok', %s), ('ok', %s), ('error', %s)",
                 (now - timedelta(seconds=50), now - timedelta(seconds=500), now - timedelta(seconds=5)))
    ctrl(conn, 9968001, 0, last_seen=now - timedelta(seconds=300))
    conn.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) VALUES (9968002,'AGE1',%s,%s,%s)",
                 ((now - timedelta(hours=1)).replace(microsecond=0), now - timedelta(hours=1), now - timedelta(seconds=200)))
    got = {t: float(scalar(conn, t)) for t in ("Newest snapshot age", "Last successful poll age", "Newest pilot sighting age",
                                               "Newest controller sighting age")}
    for title, want in [("Newest snapshot age", 100), ("Last successful poll age", 50), ("Newest pilot sighting age", 200),
                        ("Newest controller sighting age", 300)]:
        assert want <= got[title] <= want + 5, (title, got[title])      # an error poll (5 s) is not a successful poll


def test_snapshots_missed_compares_stored_snapshots_with_the_poll_interval(conn) -> None:
    set_poll_interval(conn, 30)
    now = db_now(conn)
    for k in range(60):
        snapshot(conn, now - timedelta(seconds=3600 - 60 * k))     # one a minute for an hour: half are missing
    snapshot(conn, now - timedelta(seconds=1))
    assert float(scalar(conn, "Snapshots missed (last 24 hours)")) == 49.17        # 61 of 120 expected
    for k in range(60):
        snapshot(conn, now - timedelta(seconds=3600 - 60 * k - 30))
    assert float(scalar(conn, "Snapshots missed (last 24 hours)")) == 0.0          # never below zero


def test_poll_counters_cover_the_last_24_hours_only(conn) -> None:
    now = datetime.now(UTC)
    rows = ([("ok", 1, 5, 100)] * 6 + [("skipped_unchanged", 2, None, None)] * 2 + [("error", 3, None, None)] * 3
            + [("skipped_unchanged", 30, None, None), ("error", 30, 9, 9)])
    for status, hours_ago, rejected, duration in rows:
        conn.execute("INSERT INTO poll_log (status, polled_at, rejected_values, duration_ms) VALUES (%s,%s,%s,%s)",
                     (status, now - timedelta(hours=hours_ago), rejected, duration))
    assert float(scalar(conn, "Unchanged polls (last 24 hours)")) == 18.2          # 2 of 11, the 30-hour-old rows do not count
    assert scalar(conn, "Poll errors (last 24 hours)") == 3
    assert scalar(conn, "Rejected values (last 24 hours)") == 30
    conn.execute("TRUNCATE poll_log")
    assert scalar(conn, "Rejected values (last 24 hours)") == 0 and scalar(conn, "Poll errors (last 24 hours)") == 0
    assert scalar(conn, "Unchanged polls (last 24 hours)") is None


def test_poll_series_panels(conn) -> None:
    base = datetime.fromtimestamp(int(datetime.now(UTC).timestamp()) // 300 * 300 - 7200, UTC)
    for status, off, kept, ctl, dur in [("ok", 10, 20, 4, 100), ("ok", 40, 22, 5, 300), ("skipped_unchanged", 70, 99, 9, 50),
                                        ("error", 100, None, None, None), ("ok", 320, 30, 6, 200)]:
        conn.execute("INSERT INTO poll_log (status, polled_at, pilots_kept, controllers_kept, duration_ms) VALUES (%s,%s,%s,%s,%s)",
                     (status, base + timedelta(seconds=off), kept, ctl, dur))
    end = base + timedelta(hours=1)
    outcome = run(conn, "Polls by outcome", base, end)
    assert [(r["time"], r["ok"], r["skipped_unchanged"], r["error"]) for r in outcome] == [
        (base, 2, 1, 1), (base + timedelta(minutes=5), 1, 0, 0)]
    written = run(conn, "Rows written", base, end)
    assert [(r["time"], r["Pilot rows"], r["Controller rows"]) for r in written] == [
        (base, 42, 9), (base + timedelta(minutes=5), 30, 6)]                 # only ok polls
    duration = run(conn, "Poll duration", base, end)
    assert [(r["time"], float(r["Average (ms)"]), r["Maximum (ms)"]) for r in duration] == [
        (base, 150.0, 300), (base + timedelta(minutes=5), 200.0, 200)]       # polls without a duration are left out


def test_recent_poll_errors_newest_first_at_most_twenty(conn) -> None:
    now = datetime.now(UTC)
    for i in range(25):
        conn.execute("INSERT INTO poll_log (status, polled_at, http_status, duration_ms, error) VALUES ('error',%s,503,%s,%s)",
                     (now - timedelta(minutes=i), 10 + i, f"boom {i}"))
    conn.execute("INSERT INTO poll_log (status, polled_at) VALUES ('ok', %s)", (now,))
    conn.execute("INSERT INTO poll_log (status, polled_at, http_status, duration_ms, error) VALUES ('error',%s,503,5,'boom old')",
                 (now - timedelta(days=40),))
    rows = run(conn, "Recent poll errors")
    assert len(rows) == 20 and rows[0]["error"] == "boom 0" and rows[-1]["error"] == "boom 19"
    assert rows[0]["http_status"] == 503 and rows[0]["duration_ms"] == 10
    # the panel follows the dashboard time range: an error 40 days old is outside a 24 hour range
    narrow = run(conn, "Recent poll errors", start=now - timedelta(hours=24))
    assert "boom old" not in [r["error"] for r in narrow] and len(narrow) == 20
    wide = run(conn, "Recent poll errors", start=now - timedelta(days=60))
    assert len(wide) == 20 and "boom old" not in [r["error"] for r in wide]    # newest 20 still win
    only_old = run(conn, "Recent poll errors", start=now - timedelta(days=60), end=now - timedelta(days=30))
    assert [r["error"] for r in only_old] == ["boom old"]


def test_data_freshness_table_counts_the_last_hour(conn) -> None:
    now = db_now(conn)
    snapshot(conn, now - timedelta(seconds=20))
    conn.execute("INSERT INTO poll_log (status, polled_at) VALUES ('ok',%s),('ok',%s),('skipped_unchanged',%s),('error',%s),('error',%s)",
                 (now - timedelta(seconds=15), now - timedelta(minutes=30), now - timedelta(minutes=5), now - timedelta(minutes=10),
                  now - timedelta(hours=2)))
    (row,) = run(conn, "Data freshness (data engineer view)")
    assert (row["ok_last_hour"], row["skipped_last_hour"], row["error_last_hour"]) == (2, 1, 1)
    assert 20 <= float(row["snapshot_age_seconds"]) <= 25 and 15 <= float(row["last_ok_age_seconds"]) <= 20


def test_shortest_average_session_needs_three_sessions_in_the_window_and_shows_fifteen(conn) -> None:
    for d, m in [(1, 10), (2, 10), (3, 11)]:
        ctrl(conn, 9969001, d, minutes=m)                   # average 10.33 minutes
    for d, m in [(1, 20), (2, 40), (3, 60)]:
        ctrl(conn, 9969002, d, minutes=m)                   # 40.0
    for d in (1, 2):
        ctrl(conn, 9969003, d, minutes=5)                   # only two sessions: not listed
    for d in (1, 2, 40):
        ctrl(conn, 9969004, d, minutes=5)                   # the third is outside the 30 days: two in the window
    for k in range(15):
        for d in (1, 2, 3):
            ctrl(conn, 9969100 + k, d, minutes=100)         # fifteen more controllers at 100.0
    rows = run(conn, "Controllers with the shortest average session (last 30 days)")
    assert len(rows) == 15
    assert [(r["controller"], float(r["Average session (minutes)"])) for r in rows[:3]] == [
        ("Controller 9969001 (9969001, S2)", 10.3), ("Controller 9969002 (9969002, S2)", 40.0),
        ("Controller 9969100 (9969100, S2)", 100.0)]
    assert rows[-1]["controller"] == "Controller 9969112 (9969112, S2)"   # ties are broken by CID; the 14th and 15th of the 100-minute group are cut


def _label_rows(conn, title: str) -> dict[int, str]:
    """Run the two relabelled overview charts and key each label by the CID it names."""
    rows = run(conn, title)
    return {int(r["controller"].rsplit("(", 1)[1].split(",")[0]): r["controller"] for r in rows}


def _named_ctrl(conn, cid: int, days_ago: float, name: str | None, rating: int | None, minutes: int = 60) -> None:
    start = (datetime.now(UTC) - timedelta(days=days_ago)).replace(microsecond=0)
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (cid, f"LB{cid % 1000:03d}_TWR", start, name, rating, start, start + timedelta(minutes=minutes)))


def test_controller_charts_label_bars_name_cid_and_highest_rank_like_the_median_charts(conn) -> None:
    """Issue 353: "Name (CID, rank)" on the top 30 and the shortest average session charts. The name is the latest non-empty
    one, the CID stands in for a missing, empty or blank name, and the rank is the highest rating in the window."""
    for d in (1, 2, 3):
        _named_ctrl(conn, 9971001, d + 10, "Old Name", 3)                    # three sessions; the name and rating change
    _named_ctrl(conn, 9971001, 0.5, "Tom", 5)                                # latest name Tom, highest rating C1
    _named_ctrl(conn, 9971001, 0.2, "", 3)                                   # the latest session has an empty name: Tom stays
    for cid, name, rating in [(9971002, None, 8), (9971003, "", 4), (9971004, "   ", 2), (9971005, "Ann", None),
                              (9971006, "Obs", 1), (9971007, "Two", 6)]:
        for d in (1, 2, 3):
            _named_ctrl(conn, cid, d, name, rating)
    want = {9971001: "Tom (9971001, C1)", 9971002: "9971002 (9971002, I1+)", 9971003: "9971003 (9971003, S3)",
            9971004: "9971004 (9971004, S1)", 9971005: "Ann (9971005, Unknown)",
            9971006: "Obs (9971006, Unknown)", 9971007: "Two (9971007, Unknown)"}   # ratings 1 and 6 read Unknown, as on the median charts
    for title in ("Top 30 service providers (last 30 days)", "Controllers with the shortest average session (last 30 days)"):
        assert _label_rows(conn, title) == want, title
