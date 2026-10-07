"""Database writes. One transaction per snapshot; every statement is idempotent."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb
from shapely.geometry import Polygon

from .filters import inside_polygon, is_near_miss
from .model import Snapshot, clamp_pilot, home_icao, parse_frequency_hz

_PILOT_SQL = """
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
    name = COALESCE(EXCLUDED.name, ps.name),
    home_icao = COALESCE(EXCLUDED.home_icao, ps.home_icao),
    server = COALESCE(EXCLUDED.server, ps.server),
    plan_changed_at = CASE
      WHEN (EXCLUDED.departure IS NOT NULL AND EXCLUDED.departure IS DISTINCT FROM ps.departure)
        OR (EXCLUDED.arrival IS NOT NULL AND EXCLUDED.arrival IS DISTINCT FROM ps.arrival)
      THEN EXCLUDED.last_seen ELSE ps.plan_changed_at END,
    departure = COALESCE(EXCLUDED.departure, ps.departure),
    arrival = COALESCE(EXCLUDED.arrival, ps.arrival),
    aircraft_icao = COALESCE(EXCLUDED.aircraft_icao, ps.aircraft_icao),
    aircraft_faa = COALESCE(EXCLUDED.aircraft_faa, ps.aircraft_faa),
    flight_rules = COALESCE(EXCLUDED.flight_rules, ps.flight_rules),
    route = COALESCE(EXCLUDED.route, ps.route),
    remarks = COALESCE(EXCLUDED.remarks, ps.remarks),
    planned_alt = COALESCE(EXCLUDED.planned_alt, ps.planned_alt),
    deptime = COALESCE(EXCLUDED.deptime, ps.deptime),
    max_gs_kt = GREATEST(ps.max_gs_kt, EXCLUDED.max_gs_kt),
    max_alt_ft = GREATEST(ps.max_alt_ft, EXCLUDED.max_alt_ft),
    pilot_rating = COALESCE(EXCLUDED.pilot_rating, ps.pilot_rating),
    military_rating = COALESCE(EXCLUDED.military_rating, ps.military_rating)
  RETURNING id, cid, callsign, logon_time
)
INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg)
SELECT %(ts)s, up.id, x.lat, x.lon, x.alt_ft, x.gs_kt, x.hdg
FROM up JOIN x USING (cid, callsign, logon_time)
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

_NEAR_MISS_SQL = """
INSERT INTO controller_unlisted_sighting AS u (callsign, cid, first_seen, last_seen, polls_seen)
SELECT callsign, cid, %(ts)s, %(ts)s, 1 FROM jsonb_to_recordset(%(rows)s) AS r(callsign text, cid bigint)
ON CONFLICT (callsign) DO UPDATE SET cid = EXCLUDED.cid, last_seen = EXCLUDED.last_seen,
  polls_seen = u.polls_seen + 1 WHERE EXCLUDED.last_seen > u.last_seen
RETURNING callsign, (xmax = 0) AS inserted
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
    new_near_misses: list[str] = field(default_factory=list)


def _jsonb(rows: list[dict[str, Any]]) -> Jsonb:
    return Jsonb(rows, dumps=lambda o: json.dumps(o, default=lambda v: v.isoformat()))


def apply_snapshot(
    conn: psycopg.Connection[Any],
    snapshot: Snapshot,
    polygon: Polygon,
    whitelist: frozenset[str],
    *,
    polygon_sha: str,
    whitelist_sha: str,
) -> ApplyResult:
    """Write one snapshot in a single transaction. Replaying it inserts zero rows."""
    ts = snapshot.observed_at
    res = ApplyResult(pilots_seen=snapshot.pilots_global, controllers_seen=snapshot.controllers_global)

    pilots: dict[tuple[int, str, datetime], dict[str, Any]] = {}
    for p in snapshot.pilots:
        if not inside_polygon(polygon, p.latitude, p.longitude):
            continue
        gs, alt, rejected = clamp_pilot(p.groundspeed, p.altitude)
        res.rejected_values += rejected
        pilots[(p.cid, p.callsign, p.logon_time)] = {
            "cid": p.cid, "callsign": p.callsign, "logon_time": p.logon_time, "name": p.name,
            "home_icao": home_icao(p.name), "server": p.server, "departure": p.departure,
            "arrival": p.arrival, "aircraft_icao": p.aircraft_icao, "aircraft_faa": p.aircraft_faa,
            "flight_rules": p.flight_rules, "route": p.route, "remarks": p.remarks,
            "planned_alt": p.planned_alt, "deptime": p.deptime, "gs_kt": gs, "alt_ft": alt,
            "pilot_rating": p.pilot_rating, "military_rating": p.military_rating,
            "lat": p.latitude, "lon": p.longitude, "hdg": p.heading,
        }
        if (p.departure or "").strip() and (p.arrival or "").strip():
            res.pilots_with_plan += 1
    res.pilots_inside = res.pilots_kept = len(pilots)

    controllers: dict[tuple[int, str, datetime], dict[str, Any]] = {}
    near_miss: dict[str, int] = {}
    for c in snapshot.controllers:
        if c.callsign in whitelist:
            controllers[(c.cid, c.callsign, c.logon_time)] = {
                "cid": c.cid, "callsign": c.callsign, "logon_time": c.logon_time, "name": c.name,
                "rating": c.rating, "facility": c.facility, "server": c.server,
                "frequency_hz": parse_frequency_hz(c.frequency_text), "visual_range": c.visual_range,
            }
        elif is_near_miss(c.callsign, whitelist):
            near_miss[c.callsign] = c.cid
    res.controllers_kept = len(controllers)

    with conn.transaction(), conn.cursor(row_factory=tuple_row) as cur:  # independent of the connection's row factory
        if pilots:
            cur.execute(_PILOT_SQL, {"rows": _jsonb(list(pilots.values())), "ts": ts})
            res.positions_inserted = max(cur.rowcount, 0)
        if controllers:
            cur.execute(_CONTROLLER_SQL, {"rows": _jsonb(list(controllers.values())), "ts": ts})
            ids = {(r[1], r[2], r[3]): r[0] for r in cur.fetchall()}
            keys = [k for k in controllers if k in ids]
            for table, col, typ, field_name in (
                ("controller_rating_event", "rating", "int", "rating"),
                ("controller_frequency_event", "frequency_hz", "bigint", "frequency_hz"),
            ):
                cur.execute(
                    _EVENT_SQL.format(table=table, col=col, typ=typ),
                    {"ts": ts, "ids": [ids[k] for k in keys], "vals": [controllers[k][field_name] for k in keys]},
                )
        if near_miss:
            rows = [{"callsign": cs, "cid": cid} for cs, cid in near_miss.items()]
            cur.execute(_NEAR_MISS_SQL, {"rows": _jsonb(rows), "ts": ts})
            res.new_near_misses = [r[0] for r in cur.fetchall() if r[1]]
        cur.execute(
            _NETWORK_SQL,
            (ts, res.pilots_inside, res.controllers_kept, snapshot.pilots_global, snapshot.controllers_global),
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
