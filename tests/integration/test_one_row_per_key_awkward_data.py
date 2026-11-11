"""Issue #341: awkward real-world data must not make a per-member or per-flight panel show the same key twice.

Two defects escaped CI because the test data was too tidy: #285 (the Departure board listed one flight twice after a
reconnect) and #300 (a member with two names was two rows). Here the data is awkward on purpose (two names for one CID,
a pilot who logs off and back on, a reconnect that splits a flight, a duplicate logon, one callsign with two statuses) and
the REAL panel SQL from grafana/dashboards runs as grafana_ro. Every member or flight must come back exactly once.

Names carry the CID ("Ctl9810001a", "P9820001b YSSY") so a row can be traced to its member whichever column the panel shows.
The Departure board panel itself was removed under #313; its last SQL is kept below (from the commit before removal) and runs
against the `pilot_latest` view, which stays.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from conftest import REPO
from helpers import add_flight, make_session, run, run_panel, set_up_clock, session_ending, write_all

pytestmark = pytest.mark.integration

DASH = REPO / "grafana" / "dashboards"
KEY = re.compile(r"(?<!\d)98[12]\d{4}(?!\d)")         # the CIDs seeded below
CTL, PIL = 9810000, 9820000


def as_grafana_ro(conn: Any) -> None:
    """Seed as the superuser first, then call this: SET ROLE does not apply the role's own settings, so state the limit."""
    conn.execute("SET ROLE grafana_ro")
    conn.execute("SET statement_timeout = '30s'")


def members(rows: list[dict[str, Any]]) -> list[int]:
    """The CID each row stands for: a cid column, else the CID inside any text column."""
    out: list[int] = []
    for row in rows:
        found = None
        for v in row.values():
            if isinstance(v, int) and 9810000 <= v <= 9829999:
                found = v
            elif isinstance(v, str) and (m := KEY.search(v)):
                found = int(m.group())
            if found:
                break
        assert found, f"a row names no seeded member: {row}"
        out.append(found)
    return sorted(out)


# ------------------------------------------------------------------ controllers (issue 300)
def ctl(conn: Any, cid: int, days_ago: float, minutes: float, name: str | None, rating: int, callsign: str | None = None) -> None:
    start = (datetime.now(UTC) - timedelta(days=days_ago)).replace(microsecond=0)
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, first_seen, last_seen) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (cid, callsign or f"TW{cid % 100}_TWR", start, name, rating, start, start + timedelta(minutes=minutes)),
    )


def seed_controllers(conn: Any) -> None:
    for n, rating in ((1, 2), (2, 3), (3, 4), (4, 5), (5, 7), (6, 8)):   # one member per rank, each under two names
        c = CTL + n
        ctl(conn, c, 9, 60, None, rating)                                  # an unnamed old session
        ctl(conn, c, 6, 90, f"Ctl{c}a", rating)
        ctl(conn, c, 4, 60, f"Ctl{c}b", rating)
        ctl(conn, c, 2, 75, f"Ctl{c}a", rating)
    for i, rating in enumerate((2, 2, 3, 3)):                              # 7: promoted S1 to S2, name changed with it
        ctl(conn, CTL + 7, 9 - 2 * i, 60, f"Ctl{CTL + 7}{'ab'[i // 2]}", rating)
    for i, cs in enumerate(("AD_TWR", "AD_TWR", "ML_APP")):                # 8: relogs on one callsign, then a second logon at once
        ctl(conn, CTL + 8, 8 - i, 60, f"Ctl{CTL + 8}{'ab'[i % 2]}", 3, cs)
    ctl(conn, CTL + 8, 6.99, 60, f"Ctl{CTL + 8}b", 3, "BN_APP")           # overlaps the session before it (a duplicate logon)
    for n, rating in ((11, 2), (12, 3)):                                   # lapsed: last session 40 days ago, two names
        for i, days in enumerate((60, 50, 40)):
            ctl(conn, CTL + n, days, 60, f"Ctl{CTL + n}{'ab'[i % 2]}", rating)
    ctl(conn, CTL + 13, 120, 60, f"Ctl{CTL + 13}a", 2)                     # 13: away 110 days, back with one short session
    ctl(conn, CTL + 13, 110, 60, f"Ctl{CTL + 13}b", 2)
    ctl(conn, CTL + 13, 3, 10, f"Ctl{CTL + 13}c", 2)


def cs(*n: int) -> list[int]:
    return [CTL + i for i in n]


OVERVIEW, POSITIONS, RANKS, RETENTION = (DASH / "atc" / f"atc-{x}.json" for x in ("overview", "positions", "ranks", "retention"))
LONG = "Controllers with the longest median session (last 90 days)"
SHORT = "Controllers with the shortest median session (last 90 days)"
CONTROLLER_PANELS: list[tuple[Any, str, list[int] | None]] = [
    (OVERVIEW, LONG, cs(1, 2, 3, 4, 5, 6, 7, 8, 11, 12)),
    (OVERVIEW, SHORT, cs(1, 2, 3, 4, 5, 6, 7, 8, 11, 12)),
    (POSITIONS, LONG, cs(1, 2, 3, 4, 5, 6, 7, 8, 11, 12)),
    (POSITIONS, SHORT, cs(1, 2, 3, 4, 5, 6, 7, 8, 11, 12)),
    (OVERVIEW, "Top 30 service providers (last 30 days)", cs(1, 2, 3, 4, 5, 6, 7, 8, 13)),
    (POSITIONS, "Top 30 service providers (last 30 days)", cs(1, 2, 3, 4, 5, 6, 7, 8, 13)),
    (OVERVIEW, "Controllers with the shortest average session (last 30 days)", cs(1, 2, 3, 4, 5, 6, 7, 8)),
    (OVERVIEW, "Top S1 service providers (last 30 days)", cs(1, 13)),
    (OVERVIEW, "Top S2 service providers (last 30 days)", cs(2, 7, 8)),
    (OVERVIEW, "Top S3 service providers (last 30 days)", cs(3)),
    (OVERVIEW, "Top C1 service providers (last 30 days)", cs(4)),
    (OVERVIEW, "Top C3 service providers (last 30 days)", cs(5)),
    (OVERVIEW, "Top I1+ service providers (last 30 days)", cs(6)),
    (RANKS, "Top 2 controllers for each current rank (last 30 days)", None),     # two per rank at most; no member twice
    (RETENTION, "Lapsing controllers (30+ days inactive)", cs(11, 12)),
    (RETENTION, "Who might need help reactivating?", cs(13)),
    (RETENTION, "Who reactivated after being away for more than 60 days?", cs(13)),
    (RETENTION, "Controller engagement index (last 90 days)", cs(1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13)),
]


@pytest.mark.parametrize("path,title,expected", CONTROLLER_PANELS, ids=[f"{p.stem}-{t[:45]}" for p, t, _ in CONTROLLER_PANELS])
def test_a_controller_with_awkward_sessions_is_one_row(conn: Any, path: Any, title: str, expected: list[int] | None) -> None:
    seed_controllers(conn)
    as_grafana_ro(conn)
    got = members(run_panel(conn, path, title))
    if expected is None:
        assert got and len(got) == len(set(got)), f"{title}: a member appears twice {got}"
    else:
        assert got == sorted(expected), f"{title}: expected each of {sorted(expected)} once, got {got}"


# ------------------------------------------------------------------ pilots (issues 285 and 300)
def seed_pilots(conn: Any) -> None:
    def nm(n: int, k: str) -> str:
        return f"P{PIL + n}{k} YSSY"           # the panels keep only names with a Y four characters from the end

    a = PIL + 1                                 # three flights, two names, the oldest unnamed, three routes
    for days, name, (dep, arr) in ((1, nm(1, "a"), ("YSSY", "YMML")), (2, nm(1, "b"), ("YSSY", "YBBN")), (3, None, ("YMML", "YSSY"))):
        add_flight(conn, a, days_ago=days, name=name, dep=dep, arr=arr, airborne=100)
    d = PIL + 4                                 # one callsign flown on two days under two names
    add_flight(conn, d, callsign="DUP4", days_ago=1.0, name=nm(4, "a"), airborne=100)
    add_flight(conn, d, callsign="DUP4", days_ago=2.0, name=nm(4, "b"), dep="YMML", arr="YSSY", airborne=100)
    e = PIL + 5                                 # logged on three times and never left the ground
    for days, k in ((1, "a"), (2, "b"), (3, "a")):
        add_flight(conn, e, days_ago=days, name=nm(5, k), airborne=0, dep="YSSY", arr=("YMML", "YBBN", "YPAD")[days - 1])
    start = datetime.now(UTC) - timedelta(days=2)
    first = run(70, start=start, step=60, gs=250)                           # b: logs off after 69 minutes, back on 3 minutes later
    make_session(conn, PIL + 2, first)
    make_session(conn, PIL + 2, run(70, start=first[-1][0] + timedelta(minutes=3), step=60, gs=250))
    make_session(conn, PIL + 3, run(100, start=start, step=60, gs=250))     # c: two logons one second apart, overlapping
    make_session(conn, PIL + 3, run(100, start=start + timedelta(seconds=1), step=60, gs=250))
    for cid, k in ((PIL + 2, 2), (PIL + 3, 3)):
        conn.execute("UPDATE pilot_session SET callsign = %s, aircraft_icao = 'B738', remarks = 'RAAFVIRTUAL.ORG', name = %s WHERE cid = %s",
                     (f"RRR{k}", nm(k, "a"), cid))
    conn.execute("UPDATE pilot_session SET name = %s WHERE cid = %s AND logon_time = (SELECT max(logon_time) FROM pilot_session WHERE cid = %s)",
                 (nm(2, "b"), PIL + 2, PIL + 2))
    assert write_all(conn) == 2, "the writer makes one flight for b (a reconnect) and one for c (a duplicate logon)"


def ps(*n: int) -> list[int]:
    return [PIL + i for i in n]


RECORDS, TRAFFIC, ONBOARDING, MILITARY = (DASH / d for d in ("pilots/pilots-records.json", "pilots/pilots-traffic.json",
                                                              "pilots/pilot-onboarding.json", "military/military.json"))
MOST_TIME = "Which member spent the most time online? (last 7 days)"
MOST_FLIGHTS = "Pilots with the most flights (last 30 days)"
DIVERSE = "Pilots with the most diverse routes (last 14 days)"
PILOT_PANELS: list[tuple[Any, str, list[int]]] = [
    (RECORDS, MOST_TIME, ps(1, 2, 3, 4)),
    (TRAFFIC, MOST_TIME, ps(1, 2, 3, 4)),
    (RECORDS, MOST_FLIGHTS, ps(1, 2, 3, 4)),
    (RECORDS, DIVERSE, ps(1, 2, 3, 4, 5)),
    (TRAFFIC, DIVERSE, ps(1, 2, 3, 4, 5)),
    (ONBOARDING, "Pilots who logged on but never flew (90 days)", ps(5)),
    (MILITARY, "RAAFv flights (last 7 days)", ps(2, 3)),                   # one row per flight: the split and the duplicate logon are one each
]


@pytest.mark.parametrize("path,title,expected", PILOT_PANELS, ids=[f"{p.stem}-{t[:45]}" for p, t, _ in PILOT_PANELS])
def test_a_pilot_with_awkward_logons_is_one_row(conn: Any, path: Any, title: str, expected: list[int]) -> None:
    seed_pilots(conn)
    as_grafana_ro(conn)
    got = members(run_panel(conn, path, title))
    assert got == sorted(expected), f"{title}: expected each of {sorted(expected)} once, got {got}"


def test_a_reconnect_and_a_duplicate_logon_are_each_one_flight(conn: Any) -> None:
    seed_pilots(conn)
    as_grafana_ro(conn)
    rows = conn.execute("SELECT cid, count(*) AS n FROM flight WHERE cid = ANY(%s) GROUP BY cid", (ps(1, 2, 3, 4, 5),)).fetchall()
    assert {r["cid"]: r["n"] for r in rows} == {PIL + 1: 3, PIL + 2: 1, PIL + 3: 1, PIL + 4: 2, PIL + 5: 3}


# ------------------------------------------------------------------ the Departure board (issue 285)
BOARD = """
SELECT callsign AS "Flight", status AS "Status"
FROM (
  SELECT l.*,
         CASE WHEN NOT l.is_online THEN 'LOGGED OFF'
              WHEN l.gs_kt >= 60 THEN 'AIRBORNE'
              WHEN l.gs_kt > 0 THEN 'TAXIING'
              WHEN l.gs_kt = 0 THEN 'ON GROUND'
              ELSE 'UNKNOWN' END AS status,
         CASE WHEN NOT l.is_online THEN 5 WHEN l.gs_kt >= 60 THEN 4 WHEN l.gs_kt > 0 THEN 3 WHEN l.gs_kt = 0 THEN 1 ELSE 6 END AS status_rank
  FROM pilot_latest l
  WHERE l.departure = 'YSSY'
) b
ORDER BY status_rank, last_seen DESC
LIMIT 25
"""   # the removed Departure board query (grafana/dashboards/pilots/pilots-now.json before commit ae2959d), unchanged


def test_the_departure_board_lists_each_flight_once_whatever_the_logon_history(conn: Any) -> None:
    t = set_up_clock(conn)

    def logon(cid: int, callsign: str, age_s: int, n: int, gs: int, name: str | None = None) -> None:
        sid = make_session(conn, cid, session_ending(t, age_s, n=n, gs=gs))
        conn.execute("UPDATE pilot_session SET callsign = %s, name = %s WHERE id = %s", (callsign, name, sid))

    logon(PIL + 11, "JST484", 0, 20, 0)                       # JST484: two identical logons, both online
    logon(PIL + 11, "JST484", 0, 30, 0)
    logon(PIL + 12, "QLK1440", 7200, 40, 0)                   # QLK1440: logged off two hours ago ...
    logon(PIL + 12, "QLK1440", 0, 20, 0)                      # ... and back on the ground now
    logon(PIL + 13, "ABC313", 600, 20, 250, "Pat")           # one flight, two names, two logons
    logon(PIL + 13, "ABC313", 20, 30, 250, "Pat Q")
    logon(PIL + 14, "DUP14", 10, 20, 250)                     # a duplicate logon: both seen last at the same moment
    logon(PIL + 14, "DUP14", 10, 30, 250)
    logon(PIL + 15, "SHARED", 10, 20, 250)                    # two different pilots on one callsign are two flights
    logon(PIL + 16, "SHARED", 10, 20, 250)
    as_grafana_ro(conn)
    rows = conn.execute(BOARD).fetchall()
    assert sorted(r["Flight"] for r in rows) == ["ABC313", "DUP14", "JST484", "QLK1440", "SHARED", "SHARED"]
    assert [r["Status"] for r in rows if r["Flight"] == "QLK1440"] == ["ON GROUND"]
