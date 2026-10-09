"""Review additions (08-10-2026): the pilot upsert against the statement it replaced, and startup behaviour, on Postgres.

The old statement is embedded verbatim from main f41494d so that in-order equivalence stays testable."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.types.json import Jsonb

from helpers import partition_for
from vatpac import store

pytestmark = pytest.mark.integration

OLD_PILOT_SQL = """
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
  WHERE (ps.first_seen, ps.last_seen, ps.name, ps.home_icao, ps.server, ps.departure, ps.arrival, ps.aircraft_icao,
      ps.aircraft_faa, ps.flight_rules, ps.route, ps.remarks, ps.planned_alt, ps.deptime, ps.max_gs_kt, ps.max_alt_ft,
      ps.pilot_rating, ps.military_rating) IS DISTINCT FROM (
      LEAST(ps.first_seen, EXCLUDED.first_seen), GREATEST(ps.last_seen, EXCLUDED.last_seen),
      COALESCE(EXCLUDED.name, ps.name), COALESCE(EXCLUDED.home_icao, ps.home_icao),
      COALESCE(EXCLUDED.server, ps.server), COALESCE(EXCLUDED.departure, ps.departure),
      COALESCE(EXCLUDED.arrival, ps.arrival), COALESCE(EXCLUDED.aircraft_icao, ps.aircraft_icao),
      COALESCE(EXCLUDED.aircraft_faa, ps.aircraft_faa), COALESCE(EXCLUDED.flight_rules, ps.flight_rules),
      COALESCE(EXCLUDED.route, ps.route), COALESCE(EXCLUDED.remarks, ps.remarks),
      COALESCE(EXCLUDED.planned_alt, ps.planned_alt), COALESCE(EXCLUDED.deptime, ps.deptime),
      GREATEST(ps.max_gs_kt, EXCLUDED.max_gs_kt), GREATEST(ps.max_alt_ft, EXCLUDED.max_alt_ft),
      COALESCE(EXCLUDED.pilot_rating, ps.pilot_rating), COALESCE(EXCLUDED.military_rating, ps.military_rating))
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

T0 = datetime(2026, 10, 7, 16, 0, 0, tzinfo=timezone.utc)
TEXT = ("name", "home_icao", "server", "departure", "arrival", "aircraft_icao", "aircraft_faa", "flight_rules", "route",
        "remarks", "planned_alt", "deptime")


def row(i: int, rng: random.Random) -> dict:
    r: dict = {"cid": 900 + i, "callsign": f"RV{i}", "logon_time": (T0 - timedelta(hours=1)).isoformat()}
    for c in TEXT:
        r[c] = rng.choice([None, None, "A", "B", "C"])  # NULL about 40 percent of the time
    r["home_icao"] = rng.choice([None, "YSSY", "YMML"])  # the table CHECK wants four capital letters
    for c in ("gs_kt", "alt_ft", "pilot_rating", "military_rating"):
        r[c] = rng.choice([None, 0, 10, 250, 400])
    r["hdg"] = rng.choice([None, 0, 90, 360])
    r["lat"], r["lon"] = -33.0 + i / 100, 151.0
    return r


def dump(conn) -> list[tuple]:
    cols = ("cid, callsign, first_seen, last_seen, plan_changed_at, max_gs_kt, max_alt_ft, pilot_rating, "
            "military_rating, " + ", ".join(TEXT))
    sess = conn.execute(f"SELECT {cols} FROM pilot_session ORDER BY cid, callsign").fetchall()
    pos = conn.execute("SELECT s.cid, p.observed_at, p.lat, p.alt_ft, p.gs_kt, p.hdg FROM pilot_position p "
                       "JOIN pilot_session s ON s.id = p.session_id ORDER BY 1, 2").fetchall()
    return [tuple(r.values()) for r in sess] + [tuple(r.values()) for r in pos]


def run_sequence(conn, sql: str, seq: list[tuple[datetime, list[dict]]]) -> list[tuple]:
    conn.execute("TRUNCATE pilot_position, pilot_session RESTART IDENTITY CASCADE")
    for ts, rows in seq:
        conn.execute(sql, {"rows": Jsonb(rows), "ts": ts})
    return dump(conn)


def make_sequence(seed: int, n: int = 12) -> list[tuple[datetime, list[dict]]]:
    rng = random.Random(seed)
    seq = []
    for k in range(n):
        ts = T0 + timedelta(seconds=15 * k)
        seq.append((ts, [row(i, rng) for i in range(5) if rng.random() < 0.8]))
    return seq


@pytest.fixture()
def db(conn):
    partition_for(conn, T0)
    return conn


@pytest.mark.parametrize("seed", range(40))
def test_new_upsert_equals_old_for_in_order_snapshots(db, seed) -> None:
    seq = make_sequence(seed)
    assert run_sequence(db, store._PILOT_SQL, seq) == run_sequence(db, OLD_PILOT_SQL, seq)


@pytest.mark.parametrize("seed", range(10))
def test_replay_writes_nothing(db, seed) -> None:
    seq = make_sequence(seed)
    run_sequence(db, store._PILOT_SQL, seq)
    before = db.execute("SELECT xmin::text AS x, id FROM pilot_session ORDER BY id").fetchall()
    for ts, rows in seq[-3:]:
        res = db.execute(store._PILOT_SQL, {"rows": Jsonb(rows), "ts": ts})
        assert res.rowcount == 0  # no position insert for a replayed snapshot
    assert db.execute("SELECT xmin::text AS x, id FROM pilot_session ORDER BY id").fetchall() == before


def test_equal_last_seen_still_updates_like_before(db) -> None:
    rng = random.Random(1)
    a, b = row(0, rng), row(0, rng)
    a.update(remarks="first", departure="YSSY")
    b.update(remarks="second", departure="YMML")
    seq = [(T0, [a]), (T0, [b])]
    assert run_sequence(db, store._PILOT_SQL, seq) == run_sequence(db, OLD_PILOT_SQL, seq)


def test_older_snapshot_cannot_move_plan_changed_at_or_text(db) -> None:
    rng = random.Random(2)
    new, old = row(0, rng), row(0, rng)
    new.update(departure="YSSY", arrival="YMML", remarks="newest", name="N Name")
    old.update(departure="YBBN", arrival="YPPH", remarks="stale", name="O Name")
    db.execute(store._PILOT_SQL, {"rows": Jsonb([new]), "ts": T0 + timedelta(seconds=30)})
    pca = db.execute("SELECT plan_changed_at AS p FROM pilot_session").fetchone()["p"]
    db.execute(store._PILOT_SQL, {"rows": Jsonb([old]), "ts": T0})
    r = db.execute("SELECT departure, arrival, remarks, name, plan_changed_at AS p, first_seen, last_seen "
                   "FROM pilot_session").fetchone()
    assert (r["departure"], r["arrival"], r["remarks"], r["name"]) == ("YSSY", "YMML", "newest", "N Name")
    assert r["p"] == pca
    assert r["first_seen"] == T0 and r["last_seen"] == T0 + timedelta(seconds=30)  # first/last stay order-free


def test_late_older_snapshot_cannot_fill_a_null_in_the_newer_one(db) -> None:
    """Edge, documented not endorsed: the newest snapshot has NULL remarks and a late older one carries a value. In order
    the value would survive through COALESCE; out of order it is ignored. Harmless for a replay of the same feed."""
    rng = random.Random(3)
    newer, older = row(0, rng), row(0, rng)
    newer["remarks"], older["remarks"] = None, "from-older"
    db.execute(store._PILOT_SQL, {"rows": Jsonb([newer]), "ts": T0 + timedelta(seconds=30)})
    db.execute(store._PILOT_SQL, {"rows": Jsonb([older]), "ts": T0})
    assert db.execute("SELECT remarks FROM pilot_session").fetchone()["remarks"] is None


def test_db_last_ts_ignores_a_future_row_and_reads_an_empty_table(db) -> None:
    from psycopg_pool import ConnectionPool

    from vatpac.main import Collector
    from vatpac.settings import Settings

    import os

    pool = ConnectionPool(os.environ["VATPAC_TEST_DSN"], min_size=1, max_size=1, open=True)
    try:
        col = Collector.__new__(Collector)
        col.pool, col.settings = pool, Settings(_env_file=None, archive_enabled=False)  # type: ignore[call-arg]
        assert col._db_last_ts() is None  # empty database
        for off in ("-1 hour", "+1 day", "+119 seconds"):
            db.execute("INSERT INTO network_snapshot(observed_at, pilots_in_region, controllers_in_region, pilots_global, "
                       "controllers_global) VALUES (now() + %s::interval, 1, 1, 1, 1)", (off,))
        got = col._db_last_ts()
        want = db.execute("SELECT max(observed_at) AS m FROM network_snapshot WHERE observed_at < now() + interval '120 seconds'"
                          ).fetchone()["m"]
        assert got == want and got is not None
    finally:
        pool.close()
