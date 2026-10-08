"""T15 (pilots and people): cards 180, 181, 182, 175, 183 and 155 ported from the v1 Metabase cards.

Pilot and session rows are inserted synthetically (pilot_session plus flight_summary, so the `flight` view
returns them), then the panel SQL is read from the dashboard JSON files and executed, so the test covers the
SQL that Grafana runs. Rules under test (owner's Metabase export, 08-10-2026):
  Y rule      = the fourth character from the end of the pilot name is 'Y' (v1 quirk, kept: "Joe Yoda" counts)
  180         = pilots with flights completed in the last 90 days, none with airborne time; most flights first
  181         = per UTC month of logon: pilots, and pilots whose flights that month all had zero airborne time
  182         = per UTC month of completion: pilots, and lapsed = previous row's pilots absent the month after it
  175         = per UTC month: distinct Y pilots, all pilots (logon month), distinct controllers (start month)
  183         = hours online per UTC month of completion from 1 September (UTC) of the current year
  155         = top 10 by airborne hours over 7 days, counting only flights over 30 minutes airborne
v3 mapping (see docs/t15_notes/a_pilots_people.md): enroute minutes = airborne_minutes, time online = flight
duration, completion time = flight end_time. Months are UTC even when the database session is not.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO

pytestmark = pytest.mark.integration

DASH = REPO / "grafana" / "dashboards"
ONBOARDING = DASH / "pilots" / "pilot-onboarding.json"
MANAGEMENT = DASH / "management" / "management.json"
TRAFFIC = DASH / "pilots" / "pilots-traffic.json"

NEVER_FLEW_90 = "Pilots who logged on but never flew (90 days)"
NEVER_FLEW_MONTH = "Pilots who logged on in a month but never flew that month"
LAPSED = "Pilots who flew last month but not this month (lapsed)"
ONLINE_EACH_MONTH = "Australian-based pilots and ATC online each month"
PILOT_HOURS = "Pilot hours per month (names with Y)"
MOST_ONLINE = "Which member spent the most time online? (last 7 days)"

Y_NAME = "Test Pilot YSSY"        # fourth character from the end is Y
OTHER_NAME = "Test Pilot EGLL"    # not a home name
_n = {"i": 0}


def panel_sql(path, title: str) -> str:
    panels = json.loads(path.read_text(encoding="utf-8"))["panels"]
    return next(p["targets"][0]["rawSql"] for p in panels if p.get("title") == title)


def rows(conn, path, title: str) -> list[dict]:
    return conn.execute(panel_sql(path, title)).fetchall()


def utc(year, month, day, hour=0, minute=0, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def add_flight(conn, cid: int, name: str, end: datetime, airborne: float | None = 0,
               online_minutes: float = 60, logon: datetime | None = None) -> None:
    """One flight (one session, one flight_summary row) ending at ``end``."""
    _n["i"] += 1
    start = end - timedelta(minutes=online_minutes)
    logon = logon or start
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, name, first_seen, last_seen, departure, arrival) "
        "VALUES (%s,%s,%s,%s,%s,%s,'YSSY','YMML') RETURNING id",
        (cid, f"TP{_n['i']:05d}", logon.replace(microsecond=0), name, start, end),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO flight_summary (session_id, gap_segment, span_part, cid, start_time, end_time, sample_count, "
        "airborne_minutes, max_gs_kt, max_alt_ft) VALUES (%s,0,0,%s,%s,%s,10,%s,450,35000)",
        (sid, cid, start, end, airborne),
    )


def add_controller(conn, cid: int, start: datetime, minutes: int = 30) -> None:
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) "
        "VALUES (%s,%s,%s,'Test Controller',3,%s,%s)",
        (cid, f"TEST{cid}_TWR", start.replace(microsecond=0), start, start + timedelta(minutes=minutes)),
    )


def ago(**kw) -> datetime:
    return datetime.now(UTC) - timedelta(**kw)


# ---------------------------------------------------------------- card 180

def test_180_lists_only_pilots_who_never_flew_in_90_days(conn):
    for days, online in ((1, 10), (2, 20), (3, 30)):
        add_flight(conn, 9100001, Y_NAME, ago(days=days), airborne=0, online_minutes=online)    # A: 3 zero flights
    add_flight(conn, 9100002, Y_NAME, ago(days=5), airborne=0)                                 # B: one zero flight ...
    add_flight(conn, 9100002, Y_NAME, ago(days=6), airborne=12)                                # ... and one airborne: out
    add_flight(conn, 9100003, Y_NAME, ago(days=91), airborne=0)                                # C: older than 90 days: out
    add_flight(conn, 9100004, Y_NAME, ago(days=89), airborne=0)                                # D: inside the window
    add_flight(conn, 9100005, OTHER_NAME, ago(days=2), airborne=0)                             # E: not a Y name: out
    add_flight(conn, 9100006, Y_NAME, ago(days=2), airborne=None)                              # F: airborne time unknown: out
    add_flight(conn, 9100007, Y_NAME, ago(days=3), airborne=0)                                 # G: two zero flights
    add_flight(conn, 9100007, Y_NAME, ago(days=4), airborne=0)
    got = rows(conn, ONBOARDING, NEVER_FLEW_90)
    assert [r["cid"] for r in got] == [9100001, 9100007, 9100004]      # most flights first
    a = got[0]
    assert a["total_flights"] == 3 and a["flights_with_zero_enroute_time"] == 3
    assert float(a["zero_enroute_percentage"]) == 100.0
    assert float(a["min_time_online_minutes"]) == 10 and float(a["max_time_online_minutes"]) == 30
    assert float(a["avg_time_online_minutes"]) == 20 and float(a["total_time_online_minutes"]) == 60


def test_180_old_airborne_flight_does_not_disqualify(conn):
    """v1 applies the 90-day window before grouping, so an airborne flight 100 days ago is not seen."""
    add_flight(conn, 9100008, Y_NAME, ago(days=10), airborne=0)
    add_flight(conn, 9100008, Y_NAME, ago(days=100), airborne=40)
    assert [r["cid"] for r in rows(conn, ONBOARDING, NEVER_FLEW_90)] == [9100008]


def test_y_rule_is_the_fourth_character_from_the_end(conn):
    """Kept v1 quirk: any name whose fourth-from-last character is Y counts; shorter names never do."""
    add_flight(conn, 9100010, "Joe Yoda", ago(days=1))        # 'Yoda': counts although it is not an airport
    add_flight(conn, 9100011, "YABC", ago(days=1))            # exactly four characters
    add_flight(conn, 9100012, "Yan", ago(days=1))             # too short
    add_flight(conn, 9100013, "Test Pilot YSS", ago(days=1))  # Y is the fifth from the end
    assert {r["cid"] for r in rows(conn, ONBOARDING, NEVER_FLEW_90)} == {9100010, 9100011}


# ---------------------------------------------------------------- card 181

def test_181_never_flew_that_month(conn):
    conn.execute("SET TIME ZONE 'Australia/Sydney'")      # the month must still be the UTC month
    add_flight(conn, 9200001, Y_NAME, utc(2026, 1, 5, 12), airborne=0)                 # P1: Jan, two zero flights
    add_flight(conn, 9200001, Y_NAME, utc(2026, 1, 9, 12), airborne=0)
    add_flight(conn, 9200002, Y_NAME, utc(2026, 1, 6, 12), airborne=0)                 # P2: one zero, one airborne
    add_flight(conn, 9200002, Y_NAME, utc(2026, 1, 7, 12), airborne=30)
    add_flight(conn, 9200003, Y_NAME, utc(2026, 1, 8, 12), airborne=45)                # P3: flew
    add_flight(conn, 9200004, OTHER_NAME, utc(2026, 1, 8, 12), airborne=0)             # not a Y name: ignored
    add_flight(conn, 9200005, Y_NAME, utc(2026, 2, 1, 1), airborne=0,                  # P5: logged on 31 Jan 23:30 UTC
               online_minutes=90, logon=utc(2026, 1, 31, 23, 30))                      #     (1 Feb in Sydney): January
    add_flight(conn, 9200001, Y_NAME, utc(2026, 2, 10, 12), airborne=0)                # P1 again in Feb
    add_flight(conn, 9200003, Y_NAME, utc(2026, 3, 10, 12), airborne=20)               # March: P3 flew
    got = {r["month_label"]: (r["total_pilots"], r["pilots_with_zero_enroute_time"],
                              float(r["percentage_zero_enroute"])) for r in rows(conn, ONBOARDING, NEVER_FLEW_MONTH)}
    assert got == {
        "January 2026": (4, 2, 50.0),     # P1 and P5 never flew; P2 flew once
        "February 2026": (1, 1, 100.0),
        "March 2026": (1, 0, 0.0),
    }


# ---------------------------------------------------------------- card 182

def test_182_lapsed_pilots_by_month(conn):
    conn.execute("SET TIME ZONE 'Australia/Sydney'")
    for cid in (9300001, 9300002, 9300003):                     # A, B, C fly in January
        add_flight(conn, cid, Y_NAME, utc(2026, 1, 10, 12))
    add_flight(conn, 9300004, Y_NAME, utc(2026, 1, 31, 23, 30))  # D ends 31 Jan 23:30 UTC (1 Feb in Sydney): January
    add_flight(conn, 9300001, Y_NAME, utc(2026, 2, 10, 12))     # A, B fly in February
    add_flight(conn, 9300002, Y_NAME, utc(2026, 2, 11, 12))
    add_flight(conn, 9300001, Y_NAME, utc(2026, 3, 10, 12))     # only A flies in March
    add_flight(conn, 9300009, OTHER_NAME, utc(2026, 1, 10, 12))  # not a Y name: ignored
    add_flight(conn, 9300009, OTHER_NAME, utc(2026, 2, 10, 12))
    got = rows(conn, ONBOARDING, LAPSED)
    assert [r["month_label"] for r in got] == ["January 2026", "February 2026", "March 2026"]
    jan, feb, mar = got
    assert jan["pilots_current_month"] == 4 and jan["lapsed"] is None and jan["percentage_lapsed"] is None
    assert jan["retention_summary"] == "January 2026 - 4 pilots"
    # February: 4 pilots flew in January, C and D did not fly in February
    assert feb["pilots_current_month"] == 2 and feb["lapsed"] == 2 and float(feb["percentage_lapsed"]) == 50.0
    assert feb["retention_summary"] == "February 2026 - 2 pilots, 2 lapsed from Jan"
    # March: 2 pilots flew in February, B did not fly in March
    assert mar["pilots_current_month"] == 1 and mar["lapsed"] == 1 and float(mar["percentage_lapsed"]) == 50.0


def test_182_a_month_without_flights_is_skipped_as_in_v1(conn):
    """Kept v1 quirk: LAG works on rows, so the month after an empty month compares with the month before it."""
    add_flight(conn, 9300001, Y_NAME, utc(2026, 3, 10, 12))
    add_flight(conn, 9300001, Y_NAME, utc(2026, 5, 10, 12))
    mar, may = rows(conn, ONBOARDING, LAPSED)
    assert mar["lapsed"] is None
    assert may["lapsed"] == 1 and float(may["percentage_lapsed"]) == 100.0
    assert may["retention_summary"] == "May 2026 - 1 pilots, 1 lapsed from Apr"


# ---------------------------------------------------------------- card 175

def test_175_pilots_and_atc_each_month(conn):
    conn.execute("SET TIME ZONE 'Australia/Sydney'")
    add_flight(conn, 9400001, Y_NAME, utc(2026, 1, 5, 13), logon=utc(2026, 1, 5, 12))      # P1 Y, twice: counted once
    add_flight(conn, 9400001, Y_NAME, utc(2026, 1, 20, 13), logon=utc(2026, 1, 20, 12))
    add_flight(conn, 9400002, OTHER_NAME, utc(2026, 1, 6, 13), logon=utc(2026, 1, 6, 12))  # P2 not Y
    add_flight(conn, 9400003, Y_NAME, utc(2026, 2, 1, 1), logon=utc(2026, 1, 31, 23, 30),  # P3 Y, logon 31 Jan 23:30 UTC
               online_minutes=90)
    add_flight(conn, 9400004, Y_NAME, utc(2026, 2, 10, 13), logon=utc(2026, 2, 10, 12))    # P4 Y in February
    add_controller(conn, 9450001, utc(2026, 1, 10, 9))                                      # C1 twice in January ...
    add_controller(conn, 9450001, utc(2026, 1, 20, 9))
    add_controller(conn, 9450001, utc(2026, 3, 3, 9))                                       # ... and in March
    add_controller(conn, 9450002, utc(2026, 1, 31, 23, 45))                                 # C2 starts 31 Jan 23:45 UTC
    got = {r["month"].strftime("%Y-%m"): (r["unique_pilots_with_y"], r["total_unique_pilots"], r["total_unique_atc"])
           for r in rows(conn, MANAGEMENT, ONLINE_EACH_MONTH)}
    assert got == {
        "2026-01": (2, 3, 2),     # P1 and P3 with Y, P2 without; C1 and C2
        "2026-02": (1, 1, 0),
        "2026-03": (0, 0, 1),     # month with only ATC: the full join keeps it, pilots are zero
    }


# ---------------------------------------------------------------- card 183

def test_183_hours_from_1_september_of_the_current_year(conn):
    conn.execute("SET TIME ZONE 'Australia/Sydney'")
    year = datetime.now(UTC).year
    add_flight(conn, 9500001, Y_NAME, utc(year, 9, 1) - timedelta(seconds=1), online_minutes=600)   # August: out
    add_flight(conn, 9500001, Y_NAME, utc(year - 1, 12, 15), online_minutes=600)                    # last year: out
    add_flight(conn, 9500002, Y_NAME, utc(year, 9, 1), online_minutes=90)                           # exactly 1 Sep 00:00 UTC: in
    add_flight(conn, 9500003, Y_NAME, utc(year, 9, 30, 23, 59), online_minutes=30)                  # September
    add_flight(conn, 9500004, OTHER_NAME, utc(year, 9, 15), online_minutes=600)                     # not a Y name: out
    add_flight(conn, 9500005, Y_NAME, utc(year, 10, 1), online_minutes=45)                          # October
    add_flight(conn, 9500006, Y_NAME, utc(year, 10, 15), online_minutes=15)
    got = {r["month"]: float(r["total_hours"]) for r in rows(conn, MANAGEMENT, PILOT_HOURS)}
    assert got == {f"{year}-09": 2.0, f"{year}-10": 1.0}


# ---------------------------------------------------------------- card 155

def test_155_top_pilots_by_airborne_hours(conn):
    add_flight(conn, 9600001, Y_NAME, ago(days=1), airborne=120)       # K1: 120 + 60 counted, the 25-minute flight not
    add_flight(conn, 9600001, Y_NAME, ago(days=2), airborne=60)
    add_flight(conn, 9600001, Y_NAME, ago(days=2, hours=5), airborne=25)
    add_flight(conn, 9600002, Y_NAME, ago(days=1), airborne=30)        # K2: exactly 30 is not over 30; 30.5 is
    add_flight(conn, 9600002, Y_NAME, ago(days=3), airborne=30.5)
    add_flight(conn, 9600003, Y_NAME, ago(days=8), airborne=300)       # K3: older than 7 days
    add_flight(conn, 9600004, OTHER_NAME, ago(days=1), airborne=500)   # K4: not a Y name
    add_flight(conn, 9600005, Y_NAME, ago(days=1), airborne=20)        # K5: below the 30 minute floor
    got = [(r["cid"], float(r["total_enroute_hours"])) for r in rows(conn, TRAFFIC, MOST_ONLINE)]
    assert got == [(9600001, 3.0), (9600002, 1.0)]


def test_155_limit_is_ten_and_order_is_descending(conn):
    for i in range(12):
        add_flight(conn, 9610000 + i, Y_NAME, ago(days=1), airborne=100 + 10 * i)
    got = [r["cid"] for r in rows(conn, TRAFFIC, MOST_ONLINE)]
    assert got == [9610000 + i for i in range(11, 1, -1)]


def test_155_a_changed_name_splits_the_pilot_into_two_rows(conn):
    """Kept v1 quirk: the query groups by cid and name."""
    add_flight(conn, 9620001, Y_NAME, ago(days=1), airborne=120)
    add_flight(conn, 9620001, "Test Pilot YMML", ago(days=2), airborne=120)
    assert len(rows(conn, TRAFFIC, MOST_ONLINE)) == 2


# ---------------------------------------------------------------- all panels

def test_every_new_panel_query_runs(conn):
    add_flight(conn, 9700001, Y_NAME, ago(days=1), airborne=0)
    add_controller(conn, 9750001, ago(days=1))
    for path, titles in ((ONBOARDING, None), (MANAGEMENT, None), (TRAFFIC, [MOST_ONLINE])):
        for p in json.loads(path.read_text(encoding="utf-8"))["panels"]:
            if p.get("type") == "text" or (titles and p["title"] not in titles):
                continue
            for t in p["targets"]:
                conn.execute(t["rawSql"]).fetchall()
