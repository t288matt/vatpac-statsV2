"""Replay saved raw-feed payloads through the collector's parser and write path on a THROWAWAY database (issue 103).

Test tooling only: it lives outside collector/src and is not part of the image.

    # replay files or folders (.json / .json.gz; the feed archive layout is fine), check them, dump the stored rows
    python tests/tools/replay.py run tests/fixtures/replay --dsn postgresql://.../vatpac_test --dump new.json

    # the same payloads through OLDER code (a checkout or an `git archive` of a previous commit) on another database
    python tests/tools/replay.py run tests/fixtures/replay --dsn postgresql://.../other_test \\
        --src /path/to/old/collector/src --dump old.json

    # row-by-row difference between two dumps (exit 1 when they differ)
    python tests/tools/replay.py diff old.json new.json

`run` empties the data tables, so the database name must end in `_test`. It applies every payload in
update_timestamp order, writes the poll_log row the collector would write, then checks each snapshot against an
INDEPENDENT count taken from the raw payload (not from model.py): network_snapshot, poll_log and the stored positions
must agree with what the payload contains. The dump holds natural keys only (no surrogate ids, no clock values), so two
runs of the same payloads on the same code give identical dumps and a code change shows up as a row difference.
"""
from __future__ import annotations

import argparse
import gzip
import inspect
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA_TABLES = ["pilot_position", "pilot_session", "controller_rating_event", "controller_frequency_event",
               "controller_session", "network_snapshot", "poll_log", "controller_unlisted_sighting"]


# ------------------------------------------------------------------------------------------------ loading
def load_payload(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    data = json.loads(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object")
    return data


def _stamp(payload: dict[str, Any]) -> str:
    return str(payload["general"]["update_timestamp"])


def collect(paths: list[Path]) -> list[tuple[str, dict[str, Any]]]:
    """Every .json / .json.gz under the given files or folders as (name, payload), oldest snapshot first."""
    files: list[Path] = []
    for p in paths:
        files += sorted(f for f in p.rglob("*") if f.is_file() and f.name.endswith((".json", ".json.gz"))) \
            if p.is_dir() else [p]
    found = [(f.name, load_payload(f)) for f in files]
    return sorted(found, key=lambda t: _stamp(t[1]))


# ------------------------------------------------------------------------------------------------ the oracle
@dataclass(frozen=True)
class Expected:
    """What a payload contains, counted straight from the raw dicts (a second, independent implementation)."""

    pilots_global: int
    controllers_global: int
    pilot_keys: frozenset[tuple[int, str, str]]  # (cid, callsign, logon second) of pilots inside the region
    pilots_with_plan: int
    controller_keys: frozenset[tuple[int, str, str]]  # whitelisted controllers


def _key(e: dict[str, Any]) -> tuple[int, str, str]:
    return int(e["cid"]), str(e["callsign"]).strip(), str(e["logon_time"])[:19]  # whole seconds, as stored


def expected(payload: dict[str, Any], polygon: Any, whitelist: frozenset[str]) -> Expected:
    import shapely  # a collector dependency

    pilots, ctrls = payload.get("pilots") or [], payload.get("controllers") or []
    keys: set[tuple[int, str, str]] = set()
    plan = 0
    for p in pilots:
        if not (isinstance(p, dict) and str(p.get("callsign") or "").strip()):
            continue
        lat, lon = p.get("latitude"), p.get("longitude")
        if lat is not None and lon is not None and not shapely.contains_xy(polygon, float(lon), float(lat)):
            continue  # no position at all counts as inside (the documented filter rule)
        k = _key(p)
        if k not in keys:
            fp = p.get("flight_plan")
            plan += isinstance(fp, dict) and bool(str(fp.get("departure") or "").strip()) \
                and bool(str(fp.get("arrival") or "").strip())
        keys.add(k)
    ckeys = {_key(c) for c in ctrls if isinstance(c, dict) and str(c.get("callsign") or "").strip() in whitelist}
    return Expected(len(pilots), len(ctrls), frozenset(keys), plan, frozenset(ckeys))


# ------------------------------------------------------------------------------------------------ replaying
@dataclass
class Outcome:
    name: str
    observed_at: datetime
    result: Any  # store.ApplyResult
    snapshot: Any  # model.Snapshot
    exp: Expected


def ensure_partitions(conn: Any, stamps: list[datetime]) -> None:
    for day in {s.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) for s in stamps}:
        conn.execute(f"CREATE TABLE IF NOT EXISTS pilot_position_{day:%Y%m%d} PARTITION OF pilot_position "
                     f"FOR VALUES FROM ('{day.isoformat()}') TO ('{(day + timedelta(days=1)).isoformat()}')")


def replay(conn: Any, payloads: list[tuple[str, dict[str, Any]]], ref: Any) -> list[Outcome]:
    """parse_snapshot + apply_snapshot + write_poll_log for each payload, as one poll would. Nothing is caught: a
    raise here is a finding."""
    from vatpac.model import parse_snapshot, update_timestamp
    from vatpac.store import apply_snapshot, write_poll_log

    ensure_partitions(conn, [update_timestamp(p) for _, p in payloads])
    extra = {"prefixes": ref.prefixes} if "prefixes" in inspect.signature(apply_snapshot).parameters else {}
    out: list[Outcome] = []
    for name, payload in payloads:
        snap = parse_snapshot(payload)
        res = apply_snapshot(conn, snap, ref.polygon, ref.whitelist, polygon_sha=ref.polygon_sha,
                             whitelist_sha=ref.whitelist_sha, **extra)
        write_poll_log(conn, "ok", snapshot_ts=snap.observed_at, result=res, polygon_sha=ref.polygon_sha,
                       whitelist_sha=ref.whitelist_sha)
        out.append(Outcome(name, snap.observed_at, res, snap, expected(payload, ref.polygon, ref.whitelist)))
    return out


def check(conn: Any, outcomes: list[Outcome]) -> list[str]:
    """Problems found (empty = consistent). The stored rows must match the raw payload, per snapshot."""
    bad: list[str] = []
    for o in outcomes:
        ts, e, r = o.observed_at, o.exp, o.result
        net = conn.execute("SELECT * FROM network_snapshot WHERE observed_at = %s", (ts,)).fetchone()
        if net is None:
            bad.append(f"{o.name}: no network_snapshot row")
            continue
        npos = conn.execute("SELECT count(*) AS n FROM pilot_position WHERE observed_at = %s", (ts,)).fetchone()["n"]
        want = (len(e.pilot_keys), len(e.controller_keys), e.pilots_global, e.controllers_global)
        got = (net["pilots_in_region"], net["controllers_in_region"], net["pilots_global"], net["controllers_global"])
        if got != want:
            bad.append(f"{o.name}: network_snapshot {got} but the payload holds {want} "
                       "(in region, controllers, global pilots, global controllers)")
        if npos != net["pilots_in_region"]:
            bad.append(f"{o.name}: {npos} stored positions but pilots_in_region is {net['pilots_in_region']}")
        log = conn.execute("SELECT * FROM poll_log WHERE snapshot_ts = %s ORDER BY id DESC LIMIT 1", (ts,)).fetchone()
        if log is None or log["status"] != "ok":
            bad.append(f"{o.name}: no ok poll_log row")
            continue
        for col, val in (("pilots_seen", e.pilots_global), ("controllers_seen", e.controllers_global),
                         ("pilots_inside", len(e.pilot_keys)), ("pilots_kept", npos),
                         ("controllers_kept", len(e.controller_keys)), ("pilots_with_plan", e.pilots_with_plan)):
            if log[col] != val:
                bad.append(f"{o.name}: poll_log.{col} is {log[col]} but the payload gives {val}")
        if not r.snapshot_inserted:
            bad.append(f"{o.name}: snapshot not reported as inserted")
        stored_utc = {(s["cid"], s["callsign"], s["lt"]) for s in conn.execute(
            "SELECT s.cid, s.callsign, to_char(s.logon_time AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS') AS lt "
            "FROM pilot_position p JOIN pilot_session s ON s.id = p.session_id WHERE p.observed_at = %s",
            (ts,)).fetchall()}
        if stored_utc != set(e.pilot_keys):
            bad.append(f"{o.name}: stored sessions differ from the payload: missing "
                       f"{sorted(set(e.pilot_keys) - stored_utc)[:3]}, extra {sorted(stored_utc - set(e.pilot_keys))[:3]}")
    return bad


# ------------------------------------------------------------------------------------------------ dump and diff
_DUMP_SQL = {
    "pilot_session": "SELECT to_jsonb(s) - 'id' AS r FROM pilot_session s",
    "pilot_position": "SELECT (to_jsonb(p) - 'session_id') || jsonb_build_object('cid', s.cid, 'callsign', s.callsign, "
                      "'logon_time', s.logon_time) AS r FROM pilot_position p JOIN pilot_session s ON s.id = p.session_id",
    "controller_session": "SELECT to_jsonb(s) - 'id' AS r FROM controller_session s",
    "controller_rating_event": "SELECT (to_jsonb(e) - 'session_id') || jsonb_build_object('cid', s.cid, 'callsign', "
                               "s.callsign, 'logon_time', s.logon_time) AS r FROM controller_rating_event e "
                               "JOIN controller_session s ON s.id = e.session_id",
    "controller_frequency_event": "SELECT (to_jsonb(e) - 'session_id') || jsonb_build_object('cid', s.cid, 'callsign', "
                                  "s.callsign, 'logon_time', s.logon_time) AS r FROM controller_frequency_event e "
                                  "JOIN controller_session s ON s.id = e.session_id",
    "network_snapshot": "SELECT to_jsonb(n) AS r FROM network_snapshot n",
    "controller_unlisted_sighting": "SELECT to_jsonb(u) AS r FROM controller_unlisted_sighting u",
    "poll_log": "SELECT to_jsonb(l) - 'id' - 'polled_at' - 'duration_ms' AS r FROM poll_log l",
}


def dump_state(conn: Any) -> dict[str, list[str]]:
    """Every data row as a canonical JSON string, sorted, without surrogate ids or clock values."""
    previous = conn.execute("SHOW TimeZone").fetchone()["TimeZone"]
    conn.execute("SET TIME ZONE 'UTC'")  # timestamps render in the session zone: pin it so dumps compare
    try:
        return {t: sorted(json.dumps(r["r"], sort_keys=True) for r in conn.execute(sql).fetchall())
                for t, sql in _DUMP_SQL.items()}
    finally:
        conn.execute("SELECT set_config('TimeZone', %s, false)", (previous,))


def diff_dumps(a: dict[str, list[str]], b: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for table in sorted(set(a) | set(b)):
        ra, rb = a.get(table, []), b.get(table, [])
        if ra == rb:
            continue
        sa, sb = set(ra), set(rb)
        out.append(f"{table}: {len(ra)} rows vs {len(rb)} rows; only in A {len(sa - sb)}, only in B {len(sb - sa)}")
        out += [f"  A> {r[:300]}" for r in sorted(sa - sb)[:3]] + [f"  B> {r[:300]}" for r in sorted(sb - sa)[:3]]
    return out


# ------------------------------------------------------------------------------------------------ command line
def _run(args: argparse.Namespace) -> int:
    if args.src:
        sys.path.insert(0, str(Path(args.src).resolve()))
    else:
        sys.path.insert(0, str(ROOT / "collector" / "src"))
    import psycopg
    from psycopg.rows import dict_row
    from vatpac.filters import load_reference

    ref = load_reference(ROOT / "config" / "australian_airspace_polygon.json",
                         ROOT / "config" / "controller_callsigns_list.txt")
    payloads = collect([Path(p) for p in args.paths])
    if not payloads:
        print("no payload files found", file=sys.stderr)
        return 2
    with psycopg.connect(args.dsn, autocommit=True, row_factory=dict_row) as conn:
        name = conn.execute("SELECT current_database() AS d").fetchone()["d"]
        if not name.endswith("_test"):
            print(f"refusing to empty database {name!r}: the name must end in _test", file=sys.stderr)
            return 2
        for part in conn.execute("SELECT inhrelid::regclass::text AS t FROM pg_inherits "
                                 "WHERE inhparent = 'pilot_position'::regclass").fetchall():
            conn.execute(f"DROP TABLE IF EXISTS {part['t']}")
        conn.execute("TRUNCATE " + ", ".join(DATA_TABLES) + " RESTART IDENTITY CASCADE")
        outcomes = replay(conn, payloads, ref)
        problems = check(conn, outcomes)
        if args.dump:
            Path(args.dump).write_text(json.dumps(dump_state(conn)), encoding="utf-8")
    for o in outcomes:
        r = o.result
        print(f"{o.name} {o.observed_at:%Y-%m-%dT%H:%M:%SZ}: pilots {r.pilots_seen} inside {r.pilots_inside} "
              f"kept {r.pilots_kept} controllers kept {r.controllers_kept} rejected {r.rejected_values}")
    for p in problems:
        print("PROBLEM:", p)
    print(f"{len(outcomes)} snapshots replayed, {len(problems)} problems")
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="replay payloads into a throwaway database and check them")
    run.add_argument("paths", nargs="+")
    run.add_argument("--dsn", required=True)
    run.add_argument("--src", help="collector/src of the code to test (default: this checkout)")
    run.add_argument("--dump", help="write the stored rows to this JSON file")
    d = sub.add_parser("diff", help="compare two dumps")
    d.add_argument("a")
    d.add_argument("b")
    args = ap.parse_args()
    if args.cmd == "run":
        return _run(args)
    lines = diff_dumps(json.loads(Path(args.a).read_text("utf-8")), json.loads(Path(args.b).read_text("utf-8")))
    print("\n".join(lines) or "identical")
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main())
