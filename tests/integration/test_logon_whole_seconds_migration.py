"""T36 / ADR-021: migration 20261008000100 merges twin sessions created by a logon_time precision flip,
truncates every stored logon_time to whole seconds and adds the CHECK constraints.

The test builds its own database from migrations up to ...90 (a template, then one copy per test), loads
synthetic data that reproduces the incident, and applies the new migration (up half only, as dbmate does).
Pinned to that migration on purpose (issue 89 phase 5): the database is built only from the migrations BEFORE it, where
flight_summary is still the old pieces table, so the pieces names in this file are correct and stay.
It needs the superuser DSN of the throwaway server (VATPAC_TEST_DSN); the databases it creates end in _test.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

pytestmark = pytest.mark.integration

MIG_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"
NEW = "20261008000100_logon_time_whole_seconds.sql"
TMPL = "vatpac_mig100_tmpl_test"


def utc(*a: int, us: int = 0) -> datetime:
    return datetime(*a, microsecond=us, tzinfo=timezone.utc)


def up_half(path: Path) -> str:
    return re.split(r"^-- migrate:down", path.read_text(encoding="utf-8"), maxsplit=1, flags=re.M)[0]


def apply_migration(conn) -> None:
    conn.execute(up_half(MIG_DIR / NEW))


def connect(dsn: str, dbname: str) -> psycopg.Connection:
    return psycopg.connect(make_conninfo(dsn, dbname=dbname), autocommit=True, row_factory=dict_row)


@pytest.fixture(scope="module")
def template(dsn: str):
    with psycopg.connect(make_conninfo(dsn, dbname="postgres"), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TMPL} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TMPL}")
        with connect(dsn, TMPL) as c:
            for f in sorted(MIG_DIR.glob("*.sql")):
                if f.name < NEW:
                    c.execute(up_half(f))
        yield TMPL
        admin.execute(f"DROP DATABASE IF EXISTS {TMPL} WITH (FORCE)")


@pytest.fixture()
def db(dsn: str, template: str):
    name = f"vatpac_mig100_{uuid.uuid4().hex[:8]}_test"
    with psycopg.connect(make_conninfo(dsn, dbname="postgres"), autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name} TEMPLATE {template}")
        try:
            with connect(dsn, name) as c:
                yield c
        finally:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


# ------------------------------------------------------------------ synthetic data
def add_partition(c, day: datetime) -> None:
    n = f"pilot_position_{day:%Y%m%d}"
    c.execute(f"CREATE TABLE IF NOT EXISTS {n} PARTITION OF pilot_position FOR VALUES FROM "
              f"('{day:%Y-%m-%d} 00:00:00+00') TO ('{day + timedelta(days=1):%Y-%m-%d} 00:00:00+00')")


def pilot(c, cid, callsign, logon, first, last, **kw) -> int:
    cols = {"cid": cid, "callsign": callsign, "logon_time": logon, "first_seen": first, "last_seen": last, **kw}
    return c.execute(
        f"INSERT INTO pilot_session ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
        list(cols.values())).fetchone()["id"]


def pos(c, sid, at, gs=200) -> None:
    c.execute("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) "
              "VALUES (%s,%s,-33.9,151.2,5000,%s,90)", (at, sid, gs))


def controller(c, cid, callsign, logon, first, last, **kw) -> int:
    cols = {"cid": cid, "callsign": callsign, "logon_time": logon, "first_seen": first, "last_seen": last, **kw}
    return c.execute(
        f"INSERT INTO controller_session ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
        list(cols.values())).fetchone()["id"]


def load_incident(c) -> dict:
    """Twins as the incident made them. Returns the ids by label."""
    add_partition(c, utc(2026, 10, 7))
    add_partition(c, utc(2026, 10, 8))
    ids: dict[str, int] = {}
    n = lambda q: c.execute(q).fetchone()["n"]  # noqa: E731

    # A: one flight stored twice, positions in two different daily partitions
    ids["a1"] = pilot(c, 8100001, "TWA1", utc(2026, 10, 7, 23, 50, 10, us=250000), utc(2026, 10, 7, 23, 59, 50),
                      utc(2026, 10, 8, 0, 0, 10), name="Old Name", home_icao="YSSY", server="SYD1", departure="YSSY",
                      arrival="YMML", aircraft_icao="B738", route="DCT", remarks="OLD", planned_alt="35000",
                      deptime="2350", plan_changed_at=utc(2026, 10, 7, 23, 59, 50), max_gs_kt=400, max_alt_ft=30000,
                      pilot_rating=1, military_rating=0)
    ids["a2"] = pilot(c, 8100001, "TWA1", utc(2026, 10, 7, 23, 50, 10), utc(2026, 10, 8, 0, 0, 20),
                      utc(2026, 10, 8, 0, 0, 30), name="New Name", home_icao=None, server="SYD2", departure="YSSY",
                      arrival="YBBN", aircraft_icao=None, route=None, remarks="NEW", planned_alt=None, deptime=None,
                      plan_changed_at=utc(2026, 10, 8, 0, 0, 20), max_gs_kt=450, max_alt_ft=25000,
                      pilot_rating=None, military_rating=None)
    for sid, ts in ((ids["a1"], utc(2026, 10, 7, 23, 59, 50)), (ids["a1"], utc(2026, 10, 8, 0, 0, 0)),
                    (ids["a1"], utc(2026, 10, 8, 0, 0, 10)), (ids["a2"], utc(2026, 10, 8, 0, 0, 20)),
                    (ids["a2"], utc(2026, 10, 8, 0, 0, 30))):
        pos(c, sid, ts)
    # a different session of the same pilot and callsign, 12 hours earlier: NOT a twin
    ids["a_old"] = pilot(c, 8100001, "TWA1", utc(2026, 10, 7, 11, 0, 0), utc(2026, 10, 7, 11, 1, 0),
                         utc(2026, 10, 7, 11, 2, 0), departure="YMML", arrival="YSSY")
    pos(c, ids["a_old"], utc(2026, 10, 7, 11, 1, 0))
    # the same pilot, another callsign, same second: NOT a twin
    ids["a_other"] = pilot(c, 8100001, "TWA2", utc(2026, 10, 7, 23, 50, 10, us=250000), utc(2026, 10, 8, 0, 0, 0),
                           utc(2026, 10, 8, 0, 0, 0))

    # B: three members, one instant stored by two of them (primary-key conflict), middle one is latest
    ids["b1"] = pilot(c, 8100002, "TWB1", utc(2026, 10, 8, 1, 0, 5, us=100000), utc(2026, 10, 8, 1, 1, 0),
                      utc(2026, 10, 8, 1, 2, 0), departure="YPPH", arrival="YSSY", max_gs_kt=None, max_alt_ft=None)
    ids["b2"] = pilot(c, 8100002, "TWB1", utc(2026, 10, 8, 1, 0, 5), utc(2026, 10, 8, 1, 2, 0),
                      utc(2026, 10, 8, 1, 4, 0), departure="YPPH", arrival="YSSY", max_gs_kt=300, max_alt_ft=None)
    ids["b3"] = pilot(c, 8100002, "TWB1", utc(2026, 10, 8, 1, 0, 5, us=900000), utc(2026, 10, 8, 1, 3, 0),
                      utc(2026, 10, 8, 1, 3, 30), departure="YPPH", arrival="YSSY", max_gs_kt=250, max_alt_ft=9000)
    for sid, ts in ((ids["b1"], utc(2026, 10, 8, 1, 1, 0)), (ids["b1"], utc(2026, 10, 8, 1, 2, 0)),
                    (ids["b2"], utc(2026, 10, 8, 1, 2, 0)), (ids["b2"], utc(2026, 10, 8, 1, 3, 0)),
                    (ids["b3"], utc(2026, 10, 8, 1, 3, 0)), (ids["b3"], utc(2026, 10, 8, 1, 3, 30))):
        pos(c, sid, ts)

    # C: fractional logon without a twin; D: whole-second logon without a twin
    ids["c"] = pilot(c, 8100003, "TWC1", utc(2026, 10, 8, 3, 0, 0, us=900000), utc(2026, 10, 8, 3, 1, 0),
                     utc(2026, 10, 8, 3, 2, 0), departure="YSCB", arrival="YSSY")
    ids["d"] = pilot(c, 8100004, "TWD1", utc(2026, 10, 8, 3, 0, 0), utc(2026, 10, 8, 3, 1, 0),
                     utc(2026, 10, 8, 3, 2, 0), departure="YSCB", arrival="YSSY")
    for sid in (ids["c"], ids["d"]):
        pos(c, sid, utc(2026, 10, 8, 3, 1, 0))
        pos(c, sid, utc(2026, 10, 8, 3, 2, 0))
    # the incident's flight_summary: one row per session
    c.execute("SELECT refresh_flight_summary_sessions(%s, false)", (list(ids.values()),))

    # controllers: E twins with rating and frequency events; F fractional without twin; G whole
    ids["e1"] = controller(c, 8200001, "TW_APP", utc(2026, 10, 8, 3, 30, 22, us=516991), utc(2026, 10, 8, 3, 30, 30),
                           utc(2026, 10, 8, 4, 7, 49), name="Old", rating=3, facility=5, server="SYD1",
                           frequency_hz=124000000, visual_range=150)
    ids["e2"] = controller(c, 8200001, "TW_APP", utc(2026, 10, 8, 3, 30, 22), utc(2026, 10, 8, 4, 8, 27),
                           utc(2026, 10, 8, 5, 0, 0), name="New", rating=4, facility=5, server=None,
                           frequency_hz=125000000, visual_range=200)
    for sid, ts, rating in ((ids["e1"], utc(2026, 10, 8, 3, 30, 30), 3), (ids["e2"], utc(2026, 10, 8, 4, 8, 27), 3),
                            (ids["e2"], utc(2026, 10, 8, 4, 30, 0), 4)):
        c.execute("INSERT INTO controller_rating_event (session_id, observed_at, rating) VALUES (%s,%s,%s)", (sid, ts, rating))
    for sid, ts, hz in ((ids["e1"], utc(2026, 10, 8, 3, 30, 30), 124000000), (ids["e2"], utc(2026, 10, 8, 4, 8, 27), 124000000),
                        (ids["e2"], utc(2026, 10, 8, 4, 40, 0), 125000000)):
        c.execute("INSERT INTO controller_frequency_event (session_id, observed_at, frequency_hz) VALUES (%s,%s,%s)", (sid, ts, hz))
    ids["f"] = controller(c, 8200002, "TW_TWR", utc(2026, 10, 8, 3, 0, 0, us=750000), utc(2026, 10, 8, 3, 1, 0),
                          utc(2026, 10, 8, 3, 2, 0), rating=3)
    ids["g"] = controller(c, 8200003, "TW_GND", utc(2026, 10, 8, 3, 0, 0), utc(2026, 10, 8, 3, 1, 0),
                          utc(2026, 10, 8, 3, 2, 0), rating=3)
    assert n("SELECT count(*) AS n FROM pilot_session") == 9
    return ids


def snapshot_counts(c) -> dict[str, int]:
    return {t: c.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
            for t in ("pilot_session", "controller_session", "pilot_position", "flight_summary",
                      "controller_rating_event", "controller_frequency_event")}


# ------------------------------------------------------------------ tests
def test_the_incident_reproduces_before_the_migration(db) -> None:
    load_incident(db)
    twins = db.execute(
        "SELECT cid, callsign FROM pilot_session GROUP BY cid, callsign, date_trunc('second', logon_time) "
        "HAVING count(*) > 1").fetchall()
    assert len(twins) == 2
    assert db.execute("SELECT count(*) AS n FROM flight WHERE cid = 8100001 AND callsign = 'TWA1' "
                      "AND logon_time > '2026-10-07 20:00+00'").fetchone()["n"] == 2   # the doubled flight


def test_fk_list_is_the_known_one(db) -> None:
    rows = db.execute(
        "SELECT conrelid::regclass::text AS child, confrelid::regclass::text AS parent FROM pg_constraint "
        "WHERE contype = 'f' AND conparentid = 0 AND confrelid IN ('pilot_session'::regclass, 'controller_session'::regclass) "
        "ORDER BY 1").fetchall()
    assert {(r["child"], r["parent"]) for r in rows} == {
        ("pilot_position", "pilot_session"), ("flight_summary", "pilot_session"),
        ("controller_rating_event", "controller_session"), ("controller_frequency_event", "controller_session")}


def test_migration_merges_pilot_twins_and_loses_nothing(db) -> None:
    ids = load_incident(db)
    before = snapshot_counts(db)
    apply_migration(db)
    after = snapshot_counts(db)

    # no twins left, whole seconds everywhere
    assert db.execute("SELECT count(*) AS n FROM (SELECT 1 FROM pilot_session GROUP BY cid, callsign, "
                      "date_trunc('second', logon_time) HAVING count(*) > 1) x").fetchone()["n"] == 0
    assert db.execute("SELECT count(*) AS n FROM pilot_session WHERE logon_time <> date_trunc('second', logon_time)"
                      ).fetchone()["n"] == 0
    assert after["pilot_session"] == before["pilot_session"] - 3          # a2, b2, b3 merged away
    survivors = {r["id"] for r in db.execute("SELECT id FROM pilot_session")}
    assert {ids["a1"], ids["b1"], ids["c"], ids["d"], ids["a_old"], ids["a_other"]} == survivors

    # positions: all re-pointed, none lost except the one instant stored twice by b1 and b2, and by b2 and b3
    assert after["pilot_position"] == before["pilot_position"] - 2
    got = {(r["session_id"], r["observed_at"]) for r in db.execute("SELECT session_id, observed_at FROM pilot_position")}
    assert {t for s, t in got if s == ids["a1"]} == {
        utc(2026, 10, 7, 23, 59, 50), utc(2026, 10, 8, 0, 0, 0), utc(2026, 10, 8, 0, 0, 10),
        utc(2026, 10, 8, 0, 0, 20), utc(2026, 10, 8, 0, 0, 30)}
    assert {t for s, t in got if s == ids["b1"]} == {
        utc(2026, 10, 8, 1, 1, 0), utc(2026, 10, 8, 1, 2, 0), utc(2026, 10, 8, 1, 3, 0), utc(2026, 10, 8, 1, 3, 30)}
    assert all(s in survivors for s, _ in got)

    # survivor A: lowest id, truncated logon, earliest first_seen, latest last_seen, maxima, latest non-null fields
    a = db.execute("SELECT * FROM pilot_session WHERE id = %s", (ids["a1"],)).fetchone()
    assert a["logon_time"] == utc(2026, 10, 7, 23, 50, 10)
    assert (a["first_seen"], a["last_seen"]) == (utc(2026, 10, 7, 23, 59, 50), utc(2026, 10, 8, 0, 0, 30))
    assert (a["max_gs_kt"], a["max_alt_ft"]) == (450, 30000)
    assert (a["name"], a["server"], a["arrival"], a["remarks"]) == ("New Name", "SYD2", "YBBN", "NEW")
    assert (a["home_icao"], a["aircraft_icao"], a["route"], a["planned_alt"], a["deptime"]) == (
        "YSSY", "B738", "DCT", "35000", "2350")                                        # latest non-null wins
    assert a["plan_changed_at"] == utc(2026, 10, 8, 0, 0, 20)
    assert (a["pilot_rating"], a["military_rating"]) == (1, 0)
    # survivor B: three members, maxima ignore nulls, latest by last_seen is b3
    b = db.execute("SELECT * FROM pilot_session WHERE id = %s", (ids["b1"],)).fetchone()
    assert (b["first_seen"], b["last_seen"]) == (utc(2026, 10, 8, 1, 1, 0), utc(2026, 10, 8, 1, 4, 0))
    assert (b["max_gs_kt"], b["max_alt_ft"]) == (300, 9000)

    # non-twins untouched except the fraction
    assert db.execute("SELECT logon_time FROM pilot_session WHERE id = %s", (ids["c"],)).fetchone()["logon_time"] == utc(2026, 10, 8, 3, 0, 0)
    assert db.execute("SELECT logon_time FROM pilot_session WHERE id = %s", (ids["a_other"],)).fetchone()["logon_time"] == utc(2026, 10, 7, 23, 50, 10)
    assert db.execute("SELECT logon_time FROM pilot_session WHERE id = %s", (ids["a_old"],)).fetchone()["logon_time"] == utc(2026, 10, 7, 11, 0, 0)


def test_flight_summary_is_rebuilt_once_per_flight(db) -> None:
    ids = load_incident(db)
    c_before = db.execute("SELECT computed_at FROM flight_summary WHERE session_id = %s", (ids["c"],)).fetchone()
    apply_migration(db)
    rows = db.execute("SELECT * FROM flight_summary WHERE session_id = ANY(%s) ORDER BY session_id",
                      ([ids["a1"], ids["a2"], ids["b1"], ids["b2"], ids["b3"]],)).fetchall()
    assert [r["session_id"] for r in rows] == [ids["a1"], ids["b1"]]               # one row per merged flight
    a = rows[0]
    assert (a["sample_count"], a["start_time"], a["end_time"]) == (5, utc(2026, 10, 7, 23, 59, 50), utc(2026, 10, 8, 0, 0, 30))
    assert float(a["airborne_minutes"]) == pytest.approx(0.67, abs=0.005)           # four 10 s intervals at 200 kt
    assert rows[1]["sample_count"] == 4 and not a["final"] and not rows[1]["final"]
    assert db.execute("SELECT count(*) AS n FROM flight WHERE cid = 8100001 AND callsign = 'TWA1' "
                      "AND logon_time > '2026-10-07 20:00+00'").fetchone()["n"] == 1
    assert db.execute("SELECT count(*) AS n FROM flight WHERE cid = 8100002").fetchone()["n"] == 1
    # a session without a twin keeps its summary row untouched (not deleted and recomputed)
    assert db.execute("SELECT computed_at FROM flight_summary WHERE session_id = %s", (ids["c"],)).fetchone() == c_before
    assert db.execute("SELECT count(*) AS n FROM flight_summary WHERE session_id = ANY(%s)",
                      ([ids["a_old"], ids["c"], ids["d"]],)).fetchone()["n"] == 3
    assert db.execute("SELECT count(*) AS n FROM flight_summary f LEFT JOIN pilot_session s ON s.id = f.session_id "
                      "WHERE s.id IS NULL").fetchone()["n"] == 0


def test_migration_merges_controller_twins_and_keeps_every_event(db) -> None:
    ids = load_incident(db)
    before = snapshot_counts(db)
    apply_migration(db)
    after = snapshot_counts(db)
    assert after["controller_session"] == before["controller_session"] - 1
    assert after["controller_rating_event"] == before["controller_rating_event"]
    assert after["controller_frequency_event"] == before["controller_frequency_event"]
    e = db.execute("SELECT * FROM controller_session WHERE id = %s", (ids["e1"],)).fetchone()
    assert e["logon_time"] == utc(2026, 10, 8, 3, 30, 22)
    assert (e["first_seen"], e["last_seen"]) == (utc(2026, 10, 8, 3, 30, 30), utc(2026, 10, 8, 5, 0, 0))
    assert (e["rating"], e["frequency_hz"], e["visual_range"], e["facility"]) == (4, 125000000, 200, 5)
    assert (e["name"], e["server"]) == ("New", "SYD1")                              # name latest, server latest non-null
    assert db.execute("SELECT count(*) AS n FROM controller_session WHERE id = %s", (ids["e2"],)).fetchone()["n"] == 0
    for t in ("controller_rating_event", "controller_frequency_event"):
        assert db.execute(f"SELECT count(DISTINCT session_id) AS n FROM {t}").fetchone()["n"] == 1
        assert db.execute(f"SELECT count(*) AS n FROM {t} WHERE session_id = %s", (ids["e1"],)).fetchone()["n"] == 3
    assert db.execute("SELECT logon_time FROM controller_session WHERE id = %s", (ids["f"],)).fetchone()["logon_time"] == utc(2026, 10, 8, 3, 0, 0)
    assert db.execute("SELECT count(*) AS n FROM controller_session WHERE logon_time <> date_trunc('second', logon_time)"
                      ).fetchone()["n"] == 0


def test_check_constraints_reject_a_fractional_logon_time(db) -> None:
    apply_migration(db)
    names = {r["conname"] for r in db.execute("SELECT conname FROM pg_constraint WHERE contype = 'c'")}
    assert {"pilot_session_logon_whole_second", "controller_session_logon_whole_second"} <= names
    # immutable expression (an index accepts only immutable ones): safe for pg_dump and restore
    db.execute("CREATE INDEX tmp_immutable_probe ON pilot_session "
               "(date_trunc('second', logon_time AT TIME ZONE 'UTC'))")
    for tz in ("UTC", "Australia/Sydney"):
        db.execute(f"SET timezone = '{tz}'")
        with pytest.raises(psycopg.errors.CheckViolation):
            pilot(db, 8300001, "FRAC1", utc(2026, 10, 8, 3, 0, 0, us=1), utc(2026, 10, 8, 3, 1, 0), utc(2026, 10, 8, 3, 1, 0))
        with pytest.raises(psycopg.errors.CheckViolation):
            controller(db, 8300002, "FRAC_CTL", utc(2026, 10, 8, 3, 0, 0, us=999999), utc(2026, 10, 8, 3, 1, 0), utc(2026, 10, 8, 3, 1, 0))
        pilot(db, 8300001, f"WHOLE_{tz[:3]}", utc(2026, 10, 8, 3, 0, 0), utc(2026, 10, 8, 3, 1, 0), utc(2026, 10, 8, 3, 1, 0))
        controller(db, 8300002, f"WHOLE_CTL_{tz[:3]}", utc(2026, 10, 8, 3, 0, 0), utc(2026, 10, 8, 3, 1, 0), utc(2026, 10, 8, 3, 1, 0))


def test_migration_is_a_noop_without_twins_and_can_run_twice(db) -> None:
    apply_migration(db)
    assert snapshot_counts(db) == dict.fromkeys(snapshot_counts(db), 0)
    apply_migration(db)


def test_migration_applied_twice_changes_nothing_the_second_time(db) -> None:
    load_incident(db)
    apply_migration(db)
    state = (snapshot_counts(db), db.execute("SELECT * FROM pilot_session ORDER BY id").fetchall(),
             db.execute("SELECT * FROM flight_summary ORDER BY session_id, gap_segment, span_part").fetchall())
    apply_migration(db)
    assert (snapshot_counts(db), db.execute("SELECT * FROM pilot_session ORDER BY id").fetchall(),
            db.execute("SELECT * FROM flight_summary ORDER BY session_id, gap_segment, span_part").fetchall()) == state


def test_migration_refuses_when_a_twin_already_has_a_final_summary(db) -> None:
    ids = load_incident(db)
    db.execute("UPDATE flight_summary SET final = true WHERE session_id = %s", (ids["a2"],))
    before = snapshot_counts(db)
    with pytest.raises(psycopg.errors.RaiseException):
        apply_migration(db)
    assert snapshot_counts(db) == before                                           # nothing half-applied
    assert db.execute("SELECT count(*) AS n FROM pg_constraint WHERE conname LIKE '%logon_whole_second'").fetchone()["n"] == 0


def test_migration_refuses_an_unknown_foreign_key(db) -> None:
    db.execute("CREATE TABLE extra_child (session_id bigint REFERENCES pilot_session (id))")
    with pytest.raises(psycopg.errors.RaiseException, match="unhandled foreign key"):
        apply_migration(db)
