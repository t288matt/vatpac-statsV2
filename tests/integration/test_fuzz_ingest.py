"""Fuzz the whole ingest path against the database (issue 103): parse_snapshot, then apply_snapshot, then poll_log.

Seeded variants of the real payload each carry ONE bad entry (or several). The property: whatever the entry holds, the
snapshot is stored, every other entry that is inside the region / on the whitelist is stored with it, the counts
agree, and the bad entry is either stored (its bad values nulled and counted) or skipped and counted. Cases that get
past the parser and break the database (a cid outside bigint) are the ones only this test can see: they must be
bisected out by store._write without losing the rest.
"""
from __future__ import annotations

import copy
import gzip
import json
import random
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "tests" / "tools") not in sys.path:
    sys.path.insert(0, str(REPO / "tests" / "tools"))
import fuzz_payloads as fz  # noqa: E402
import replay as rp  # noqa: E402

from vatpac.filters import inside_polygon, load_reference  # noqa: E402
from vatpac.model import parse_snapshot, update_timestamp  # noqa: E402
from vatpac.store import apply_snapshot, write_poll_log  # noqa: E402

pytestmark = pytest.mark.integration

SEED = 1030
CASES = 160
_LOGON = "to_char(s.logon_time AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS')"


@pytest.fixture(scope="module")
def ref():
    return load_reference(REPO / "config" / "australian_airspace_polygon.json",
                          REPO / "config" / "controller_callsigns_list.txt")


@pytest.fixture(scope="module")
def base() -> dict[str, Any]:
    data = json.loads(gzip.decompress((REPO / "tests" / "fixtures" / "replay" / "real_full.json.gz").read_bytes()))
    assert isinstance(data, dict)
    return data


@pytest.fixture()
def db(conn, base):
    rp.ensure_partitions(conn, [update_timestamp(base)])
    return conn


def _shift(payload: dict[str, Any], n: int) -> dict[str, Any]:
    """The payload with its own snapshot time, 30 s apart per case, so cases never collide."""
    out = copy.deepcopy(payload)
    stamp = update_timestamp(payload) + timedelta(seconds=30 * (n + 1))
    out["general"]["update_timestamp"] = stamp.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return out


def _ingest(conn, ref, payload: dict[str, Any]):
    snap = parse_snapshot(payload)
    rp.ensure_partitions(conn, [snap.observed_at])  # the whole-grid test runs past midnight (30 s per case)
    res = apply_snapshot(conn, snap, ref.polygon, ref.whitelist, polygon_sha=ref.polygon_sha,
                         whitelist_sha=ref.whitelist_sha)
    write_poll_log(conn, "ok", snapshot_ts=snap.observed_at, result=res, polygon_sha=ref.polygon_sha,
                   whitelist_sha=ref.whitelist_sha)
    return snap, res


def _stored(conn, ts) -> tuple[set[tuple[int, str, str]], set[tuple[int, str, str]]]:
    pil = conn.execute(f"SELECT s.cid, s.callsign, {_LOGON} AS lt FROM pilot_position p JOIN pilot_session s "
                       "ON s.id = p.session_id WHERE p.observed_at = %s", (ts,)).fetchall()
    ctl = conn.execute(f"SELECT s.cid, s.callsign, {_LOGON} AS lt FROM controller_session s WHERE s.last_seen = %s",
                       (ts,)).fetchall()
    return {(r["cid"], r["callsign"], r["lt"]) for r in pil}, {(r["cid"], r["callsign"], r["lt"]) for r in ctl}


def _first_inside(base, ref) -> int:
    return next(i for i, p in enumerate(base["pilots"]) if inside_polygon(ref.polygon, p["latitude"], p["longitude"]))


def _check_snapshot(conn, ref, base, payload, bad: dict[str, set[int]], where: str) -> tuple[Any, Any]:
    """The property, for a payload built from `base` where only the entries listed in `bad` were changed."""
    ts = update_timestamp(payload)
    snap, res = _ingest(conn, ref, payload)                              # nothing may raise
    good = copy.deepcopy(base)
    for section in fz.SECTIONS:
        good[section] = [e for i, e in enumerate(good[section]) if i not in bad[section]]
    want = rp.expected(good, ref.polygon, ref.whitelist)                 # counted from the raw dicts, not model.py
    pilots, ctrls = _stored(conn, ts)
    assert want.pilot_keys <= pilots, f"{where}: good pilots lost: {sorted(want.pilot_keys - pilots)[:3]}"
    assert want.controller_keys <= ctrls, f"{where}: good controllers lost: {sorted(want.controller_keys - ctrls)[:3]}"
    assert len(pilots - want.pilot_keys) <= len(bad["pilots"]), where    # nothing stored beyond the bad entries
    assert len(ctrls - want.controller_keys) <= len(bad["controllers"]), where
    net = conn.execute("SELECT * FROM network_snapshot WHERE observed_at = %s", (ts,)).fetchone()
    assert net is not None, f"{where}: snapshot not stored"
    npos = conn.execute("SELECT count(*) AS n FROM pilot_position WHERE observed_at = %s", (ts,)).fetchone()["n"]
    assert net["pilots_in_region"] == npos == res.pilots_kept, where
    assert net["controllers_in_region"] == res.controllers_kept == len(ctrls), where
    assert (net["pilots_global"], net["controllers_global"]) == (len(payload["pilots"]), len(payload["controllers"])), where
    log = conn.execute("SELECT * FROM poll_log WHERE snapshot_ts = %s", (ts,)).fetchone()
    assert (log["pilots_seen"], log["pilots_kept"], log["controllers_kept"], log["rejected_values"]) == (
        len(payload["pilots"]), npos, len(ctrls), res.rejected_values), where
    # every skipped, nulled or database-refused value is counted: at least the parser's skips plus the refused pilots
    assert res.rejected_values >= snap.malformed + (res.pilots_inside - res.pilots_kept), where
    # stored VALUES of every good entry, worked out from the raw dicts (not from model.py)
    problems = rp.value_problems(conn, ts, want.pilot_values, where)
    assert problems == [], problems[:2]
    for key, (rating, facility, hz) in want.controller_values.items():
        row = conn.execute(f"SELECT s.rating, s.facility, s.frequency_hz FROM controller_session s WHERE s.cid = %s "
                           f"AND s.callsign = %s AND {_LOGON} = %s", key).fetchone()
        assert (row["rating"], row["facility"], row["frequency_hz"]) == (rating, facility, hz), f"{where}: {key}"
    # a second application of the same snapshot changes nothing, even with records the database rejects
    again = apply_snapshot(conn, snap, ref.polygon, ref.whitelist, polygon_sha=ref.polygon_sha,
                           whitelist_sha=ref.whitelist_sha)
    assert again.positions_inserted == 0 and again.snapshot_inserted is False, where
    return snap, res


def test_one_bad_entry_never_loses_the_rest_of_the_snapshot(db, ref, base) -> None:
    inside = {i for i, p in enumerate(base["pilots"]) if inside_polygon(ref.polygon, p["latitude"], p["longitude"])}
    listed = {i for i, c in enumerate(base["controllers"]) if c["callsign"] in ref.whitelist}
    cases = list(fz.sample_cases(SEED, base, inside, listed, CASES))
    assert len(cases) >= CASES - 2
    skipped_by_parser = rejected_by_database = kept = 0
    for n, (section, i, label, replacement) in enumerate(cases):
        payload = _shift(fz.wire(fz.replace_entry(base, section, i, replacement)), n)
        bad: dict[str, set[int]] = {"pilots": set(), "controllers": set()}
        bad[section].add(i)
        snap, res = _check_snapshot(db, ref, base, payload, bad, f"{section}[{i}] {label}")
        skipped_by_parser += snap.malformed
        rejected_by_database += res.pilots_inside - res.pilots_kept       # parsed, then refused by Postgres
        kept += snap.malformed == 0
    # the cases must reach all three outcomes, or the property is only being tested on one of them
    assert skipped_by_parser >= 10 and kept >= 10, (skipped_by_parser, kept)
    print(f"fuzz cases {CASES}: parser skipped {skipped_by_parser}, database rejected {rejected_by_database}, kept {kept}")


def test_several_bad_entries_in_one_snapshot(db, ref, base) -> None:
    rng = random.Random(SEED + 7)
    for n in range(CASES, CASES + 25):
        payload = fz.wire(base)
        bad: dict[str, set[int]] = {"pilots": set(), "controllers": set()}
        for section, k in (("pilots", 5), ("controllers", 3)):
            for i in rng.sample(range(len(base[section])), k):
                _, rep = rng.choice(list(fz.entry_mutations(section, base[section][i])))
                payload[section][i] = rep
                bad[section].add(i)
        _check_snapshot(db, ref, base, _shift(fz.wire(payload), n), bad, f"multi case {n}")


# ---- named cases that reach the database layer (the parser accepts them; Postgres does not)
@pytest.mark.parametrize("label,cid", [("above_bigint", 2**63), ("far_above", 10**30), ("negative_far", -(10**30))])
def test_a_cid_the_database_cannot_hold_is_bisected_out_and_counted(db, ref, base, label, cid) -> None:
    i = _first_inside(base, ref)
    payload = _shift(fz.replace_entry(base, "pilots", i, {**base["pilots"][i], "cid": cid}), 900)
    _check_snapshot(db, ref, base, payload, {"pilots": {i}, "controllers": set()}, label)
    assert db.execute("SELECT rejected_values FROM poll_log").fetchone()["rejected_values"] >= 1   # counted, not silent


def test_a_fractional_logon_time_is_cut_to_the_second_before_the_table_check(db, ref, base) -> None:
    """The parser truncates to whole seconds, so the whole-second CHECK is never what rejects a real feed entry."""
    i = _first_inside(base, ref)
    p = {**base["pilots"][i], "logon_time": "2026-10-10T03:06:03.9999999Z"}
    snap, res = _ingest(db, ref, _shift(fz.replace_entry(base, "pilots", i, p), 901))
    assert snap.malformed == 0 and res.pilots_kept == res.pilots_inside
    got = db.execute(f"SELECT {_LOGON} AS lt FROM pilot_session s WHERE callsign = %s", (p["callsign"],)).fetchone()
    assert got["lt"] == "2026-10-10T03:06:03"


def test_duplicate_sessions_in_one_snapshot_store_one_position(db, ref, base) -> None:
    i = _first_inside(base, ref)
    payload = _shift(copy.deepcopy(base), 902)
    payload["pilots"].append(copy.deepcopy(payload["pilots"][i]))
    payload["pilots"].append({**payload["pilots"][i], "latitude": -33.0, "longitude": 151.0})
    snap, res = _ingest(db, ref, payload)
    npos = db.execute("SELECT count(*) AS n FROM pilot_position WHERE observed_at = %s", (snap.observed_at,)).fetchone()["n"]
    net = db.execute("SELECT pilots_in_region FROM network_snapshot WHERE observed_at = %s", (snap.observed_at,)).fetchone()
    assert npos == net["pilots_in_region"] == res.pilots_kept


@pytest.mark.parametrize("label", [
    "long_random_cjk", "long_text", "surrogate", "nul"])
def test_an_oversized_or_odd_pilot_callsign_never_loses_the_snapshot(db, ref, base, label) -> None:
    """The callsign is part of a unique index: text far over a btree row (about 2700 bytes) must not abort the apply."""
    hot = _first_inside(base, ref)
    payload = _shift(fz.replace_entry(base, "pilots", hot, {**base["pilots"][hot], "callsign": dict(fz.BAD_VALUES)[label]}), 903)
    _check_snapshot(db, ref, base, payload, {"pilots": {hot}, "controllers": set()}, f"pilots {label}")


@pytest.mark.slow_fuzz
def test_every_fault_on_one_listed_controller_and_one_inside_pilot(db, ref, base) -> None:
    """The whole grid (not a sample) for one whitelisted controller and one pilot inside the region: every key, every
    bad value, every flight_plan variant. CI runs it; skip locally with -m "not slow_fuzz"."""
    ci = next(i for i, c in enumerate(base["controllers"]) if c["callsign"] in ref.whitelist)
    n = 2000
    for section, i in (("controllers", ci), ("pilots", _first_inside(base, ref))):
        for label, replacement in fz.entry_mutations(section, base[section][i]):
            n += 1
            payload = _shift(fz.wire(fz.replace_entry(base, section, i, replacement)), n)
            bad: dict[str, set[int]] = {"pilots": set(), "controllers": set()}
            bad[section].add(i)
            _check_snapshot(db, ref, base, payload, bad, f"{section}[{i}] {label}")


@pytest.mark.parametrize("value", ["inf", "Infinity", "-Infinity", float("inf"), "1e999999", "1E+999999999", "-1e999999"])
def test_a_listed_controller_with_an_infinite_frequency_never_loses_the_snapshot(db, ref, base, value) -> None:
    """Issue 114: a whitelisted controller's frequency "inf" must not raise OverflowError out of the apply."""
    ci = next(i for i, c in enumerate(base["controllers"]) if c["callsign"] in ref.whitelist)
    payload = _shift(fz.replace_entry(base, "controllers", ci, {**base["controllers"][ci], "frequency": value}), 905)
    _check_snapshot(db, ref, base, payload, {"pilots": set(), "controllers": {ci}}, f"frequency {value!r}")


def test_a_padded_callsign_is_the_same_session_as_the_plain_one(db, ref, base) -> None:
    """' X ' collapses to 'X': one session, two positions (removing the strip in the parser would store two sessions)."""
    i = _first_inside(base, ref)
    plain = base["pilots"][i]["callsign"]
    ts = []
    for n, callsign in enumerate((plain, f"  {plain} 	")):
        payload = _shift(fz.replace_entry(base, "pilots", i, {**base["pilots"][i], "callsign": callsign}), 910 + n)
        _, res = _ingest(db, ref, payload)
        ts.append(update_timestamp(payload))
        assert res.pilots_kept == res.pilots_inside
    rows = db.execute("SELECT count(*) AS n, count(DISTINCT session_id) AS s FROM pilot_position p JOIN pilot_session q "
                      "ON q.id = p.session_id WHERE q.cid = %s", (base["pilots"][i]["cid"],)).fetchone()
    assert (rows["n"], rows["s"]) == (2, 1)
    assert db.execute("SELECT count(*) AS n FROM pilot_session WHERE callsign <> btrim(callsign)").fetchone()["n"] == 0
