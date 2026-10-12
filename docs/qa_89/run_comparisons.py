#!/usr/bin/env python3
"""Runner for the #138 comparisons. Read only. The owner runs it; no VM access from here.

  python run_comparisons.py a --from-ts '2026-10-07 21:38+00' --to-ts '2026-10-12 00:00+00' --out outA
  python run_comparisons.py b --out outB \
        --from-ts '2026-10-08 07:52+00' --to-ts '2026-10-10 18:00+00' \
        --gap-from '2026-10-09 15:30+00' --gap-to '2026-10-09 15:50+00'
  python run_comparisons.py b --offline outB          # re-match saved b_v3.csv / b_v1.csv, no database
  python run_comparisons.py b --v3-csv b_v3.csv --v1-csv b_v1.csv   # plain SELECT exports made over ssh
  python run_comparisons.py extract --file comparison_b.sql --name b_v3 > b_v3.sql   # one query, linted, for ssh
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
    """Parse psql / export timestamps: '2026-10-09 15:30:00+00', with fraction, 'T', 'Z' or '+00:00'."""
    x = x.strip().replace(" ", "T").replace("Z", "+00:00")
    m = re.search(r"([+-]\d\d)$", x)
    if m:
        x += ":00"
    d = datetime.fromisoformat(x)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def lower_keys(rows: list[dict]) -> list[dict]:
    """Plain SELECT exports: tolerate header case, spaces and extra columns."""
    return [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in rows]


def prep(v3: list[dict], v1: list[dict]) -> tuple[list[dict], list[dict]]:
    v3, v1 = lower_keys(v3), lower_keys(v1)
    for r in v3:
        r.setdefault("start_time", r.get("logon_time", ""))
        if not r.get("end_time"):
            raise SystemExit("v3 export needs an end_time column")
        for need in ("cid", "callsign", "departure", "arrival", "logon_time"):
            if need not in r: raise SystemExit(f"v3 export lacks column {need}")
    for r in v1:
        if "completion_time" not in r:
            raise SystemExit("v1 export needs a completion_time column")
        for need in ("cid", "callsign", "departure", "arrival", "logon_time"):
            if need not in r: raise SystemExit(f"v1 export lacks column {need}")
    return v3, v1


@dataclass
class Result:
    exact: list
    v1_only: list          # (v1 row, reason)
    v3_only: list          # (v3 row, reason)
    split: list            # (v1 row, [v3 rows])
    merged: list           # (v3 row, [v1 rows])
    measurement: list      # (v1 row, v3 row): same key, v1 logon later because v1 missed a short leading session
    v1_total: int
    v3_total: int
    excluded_v1: int = 0


def match(v3: list[dict], v1: list[dict], exclude_cids: set[int] | None = None,
          cutoff: datetime | None = None) -> Result:
    """Match on (cid, callsign, departure, arrival, whole-second logon_time), airport text normalised.

    Classes: exact; split (one v1 row, two or more v3 flights of its key inside its interval); merged;
    measurement (same key, v3 logon earlier than v1 logon, v3 flight overlaps the v1 interval: v1 polls about
    every 92 s and can miss a short leading session; a documented measurement difference, not a defect);
    v1-only amended plan (expected); everything else unexplained. v3-only flights are always listed."""
    v3, v1 = prep(v3, v1)
    excluded = 0
    if exclude_cids:
        before = len(v1)
        v1 = [r for r in v1 if int(r["cid"]) not in exclude_cids]
        excluded += before - len(v1)
        v3 = [r for r in v3 if int(r["cid"]) not in exclude_cids]
    if cutoff:
        before = len(v1)
        v1 = [r for r in v1 if ts(r["completion_time"]) < cutoff]
        excluded += before - len(v1)
        v3 = [r for r in v3 if ts(r["end_time"]) < cutoff]

    def k(r): return (int(r["cid"]), r["callsign"], norm_air(r["departure"]), norm_air(r["arrival"]), ts(r["logon_time"]))
    by3, by1 = defaultdict(list), defaultdict(list)
    for r in v3: by3[k(r)].append(r)
    for r in v1: by1[k(r)].append(r)

    exact, merged = [], []
    done1, done3 = set(), set()      # ids of consumed rows
    for key, rows1 in by1.items():
        rows3 = by3.get(key, [])
        if len(rows1) == 1 and len(rows3) == 1:
            exact.append((rows1[0], rows3[0])); done1.add(id(rows1[0])); done3.add(id(rows3[0]))
        elif rows3 and len(rows1) > 1:
            merged.append((rows3[0], rows1))
            done1.update(id(r) for r in rows1); done3.update(id(r) for r in rows3)

    def same_pilot_key(r3, r1):
        return (int(r3["cid"]), r3["callsign"], norm_air(r3["departure"]), norm_air(r3["arrival"])) == k(r1)[:4]

    def overlaps(r3, r1):
        return ts(r3["start_time"]) <= ts(r1["completion_time"]) and ts(r3["end_time"]) >= ts(r1["logon_time"])

    split, measurement, v1_only = [], [], []
    left1 = [r for r in v1 if id(r) not in done1]
    for r1 in left1:
        parts = [r3 for r3 in v3 if id(r3) not in done3 and same_pilot_key(r3, r1)
                 and ts(r3["start_time"]) >= ts(r1["logon_time"]) and ts(r3["start_time"]) <= ts(r1["completion_time"])]
        if len(parts) >= 2:
            split.append((r1, parts)); done1.add(id(r1)); done3.update(id(p) for p in parts); continue
        late = [r3 for r3 in v3 if id(r3) not in done3 and same_pilot_key(r3, r1)
                and ts(r3["logon_time"]) < ts(r1["logon_time"]) and overlaps(r3, r1)]
        if late:
            measurement.append((r1, late[0])); done1.add(id(r1)); done3.add(id(late[0])); continue
        other = [r3 for r3 in v3 if int(r3["cid"]) == int(r1["cid"]) and r3["callsign"] == r1["callsign"]
                 and not same_pilot_key(r3, r1) and overlaps(r3, r1)]
        if other:
            v1_only.append((r1, "amended plan: v3 holds this pilot and callsign under a different departure or arrival (expected, E5)"))
        else:
            v1_only.append((r1, "UNEXPLAINED: no v3 flight for this pilot and callsign overlaps it"))
        done1.add(id(r1))

    v3_only = []
    for r3 in v3:
        if id(r3) in done3: continue
        near = [r1 for r1 in v1 if int(r1["cid"]) == int(r3["cid"]) and r1["callsign"] == r3["callsign"] and overlaps(r3, r1)]
        v3_only.append((r3, "v1 holds this pilot and callsign overlapping it under another key or logon (check by hand)"
                        if near else "no v1 summary overlaps it"))
    return Result(exact, v1_only, v3_only, split, merged, measurement, len(v1), len(v3), excluded)


def report_b(res: Result, strict: dict[str, int] | None, known_v3_only: set | None) -> int:
    L = ["COMPARISON B: flight_record against v1 flight_summaries",
         f"rows compared: v1 {res.v1_total}   v3 {res.v3_total}   (v1 rows dropped by cutoff or excluded pilots: {res.excluded_v1})",
         f"exact 1-to-1 matches                      {res.exact.__len__()}",
         f"measurement differences (v1 missed a short leading session; v3 logon earlier) {len(res.measurement)}",
         f"v1 flights split by v3                    {len(res.split)}   (expect 0)",
         f"v3 flights merging several v1 rows        {len(res.merged)}   (expect 0)",
         f"v1-only, amended plan (expected)          {sum(1 for _, w in res.v1_only if w.startswith('amended'))}",
         f"v1-only, UNEXPLAINED                      {sum(1 for _, w in res.v1_only if w.startswith('UNEXPLAINED'))}   (expect 0)",
         f"v3-only (listed below for the owner)      {len(res.v3_only)}",
         ""]
    fails = []
    if res.split: fails.append("v1 flights split by v3")
    if res.merged: fails.append("v3 merges several v1 rows")
    unexpl = [r for r, w in res.v1_only if w.startswith("UNEXPLAINED")]
    if unexpl: fails.append(f"{len(unexpl)} unexplained v1-only flights")
    if strict:
        if len(res.exact) != strict["exact"]: fails.append(f"exact {len(res.exact)} != {strict['exact']}")
        if res.v1_total != strict["v1_total"]: fails.append(f"v1 total {res.v1_total} != {strict['v1_total']}")
        if res.v3_total != strict["v3_total"]: fails.append(f"v3 total {res.v3_total} != {strict['v3_total']}")
        if len(res.v1_only) != strict["v1_only"]: fails.append(f"v1-only {len(res.v1_only)} != {strict['v1_only']}")
        if len(res.v3_only) != strict["v3_only"]: fails.append(f"v3-only {len(res.v3_only)} != {strict['v3_only']}")
    if known_v3_only is not None:
        got = {(int(r["cid"]), r["callsign"], norm_air(r["departure"]), norm_air(r["arrival"]), ts(r["logon_time"]).isoformat())
               for r, _ in res.v3_only}
        if got != known_v3_only: fails.append("v3-only flights differ from the owner's list")
    L.append("--- v1-only ---")
    for r, w in res.v1_only: L.append(f"  v1 {r['cid']} {r['callsign']} {r['departure']}-{r['arrival']} logon {r['logon_time']} : {w}")
    L.append("--- v3-only (owner: judge each) ---")
    for r, w in res.v3_only:
        L.append(f"  v3 {r['cid']} {r['callsign']} {norm_air(r['departure'])}-{norm_air(r['arrival'])} logon {r['logon_time']} "
                 f"start {r['start_time']} end {r['end_time']} : {w}")
    L.append("--- measurement differences ---")
    for r1, r3 in res.measurement:
        L.append(f"  {r1['cid']} {r1['callsign']} v1 logon {r1['logon_time']} v3 logon {r3['logon_time']}")
    for r1, parts in res.split:
        L.append(f"  SPLIT v1 {r1['cid']} {r1['callsign']} logon {r1['logon_time']} -> {len(parts)} v3 flights")
    L += ["", "RESULT: " + ("PASS" if not fails else "FAIL: " + "; ".join(fails))]
    print("\n".join(L))
    return 0 if not fails else 1


# ---------------------------------------------------------------- self test

def _t(m: int) -> str:
    return f"2026-10-09 {m // 60:02d}:{m % 60:02d}:00+00"


def selftest() -> int:
    def f3(cid, cs, dep, arr, lg, st, en): return dict(cid=str(cid), callsign=cs, departure=dep, arrival=arr,
                                                       logon_time=_t(lg), start_time=_t(st), end_time=_t(en))
    def f1(cid, cs, dep, arr, lg, co): return dict(v1_id="1", cid=str(cid), callsign=cs, departure=dep, arrival=arr,
                                                   logon_time=_t(lg), completion_time=_t(co))
    v3 = [f3(1, "QFA1", "YSSY", "YMML", 60, 61, 120),            # exact (v1 text padded, lower case)
          f3(2, "VOZ2", "YSSY", "YBBN", 60, 61, 130),            # v1 also holds an earlier-plan duplicate
          f3(3, "JST3", "YMML", "YSCB", 60, 61, 200),            # v3-only
          f3(4, "RXA4", "YPPH", "YPAD", 65, 65, 90), f3(4, "RXA4", "YPPH", "YPAD", 100, 100, 150),  # split
          f3(5, "ANZ5", "YSSY", "YMML", 60, 60, 180),            # v1 missed the first 10 min: later logon
          f3(6, "QFA6", "YSSY", "YMML", 60, 61, 100)]            # pilot 6 is excluded by --exclude-cids
    v1 = [f1(1, "QFA1", " ysSY", "YMML", 60, 125),
          f1(2, "VOZ2", "YSSY", "YBBN", 60, 135),
          f1(2, "VOZ2", "YSSY", "YMML", 60, 135),                # amended plan duplicate
          f1(4, "RXA4", "YPPH", "YPAD", 60, 160),
          f1(5, "ANZ5", "YSSY", "YMML", 70, 185),
          f1(6, "QFA6", "YSSY", "YMML", 60, 105),
          f1(7, "XXX7", "YSSY", "YMML", 60, 105)]                # genuinely unexplained
    res = match(v3, v1, exclude_cids={6})
    errs = []
    if len(res.exact) != 2: errs.append(f"exact {len(res.exact)} != 2")
    if len(res.split) != 1: errs.append(f"split {len(res.split)} != 1")
    if len(res.measurement) != 1: errs.append(f"measurement {len(res.measurement)} != 1")
    amended = [w for _, w in res.v1_only if w.startswith("amended")]
    unexpl = [w for _, w in res.v1_only if w.startswith("UNEXPLAINED")]
    if len(amended) != 1 or len(unexpl) != 1: errs.append(f"v1-only amended {len(amended)} unexplained {len(unexpl)}")
    if len(res.v3_only) != 1: errs.append(f"v3-only {len(res.v3_only)} != 1")
    if res.excluded_v1 != 1: errs.append(f"excluded {res.excluded_v1} != 1")
    # plain SELECT exports: upper-case headers, extra columns, fractional seconds, no start_time
    plain3 = [{"CID": "9", "Callsign": "ZZZ9", "Departure": "YSSY", "Arrival": "YMML", "Logon_Time": "2026-10-09 01:00:00.000+00",
               "End_Time": "2026-10-09 02:00:00+00", "extra": "x"}]
    plain1 = [{"cid": "9", "callsign": "ZZZ9", "departure": "YSSY", "arrival": "YMML", "logon_time": "2026-10-09 01:00:00+00",
               "completion_time": "2026-10-09 02:05:00+00"}]
    if len(match(plain3, plain1).exact) != 1: errs.append("plain export did not match")
    # cutoff drops late rows on both sides
    if match(plain3, plain1, cutoff=ts("2026-10-09 01:30:00+00")).v3_total != 0: errs.append("cutoff ignored")
    for bad in ("INSERT INTO x VALUES (1)", "SELECT 1; DROP TABLE x"):
        try:
            lint(bad); errs.append(f"lint accepted {bad!r}")
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
    ap.add_argument("mode", choices=["a", "b", "selftest", "extract"])
    ap.add_argument("--out", default="qa89_out")
    ap.add_argument("--file", help="extract: comparison_a.sql or comparison_b.sql")
    ap.add_argument("--name", help="extract: query name, e.g. a_summary")
    ap.add_argument("--offline", help="directory of saved CSVs (mode b): match without a database")
    ap.add_argument("--from-ts"); ap.add_argument("--to-ts")
    ap.add_argument("--gap-from", default="2026-10-09 15:30:00+00"); ap.add_argument("--gap-to", default="2026-10-09 15:50:00+00")
    ap.add_argument("--timeout", type=int, default=30, help="statement timeout in seconds (v1 rule: 30)")
    ap.add_argument("--known-v3-only", help="CSV (cid,callsign,departure,arrival,logon_time) of the owner's 3 v3-only flights")
    ap.add_argument("--v3-csv", help="mode b offline: plain SELECT export from v3 (cid,callsign,departure,arrival,logon_time,start_time,end_time)")
    ap.add_argument("--v1-csv", help="mode b offline: plain SELECT export from v1 (cid,callsign,departure,arrival,logon_time,completion_time)")
    ap.add_argument("--exclude-cids", help="file with one CID per line (pilots still held back by the writer); dropped from both sides")
    ap.add_argument("--cutoff", help="drop v1 flights completing, and v3 flights ending, at or after this instant (use newest snapshot minus 9 h)")
    ap.add_argument("--strict", action="store_true", help="also compare counts with the design window numbers below")
    ap.add_argument("--expect-exact", type=int, default=864)
    ap.add_argument("--expect-v1", type=int, default=887)
    ap.add_argument("--expect-v3", type=int, default=867)
    ap.add_argument("--expect-v1-only", type=int, default=23)
    ap.add_argument("--expect-v3-only", type=int, default=3)
    a = ap.parse_args()
    out = Path(a.out)

    if a.mode == "selftest":
        return selftest()
    if a.mode == "extract":
        q = split_queries(HERE / a.file)[a.name]
        lint(q)
        print(q)
        return 0

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
    if a.v3_csv or a.v1_csv:
        if not (a.v3_csv and a.v1_csv): raise SystemExit("--v3-csv and --v1-csv go together")
        v3, v1 = read_csv(Path(a.v3_csv)), read_csv(Path(a.v1_csv))
    elif a.offline:
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
    excl = None
    if a.exclude_cids:
        excl = {int(x) for x in Path(a.exclude_cids).read_text().split() if x.strip().isdigit()}
    res = match(v3, v1, excl, ts(a.cutoff) if a.cutoff else None)
    strict = dict(exact=a.expect_exact, v1_total=a.expect_v1, v3_total=a.expect_v3,
                  v1_only=a.expect_v1_only, v3_only=a.expect_v3_only) if a.strict else None
    return report_b(res, strict, known)


if __name__ == "__main__":
    sys.exit(main())
