"""Issue #109: online_now looks the newest position up with LEFT JOIN LATERAL ... LIMIT 1 (a forced nested loop)
instead of a plain LEFT JOIN. The result must be identical, and the plan must never put a hash or merge join over
pilot_position (that read every partition: about 25 s at 240 partitions)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from helpers import partition_for, set_poll_interval

pytestmark = pytest.mark.integration

BASE = 9700000

OLD_ONLINE_NOW = """
CREATE TEMP VIEW old_online_now AS
WITH ref AS (
    SELECT max(observed_at) AS as_of FROM network_snapshot HAVING max(observed_at) > now() - interval '5 minutes'
)
SELECT 'pilot'::text AS kind, ps.id AS session_id, ps.cid, ps.callsign, ps.name, ps.logon_time,
       ps.first_seen, ps.last_seen, ref.as_of, ps.departure, ps.arrival, ps.aircraft_icao,
       pos.lat, pos.lon, pos.alt_ft, pos.gs_kt,
       NULL::int AS rating, NULL::int AS facility, NULL::bigint AS frequency_hz
FROM ref
JOIN pilot_session ps ON ps.last_seen > ref.as_of - make_interval(secs => 2 * (SELECT value::numeric FROM app_setting WHERE key = 'poll_interval_seconds'))
LEFT JOIN pilot_position pos ON pos.session_id = ps.id AND pos.observed_at = ps.last_seen
UNION ALL
SELECT 'controller'::text, cs.id, cs.cid, cs.callsign, cs.name, cs.logon_time, cs.first_seen, cs.last_seen, ref.as_of,
       NULL, NULL, NULL, NULL, NULL, NULL, NULL, cs.rating, cs.facility, cs.frequency_hz
FROM ref
JOIN controller_session cs ON cs.last_seen > ref.as_of - make_interval(secs => 2 * (SELECT value::numeric FROM app_setting WHERE key = 'poll_interval_seconds'))
"""


def _pilot(conn, cid: int, last_seen: datetime, positions: list[datetime]) -> None:
    """A session with the given last_seen and positions at the given times (possibly none, possibly not at last_seen)."""
    for t in positions + [last_seen]:
        partition_for(conn, t)
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (cid, f"L{cid}", (last_seen - timedelta(hours=1)).replace(microsecond=0), last_seen - timedelta(hours=1), last_seen),
    ).fetchone()["id"]
    for t in positions:
        conn.execute("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) "
                     "VALUES (%s,%s,-33.9,151.2,5000,250,90)", (t, sid))


def _build_cases(conn) -> None:
    set_poll_interval(conn, 30)
    t = datetime.now(timezone.utc) - timedelta(seconds=5)
    conn.execute("INSERT INTO network_snapshot VALUES (%s,1,1,1,1)", (t,))
    w = 60  # 2 x the 30 s poll interval
    _pilot(conn, BASE + 1, t, [t - timedelta(seconds=30), t])                              # online, position at last_seen
    _pilot(conn, BASE + 2, t - timedelta(seconds=10), [])                                  # online, no position at all
    _pilot(conn, BASE + 3, t - timedelta(seconds=10), [t - timedelta(seconds=40)])         # online, only an older position
    _pilot(conn, BASE + 4, t - timedelta(seconds=w - 1), [t - timedelta(seconds=w - 1)])   # just inside the window
    _pilot(conn, BASE + 5, t - timedelta(seconds=w), [t - timedelta(seconds=w)])           # exactly at the edge: excluded
    _pilot(conn, BASE + 6, t - timedelta(seconds=w + 1), [t - timedelta(seconds=w + 1)])   # just outside
    _pilot(conn, BASE + 7, t - timedelta(minutes=10), [t - timedelta(minutes=10)])         # offline
    conn.execute(
        "INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen) VALUES (%s,'ML_TWR',%s,%s,%s)",
        (BASE + 8, (t - timedelta(hours=1)).replace(microsecond=0), t - timedelta(hours=1), t - timedelta(seconds=5)))


def test_online_now_is_identical_to_the_plain_left_join(conn) -> None:
    _build_cases(conn)
    conn.execute(OLD_ONLINE_NOW)
    new_rows = conn.execute("SELECT * FROM online_now WHERE cid BETWEEN %s AND %s", (BASE, BASE + 9)).fetchall()
    assert {r["cid"] for r in new_rows} == {BASE + 1, BASE + 2, BASE + 3, BASE + 4, BASE + 8}
    for a, b in (("online_now", "old_online_now"), ("old_online_now", "online_now")):
        extra = conn.execute(f"SELECT * FROM {a} EXCEPT ALL SELECT * FROM {b}").fetchall()
        assert extra == [], f"{a} has rows that {b} does not: {extra}"
    assert conn.execute("SELECT count(*) AS n FROM online_now").fetchone()["n"] == \
        conn.execute("SELECT count(*) AS n FROM old_online_now").fetchone()["n"]
    by_cid = {r["cid"]: r for r in new_rows}
    assert by_cid[BASE + 1]["lat"] is not None  # position found at last_seen
    assert by_cid[BASE + 2]["lat"] is None      # no position: still one row, NULL position
    assert by_cid[BASE + 3]["lat"] is None      # only an older position: not used


def test_view_columns_are_unchanged(conn) -> None:
    cols = [r["attname"] for r in conn.execute(
        "SELECT attname FROM pg_attribute WHERE attrelid = 'online_now'::regclass AND attnum > 0 AND NOT attisdropped "
        "ORDER BY attnum").fetchall()]
    assert cols == ["kind", "session_id", "cid", "callsign", "name", "logon_time", "first_seen", "last_seen", "as_of",
                    "departure", "arrival", "aircraft_icao", "lat", "lon", "alt_ft", "gs_kt", "rating", "facility",
                    "frequency_hz"]


def _nodes(plan: dict, ancestors: tuple[str, ...] = ()):
    yield plan, ancestors
    for child in plan.get("Plans", []):
        yield from _nodes(child, ancestors + (plan["Node Type"],))


def test_plan_never_hash_or_merge_joins_over_pilot_position(conn) -> None:
    _build_cases(conn)
    conn.execute("SET enable_nestloop = off")  # the shape must be forced by the query, not by the planner's mood
    raw = conn.execute("EXPLAIN (FORMAT JSON) SELECT * FROM online_now").fetchone()["QUERY PLAN"]
    plan = (raw if isinstance(raw, list) else json.loads(raw))[0]["Plan"]
    bad = [(n.get("Relation Name"), anc) for n, anc in _nodes(plan)
           if str(n.get("Relation Name", "")).startswith("pilot_position")
           and any(a in ("Hash Join", "Merge Join", "Hash") for a in anc)]
    assert bad == [], f"pilot_position is read under a hash or merge join again: {bad}"


def test_pilot_position_primary_key_is_session_and_time(conn) -> None:
    """LIMIT 1 in online_now is only exact while (session_id, observed_at) is unique."""
    cols = [r["attname"] for r in conn.execute(
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey) "
        "WHERE i.indrelid = 'pilot_position'::regclass AND i.indisprimary "
        "ORDER BY array_position(i.indkey::int2[], a.attnum)").fetchall()]
    assert cols == ["session_id", "observed_at"]
