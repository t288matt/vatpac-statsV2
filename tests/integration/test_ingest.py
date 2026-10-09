"""SPEC s3 / s7 / s11: ingest of the fixture through vatpac.store.apply_snapshot.

Run against a real PostgreSQL 16.4 on 07-10-2026 (all pass).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from helpers import apply, at, counts, partition_for

pytestmark = pytest.mark.integration


@pytest.fixture()
def db(conn):
    # the collector's maintenance task would normally have created today's partition
    partition_for(conn, datetime(2026, 10, 7, tzinfo=timezone.utc))
    return conn


def test_first_apply_writes_expected_rows(db, payload, expected, refdata) -> None:
    apply(db, at(payload), refdata)
    c = counts(db)
    assert c["pilot_session"] == expected["pilots_kept_by_polygon"]
    assert c["pilot_position"] == expected["pilots_kept_by_polygon"]
    assert c["controller_session"] == expected["controllers_kept_by_whitelist"]
    assert c["controller_rating_event"] == expected["controllers_kept_by_whitelist"]  # first sighting
    assert c["controller_frequency_event"] == expected["controllers_kept_by_whitelist"]
    assert c["network_snapshot"] == 1


def test_only_kept_callsigns_stored(db, payload, expected, refdata) -> None:
    apply(db, at(payload), refdata)
    pilots = sorted(r["callsign"] for r in db.execute("SELECT callsign FROM pilot_session").fetchall())
    ctrls = sorted(r["callsign"] for r in db.execute("SELECT callsign FROM controller_session").fetchall())
    assert pilots == expected["pilots_kept_callsigns"]
    assert ctrls == expected["controllers_kept_callsigns"]
    assert not any(c.endswith(("_OBS", "_ATIS")) for c in ctrls)


def test_network_snapshot_counts(db, payload, expected, refdata) -> None:
    apply(db, at(payload), refdata)
    row = db.execute("SELECT * FROM network_snapshot").fetchone()
    assert row["pilots_in_region"] == expected["pilots_kept_by_polygon"]
    assert row["controllers_in_region"] == expected["controllers_kept_by_whitelist"]
    assert row["pilots_global"] == expected["pilots_global"]
    assert row["controllers_global"] == expected["controllers_global"]


def test_observed_at_is_snapshot_time_not_wall_clock(db, payload, refdata) -> None:
    apply(db, at(payload), refdata)
    row = db.execute("SELECT min(observed_at) AS lo, max(observed_at) AS hi FROM pilot_position").fetchone()
    assert row["lo"] == row["hi"]
    lo = row["lo"].astimezone(timezone.utc)  # the session time zone must not matter
    assert (lo.year, lo.month, lo.day) == (2026, 10, 7)


def test_idempotent_second_apply_adds_zero_rows(db, payload, refdata) -> None:
    snapshot = at(payload)
    apply(db, snapshot, refdata)
    before = counts(db)
    q = "SELECT id, first_seen, last_seen, max_gs_kt, max_alt_ft FROM pilot_session ORDER BY id"
    state = db.execute(q).fetchall()
    apply(db, snapshot, refdata)
    assert counts(db) == before
    assert db.execute(q).fetchall() == state


def test_failure_mid_snapshot_leaves_nothing_behind(db, payload, refdata) -> None:
    """SPEC s3: one transaction per snapshot. A failure that is not a bad record (here: no partition for the
    snapshot time) must roll everything back. A bad record is skipped instead: test_poison_records.py."""
    import dataclasses

    from vatpac.model import parse_snapshot
    from vatpac.store import apply_snapshot

    poly, wl = refdata
    snap = parse_snapshot(at(payload))
    broken = dataclasses.replace(snap, observed_at=datetime(2031, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(Exception):
        apply_snapshot(db, broken, poly, wl, polygon_sha="p" * 64, whitelist_sha="w" * 64)
    if not db.autocommit:
        db.rollback()
    assert set(counts(db).values()) == {0}


def test_implausible_values_stored_as_null(db, payload, refdata) -> None:
    apply(db, at(payload), refdata)
    q = (
        "SELECT p.gs_kt, p.alt_ft FROM pilot_position p JOIN pilot_session s ON s.id = p.session_id "
        "WHERE s.callsign = %s"
    )
    row = db.execute(q, ("TESTGS",)).fetchone()
    assert row["gs_kt"] is None and row["alt_ft"] == 5000
    row = db.execute(q, ("TESTALTLOW",)).fetchone()
    assert row["alt_ft"] is None and row["gs_kt"] == 120
    assert db.execute(q, ("TESTALT",)).fetchone()["alt_ft"] == -157


def test_poll_log_row_counts(db, payload, expected, refdata) -> None:
    """SPEC s6 poll_log columns. apply_snapshot returns the counts and main.py writes them with write_poll_log; this
    test does the same two steps and checks every column against the fixture's independently generated numbers."""
    from vatpac.store import write_poll_log

    result = apply(db, at(payload), refdata, polygon_sha="a" * 64, whitelist_sha="b" * 64)
    snap_ts = datetime(2026, 10, 7, 16, 11, 19, tzinfo=timezone.utc)
    write_poll_log(db, "ok", snapshot_ts=snap_ts, duration_ms=1234, http_status=200, result=result,
                   polygon_sha="a" * 64, whitelist_sha="b" * 64)
    row = db.execute("SELECT * FROM poll_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "ok" and row["snapshot_ts"] == snap_ts
    assert row["duration_ms"] == 1234 and row["http_status"] == 200 and row["error"] is None
    assert row["pilots_seen"] == expected["pilots_global"]
    assert row["pilots_inside"] == expected["pilots_kept_by_polygon"]
    assert row["pilots_with_plan"] == len(expected["pilots_passing_flight_gate_inside"])
    assert row["pilots_kept"] == expected["pilots_kept_by_polygon"]
    assert row["controllers_seen"] == expected["controllers_global"]
    assert row["controllers_kept"] == expected["controllers_kept_by_whitelist"]
    assert row["rejected_values"] == expected["rejected_values_expected"]
    assert row["polygon_sha"] == "a" * 64 and row["whitelist_sha"] == "b" * 64


def test_poll_log_rows_for_errors_and_skipped_polls_carry_no_counts(db) -> None:
    from vatpac.store import write_poll_log

    write_poll_log(db, "error", http_status=503, error="x" * 3000, polygon_sha="a" * 64, whitelist_sha="b" * 64)
    write_poll_log(db, "skipped_unchanged", snapshot_ts=datetime(2026, 10, 7, 16, 11, 19, tzinfo=timezone.utc))
    err, skipped = db.execute("SELECT * FROM poll_log ORDER BY id").fetchall()
    assert err["status"] == "error" and err["http_status"] == 503 and len(err["error"]) == 2000
    for row in (err, skipped):
        assert all(row[c] is None for c in ("pilots_seen", "pilots_inside", "pilots_with_plan", "pilots_kept",
                                            "controllers_seen", "controllers_kept", "rejected_values"))
    assert skipped["status"] == "skipped_unchanged" and skipped["error"] is None


def test_reference_data_change_is_recorded_only_when_the_hash_changes(db) -> None:
    from vatpac.store import record_reference_change

    rows = "SELECT file, old_sha, new_sha FROM reference_data_change ORDER BY id"
    assert record_reference_change(db, "polygon.json", "1" * 64) is True        # first sighting: old_sha is empty
    assert record_reference_change(db, "polygon.json", "1" * 64) is False       # same hash: nothing
    assert record_reference_change(db, "whitelist.txt", "1" * 64) is True       # another file has its own history
    assert record_reference_change(db, "polygon.json", "2" * 64) is True        # changed: old_sha is the previous hash
    assert [(r["file"], r["old_sha"], r["new_sha"]) for r in db.execute(rows).fetchall()] == [
        ("polygon.json", None, "1" * 64), ("whitelist.txt", None, "1" * 64), ("polygon.json", "1" * 64, "2" * 64)]


def test_home_icao_and_names_stored(db, payload, refdata) -> None:
    apply(db, at(payload), refdata)
    row = db.execute("SELECT name, home_icao FROM pilot_session WHERE callsign = 'TESTYPDN'").fetchone()
    assert row["name"].endswith("YPDN") and row["home_icao"] == "YPDN"
    row = db.execute("SELECT name, home_icao FROM pilot_session WHERE callsign = 'TESTCIRCUIT'").fetchone()
    assert row["home_icao"] is None


def test_no_frequency_stored_as_null(db, payload, refdata) -> None:
    apply(db, at(payload), refdata)
    q = "SELECT frequency_hz FROM controller_session WHERE callsign = %s"
    assert db.execute(q, ("BN_GND",)).fetchone()["frequency_hz"] is None
    assert db.execute(q, ("ML_APP",)).fetchone()["frequency_hz"] == 132_000_000


def test_near_miss_sighting_recorded(db, payload, refdata) -> None:
    """SPEC s4: near-miss controllers go to controller_unlisted_sighting, never to controller_session."""
    apply(db, at(payload), refdata)
    q = "SELECT callsign, polls_seen FROM controller_unlisted_sighting ORDER BY callsign"
    rows = db.execute(q).fetchall()
    assert [r["callsign"] for r in rows] == ["AD_XYZ_TWR", "ML_XYZ_CTR"]
    apply(db, at(payload, 15), refdata)
    assert [r["polls_seen"] for r in db.execute(q).fetchall()] == [2, 2]
    assert db.execute("SELECT count(*) AS n FROM controller_session WHERE callsign LIKE '%XYZ%'").fetchone()["n"] == 0
