import glob
import json
import os
import re
import sys
import tempfile
import time

import psycopg

dsn = sys.argv[1]
label = sys.argv[2]
dashdir = sys.argv[3]
runs = int(sys.argv[4]) if len(sys.argv) > 4 else 2
only = sys.argv[5].split(",") if len(sys.argv) > 5 else None


def days(tf, dflt):
    tf = tf or dflt
    m = re.match(r"(?:now-)?(\d+)([hdM])", tf)
    n, u = int(m.group(1)), m.group(2)
    return n * {"h": 1 / 24, "d": 1, "M": 30}[u]


out = []
with psycopg.connect(dsn, autocommit=True) as c:
    c.execute("SHOW statement_timeout")
    print("role settings:", c.execute("SELECT current_user, current_setting('statement_timeout'), current_setting('work_mem')").fetchone())
    for f in sorted(glob.glob(dashdir + "/**/*.json", recursive=True)):
        d = json.load(open(f, encoding="utf-8-sig"))
        for p in d.get("panels", []):
            for t in p.get("targets", []):
                q = t.get("rawSql", "")
                if not re.search(r"\b(flight|flight_ext|movements|cohorts)\b", q):
                    continue
                key = os.path.relpath(f, dashdir).replace("\\", "/") + ":" + str(p["id"])
                if only and key not in only:
                    continue
                nd = days(p.get("timeFrom"), d["time"]["from"])
                sql = q.replace("$__timeFrom()", f"(now() - interval '{int(nd*24)} hours')").replace("$__timeTo()", "now()").rstrip().rstrip(";")
                best = None
                err = None
                for r in range(runs):
                    t0 = time.time()
                    try:
                        cur = c.execute("EXPLAIN (ANALYZE, BUFFERS, TIMING OFF) " + sql)
                        txt = [x[0] for x in cur.fetchall()]
                    except Exception as e:
                        err = type(e).__name__ + ": " + str(e).splitlines()[0][:80]
                        break
                    ex = [float(re.search(r"Execution Time: ([\d.]+)", ln).group(1)) for ln in txt if "Execution Time" in ln][0]
                    tmp = [ln.strip() for ln in txt if "temp read" in ln or "temp written" in ln]
                    if best is None or ex < best[0]:
                        best = (ex, tmp[:1])
                out.append((key, p["title"][:55], best[0] if best else None, err, (best[1] if best else None)))
                print(f"{key:45s} {best[0] if best else float('nan'):10.0f} ms  {err or ''} {(best[1] if best else '') }", flush=True)
with tempfile.NamedTemporaryFile("w", prefix=f"panels_{label}_", suffix=".json", delete=False) as fh:
    json.dump(out, fh)
print("results written to", fh.name)
print("TOP 10 (ms):")
for r in sorted([o for o in out if o[2]], key=lambda o: -o[2])[:10]:
    print(f"  {r[2]:9.0f} {r[0]} {r[1]}")
