"""Replay real raw-feed payloads through parse_snapshot and apply_snapshot (issue 103).

The committed set (tests/fixtures/replay, two cut-down real snapshots: one full, one compact) plus the anonymised
sample are replayed with tests/tools/replay.py. Real data is the witness that does not share the authors' blind spots:
nothing may raise, every snapshot must be stored, and network_snapshot, poll_log and the stored positions must agree
with a count taken straight from the raw payload (not from model.py).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "tools"))
import replay as rp  # noqa: E402

from vatpac.filters import load_reference  # noqa: E402

pytestmark = pytest.mark.integration

FIXTURES = REPO / "tests" / "fixtures"
SET = [FIXTURES / "replay", FIXTURES / "vatsim-data.sample.json"]


@pytest.fixture(scope="module")
def ref():
    return load_reference(REPO / "config" / "australian_airspace_polygon.json",
                          REPO / "config" / "controller_callsigns_list.txt")


@pytest.fixture()
def payloads():
    found = rp.collect(SET)
    assert len(found) == 3
    return found


def test_replay_stores_every_snapshot_and_the_counts_match_the_payload(conn, ref, payloads) -> None:
    outcomes = rp.replay(conn, payloads, ref)                    # a raise here fails the test
    assert [o.observed_at for o in outcomes] == sorted(o.observed_at for o in outcomes) and len(outcomes) == 3
    assert rp.check(conn, outcomes) == []
    assert all(o.result.snapshot_inserted for o in outcomes)
    assert conn.execute("SELECT count(*) AS n FROM network_snapshot").fetchone()["n"] == 3
    assert conn.execute("SELECT count(*) AS n FROM poll_log WHERE status = 'ok'").fetchone()["n"] == 3
    # the real set is not trivial: pilots inside, whitelisted controllers, and pilots with and without a plan
    full = next(o for o in outcomes if o.name == "real_full.json.gz")
    assert full.result.pilots_kept >= 20 and full.result.controllers_kept >= 1 and full.result.pilots_with_plan >= 1
    assert full.result.pilots_seen == full.exp.pilots_global > full.result.pilots_inside


def test_real_data_is_not_rejected(conn, ref, payloads) -> None:
    """Clean real feed entries lose no values to the plausibility caps (a change that starts rejecting them shows here)."""
    outcomes = rp.replay(conn, payloads, ref)
    real = [o for o in outcomes if o.name.startswith("real_")]
    assert len(real) == 2 and [o.result.rejected_values for o in real] == [0, 0]
    assert all(o.snapshot.malformed == 0 for o in outcomes)


def test_replaying_the_same_payloads_again_changes_nothing(conn, ref, payloads) -> None:
    outcomes = rp.replay(conn, payloads, ref)
    before = rp.dump_state(conn)
    from vatpac.model import parse_snapshot
    from vatpac.store import apply_snapshot
    for _, payload in payloads:
        res = apply_snapshot(conn, parse_snapshot(payload), ref.polygon, ref.whitelist, polygon_sha=ref.polygon_sha,
                             whitelist_sha=ref.whitelist_sha)
        assert res.positions_inserted == 0 and res.snapshot_inserted is False
    assert rp.dump_state(conn) == before
    assert len(outcomes) == 3


def test_two_snapshots_of_one_flight_make_one_session_with_two_positions(conn, ref, payloads) -> None:
    """Real continuity: the full and the compact file share pilots, so a session grows a second position."""
    rp.replay(conn, [(n, p) for n, p in payloads if n.startswith("real_")], ref)
    row = conn.execute("SELECT count(*) AS shared FROM (SELECT session_id FROM pilot_position GROUP BY session_id "
                       "HAVING count(*) = 2) t").fetchone()
    assert row["shared"] >= 10
    assert conn.execute("SELECT count(*) AS n FROM (SELECT cid, callsign, logon_time FROM pilot_session "
                        "GROUP BY 1, 2, 3 HAVING count(*) > 1) t").fetchone()["n"] == 0


def test_the_stored_time_zone_does_not_change_the_dump(conn, ref, payloads) -> None:
    rp.replay(conn, payloads, ref)
    utc = rp.dump_state(conn)
    conn.execute("SET TIME ZONE 'Australia/Sydney'")
    try:
        assert rp.dump_state(conn) == utc
        assert conn.execute("SHOW TimeZone").fetchone()["TimeZone"] == "Australia/Sydney"   # and the zone is put back
    finally:
        conn.execute("RESET TIME ZONE")


def test_check_finds_a_stored_row_that_disagrees_with_the_payload(conn, ref, payloads) -> None:
    """The checker itself must be able to fail: damage the stored rows three ways and it must say so."""
    outcomes = rp.replay(conn, payloads, ref)
    assert rp.check(conn, outcomes) == []
    conn.execute("UPDATE network_snapshot SET pilots_in_region = pilots_in_region + 1 WHERE observed_at = %s",
                 (outcomes[0].observed_at,))
    conn.execute("UPDATE poll_log SET pilots_seen = pilots_seen - 1 WHERE snapshot_ts = %s", (outcomes[1].observed_at,))
    conn.execute("DELETE FROM pilot_position WHERE observed_at = %s AND session_id = "
                 "(SELECT min(session_id) FROM pilot_position WHERE observed_at = %s)",
                 (outcomes[2].observed_at, outcomes[2].observed_at))
    problems = rp.check(conn, outcomes)
    assert any("network_snapshot" in p for p in problems)
    assert any("poll_log.pilots_seen" in p for p in problems)
    assert any("stored sessions differ" in p for p in problems)


# ---- the command line: old code against new code, rows diffed
def _cli(*args: str, dsn: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(REPO / "tests" / "tools" / "replay.py"), *args],
                          capture_output=True, text=True, timeout=300, env=os.environ.copy())


def test_cli_runs_twice_and_the_dumps_are_identical(conn, dsn, tmp_path) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    for out in (a, b):
        done = _cli("run", str(SET[0]), "--dsn", dsn, "--dump", str(out), dsn=dsn)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "2 snapshots replayed, 0 problems" in done.stdout
    diff = _cli("diff", str(a), str(b), dsn=dsn)
    assert diff.returncode == 0 and diff.stdout.strip() == "identical"
    assert json.loads(a.read_text())["network_snapshot"]


def test_cli_diff_reports_a_changed_row(conn, dsn, tmp_path) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    assert _cli("run", str(SET[0]), "--dsn", dsn, "--dump", str(a), dsn=dsn).returncode == 0
    changed = json.loads(a.read_text())
    changed["pilot_session"][0] = changed["pilot_session"][0].replace('"callsign": "', '"callsign": "X', 1)
    changed["network_snapshot"] = changed["network_snapshot"][:-1]
    b.write_text(json.dumps(changed))
    diff = _cli("diff", str(a), str(b), dsn=dsn)
    assert diff.returncode == 1
    assert "pilot_session" in diff.stdout and "network_snapshot: 2 rows vs 1 rows" in diff.stdout


def test_cli_refuses_a_database_that_does_not_end_in_test(dsn) -> None:
    parts = urlsplit(dsn)
    other = urlunsplit(parts._replace(path="/postgres"))          # a database that exists and does not end in _test
    done = _cli("run", str(SET[0]), "--dsn", other, dsn=dsn)
    assert done.returncode == 2 and "must end in _test" in done.stderr
    assert "snapshots replayed" not in done.stdout
