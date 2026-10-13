"""Helpers shared by the integration tests (see conftest.py)."""
from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def partition_for(conn, day: datetime) -> str:
    """Create the daily partition pilot_position_YYYYMMDD for the UTC day containing ``day``."""
    start = day.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    name = f"pilot_position_{start:%Y%m%d}"
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF pilot_position "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )
    return name


def at(payload: dict, seconds: int = 0, **changes) -> dict:
    """Copy of the fixture payload with update_timestamp shifted by ``seconds``."""
    out = copy.deepcopy(payload)
    base = datetime(2026, 10, 7, 16, 11, 19, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    out["general"]["update_timestamp"] = base.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return out


def apply(conn, payload: dict, refdata, polygon_sha: str = "p" * 64, whitelist_sha: str = "w" * 64):
    from vatpac.model import parse_snapshot
    from vatpac.store import apply_snapshot

    poly, wl = refdata
    result = apply_snapshot(conn, parse_snapshot(payload), poly, wl,
                            polygon_sha=polygon_sha, whitelist_sha=whitelist_sha)
    if not conn.autocommit:
        conn.commit()
    return result


def counts(conn) -> dict[str, int]:
    tables = ["pilot_session", "pilot_position", "controller_session", "controller_rating_event",
              "controller_frequency_event", "network_snapshot"]
    return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in tables}


# ------------------------------------------------------------------ flight view and summary (used by several files)
T0 = datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc)


def make_session(conn, cid: int, samples: list[tuple[datetime, int | None]], dep="YSSY", arr="YMML") -> int:
    """Insert a pilot_session and one position per (observed_at, gs) sample."""
    first, last = samples[0][0], samples[-1][0]
    for t, _ in samples:
        partition_for(conn, t)
    sid = conn.execute(
        "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (cid, f"TEST{cid}", first.replace(microsecond=0), first, last, dep, arr),
    ).fetchone()["id"]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) "
            "VALUES (%s,%s,-33.9,151.2,5000,%s,90)",
            [(t, sid, gs) for t, gs in samples],
        )
    return sid


def run(n: int, start: datetime = T0, step: int = 15, gs: int | None = 250):
    return [(start + timedelta(seconds=step * i), gs) for i in range(n)]


def write_all(conn, hours: float = 9.0) -> int:
    """Put a network_snapshot ``hours`` after the newest session sighting (never later than now, which the writer would ignore
    anyway) and call write_finished_flights until it has nothing left. Returns the number of flights written. The flight table is
    written only for pilots quiet for 8 hours of STORED data, so a test that builds sessions has to say when the data ends."""
    last = conn.execute("SELECT max(last_seen) AS t FROM pilot_session").fetchone()["t"]
    if last is not None:
        ref = min(last + timedelta(hours=hours), datetime.now(UTC))
        conn.execute("INSERT INTO network_snapshot VALUES (%s,1,1,1,1) ON CONFLICT DO NOTHING", (ref,))
    total = 0
    while True:
        n = conn.execute("SELECT write_finished_flights(500) AS n").fetchone()["n"]
        total += n
        if n == 0:
            return total


def flights(conn, cid: int) -> list[dict]:
    # `flight` reads flight_summary (issue 89 phase 3), which write_finished_flights() fills once a pilot has been quiet for
    # 8 hours of stored data: build the sessions, say when the data ends (write_all), then read the view.
    write_all(conn)
    return conn.execute("SELECT * FROM flight WHERE cid = %s ORDER BY start_time", (cid,)).fetchall()


# ------------------------------------------------------------------ in-progress flights (the online_now clock)
CID = 9700000


def set_poll_interval(conn, seconds: int = 30) -> None:
    """The collector publishes poll_interval_seconds to app_setting on start; the migrations do not seed it, and the
    online-now windows (2 x this value, ADR-024) are empty without it. The conn fixture removes it after each test."""
    conn.execute("INSERT INTO app_setting (key, value) VALUES ('poll_interval_seconds', %s) "
                 "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (str(seconds),))


def set_up_clock(conn) -> datetime:
    """30 s polling and a newest snapshot 5 s ago; returns the snapshot time."""
    set_poll_interval(conn, 30)
    t = datetime.now(timezone.utc) - timedelta(seconds=5)
    conn.execute("INSERT INTO network_snapshot VALUES (%s,1,1,1,1)", (t,))
    return t


def session_ending(t: datetime, age_s: int, n: int = 40, gs: int | None = 250):
    """n samples 15 s apart whose last one is age_s seconds before t."""
    return run(n, start=t - timedelta(seconds=age_s + 15 * (n - 1)), gs=gs)


# ------------------------------------------------------------------ feed payload lookups and controller events
def pilot(payload: dict, callsign: str) -> dict:
    return next(p for p in payload["pilots"] if p["callsign"] == callsign)


def ctrl(payload: dict, callsign: str) -> dict:
    return next(c for c in payload["controllers"] if c["callsign"] == callsign)


def events(db, table: str, callsign: str) -> list[dict]:
    return db.execute(
        f"SELECT e.* FROM {table} e JOIN controller_session s ON s.id = e.session_id "
        "WHERE s.callsign = %s ORDER BY e.observed_at",
        (callsign,),
    ).fetchall()


# ------------------------------------------------------------------ dashboard panels run against synthetic flights
TRAFFIC = REPO / "grafana" / "dashboards" / "pilots" / "pilots-traffic.json"
ORIGINS = REPO / "grafana" / "dashboards" / "demographics" / "pilot-origins.json"
def panel(path, title: str) -> dict:
    dash = json.loads(path.read_text(encoding="utf-8"))
    return next(p for p in dash["panels"] if p.get("title") == title)


def run_panel(conn, path, title: str) -> list[dict]:
    p = panel(path, title)
    days = int(str(p.get("timeFrom", "30d")).rstrip("d"))
    sql = (p["targets"][0]["rawSql"]
           .replace("$__timeFrom()", f"(now() - interval '{days * 24} hours')")  # hours: day intervals shift by an hour across DST in a non-UTC session
           .replace("$__timeTo()", "now()"))
    return conn.execute(sql).fetchall()


_serial = {"n": 0}


def record_flight(conn, sid: int, start: datetime, end: datetime, *, airborne: float | None = None, gs: int | None = 450,
                  alt: int | None = 35000, samples: int = 10, start_offset_s: int = 0) -> None:
    """Write the flight_summary row the writer would write for pilot_session ``sid`` (issue 89 phase 3), without positions.

    For the panel tests, which assert the panel arithmetic and not the writer (the writer has its own tests, test_flight_writer.py
    and test_flight_views_switch.py). The flight gate is applied as the writer applies it: a session whose departure or arrival is
    empty after upper(btrim) makes no row. Plan fields are copied from the session, as the writer freezes them.
    ``start_offset_s`` lets a test create a second flight of one session (a returning session): the key is (session_id, start_time)."""
    conn.execute(
        r"""
        INSERT INTO flight_summary (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, logon_time,
               name, home_icao, server, aircraft_icao, aircraft_faa, flight_rules, route, remarks, planned_alt, deptime,
               pilot_rating, military_rating, airborne_minutes, max_gs_kt, max_alt_ft, sample_count)
        SELECT ps.id, %s::timestamptz + make_interval(secs => %s::double precision), %s::timestamptz, ps.cid, ps.callsign,
               upper(btrim(ps.departure, E' 	
')), upper(btrim(ps.arrival, E' 	
')), ARRAY[ps.id], ps.logon_time,
               ps.name, ps.home_icao, ps.server, ps.aircraft_icao, ps.aircraft_faa, ps.flight_rules, ps.route, ps.remarks,
               ps.planned_alt, ps.deptime, ps.pilot_rating, ps.military_rating, %s::numeric, %s::int, %s::int, %s::bigint
          FROM pilot_session ps
         WHERE ps.id = %s
           AND NULLIF(upper(btrim(ps.departure, E' 	
')), '') IS NOT NULL
           AND NULLIF(upper(btrim(ps.arrival, E' 	
')), '') IS NOT NULL
        """,
        (start, start_offset_s, end, airborne, gs, alt, samples, sid),
    )


def add_flight(conn, cid: int, *, callsign: str | None = None, dep="YSSY", arr="YMML", aircraft="B738",
               days_ago: float = 1.0, minutes: int = 120, airborne: float | None = 100, gs: int | None = 450,
               alt: int | None = 35000, logon_days_ago: float | None = None, **plan) -> int:
    """One pilot session plus its flight_summary row. The flight ends ``days_ago`` days ago.

    ``plan`` sets further plan fields of the session (flight_rules, remarks, home_icao, server, route, planned_alt, deptime,
    pilot_rating, military_rating): the row is frozen when it is written, so a test cannot change the session afterwards and
    expect the view to follow (it did before issue 89 phase 3, when the view read the session live)."""
    _serial["n"] += 1
    end = datetime.now(UTC) - timedelta(days=days_ago)
    start = end - timedelta(minutes=minutes)
    logon = datetime.now(UTC) - timedelta(days=logon_days_ago) if logon_days_ago is not None else start
    logon = min(logon, start)
    fields = {"cid": cid, "callsign": callsign or f"TST{_serial['n']:04d}", "logon_time": logon.replace(microsecond=0),
              "name": f"Pilot {cid}", "first_seen": logon, "last_seen": end, "departure": dep, "arrival": arr,
              "aircraft_icao": aircraft, **plan}
    sid = conn.execute(
        f"INSERT INTO pilot_session ({', '.join(fields)}) VALUES ({', '.join(['%s'] * len(fields))}) RETURNING id",  # noqa: S608
        list(fields.values()),
    ).fetchone()["id"]
    record_flight(conn, sid, start, end, airborne=airborne, gs=gs, alt=alt)
    return sid
