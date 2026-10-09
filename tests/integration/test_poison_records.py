"""Reviewer H3: one poisoned record must not block the other records of the snapshot."""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from helpers import at, counts, partition_for
from vatpac.filters import inside_polygon
from vatpac.model import parse_snapshot
from vatpac.store import apply_snapshot

pytestmark = pytest.mark.integration


@pytest.fixture()
def db(conn):
    partition_for(conn, datetime(2026, 10, 7, tzinfo=timezone.utc))
    return conn


def _apply(db, snap, refdata):
    poly, wl = refdata
    return apply_snapshot(db, snap, poly, wl, polygon_sha="p" * 64, whitelist_sha="w" * 64)


def test_bad_pilot_is_skipped_and_counted(db, payload, refdata) -> None:
    poly, _ = refdata
    snap = parse_snapshot(at(payload))
    inside = [p for p in snap.pilots if inside_polygon(poly, p.latitude, p.longitude)]
    bad = dataclasses.replace(inside[0], callsign="POISON1", cid="not-a-number")  # bigint cast fails in SQL
    broken = dataclasses.replace(snap, pilots=[*snap.pilots, bad])
    clean = _apply(db, snap, refdata)
    before = counts(db)
    db.execute("TRUNCATE pilot_position, pilot_session, controller_session, controller_rating_event, "
               "controller_frequency_event, network_snapshot RESTART IDENTITY CASCADE")
    res = _apply(db, broken, refdata)
    assert counts(db) == before  # every good record stored, nothing for the offender
    assert res.rejected_values == clean.rejected_values + 1
    assert res.pilots_inside == clean.pilots_inside + 1 and res.pilots_kept == clean.pilots_kept
    assert db.execute("SELECT count(*) AS n FROM pilot_session WHERE callsign = 'POISON1'").fetchone()["n"] == 0


def test_bad_controller_is_skipped_and_counted(db, payload, refdata) -> None:
    _, wl = refdata
    snap = parse_snapshot(at(payload))
    good = next(c for c in snap.controllers if c.callsign in wl)
    bad = dataclasses.replace(good, cid="x")  # bigint cast fails in SQL
    clean = _apply(db, snap, refdata)
    db.execute("TRUNCATE controller_session RESTART IDENTITY CASCADE")
    res = _apply(db, dataclasses.replace(snap, controllers=[*snap.controllers, bad]), refdata)
    assert res.rejected_values == clean.rejected_values + 1 and res.controllers_kept == clean.controllers_kept
    n = db.execute("SELECT count(*) AS n FROM controller_session").fetchone()["n"]
    assert n == clean.controllers_kept


def test_replay_does_not_rewrite_unchanged_sessions(db, payload, refdata) -> None:
    snap = parse_snapshot(at(payload))
    _apply(db, snap, refdata)
    q = "SELECT callsign, xmin::text AS x, last_seen FROM pilot_session ORDER BY callsign"
    before = db.execute(q).fetchall()
    res = _apply(db, snap, refdata)
    assert db.execute(q).fetchall() == before  # untouched rows keep their xmin
    assert res.positions_inserted == 0


def test_older_snapshot_still_gets_its_position(db, payload, refdata) -> None:
    """Out-of-order replay: first_seen moves back, last_seen stays, and the position row is still written."""
    _apply(db, parse_snapshot(at(payload, 15)), refdata)
    q = "SELECT callsign, last_seen FROM pilot_session ORDER BY callsign"
    before = db.execute(q).fetchall()
    res = _apply(db, parse_snapshot(at(payload)), refdata)  # 15 s older, same pilots
    assert db.execute(q).fetchall() == before
    assert res.positions_inserted == len(before)
    assert db.execute("SELECT count(*) AS n FROM pilot_position").fetchone()["n"] == 2 * len(before)


# issue 85: a record that breaks a CHECK is skipped even when its text says "partition"; a missing partition is not
def _poison(snap, poly):
    inside = next(p for p in snap.pilots if inside_polygon(poly, p.latitude, p.longitude))
    # a fractional logon_time breaks the pilot_session_logon_whole_second CHECK, and the failing row is quoted in the
    # error text together with these words
    return dataclasses.replace(inside, callsign="PARTITIONER", route="no partition of relation found",
                               remarks="partition", logon_time=inside.logon_time.replace(microsecond=500_000))


def test_poison_record_mentioning_partition_is_bisected_and_skipped(db, payload, refdata) -> None:
    import psycopg

    poly, _ = refdata
    snap = parse_snapshot(at(payload))
    clean = _apply(db, snap, refdata)
    good = counts(db)
    db.execute("TRUNCATE pilot_position, pilot_session, controller_session, controller_rating_event, "
               "controller_frequency_event, network_snapshot RESTART IDENTITY CASCADE")
    bad = _poison(snap, poly)
    with pytest.raises(psycopg.errors.CheckViolation) as err:  # prove the setup: the text has the word, the class is CHECK
        db.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, route) "
                   "VALUES (1, 'X', %s, now(), now(), %s)", (bad.logon_time, bad.route))
    assert "partition" in str(err.value) and err.value.diag.constraint_name == "pilot_session_logon_whole_second"
    res = _apply(db, dataclasses.replace(snap, pilots=[*snap.pilots, bad]), refdata)
    assert counts(db) == good
    assert res.rejected_values == clean.rejected_values + 1
    assert db.execute("SELECT count(*) AS n FROM pilot_session WHERE callsign = 'PARTITIONER'").fetchone()["n"] == 0


def test_a_missing_partition_still_propagates_to_the_caller(db, payload, refdata) -> None:
    import psycopg

    snap = parse_snapshot(at(payload, 3 * 86_400))  # three days on: no partition exists for it
    with pytest.raises(psycopg.errors.CheckViolation) as err:
        _apply(db, snap, refdata)
    assert err.value.diag.constraint_name is None  # what tells it apart from a record that broke a CHECK
    assert counts(db)["pilot_session"] == 0 and counts(db)["network_snapshot"] == 0  # whole snapshot rolled back


# issue 92: positions per snapshot equal network_snapshot.pilots_in_region even when a record is rejected
def test_pilots_in_region_excludes_rejected_pilots(db, payload, refdata) -> None:
    poly, _ = refdata
    snap = parse_snapshot(at(payload))
    bad = _poison(snap, poly)
    res = _apply(db, dataclasses.replace(snap, pilots=[*snap.pilots, bad]), refdata)
    row = db.execute("SELECT pilots_in_region FROM network_snapshot").fetchone()
    positions = db.execute("SELECT count(*) AS n FROM pilot_position").fetchone()["n"]
    assert row["pilots_in_region"] == positions == res.pilots_kept
    assert res.pilots_inside == res.pilots_kept + 1  # poll_log still records how many were inside the polygon
