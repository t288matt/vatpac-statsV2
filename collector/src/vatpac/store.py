"""Database writes. One transaction per snapshot; every statement is idempotent."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb
from shapely.geometry import Polygon

from .filters import inside_polygon
from .model import Snapshot, clamp_pilot, home_icao, parse_frequency_hz

log = logging.getLogger(__name__)

# An older snapshot never overwrites what a newer one stored (issue 41): the COALESCE'd fields only change when
# EXCLUDED.last_seen >= ps.last_seen (a replay of the same snapshot is a no-op). first_seen/max_* stay order-free.
_FIELDS = ("name", "home_icao", "server", "departure", "arrival", "aircraft_icao", "aircraft_faa", "flight_rules",
           "route", "remarks", "planned_alt", "deptime", "pilot_rating", "military_rating")
_NEWER = "EXCLUDED.last_seen >= ps.last_seen"
_NEW = {c: f"CASE WHEN {_NEWER} THEN COALESCE(EXCLUDED.{c}, ps.{c}) ELSE ps.{c} END" for c in _FIELDS}
_SET = ",\n    ".join(f"{c} = {_NEW[c]}" for c in _FIELDS)
_OLD_ROW = ", ".join(f"ps.{c}" for c in ("first_seen", "last_seen", *_FIELDS, "max_gs_kt", "max_alt_ft"))
_NEW_ROW = ", ".join(("LEAST(ps.first_seen, EXCLUDED.first_seen)", "GREATEST(ps.last_seen, EXCLUDED.last_seen)",
                      *(_NEW[c] for c in _FIELDS), "GREATEST(ps.max_gs_kt, EXCLUDED.max_gs_kt)",
                      "GREATEST(ps.max_alt_ft, EXCLUDED.max_alt_ft)"))

_PILOT_SQL = f"""
WITH x AS (
  SELECT * FROM jsonb_to_recordset(%(rows)s) AS r(
    cid bigint, callsign text, logon_time timestamptz, name text, home_icao text, server text,
    departure text, arrival text, aircraft_icao text, aircraft_faa text, flight_rules text, route text,
    remarks text, planned_alt text, deptime text, gs_kt int, alt_ft int, pilot_rating int,
    military_rating int, lat double precision, lon double precision, hdg int)
), up AS (
  INSERT INTO pilot_session AS ps (cid, callsign, logon_time, name, home_icao, server, first_seen, last_seen,
    departure, arrival, aircraft_icao, aircraft_faa, flight_rules, route, remarks, planned_alt, deptime,
    max_gs_kt, max_alt_ft, pilot_rating, military_rating)
  SELECT cid, callsign, logon_time, name, home_icao, server, %(ts)s, %(ts)s,
    departure, arrival, aircraft_icao, aircraft_faa, flight_rules, route, remarks, planned_alt, deptime,
    gs_kt, alt_ft, pilot_rating, military_rating FROM x
  ON CONFLICT (cid, callsign, logon_time) DO UPDATE SET
    first_seen = LEAST(ps.first_seen, EXCLUDED.first_seen),
    last_seen = GREATEST(ps.last_seen, EXCLUDED.last_seen),
    plan_changed_at = CASE
      WHEN {_NEWER} AND ((EXCLUDED.departure IS NOT NULL AND EXCLUDED.departure IS DISTINCT FROM ps.departure)
        OR (EXCLUDED.arrival IS NOT NULL AND EXCLUDED.arrival IS DISTINCT FROM ps.arrival))
      THEN EXCLUDED.last_seen ELSE ps.plan_changed_at END,
    {_SET},
    max_gs_kt = GREATEST(ps.max_gs_kt, EXCLUDED.max_gs_kt),
    max_alt_ft = GREATEST(ps.max_alt_ft, EXCLUDED.max_alt_ft)
  WHERE ({_OLD_ROW}) IS DISTINCT FROM (
      {_NEW_ROW})
  RETURNING id, cid, callsign, logon_time
), ids AS (  -- rows left untouched by the WHERE above are not returned, so read them from the table
  SELECT id, cid, callsign, logon_time FROM up
  UNION ALL
  SELECT s.id, s.cid, s.callsign, s.logon_time FROM pilot_session s JOIN x USING (cid, callsign, logon_time)
  WHERE NOT EXISTS (SELECT 1 FROM up WHERE up.id = s.id)
)
INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg)
SELECT %(ts)s, ids.id, x.lat, x.lon, x.alt_ft, x.gs_kt, x.hdg
FROM ids JOIN x USING (cid, callsign, logon_time)
ON CONFLICT DO NOTHING
"""

_CONTROLLER_SQL = """
WITH x AS (
  SELECT * FROM jsonb_to_recordset(%(rows)s) AS r(
    cid bigint, callsign text, logon_time timestamptz, name text, rating int, facility int,
    server text, frequency_hz bigint, visual_range int)
)
INSERT INTO controller_session AS cs (cid, callsign, logon_time, name, rating, facility, server,
  frequency_hz, visual_range, first_seen, last_seen)
SELECT cid, callsign, logon_time, name, rating, facility, server, frequency_hz, visual_range, %(ts)s, %(ts)s FROM x
ON CONFLICT (cid, callsign, logon_time) DO UPDATE SET
  first_seen = LEAST(cs.first_seen, EXCLUDED.first_seen),
  last_seen = GREATEST(cs.last_seen, EXCLUDED.last_seen),
  name = CASE WHEN EXCLUDED.last_seen >= cs.last_seen THEN COALESCE(EXCLUDED.name, cs.name) ELSE cs.name END,
  rating = CASE WHEN EXCLUDED.last_seen >= cs.last_seen THEN EXCLUDED.rating ELSE cs.rating END,
  facility = CASE WHEN EXCLUDED.last_seen >= cs.last_seen THEN EXCLUDED.facility ELSE cs.facility END,
  server = CASE WHEN EXCLUDED.last_seen >= cs.last_seen THEN COALESCE(EXCLUDED.server, cs.server) ELSE cs.server END,
  frequency_hz = CASE WHEN EXCLUDED.last_seen >= cs.last_seen THEN EXCLUDED.frequency_hz ELSE cs.frequency_hz END,
  visual_range = CASE WHEN EXCLUDED.last_seen >= cs.last_seen THEN EXCLUDED.visual_range ELSE cs.visual_range END
RETURNING cs.id, cs.cid, cs.callsign, cs.logon_time
"""

# Insert an event row only on first sighting or when the value differs from the latest earlier event.
_EVENT_SQL = """
INSERT INTO {table} (session_id, observed_at, {col})
SELECT v.sid, %(ts)s, v.val
FROM unnest(%(ids)s::bigint[], %(vals)s::{typ}[]) AS v(sid, val)
LEFT JOIN LATERAL (
  SELECT e.session_id AS sid, e.{col} AS val FROM {table} e
  WHERE e.session_id = v.sid AND e.observed_at <= %(ts)s ORDER BY e.observed_at DESC LIMIT 1) p ON true
WHERE p.sid IS NULL OR p.val IS DISTINCT FROM v.val
ON CONFLICT DO NOTHING
"""

_NETWORK_SQL = """
INSERT INTO network_snapshot (observed_at, pilots_in_region, controllers_in_region, pilots_global, controllers_global)
VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
"""

_POLL_LOG_SQL = """
INSERT INTO poll_log (status, snapshot_ts, duration_ms, http_status, pilots_seen, pilots_inside,
  pilots_with_plan, pilots_kept, controllers_seen, controllers_kept, rejected_values,
  polygon_sha, whitelist_sha, error)
VALUES (%(status)s, %(snapshot_ts)s, %(duration_ms)s, %(http_status)s, %(pilots_seen)s, %(pilots_inside)s,
  %(pilots_with_plan)s, %(pilots_kept)s, %(controllers_seen)s, %(controllers_kept)s, %(rejected_values)s,
  %(polygon_sha)s, %(whitelist_sha)s, %(error)s)
"""

_REFERENCE_SQL = """
INSERT INTO reference_data_change (file, old_sha, new_sha)
SELECT %(f)s, (SELECT new_sha FROM reference_data_change WHERE file = %(f)s ORDER BY id DESC LIMIT 1), %(sha)s
WHERE (SELECT new_sha FROM reference_data_change WHERE file = %(f)s ORDER BY id DESC LIMIT 1)
      IS DISTINCT FROM %(sha)s
"""


@dataclass(slots=True)
class ApplyResult:
    """Counts for one snapshot (these become the poll_log row)."""

    pilots_seen: int = 0
    pilots_inside: int = 0
    pilots_with_plan: int = 0
    pilots_kept: int = 0
    controllers_seen: int = 0
    controllers_kept: int = 0
    rejected_values: int = 0
    positions_inserted: int = 0
    snapshot_inserted: bool = False


def _jsonb(rows: list[dict[str, Any]]) -> Jsonb:
    return Jsonb(rows, dumps=lambda o: json.dumps(o, default=lambda v: v.isoformat()))


def _guard(cur: psycopg.Cursor[Any], deadline: float | None) -> None:
    """Transaction-scope deadline (a time.monotonic() value): each statement may run only for the time that is left,
    so a slow or blocked apply ends by the deadline instead of after a per-statement timeout for every statement."""
    if deadline is not None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("snapshot apply passed its deadline")
        cur.execute("SELECT set_config('statement_timeout', %s, true)", (str(int(left * 1000) + 1),))


def _write(conn: psycopg.Connection[Any], cur: psycopg.Cursor[Any], sql: str, rows: list[dict[str, Any]],
           ts: datetime, fetch: bool, deadline: float | None = None) -> tuple[int, list[Any], list[str]]:
    """Run a batch statement in a savepoint. If one poisoned record makes it fail (DataError, IntegrityError,
    ProgramLimitExceeded), bisect down to the offender and skip it. Returns (rowcount, fetched rows, rejected
    callsigns)."""
    try:
        with conn.transaction():
            _guard(cur, deadline)
            cur.execute(sql, {"rows": _jsonb(rows), "ts": ts})
            return max(cur.rowcount, 0), cur.fetchall() if fetch else [], []
    except (psycopg.DataError, psycopg.IntegrityError, psycopg.errors.ProgramLimitExceeded) as exc:
        # a missing partition is a check_violation (23514) from tuple routing that names no constraint; a bad record
        # that breaks a real CHECK names it. The message quotes row values, so its text is never inspected.
        if isinstance(exc, psycopg.errors.CheckViolation) and exc.diag.constraint_name is None:
            raise
        if len(rows) == 1:
            log.warning("record rejected", extra={"callsign": rows[0]["callsign"], "error": type(exc).__name__})
            return 0, [], [rows[0]["callsign"]]
        mid = len(rows) // 2
        (n1, f1, b1), (n2, f2, b2) = (
            _write(conn, cur, sql, half, ts, fetch, deadline) for half in (rows[:mid], rows[mid:]))
        return n1 + n2, f1 + f2, b1 + b2


def apply_snapshot(
    conn: psycopg.Connection[Any],
    snapshot: Snapshot,
    polygon: Polygon,
    whitelist: frozenset[str],
    *,
    polygon_sha: str,
    whitelist_sha: str,
    deadline: float | None = None,
) -> ApplyResult:
    """Write one snapshot in a single transaction. Replaying it inserts zero rows."""
    ts = snapshot.observed_at
    res = ApplyResult(pilots_seen=snapshot.pilots_global, controllers_seen=snapshot.controllers_global,
                      rejected_values=snapshot.malformed)  # malformed feed entries count as rejected too

    pilots: dict[tuple[int, str, datetime], dict[str, Any]] = {}
    for p in snapshot.pilots:
        if not inside_polygon(polygon, p.latitude, p.longitude):
            continue
        gs, alt, rejected = clamp_pilot(p.groundspeed, p.altitude)
        res.rejected_values += rejected + p.rejected
        pilots[(p.cid, p.callsign, p.logon_time)] = {
            **asdict(p), "home_icao": home_icao(p.name), "gs_kt": gs, "alt_ft": alt,
            "lat": p.latitude, "lon": p.longitude, "hdg": p.heading,
        }
        if (p.departure or "").strip() and (p.arrival or "").strip():
            res.pilots_with_plan += 1
    res.pilots_inside = len(pilots)

    controllers: dict[tuple[int, str, datetime], dict[str, Any]] = {}
    for c in snapshot.controllers:
        if c.callsign in whitelist:
            res.rejected_values += c.rejected
            controllers[(c.cid, c.callsign, c.logon_time)] = {
                **asdict(c), "frequency_hz": parse_frequency_hz(c.frequency_text)}
    res.controllers_kept = len(controllers)

    with conn.transaction(), conn.cursor(row_factory=tuple_row) as cur:  # independent of the connection's row factory
        res.pilots_kept = len(pilots)
        if pilots:
            res.positions_inserted, _, bad = _write(conn, cur, _PILOT_SQL, list(pilots.values()), ts, False, deadline)
            res.pilots_kept -= len(bad)
            res.rejected_values += len(bad)
        if controllers:
            _, fetched, bad = _write(conn, cur, _CONTROLLER_SQL, list(controllers.values()), ts, True, deadline)
            res.controllers_kept -= len(bad)
            res.rejected_values += len(bad)
            ids = {(r[1], r[2], r[3]): r[0] for r in fetched}
            keys = [k for k in controllers if k in ids]
            for table, col, typ, field_name in (
                ("controller_rating_event", "rating", "int", "rating"),
                ("controller_frequency_event", "frequency_hz", "bigint", "frequency_hz"),
            ):
                _guard(cur, deadline)
                cur.execute(
                    _EVENT_SQL.format(table=table, col=col, typ=typ),
                    {"ts": ts, "ids": [ids[k] for k in keys], "vals": [controllers[k][field_name] for k in keys]},
                )
        _guard(cur, deadline)
        cur.execute(
            _NETWORK_SQL,
            (ts, res.pilots_kept, res.controllers_kept, snapshot.pilots_global, snapshot.controllers_global),
        )
        res.snapshot_inserted = cur.rowcount == 1
    return res


def write_poll_log(
    conn: psycopg.Connection[Any],
    status: str,
    *,
    snapshot_ts: datetime | None = None,
    duration_ms: int | None = None,
    http_status: int | None = None,
    result: ApplyResult | None = None,
    polygon_sha: str | None = None,
    whitelist_sha: str | None = None,
    error: str | None = None,
) -> None:
    """One poll_log row per attempt (ok, skipped_unchanged, error)."""
    r = result or ApplyResult()
    counts = {k: getattr(r, k) if result else None for k in (
        "pilots_seen", "pilots_inside", "pilots_with_plan", "pilots_kept",
        "controllers_seen", "controllers_kept", "rejected_values")}
    params = {
        "status": status, "snapshot_ts": snapshot_ts, "duration_ms": duration_ms,
        "http_status": http_status, "polygon_sha": polygon_sha, "whitelist_sha": whitelist_sha,
        "error": error[:2000] if error else None, **counts,
    }
    conn.execute(_POLL_LOG_SQL, params)


def record_reference_change(conn: psycopg.Connection[Any], file: str, new_sha: str) -> bool:
    """Insert a reference_data_change row if the file hash differs from the last recorded one."""
    cur = conn.execute(_REFERENCE_SQL, {"f": file, "sha": new_sha})
    return cur.rowcount == 1
