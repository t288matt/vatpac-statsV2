#!/usr/bin/env python3
"""Runner for the #138 comparisons. Read only. The owner runs it; no VM access from here.

  python run_comparisons.py a --from-ts '2026-10-07 21:38+00' --to-ts '2026-10-12 00:00+00' --out outA
  python run_comparisons.py b --out outB \
        --from-ts '2026-10-08 07:52+00' --to-ts '2026-10-10 18:00+00' \
        --gap-from '2026-10-09 15:30+00' --gap-to '2026-10-09 15:50+00'
  python run_comparisons.py b --offline outB          # re-match saved CSVs, no database
  python run_comparisons.py selftest                  # synthetic checks of the matcher, no database

Connections come from the environment (never printed, never passed on the command line):
  V3_DSN, V1_DSN   libpq connection strings or URIs; passwords belong in ~/.pgpass or PGPASSWORD.
Each query is linted (SELECT / WITH only, no write keywords) and run in a read-only session with a
statement timeout. Standard library only; needs the psql client for the database modes.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|do|vacuum|analyze|"
    r"refresh|lock|set|reset|begin|commit|rollback)\b", re.I)


def split_queries(path: Path) -> dict[str, str]:
    """Split a .sql file on '-- @@QUERY name' markers."""
    out: dict[str, str] = {}
    name = None
    buf: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"--\s*@@QUERY\s+(\w+)", line)
        if m:
            if name:
                out[name] = "\n".join(buf).strip()
            name, buf = m.group(1), []
        elif name:
            buf.append(line)
    if name:
        out[name] = "\n".join(buf).strip()
    return out


def lint(sql: str) -> None:
    """Refuse anything that is not a single SELECT / WITH statement."""
    bare = re.sub(r"--[^\n]*", "", sql)
    bare = re.sub(r"'(?:[^']|'')*'", "''", bare)  # drop string literals, including :'name' values
    bare = bare.replace(":''", "")
    body = bare.strip().rstrip(";")
    if ";" in body:
        raise SystemExit("lint: more than one statement")
    if not re.match(r"(select|with)\b", body, re.I):
        raise SystemExit("lint: statement must start with SELECT or WITH")
    hit = FORBIDDEN.search(body.replace("FOR UPDATE", ""))
    if hit and hit.group(1).lower() not in {"do"}:  # 'do' never appears as a word in these files
        raise SystemExit(f"lint: forbidden keyword {hit.group(1)!r}")


def run_psql(dsn_env: str, sql: str, variables: dict[str, str], timeout_s: int) -> list[dict[str, str]]:
    dsn = os.environ.get(dsn_env)
    if not dsn:
        raise SystemExit(f"{dsn_env} is not set")
    lint(sql)
    cmd = ["psql", "-X", "-q", "--csv", "-v", "ON_ERROR_STOP=1", dsn, "-f", "-"]
    for k, v in variables.items():
        cmd[1:1] = ["-v", f"{k}={v}"]
    env = dict(os.environ)
    env["PGOPTIONS"] = (f"-c default_transaction_read_only=on -c statement_timeout={timeout_s}s "
                        "-c idle_in_transaction_session_timeout=60s")
    proc = subprocess.run(cmd, input=sql, text=True, capture_output=True, env=env, check=False)
    if proc.returncode != 0:
        # stderr from psql can echo the query but never the DSN; still trim it.
        raise SystemExit(f"psql failed ({dsn_env}): {proc.stderr.strip()[:2000]}")
    return list(csv.DictReader(io.StringIO(proc.stdout)))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------- comparison B matcher

def norm_air(x: str | None) -> str:
    return (x or "").strip(" \t\r\n").upper()


def ts(x: str) -> datetime:
    x = x.strip().replace(" ", "T")
    if x.endswith("+00"):
        x += ":00"
    d = datetime.fromisoformat(x)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass
class Result:
    exact: list
    v1_only: list          # (v1 row, reason)
    v3_only: list          # (v3 row, reason)
    split: list            # (v1 row, [v3 rows])
    merged: list           # (v3 row, [v1 rows])
    v1_total: int
    v3_total: int


def match(v3: list[dict], v1: list[dict]) -> Result:
    """Match on (cid, callsign, departure, arrival, whole-second logon_time), v1 text normalised."""
    def key3(r): return (int(r["cid"]), r["callsign"], norm_air(r["departure"]), norm_air(r["arrival"]), ts(r["logon_time"]))
    def key1(r): return key3(r)
    by3: dict = defaultdict(list)
    by1: dict = defaultdict(list)
    for r in v3: by3[key3(r)].append(r)
    for r in v1: by1[key1(r)].append(r)

    exact, merged = [], []
    used3, used1 = set(), set()
    for k, rows1 in by1.items():
        rows3 = by3.get(k, [])
        if len(rows1) == 1 and len(rows3) == 1:
            exact.append((rows1[0], rows3[0])); used1.add(k); used3.add(k)
        elif rows3 and len(rows1) > 1:
            merged.append((rows3[0], rows1)); used1.add(k); used3.add(k)

    # splits: a v1 row with no exact key, covered by two or more v3 flights of the same cid/callsign/airports
    split, v1_only = [], []
    for k, rows1 in by1.items():
        if k in used1: continue
        for r1 in rows1:
            cid, cs, dep, arr, logon = key1(r1)
            end1 = ts(r1["completion_time"])
            parts = [r3 for kk, rs in by3.items() if kk not in used3 or True for r3 in rs
                     if kk[0] == cid and kk[1] == cs and kk[2] == dep and kk[3] == arr
                     and ts(r3["start_time"]) >= logon and ts(r3["start_time"]) <= end1]
            if len(parts) >= 2:
                split.append((r1, parts)); continue
            cousins = [r3 for rs in by3.values() for r3 in rs
                       if int(r3["cid"]) == cid and r3["callsign"] == cs
                       and ts(r3["logon_time"]) - logon <= (ts(r3["end_time"]) - ts(r3["logon_time"]))
                       and logon <= ts(r3["end_time"]) and ts(r3["logon_time"]) <= end1
                       and (norm_air(r3["departure"]), norm_air(r3["arrival"])) != (dep, arr)]
            if cousins:
                v1_only.append((r1, "amended plan: v3 holds the same pilot and callsign under a different departure or arrival (expected, E5)"))
            else:
                v1_only.append((r1, "UNEXPLAINED: no v3 flight for this pilot and callsign overlapping it"))

    v3_only = []
    v1_keys_by_pilot = defaultdict(list)
    for r in v1: v1_keys_by_pilot[(int(r["cid"]), r["callsign"])].append(r)
    for k, rows3 in by3.items():
        if k in used3: continue
        for r3 in rows3:
            if any(r3 is p for _, parts in split for p in parts):
                continue  # counted under the split
            cid, cs = int(r3["cid"]), r3["callsign"]
            near = [r1 for r1 in v1_keys_by_pilot.get((cid, cs), [])
                    if ts(r1["logon_time"]) <= ts(r3["end_time"]) and ts(r1["completion_time"]) >= ts(r3["start_time"])]
            if near:
                v3_only.append((r3, "v1 holds this pilot and callsign overlapping it under another key (check by hand)"))
            else:
                v3_only.append((r3, "no v1 summary overlapping it (the design lists 3 of these, unexplained)"))
    return Result(exact, v1_only, v3_only, split, merged, len(v1), len(v3))


def report_b(res: Result, expect: dict[str, int], known_v3_only: set | None) -> int:
    lines = [
        "COMPARISON B: flight_record against v1 flight_summaries",
        f"v1 rows {res.v1_total}   v3 rows {res.v3_total}",
        f"exact 1-to-1 matches   {len(res.exact)}   (expect {expect['exact']})",
        f"v1 flights split by v3 {len(res.split)}   (expect 0)",
        f"v3 flights merging several v1 rows {len(res.merged)}   (expect 0)",
        f"v1-only {len(res.v1_only)}   (expect {expect['v1_only']}, all amended plans)",
        f"v3-only {len(res.v3_only)}   (expect {expect['v3_only']})",
        "",
    ]
    failures = []
    unexplained = [r for r, why in res.v1_only if why.startswith("UNEXPLAINED")]
    if len(res.exact) != expect["exact"]: failures.append("exact matches differ from expectation")
    if res.v1_total != expect["v1_total"]: failures.append(f"v1 total is {res.v1_total}, expected {expect['v1_total']}")
    if res.v3_total != expect["v3_total"]: failures.append(f"v3 total is {res.v3_total}, expected {expect['v3_total']}")
    if res.split: failures.append("v1 flights split by v3")
    if res.merged: failures.append("v3 merges several v1 rows")
    if len(res.v1_only) != expect["v1_only"]: failures.append("v1-only count differs")
    if unexplained: failures.append(f"{len(unexplained)} v1-only flights are not amended plans")
    if len(res.v3_only) != expect["v3_only"]: failures.append("v3-only count differs")
    if known_v3_only is not None:
        got = {(int(r["cid"]), r["callsign"], norm_air(r["departure"]), norm_air(r["arrival"]), ts(r["logon_time"]).isoformat())
               for r, _ in res.v3_only}
        if got != known_v3_only: failures.append("v3-only flights differ from the owner's list of 3")
    for title, rows in (("v1-only", [(r, w) for r, w in res.v1_only]), ("v3-only", res.v3_only)):
        lines.append(f"--- {title} ---")
        for r, why in rows:
            lines.append(f"  {r['cid']} {r['callsign']} {r['departure']}-{r['arrival']} logon {r['logon_time']} : {why}")
    for r1, parts in res.split:
        lines.append(f"  SPLIT v1 {r1['cid']} {r1['callsign']} logon {r1['logon_time']} -> {len(parts)} v3 flights")
    lines.append("")
    lines.append("RESULT: " + ("PASS" if not failures else "FAIL: " + "; ".join(failures)))
    print("\n".join(lines))
    return 0 if not failures else 1


# ---------------------------------------------------------------- self test

def _t(m: int) -> str:
    return f"2026-10-09 {m // 60:02d}:{m % 60:02d}:00+00"


def selftest() -> int:
    def f3(cid, cs, dep, arr, lg, st, en): return dict(cid=str(cid), callsign=cs, departure=dep, arrival=arr,
                                                       logon_time=_t(lg), start_time=_t(st), end_time=_t(en),
                                                       session_id="1", session_count="1", sample_count="10")
    def f1(cid, cs, dep, arr, lg, co): return dict(v1_id="1", cid=str(cid), callsign=cs, departure=dep, arrival=arr,
                                                   logon_time=_t(lg), completion_time=_t(co))
    v3 = [f3(1, "QFA1", "YSSY", "YMML", 60, 61, 120),          # exact (v1 text padded, lower case)
          f3(2, "VOZ2", "YSSY", "YBBN", 60, 61, 130),          # v1 has an earlier-plan duplicate
          f3(3, "JST3", "YMML", "YSCB", 60, 61, 200),          # v3-only, nothing in v1
          f3(4, "RXA4", "YPPH", "YPAD", 60, 61, 90), f3(4, "RXA4", "YPPH", "YPAD", 70, 100, 150)]  # split
    v1 = [f1(1, "QFA1", " ysSY", "YMML", 60, 125),
          f1(2, "VOZ2", "YSSY", "YBBN", 60, 135),
          f1(2, "VOZ2", "YSSY", "YMML", 60, 135),              # amended plan duplicate
          f1(4, "RXA4", "YPPH", "YPAD", 60, 160)]
    # the split case: v1 logon 60, v3 flights logon 60 and 70 -> v1 key matches first v3 flight exactly, so adjust
    v3[3]["logon_time"] = _t(65)
    res = match(v3, v1)
    errs = []
    if len(res.exact) != 2: errs.append(f"exact {len(res.exact)} != 2")
    if len(res.split) != 1: errs.append(f"split {len(res.split)} != 1")
    if len(res.v1_only) != 1 or "amended" not in res.v1_only[0][1]: errs.append("v1-only amended plan not recognised")
    if len(res.v3_only) != 1: errs.append(f"v3-only {len(res.v3_only)} != 1")
    try:
        lint("INSERT INTO x VALUES (1)"); errs.append("lint accepted INSERT")
    except SystemExit:
        pass
    try:
        lint("SELECT 1; DROP TABLE x"); errs.append("lint accepted two statements")
    except SystemExit:
        pass
    lint("WITH a AS (SELECT :'from_ts'::timestamptz AS t) SELECT * FROM a WHERE x IS DISTINCT FROM y")
    for p in (HERE / "comparison_a.sql", HERE / "comparison_b.sql"):
        for name, q in split_queries(p).items():
            try:
                lint(q)
            except SystemExit as e:
                errs.append(f"{p.name}:{name}: {e}")
    print("selftest:", "OK" if not errs else "FAILED " + "; ".join(errs))
    return 0 if not errs else 1


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["a", "b", "selftest"])
    ap.add_argument("--out", default="qa89_out")
    ap.add_argument("--offline", help="directory of saved CSVs (mode b): match without a database")
    ap.add_argument("--from-ts"); ap.add_argument("--to-ts")
    ap.add_argument("--gap-from", default="2026-10-09 15:30:00+00"); ap.add_argument("--gap-to", default="2026-10-09 15:50:00+00")
    ap.add_argument("--timeout", type=int, default=30, help="statement timeout in seconds (v1 rule: 30)")
    ap.add_argument("--known-v3-only", help="CSV (cid,callsign,departure,arrival,logon_time) of the owner's 3 v3-only flights")
    ap.add_argument("--expect-exact", type=int, default=864)
    ap.add_argument("--expect-v1", type=int, default=887)
    ap.add_argument("--expect-v3", type=int, default=867)
    ap.add_argument("--expect-v1-only", type=int, default=23)
    ap.add_argument("--expect-v3-only", type=int, default=3)
    a = ap.parse_args()
    out = Path(a.out)

    if a.mode == "selftest":
        return selftest()

    if a.mode == "a":
        if not (a.from_ts and a.to_ts): raise SystemExit("--from-ts and --to-ts are required")
        qs = split_queries(HERE / "comparison_a.sql")
        v = {"from_ts": a.from_ts, "to_ts": a.to_ts}
        rc = 0
        for name, sql in qs.items():
            rows = run_psql("V3_DSN", sql, v, max(a.timeout, 120))
            write_csv(out / f"{name}.csv", rows)
            print(f"{name}: {len(rows)} rows -> {out / (name + '.csv')}")
        diffs = read_csv(out / "a_differences.csv")
        counts: dict[str, int] = defaultdict(int)
        for r in diffs: counts[r["reason"]] += 1
        print("\nDifferences by reason (design 5.2 A expected list; UNEXPECTED is a bug):")
        for k, n in sorted(counts.items()): print(f"  {n:6d}  {k}")
        ind = read_csv(out / "a_independent.csv")
        if ind:
            same = ind[0]["flight_record_sample_sum"] == ind[0]["gate_positions_processed"]
            print(f"\nIndependent positions check: {ind[0]} -> {'EQUAL' if same else 'DIFFERENT (explain)'}")
            if not same: rc = 1
        dup = read_csv(out / "a_duplicates.csv")
        print(f"Overlapping flights sharing a session: {len(dup)} (expect 0)")
        if dup or counts.get("UNEXPECTED"): rc = 1
        print("RESULT:", "PASS" if rc == 0 else "FAIL (see above)")
        return rc

    # mode b
    if a.offline:
        d = Path(a.offline)
        v3, v1 = read_csv(d / "b_v3.csv"), read_csv(d / "b_v1.csv")
    else:
        if not (a.from_ts and a.to_ts): raise SystemExit("--from-ts and --to-ts are required")
        qs = split_queries(HERE / "comparison_b.sql")
        v = {"from_ts": a.from_ts, "to_ts": a.to_ts, "gap_from": a.gap_from, "gap_to": a.gap_to}
        v3 = run_psql("V3_DSN", qs["b_v3"], v, a.timeout)
        v1 = run_psql("V1_DSN", qs["b_v1"], v, a.timeout)
        write_csv(out / "b_v3.csv", v3); write_csv(out / "b_v1.csv", v1)
    known = None
    if a.known_v3_only:
        known = {(int(r["cid"]), r["callsign"], norm_air(r["departure"]), norm_air(r["arrival"]), ts(r["logon_time"]).isoformat())
                 for r in read_csv(Path(a.known_v3_only))}
    res = match(v3, v1)
    return report_b(res, dict(exact=a.expect_exact, v1_total=a.expect_v1, v3_total=a.expect_v3,
                              v1_only=a.expect_v1_only, v3_only=a.expect_v3_only), known)


if __name__ == "__main__":
    sys.exit(main())
