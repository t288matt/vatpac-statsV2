"""Groups D and E of the database-questions catalogue (issue #267, 04-11-2026): two fixed-panel dashboards (group F, Live now and events, was deleted in issue 323).

  atc/atc-positions.json  "ATC: positions and controllers"  (D1 to D9, short address /atc-positions)
  atc/atc-ranks.json      "ATC: hours by rank"               (E1 to E10, short address /atc-ranks)

New queries under test: D2 to D5 (D6 was removed on 08-11-2026, issue 320) and E2 to E10. Panels copied from existing dashboards (D1, D7 to D9, E1)
are checked to be identical to their source panel; their rules are asserted by the tests of the original cards.

Expected numbers are worked out by hand from synthetic sessions and flights, never by the SQL under test. Hours panels are
position-hours clipped to the last 30 days: the boundary tests start a session before the window and would fail with the clipping removed.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import REPO

pytestmark = pytest.mark.integration

DASH = REPO / "grafana" / "dashboards"
POSITIONS = DASH / "atc" / "atc-positions.json"
RANKS = DASH / "atc" / "atc-ranks.json"
SIX = ["S1", "S2", "S3", "C1", "C3", "I1+"]
S1, S2, S3, C1, C3, I1, SUP, OBS, C2 = 2, 3, 4, 5, 7, 8, 11, 1, 6   # VATSIM rating numbers


def panels(path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["panels"]


def panel(path, title: str) -> dict:
    return next(p for p in panels(path) if p.get("title") == title)


def run(conn, path, title: str) -> list[dict]:
    return conn.execute(panel(path, title)["targets"][0]["rawSql"]).fetchall()


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def add(conn, cid: int, callsign: str, start: datetime, minutes: int, rating: int | None = C1, name: str | None = None) -> None:
    start = start.replace(microsecond=0)
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, name, rating, facility, first_seen, last_seen) "
        "VALUES (%s, %s, %s, %s, %s, NULL, %s, %s)",
        (cid, callsign, start, name or f"Controller {cid}", rating, start, start + timedelta(minutes=minutes)),
    )


# ------------------------------------------------------------------ the dashboards themselves
@pytest.mark.parametrize("path,uid,title,questions", [
    (POSITIONS, "vatpac-atc-positions", "ATC: positions and controllers", [f"D{i}" for i in range(1, 10) if i != 6]),
    (RANKS, "vatpac-atc-ranks", "ATC: hours by rank", [f"E{i}" for i in range(1, 11)]),
])
def test_dashboard_has_every_catalogue_question_and_no_inputs(path, uid, title, questions):
    d = json.loads(path.read_text(encoding="utf-8"))
    assert d["uid"] == uid and d["title"] == title
    assert [v["name"] for v in d["templating"]["list"]] == ["go_live"]       # no inputs: only the hidden constant
    described = [p for p in d["panels"] if p.get("description")]
    assert sorted({p["description"].split()[1] for p in described}, key=lambda q: int(q[1:])) == questions
    assert all("Definition" in p["description"] and "UTC" in p["description"] for p in described)
    ids = [p["id"] for p in d["panels"]]
    assert len(ids) == len(set(ids))


def test_every_rank_panel_says_it_uses_the_current_rank_and_lists_the_ranks_included_and_left_out():
    for p in panels(RANKS):
        if not p.get("description"):
            continue
        d = p["description"]
        if d.startswith("Question E1 "):
            assert "RANK AT THE TIME" in d and "supervisors" in d             # the two copies keep the old rule and say so
        else:
            assert "CURRENT RANK" in d and "will not match" in d
            assert all(r in d for r in SIX) and "Left out: observers" in d and "C2" in d and "unknown" in d


def test_new_hours_panels_state_their_window_and_size_in_the_title():
    for path in (POSITIONS, RANKS):
        for p in panels(path):
            if p.get("description", "").split(" ")[2:3] == ["(new)."] and not p["title"].startswith(("Ranks that staffed", "Whitelisted")):
                assert "(last 30 days)" in p["title"] or "(current rank, last 30 days)" in p["title"], p["title"]


COPIES = [  # (dashboard, title, source file)
    (POSITIONS, "Top 30 service providers (last 30 days)", "atc/atc-overview.json"),
    (POSITIONS, "CTR and FSS controllers active (30 days to month end, monthly)", "atc/atc-overview.json"),
    (POSITIONS, "Controllers active (30 days to week end, weekly)", "atc/atc-overview.json"),
    (POSITIONS, "Controllers with the longest median session (last 90 days)", "atc/atc-overview.json"),
    (POSITIONS, "Controllers with the shortest median session (last 90 days)", "atc/atc-overview.json"),
    (RANKS, "Controllers logged on (last 30 days)", "atc/atc-overview.json"),
    (RANKS, "Controllers logged on each week, by rating group", "atc/atc-overview.json"),
]


RESTYLED: set[tuple[str, str]] = set()   # issue 315: the only entries were the two hour charts copied from Pilots: online now; keep the mechanism for the next restyle


@pytest.mark.parametrize("path,title,src", COPIES)
def test_copied_panels_run_the_same_sql_and_window_as_their_source(path, title, src):
    mine, theirs = panel(path, title), panel(DASH / src, title)
    assert mine["targets"][0]["rawSql"] == theirs["targets"][0]["rawSql"]
    assert mine["targets"][0]["format"] == theirs["targets"][0]["format"]
    assert mine.get("timeFrom") == theirs.get("timeFrom")
    if (src, title) in RESTYLED:
        return          # issue 280: restyled on pilots-now (chart type, options, fieldConfig); query, format and window must stay the same
    assert mine["type"] == theirs["type"]
    assert mine.get("options") == theirs.get("options") and mine.get("fieldConfig") == theirs.get("fieldConfig")


def test_every_restyled_exemption_still_differs_from_its_copy():
    """Issue 280: an entry in RESTYLED is only allowed while a copy really differs from its source; remove it once they match again."""
    for src, title in sorted(RESTYLED):
        differs = [path for path, t, s in COPIES if (s, t) == (src, title)
                   and any(panel(path, title).get(k) != panel(DASH / src, title).get(k) for k in ("type", "options", "fieldConfig"))]
        assert differs, f"stale RESTYLED entry: {src}: {title}"

# ------------------------------------------------------------------ D2: positions by hours
def test_d2_ranks_positions_by_hours_clipped_to_thirty_days(conn):
    n = now()
    add(conn, 1, "T_A_TWR", n - timedelta(days=2), 120)                       # 2.0 h
    add(conn, 2, "T_A_TWR", n - timedelta(days=3), 60)                        # +1.0 h: the same position, another controller = 3.0
    add(conn, 3, "T_B_APP", n - timedelta(days=5), 150)                       # 2.5 h
    add(conn, 4, "T_C_GND", n - timedelta(days=30, hours=1), 180)             # starts 1 h before the window: only 2.0 h inside
    add(conn, 5, "T_D_DEL", n - timedelta(days=31), 60)                       # wholly outside
    rows = run(conn, POSITIONS, "Top 15 positions by hours staffed (last 30 days)")
    assert [(r["Position"], float(r["Hours staffed"])) for r in rows] == [("T_A_TWR", 3.0), ("T_B_APP", 2.5), ("T_C_GND", 2.0)]


def test_d2_shows_fifteen_rows_at_most_and_breaks_ties_by_callsign(conn):
    n = now()
    for i in range(17):
        add(conn, 100 + i, f"T{i:02d}_TWR", n - timedelta(days=1), 60)       # seventeen positions, one hour each
    rows = run(conn, POSITIONS, "Top 15 positions by hours staffed (last 30 days)")
    assert [r["Position"] for r in rows] == [f"T{i:02d}_TWR" for i in range(15)]


def test_d2_a_reconnect_within_five_minutes_is_one_session_and_a_short_session_counts(conn):
    n = now()
    add(conn, 1, "T_A_GND", n - timedelta(days=1), 30)
    add(conn, 1, "T_A_GND", n - timedelta(days=1) + timedelta(minutes=32), 28)   # 2 minutes later: merged, 60 minutes in all
    add(conn, 2, "T_B_DEL", n - timedelta(days=1), 6)                            # six minutes: no minimum length, 0.1 h
    rows = {r["Position"]: float(r["Hours staffed"]) for r in run(conn, POSITIONS, "Top 15 positions by hours staffed (last 30 days)")}
    assert rows == {"T_A_GND": 1.0, "T_B_DEL": 0.1}


# ------------------------------------------------------------------ D3 and D4: sessions and different controllers
def test_d3_counts_merged_sessions_started_in_thirty_days(conn):
    n = now()
    for i in range(3):                                                           # three separate sessions (hours apart)
        add(conn, 1, "T_X_TWR", n - timedelta(days=2, hours=3 * i), 30)
    add(conn, 2, "T_Y_APP", n - timedelta(days=3), 30)
    add(conn, 3, "T_Y_APP", n - timedelta(days=4), 30)
    add(conn, 4, "T_W_DEL", n - timedelta(days=1), 10)
    add(conn, 4, "T_W_DEL", n - timedelta(days=1) + timedelta(minutes=12), 10)   # 2 minutes later: the same session
    add(conn, 5, "T_V_GND", n - timedelta(days=29, hours=22), 30)                # started inside the window
    add(conn, 6, "T_U_GND", n - timedelta(days=30, hours=2), 600)                # started before the window: not a session of the period
    rows = run(conn, POSITIONS, "Top 15 positions by sessions (last 30 days)")
    assert [(r["Position"], r["Sessions"]) for r in rows] == [("T_X_TWR", 3), ("T_Y_APP", 2), ("T_V_GND", 1), ("T_W_DEL", 1)]


def test_d4_counts_different_controllers_not_sessions(conn):
    n = now()
    for cid in (1, 2, 3):
        add(conn, cid, "T_P_TWR", n - timedelta(days=2), 30)
    add(conn, 1, "T_P_TWR", n - timedelta(days=1), 30)                           # a fourth session by a controller already counted
    add(conn, 1, "T_Q_APP", n - timedelta(days=2), 30)
    add(conn, 1, "T_Q_APP", n - timedelta(days=1), 30)                           # one controller, two sessions
    add(conn, 4, "T_R_GND", n - timedelta(days=2), 30)
    add(conn, 5, "T_R_GND", n - timedelta(days=40), 30)                          # outside the window
    rows = run(conn, POSITIONS, "Top 15 positions by different controllers (last 30 days)")
    assert [(r["Position"], r["Different controllers"]) for r in rows] == [("T_P_TWR", 3), ("T_Q_APP", 1), ("T_R_GND", 1)]


# ------------------------------------------------------------------ D5: airports by position-hours
def test_d5_sums_position_hours_per_airport_prefix_and_leaves_out_centre_fss_and_fmp(conn):
    n = now()
    t = n - timedelta(days=2)
    add(conn, 1, "SY_TWR", t, 120)                                               # SY: 2.0 + 1.0 (the same time, two positions = 3.0) + 0.5
    add(conn, 2, "SY-C_DEP", t, 60)
    add(conn, 3, "SY_APP", t + timedelta(hours=3), 30)
    add(conn, 4, "ML_TWR", t, 180)                                               # ML: 3.0 from the tower only
    add(conn, 5, "ML-ARL_CTR", t, 300)                                           # centre, FSS and FMP are not airport positions
    add(conn, 6, "ML-IND_FSS", t, 300)
    add(conn, 7, "ML_FMP", t, 240)
    add(conn, 8, "AD-W_APP", t, 60)                                              # AD: 1.0 + 1.0 inside the window of a session that starts 1 h before it
    add(conn, 9, "AD_GND", n - timedelta(days=30, hours=1), 120)
    rows = run(conn, POSITIONS, "Top 15 airports by ATC hours staffed (last 30 days)")
    assert [(r["Airport (callsign prefix)"], float(r["Position-hours"])) for r in rows] == [("SY", 3.5), ("ML", 3.0), ("AD", 2.0)]


# ------------------------------------------------------------------ E: ranks (current rank, each controller once)
def rank_rows(conn, title: str) -> dict[str, dict]:
    return {r["Rank (current)"]: r for r in run(conn, RANKS, title)}


def stage_ranks(conn) -> None:
    """Controllers at every rank, with the cases that matter: a rank change, a missing rating, a boundary session, excluded ranks."""
    n = now()
    add(conn, 100, "T1_TWR", n - timedelta(days=20), 60, rating=S1)              # 100 was S1 then; now C1 (below): all 3.0 h belong to C1
    add(conn, 100, "T2_TWR", n - timedelta(days=2), 120, rating=C1)
    add(conn, 101, "T3_APP", n - timedelta(days=1), 90, rating=S2)               # S2: 1.5 h
    add(conn, 102, "T4_GND", n - timedelta(days=1), 60, rating=OBS)              # observer: only in "Other"
    add(conn, 103, "T5_TWR", n - timedelta(days=1), 60, rating=I1)               # I1+: I1 1.0 h
    add(conn, 104, "T6_TWR", n - timedelta(days=1), 30, rating=SUP)              # and a supervisor 0.5 h
    add(conn, 105, "T7_TWR", n - timedelta(days=10), 60, rating=S3)              # S3: the rating was seen at 4, then the feed gave none
    add(conn, 105, "T8_TWR", n - timedelta(days=1), 30, rating=None)             # latest non-empty rating counts: still S3, 1.5 h
    add(conn, 106, "T9_TWR", n - timedelta(days=30, hours=1), 180, rating=S1)    # starts 1 h before the window: 2.0 h inside, S1


def test_e2_counts_each_controller_once_under_the_current_rank(conn):
    stage_ranks(conn)
    rows = rank_rows(conn, "Hours staffed by current rank (last 30 days)")
    assert list(rows) == SIX
    got = {k: (r["Controllers"], float(r["Hours staffed"])) for k, r in rows.items()}
    assert got == {"S1": (1, 2.0), "S2": (1, 1.5), "S3": (1, 1.5), "C1": (1, 3.0), "C3": (0, 0.0), "I1+": (2, 1.5)}
    # controller 100 held S1 and C1 sessions: counted once, with all 3.0 h under C1 and none under S1
    assert sum(r["Controllers"] for r in rows.values()) == 6


def test_e2_a_rank_with_no_controller_shows_zero_and_unknown_ranks_are_left_out(conn):
    n = now()
    add(conn, 1, "T_A_TWR", n - timedelta(days=1), 60, rating=C2)                # C2 (not in use): left out
    add(conn, 2, "T_B_TWR", n - timedelta(days=1), 60, rating=None)              # no rating at all: left out
    rows = rank_rows(conn, "Hours staffed by current rank (last 30 days)")
    assert all(r["Controllers"] == 0 and float(r["Hours staffed"]) == 0.0 for r in rows.values()) and list(rows) == SIX


def test_e3_shares_of_all_hours_add_to_one_hundred_with_an_other_row(conn):
    stage_ranks(conn)
    rows = rank_rows(conn, "Share of ATC hours by current rank (last 30 days)")
    assert list(rows) == SIX + ["Other"]
    # 2.0 + 1.5 + 1.5 + 3.0 + 0 + 1.5 + 1.0 (the observer) = 10.5 hours in all
    assert {k: float(r["Hours staffed"]) for k, r in rows.items()} == {"S1": 2.0, "S2": 1.5, "S3": 1.5, "C1": 3.0, "C3": 0.0, "I1+": 1.5, "Other": 1.0}
    assert {k: float(r["Share of ATC hours (%)"]) for k, r in rows.items()} == {"S1": 19.0, "S2": 14.3, "S3": 14.3, "C1": 28.6, "C3": 0.0, "I1+": 14.3, "Other": 9.5}


def test_e3_with_no_sessions_has_no_share(conn):
    rows = rank_rows(conn, "Share of ATC hours by current rank (last 30 days)")
    assert all(r["Share of ATC hours (%)"] is None for r in rows.values())


def test_e4_counts_sessions_started_in_the_window_under_the_current_rank(conn):
    stage_ranks(conn)
    rows = rank_rows(conn, "Sessions by current rank (last 30 days)")
    # 106's session started before the window, so S1 has none; 100's two sessions are both C1's; 105's two are S3's
    assert {k: r["Sessions"] for k, r in rows.items()} == {"S1": 0, "S2": 1, "S3": 2, "C1": 2, "C3": 0, "I1+": 2}


def test_e5_averages_the_full_length_of_sessions_by_current_rank(conn):
    stage_ranks(conn)
    rows = rank_rows(conn, "Average session length by current rank (last 30 days)")
    assert (rows["C1"]["Sessions"], float(rows["C1"]["Average minutes"]), float(rows["C1"]["Average hours"])) == (2, 90.0, 1.5)    # 60 and 120 minutes
    assert (float(rows["S3"]["Average minutes"]), float(rows["S3"]["Average hours"])) == (45.0, 0.75)                               # 60 and 30
    assert float(rows["I1+"]["Average minutes"]) == 45.0 and float(rows["S2"]["Average minutes"]) == 90.0
    assert rows["S1"]["Sessions"] == 0 and rows["S1"]["Average minutes"] is None and rows["C3"]["Average hours"] is None


def test_e6_top_two_positions_per_rank_use_the_current_rank(conn):
    n = now()
    for i in range(1, 8):                                                        # controller 110, C1: seven positions of 1 to 7 hours
        add(conn, 110, f"P{i}_TWR", n - timedelta(days=3), 60 * i, rating=C1)
    add(conn, 111, "OLD_GND", n - timedelta(days=9), 480, rating=S1)             # 8 h flown as S1, but 111 is C1 now
    add(conn, 111, "NEW_TWR", n - timedelta(days=1), 30, rating=C1)
    add(conn, 112, "OTHER_APP", n - timedelta(days=1), 60, rating=S2)
    rows = run(conn, RANKS, "Top 2 positions for each current rank (last 30 days)")
    assert [(r["Rank (current)"], r["#"], r["Position"], float(r["Hours staffed"])) for r in rows] == [
        ("S2", 1, "OTHER_APP", 1.0),
        ("C1", 1, "OLD_GND", 8.0), ("C1", 2, "P7_TWR", 7.0)]            # issue 321: two per rank; P6_TWR (6.0) is third and is cut


def test_e7_top_two_controllers_per_rank_break_ties_by_cid(conn):
    n = now()
    for cid, hours in zip(range(200, 207), [5, 4, 4, 3, 2, 1, 1], strict=True):  # seven C3 controllers
        add(conn, cid, f"T{cid}_CTR", n - timedelta(days=2), 60 * hours, rating=C3)
    add(conn, 300, "TX_TWR", n - timedelta(days=2), 600, rating=S1)              # an S1 controller with 10 h: top of S1 only
    add(conn, 201, "TY_TWR", n - timedelta(days=15), 60, rating=S3)              # 201 flew a session as S3: it adds to 201's C3 hours (5.0), no S3 row
    rows = run(conn, RANKS, "Top 2 controllers for each current rank (last 30 days)")
    assert [(r["Rank (current)"], r["#"], r["Controller"], float(r["Hours staffed"])) for r in rows] == [
        ("S1", 1, "Controller 300", 10.0),
        ("C3", 1, "Controller 200", 5.0), ("C3", 2, "Controller 201", 5.0)]  # issue 321: two per rank; 202 (4.0 h) is third and is cut


def test_e8_centre_and_fss_hours_by_rank_with_shares_of_all(conn):
    n = now()
    t = n - timedelta(days=2)
    add(conn, 1, "T_A_CTR", t, 120, rating=C1)                                   # C1: CTR 2.0 + FSS 1.0
    add(conn, 1, "T_A_FSS", t, 60, rating=C1)
    add(conn, 2, "T_B_CTR", t, 60, rating=S3)                                    # S3: CTR 1.0
    add(conn, 3, "T_C_CTR", t, 60, rating=OBS)                                   # observer: in the total only
    add(conn, 4, "T_D_TWR", t, 180, rating=I1)                                   # a tower: not centre or FSS
    add(conn, 5, "T_E_FMP", t, 180, rating=C3)                                   # flow management: not centre
    rows = run(conn, RANKS, "Centre and FSS hours by current rank (last 30 days)")
    got = [(r["Rank (current)"], float(r["CTR hours"]), float(r["FSS hours"]), float(r["CTR + FSS hours"]), r["Share of CTR + FSS hours (%)"] and float(r["Share of CTR + FSS hours (%)"]))
           for r in rows]
    # 5.0 centre and FSS hours in all (3.0 + 1.0 + the observer's 1.0)
    assert got == [("C1", 2.0, 1.0, 3.0, 60.0), ("S3", 1.0, 0.0, 1.0, 20.0), ("S1", 0.0, 0.0, 0.0, 0.0), ("S2", 0.0, 0.0, 0.0, 0.0),
                   ("C3", 0.0, 0.0, 0.0, 0.0), ("I1+", 0.0, 0.0, 0.0, 0.0)]


def last_monday(n: datetime) -> datetime:
    """00:00 UTC on the most recent Monday whose 0700Z start has passed (an independent version of the panel's rule)."""
    m = n - timedelta(hours=7)
    return datetime(m.year, m.month, m.day, tzinfo=UTC) - timedelta(days=m.weekday())


def test_e10_counts_ranks_present_in_the_latest_monday_window_only(conn):
    n = now()
    mon = last_monday(n)
    add(conn, 1, "T_A_CTR", mon + timedelta(hours=6), 120, rating=C1)             # 06:00 to 08:00: 1.0 h inside the window
    add(conn, 2, "T_B_TWR", mon + timedelta(hours=11, minutes=30), 90, rating=S2)  # 11:30 to 13:00: 0.5 h inside
    add(conn, 3, "T_C_TWR", mon + timedelta(hours=5), 119, rating=S1)             # ends 06:59: outside
    add(conn, 4, "T_D_TWR", mon - timedelta(days=7) + timedelta(hours=8), 120, rating=S3)   # the Monday before: outside
    add(conn, 5, "T_E_TWR", mon + timedelta(hours=8), 60, rating=OBS)             # an observer: not one of the six ranks
    add(conn, 1, "T_F_TWR", mon + timedelta(hours=8), 60, rating=C1)              # the same controller on a second position: one controller, 2.0 h
    rows = rank_rows(conn, "Ranks that staffed the latest Monday event (0700-1200Z, current rank)")
    assert list(rows) == SIX
    assert {k: (r["Controllers present"], float(r["Hours staffed in the event"])) for k, r in rows.items()} == {
        "S1": (0, 0.0), "S2": (1, 0.5), "S3": (0, 0.0), "C1": (1, 2.0), "C3": (0, 0.0), "I1+": (0, 0.0)}
    assert {r["Event date (UTC)"] for r in rows.values()} == {mon.strftime("%d-%m-%Y")}


# ------------------------------------------------------------------ the latest-Monday rule at fixed clock times
E10 = "Ranks that staffed the latest Monday event (0700-1200Z, current rank)"


@pytest.mark.parametrize("offset,event_offset_days", [
    (timedelta(minutes=-1), -7),                    # Sunday 23:59Z: the Monday before is the latest event
    (timedelta(hours=6, minutes=59), -7),           # Monday 06:59Z: the event has not started, still last Monday's
    (timedelta(hours=7), 0),                        # Monday 07:00Z: the new event
    (timedelta(hours=12), 0),                       # Monday 12:00Z: still that event
])
def test_latest_monday_rule_with_a_fixed_clock(conn, offset, event_offset_days):
    """now() in each panel is replaced by a fixed instant, so reverting the '- interval 7 hours' rule fails without waiting for a Monday."""
    today = datetime.now(UTC)
    monday = datetime(today.year, today.month, today.day, tzinfo=UTC) - timedelta(days=today.weekday() + 14)   # a Monday two weeks back
    clock = monday + offset
    event = monday + timedelta(days=event_offset_days)
    conn.execute("INSERT INTO network_snapshot VALUES (%s,1,1,1,1)", (monday - timedelta(days=35),))   # go-live, so monday_attendance lists these Mondays

    def at_clock(path, title):
        sql = panel(path, title)["targets"][0]["rawSql"]
        assert "now()" in sql
        return conn.execute(sql.replace("now()", f"TIMESTAMPTZ '{clock.isoformat()}'")).fetchall()

    want = event.strftime("%d-%m-%Y")
    assert {r["Event date (UTC)"] for r in at_clock(RANKS, E10)} == {want}
