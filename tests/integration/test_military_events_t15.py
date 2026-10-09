"""T15: RAAFv cards 142, 143, 161, 184 and Monday event card 156, rebuilt from the owner's current Metabase SQL.

Rows are inserted synthetically (pilot_session plus flight_summary, which the `flight` view joins; pilot_position
for card 156). The panel SQL is read from the dashboard JSON, the Grafana time macros are replaced by literal UTC
timestamps, and the query is executed. Rules under test:
  all RAAFv cards  RAAFv = flight-plan remarks contain RAAFVIRTUAL.ORG (any case), whatever the aircraft type.
  card 142         logon time in the window (inclusive edges), distinct rows, newest first, names and CIDs shown.
  card 143         flights ending in the window; per aircraft type: distinct sessions, airborne minutes and hours.
  card 161         flights with logon in the window; one count per session (split rows count once); shares.
  card 184         flights per UTC week of the flight end (weeks start Monday).
  card 156         distinct sessions with a sample above 1000 ft in each 5-minute slot, 0700-1200Z on the latest
                   Monday; no slot at 1200; no sector logic (STAYS-DROPPED parts: none are built).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO
from helpers import partition_for

pytestmark = pytest.mark.integration

MILITARY = REPO / "grafana" / "dashboards" / "military" / "military.json"
EVENTS = REPO / "grafana" / "dashboards" / "events" / "monday-events.json"

RAAFV_REMARKS = "PBN/A1 OPR/RAAFVIRTUAL.ORG RMK/TEST"


def panel(dashboard, title_start: str) -> dict:
    panels = json.loads(dashboard.read_text(encoding="utf-8"))["panels"]
    return next(p for p in panels if p.get("title", "").startswith(title_start))


def run_panel(conn, dashboard, title_start: str, start: datetime, end: datetime, now: datetime | None = None) -> list[dict]:
    """Run a panel's SQL. ``now`` is a clock seam for panels that read now(): the literal text now() is replaced by that
    instant, so a test does not depend on the day or hour it runs."""
    sql = panel(dashboard, title_start)["targets"][0]["rawSql"]
    sql = sql.replace("$__timeFrom()", f"'{start.isoformat()}'::timestamptz")
    sql = sql.replace("$__timeTo()", f"'{end.isoformat()}'::timestamptz")
    if now is not None:
        assert "now()" in sql
        sql = sql.replace("now()", f"'{now.isoformat()}'::timestamptz")
    return conn.execute(sql.rstrip().rstrip(";")).fetchall()


def add_flight(conn, cid: int, start: datetime, end: datetime, airborne: float = 60.0, dep="YSSY", arr="YMML",
               remarks: str | None = None, aircraft: str = "B738", name: str | None = None,
               segments: int = 1) -> int:
    """One pilot_session with `segments` flight_summary rows (segments > 1 mimics a gap or span split)."""
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival, "
        "aircraft_icao, remarks, name) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (cid, f"TST{cid}", start.replace(microsecond=0), start, end, dep, arr, aircraft, remarks, name),
    ).fetchone()["id"]
    for seg in range(1, segments + 1):
        seg_end = end - timedelta(minutes=10 * (segments - seg))
        conn.execute(
            "INSERT INTO flight_summary (session_id, gap_segment, span_part, cid, start_time, end_time, "
            "sample_count, airborne_minutes, final) VALUES (%s,%s,0,%s,%s,%s,10,%s,true)",
            (sid, seg, cid, start, seg_end, airborne),
        )
    return sid


def monday_utc(weeks_back: int) -> datetime:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=today.weekday()) - timedelta(weeks=weeks_back)


# ------------------------------------------------------------------ card 142
def test_card_142_lists_raafv_flights_by_remarks_in_logon_window(conn) -> None:
    now = datetime.now(UTC)
    t = now - timedelta(days=2)
    add_flight(conn, 9500001, t, t + timedelta(hours=2), remarks=RAAFV_REMARKS, name="Test Pilot YSSY",
               aircraft="C130")
    add_flight(conn, 9500002, t + timedelta(hours=1), t + timedelta(hours=3), remarks="opr/raafvirtual.org",
               aircraft="F18")
    add_flight(conn, 9500003, t, t + timedelta(hours=2), aircraft="F18")                  # military type, no remarks
    add_flight(conn, 9500004, t, t + timedelta(hours=2), remarks="RMK/OTHER")             # other remarks
    add_flight(conn, 9500005, t, t + timedelta(hours=2), remarks=RAAFV_REMARKS, dep="")   # flight gate: not listed
    old = now - timedelta(days=8)
    add_flight(conn, 9500006, old, old + timedelta(hours=1), remarks=RAAFV_REMARKS)       # outside 7 days
    rows = run_panel(conn, MILITARY, "RAAFv flights (last 7 days)", now - timedelta(days=7), now)
    assert [r["cid"] for r in rows] == [9500002, 9500001]          # newest logon first
    assert rows[1]["name"] == "Test Pilot YSSY"                    # names and CIDs are shown (ADR-009)
    assert rows[1]["aircraft_type"] == "C130"
    assert (rows[1]["departure"], rows[1]["arrival"]) == ("YSSY", "YMML")


def test_card_142_window_edges_and_distinct_rows(conn) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(days=7)
    # logon exactly on the lower edge is inside; a session split in two flight rows is listed once
    add_flight(conn, 9500011, start, start + timedelta(hours=3), remarks=RAAFV_REMARKS, segments=2)
    add_flight(conn, 9500012, start - timedelta(seconds=1), start + timedelta(hours=1), remarks=RAAFV_REMARKS)
    rows = run_panel(conn, MILITARY, "RAAFv flights (last 7 days)", start, now)
    assert [r["cid"] for r in rows] == [9500011]
    assert len(rows) == 1


# ------------------------------------------------------------------ card 143
def test_card_143_airborne_time_by_type_counts_sessions_once(conn) -> None:
    now = datetime.now(UTC)
    t = now - timedelta(days=3)
    # C130: two sessions, 120 and 60 airborne minutes (the 120 minute one is split into two rows of 60)
    add_flight(conn, 9500021, t - timedelta(hours=4), t, 60, aircraft="C130", remarks=RAAFV_REMARKS, segments=2)
    add_flight(conn, 9500022, t - timedelta(hours=2), t, 60, aircraft="c130 ", remarks=RAAFV_REMARKS)
    # F18: one session, 30 minutes
    add_flight(conn, 9500023, t - timedelta(hours=1), t, 30, aircraft="F18", remarks=RAAFV_REMARKS)
    # excluded: no RAAFv remarks; ended outside the 30 days
    add_flight(conn, 9500024, t - timedelta(hours=1), t, 500, aircraft="F18")
    old = now - timedelta(days=40)
    add_flight(conn, 9500025, old - timedelta(hours=1), old, 500, aircraft="C130", remarks=RAAFV_REMARKS)
    rows = run_panel(conn, MILITARY, "RAAFv flights by aircraft type", now - timedelta(days=30), now)
    got = [(r["aircraft_type"], r["total_flights"], float(r["total_airborne_minutes"]),
            float(r["total_airborne_hours"]), float(r["avg_airborne_hours_per_flight"])) for r in rows]
    # C130: 2 sessions, 3 flight rows of 60 min -> 180 min = 3.0 h; average over rows = 1.0 h
    assert got == [("C130", 2, 180.0, 3.0, 1.0), ("F18", 1, 30.0, 0.5, 0.5)]


def test_card_143_window_edge_on_flight_end(conn) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(days=30)
    add_flight(conn, 9500031, start - timedelta(hours=2), start, 60, remarks=RAAFV_REMARKS)                # edge: in
    add_flight(conn, 9500032, start - timedelta(hours=2), start - timedelta(seconds=1), 60,
               remarks=RAAFV_REMARKS)                                                                      # out
    rows = run_panel(conn, MILITARY, "RAAFv flights by aircraft type", start, now)
    assert [(r["aircraft_type"], r["total_flights"]) for r in rows] == [("B738", 1)]


# ------------------------------------------------------------------ card 161
def test_card_161_flights_per_type_one_per_session_with_shares(conn) -> None:
    now = datetime.now(UTC)
    t = now - timedelta(days=2)
    add_flight(conn, 9500041, t - timedelta(hours=3), t, aircraft="C130", remarks=RAAFV_REMARKS, segments=3)
    add_flight(conn, 9500042, t - timedelta(hours=3), t, aircraft="C130", remarks=RAAFV_REMARKS)
    add_flight(conn, 9500043, t - timedelta(hours=3), t, aircraft="F18", remarks=RAAFV_REMARKS)
    add_flight(conn, 9500044, t - timedelta(hours=3), t, aircraft="A332", remarks=RAAFV_REMARKS)
    add_flight(conn, 9500045, t - timedelta(hours=3), t, aircraft="F18")                       # not RAAFv
    old = now - timedelta(days=45)
    add_flight(conn, 9500046, old, old + timedelta(hours=1), aircraft="A332", remarks=RAAFV_REMARKS)  # too old
    rows = run_panel(conn, MILITARY, "Most popular RAAFv aircraft", now - timedelta(days=30), now)
    assert [(r["aircraft"], r["Flights"]) for r in rows] == [("C130", 2), ("A332", 1), ("F18", 1)]
    assert [float(r["% of RAAFv flights"]) for r in rows] == [50.0, 25.0, 25.0]


def test_card_161_window_is_logon_time_and_blank_type_is_labelled(conn) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(days=30)
    add_flight(conn, 9500051, start, start + timedelta(hours=1), aircraft="  ", remarks=RAAFV_REMARKS)  # edge: in
    add_flight(conn, 9500052, start - timedelta(seconds=1), start + timedelta(hours=1), aircraft="C130",
               remarks=RAAFV_REMARKS)                                                                    # out
    rows = run_panel(conn, MILITARY, "Most popular RAAFv aircraft", start, now)
    assert [(r["aircraft"], r["Flights"]) for r in rows] == [("(none)", 1)]


# ------------------------------------------------------------------ card 184
def test_card_184_flights_per_utc_week_of_end(conn) -> None:
    now = datetime.now(UTC)
    w3, w2, w1 = monday_utc(3), monday_utc(2), monday_utc(1)
    add_flight(conn, 9500061, w3 + timedelta(hours=1), w3 + timedelta(hours=2), remarks=RAAFV_REMARKS)
    add_flight(conn, 9500062, w2 + timedelta(hours=1), w2 + timedelta(hours=2), remarks=RAAFV_REMARKS)
    # ends Sunday 23:59 of week w2 and Monday 00:00 of week w1: the edge falls into the new week
    add_flight(conn, 9500063, w1 - timedelta(hours=3), w1 - timedelta(minutes=1), remarks=RAAFV_REMARKS)
    add_flight(conn, 9500064, w1 - timedelta(hours=3), w1, remarks=RAAFV_REMARKS)
    add_flight(conn, 9500065, w1 + timedelta(hours=1), w1 + timedelta(hours=2), aircraft="F18")   # not RAAFv
    old = now - timedelta(days=120)
    add_flight(conn, 9500066, old - timedelta(hours=1), old, remarks=RAAFV_REMARKS)               # outside 98 days
    rows = run_panel(conn, MILITARY, "Flights flown by RAAFv aircraft per week", now - timedelta(days=98), now)
    assert {r["time"]: r["RAAFv flights"] for r in rows} == {w3: 1, w2: 2, w1: 1}
    assert [r["time"] for r in rows] == sorted(r["time"] for r in rows)       # oldest first for the bar chart


# ------------------------------------------------------------------ card 156
# The panel reads now() (latest Monday, and no slots after the present). The tests fix the clock, so they run on any day
# and at any hour: "now" is Wednesday 07-10-2026 10:00 UTC, the latest Monday is 05-10-2026.
MON = datetime(2026, 10, 5, tzinfo=UTC)
WED_NOW = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)


def _add_samples(conn, cid: int, samples: list[tuple[datetime, int | None]]) -> int:
    first, last = min(t for t, _ in samples), max(t for t, _ in samples)
    for t, _ in samples:
        partition_for(conn, t)
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING id", (cid, f"TST{cid}", first.replace(microsecond=0), first, last)).fetchone()["id"]
    for t, alt in samples:
        conn.execute("INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES (%s,%s,%s,250)",
                     (t, sid, alt))
    return sid


def test_card_156_airborne_count_per_slot_on_latest_monday(conn) -> None:
    mon = MON

    def h(hours: float) -> datetime:
        return mon + timedelta(minutes=round(hours * 60))

    # A: two samples in the 07:00 slot (counted once), then one at 11:57 (slot 11:55)
    _add_samples(conn, 9500071, [(h(7.0), 5000), (h(7.0) + timedelta(minutes=1), 5000), (h(11.0) + timedelta(minutes=57), 5000)])
    # B: exactly 1000 ft is not above 1000 ft (07:10); 1001 ft counts (07:15); 06:59:45 is before the window
    _add_samples(conn, 9500072, [(h(7.0) - timedelta(seconds=15), 5000), (h(7.0) + timedelta(minutes=10), 1000), (h(7.25), 1001)])
    # C: sample exactly at 12:00 is outside (no slot at 12:00); NULL altitude never counts
    _add_samples(conn, 9500073, [(h(12.0), 5000), (h(7.5), None)])
    # D: a sample 5 minutes before the same weekday a week earlier must not leak in
    _add_samples(conn, 9500074, [(h(7.0) - timedelta(days=7), 5000)])
    rows = run_panel(conn, EVENTS, "Airborne aircraft during the latest Monday", mon, mon + timedelta(days=1), now=WED_NOW)
    got = {r["time"]: r["Airborne aircraft"] for r in rows}
    assert len(rows) == 60
    assert rows[0]["time"] == h(7) and rows[-1]["time"] == h(12) - timedelta(minutes=5)
    assert got[h(7)] == 1                                  # A only
    assert got[h(7.25)] == 1                               # B (1001 ft)
    assert got[h(7.0) + timedelta(minutes=10)] == 0                        # B at exactly 1000 ft does not count
    assert got[h(11.0) + timedelta(minutes=55)] == 1                       # A
    assert sum(got.values()) == 3                          # nothing else anywhere in the window


def test_card_156_slots_after_now_are_not_drawn_while_the_event_is_on(conn) -> None:
    """Mutant P35 (slot <= now() removed). Monday 09:07 UTC: the 07:00 to 09:05 slots (26) exist, later ones do not, and
    a sample later than now is ignored. Before 07:00 nothing is drawn."""
    _add_samples(conn, 9500075, [(MON + timedelta(hours=8), 5000)])
    _add_samples(conn, 9500076, [(MON + timedelta(hours=10), 5000)])         # in the future at 09:07
    title = "Airborne aircraft during the latest Monday"
    now = MON + timedelta(hours=9, minutes=7)
    rows = run_panel(conn, EVENTS, title, MON, MON + timedelta(days=1), now=now)
    assert [r["time"] for r in rows] == [MON + timedelta(hours=7, minutes=5 * n) for n in range(26)]
    assert rows[-1]["time"] == MON + timedelta(hours=9, minutes=5)
    assert {r["time"]: r["Airborne aircraft"] for r in rows}[MON + timedelta(hours=8)] == 1
    assert sum(r["Airborne aircraft"] for r in rows) == 1                    # the 10:00 sample is not counted
    # (before 07:00Z on Monday the panel shows the previous Monday: test_card_156_before_0700z_on_monday_shows_last_monday)


def test_card_156_uses_the_latest_monday_not_the_previous_one(conn) -> None:
    """On Sunday evening the latest Monday is still the one just gone; just after midnight on the next Monday it is the new one."""
    title = "Airborne aircraft during the latest Monday"
    next_mon = MON + timedelta(days=7)
    _add_samples(conn, 9500077, [(MON + timedelta(hours=7), 5000)])
    _add_samples(conn, 9500078, [(next_mon + timedelta(hours=7), 5000)])
    sunday = run_panel(conn, EVENTS, title, MON, next_mon, now=next_mon - timedelta(minutes=1))
    assert sunday[0]["time"] == MON + timedelta(hours=7) and sunday[0]["Airborne aircraft"] == 1 and len(sunday) == 60
    monday = run_panel(conn, EVENTS, title, MON, next_mon, now=next_mon + timedelta(hours=7, minutes=30))
    assert [r["time"] for r in monday][0] == next_mon + timedelta(hours=7) and len(monday) == 7
    assert [r["Airborne aircraft"] for r in monday][0] == 1 and sum(r["Airborne aircraft"] for r in monday) == 1


def test_card_156_has_no_sector_or_frequency_logic() -> None:
    sql = panel(EVENTS, "Airborne aircraft during the latest Monday")["targets"][0]["rawSql"].lower()
    for word in ("sector", "frequency", "transceiver"):
        assert word not in sql


def test_card_156_before_0700z_on_monday_shows_last_monday(conn) -> None:
    """Issue 94: from Monday 00:00 to 06:59 UTC the most recent event that has started is last Monday's, so it is shown in
    full (60 slots); at exactly 07:00:00 the new Monday begins with its first slot."""
    title = "Airborne aircraft during the latest Monday"
    next_mon = MON + timedelta(days=7)
    _add_samples(conn, 9500079, [(MON + timedelta(hours=8), 5000)])
    _add_samples(conn, 9500080, [(next_mon + timedelta(hours=7), 5000)])
    for now in (next_mon, next_mon + timedelta(hours=3), next_mon + timedelta(hours=6, minutes=59, seconds=59)):
        rows = run_panel(conn, EVENTS, title, MON, next_mon + timedelta(days=1), now=now)
        assert len(rows) == 60 and rows[0]["time"] == MON + timedelta(hours=7), now
        assert sum(r["Airborne aircraft"] for r in rows) == 1
    started = run_panel(conn, EVENTS, title, MON, next_mon + timedelta(days=1), now=next_mon + timedelta(hours=7))
    assert [r["time"] for r in started] == [next_mon + timedelta(hours=7)]
    assert started[0]["Airborne aircraft"] == 1
