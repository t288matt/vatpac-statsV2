"""Fetch the public VATSIM feed a modest number of times (read-only GET) and store the raw payload plus the
independent analysis of it. Never fetches more often than the 15 s cache lifetime; default spacing is 75 s.

Usage: python feed_sampler.py OUTDIR [--count N] [--interval SECONDS]
"""

from __future__ import annotations

import argparse
import gzip
import json
import time
from pathlib import Path

import requests
from independent import analyse, load_ring, load_whitelist

URL = "https://data.vatsim.net/v3/vatsim-data.json"
ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--count", type=int, default=40)
    ap.add_argument("--interval", type=float, default=75.0)
    a = ap.parse_args()
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    ring = load_ring(ROOT / "config/australian_airspace_polygon.json")
    wl = load_whitelist(ROOT / "config/controller_callsigns_list.txt")
    seen: set[str] = set()
    for i in range(a.count):
        t0 = time.monotonic()
        r = requests.get(URL, timeout=15, headers={"User-Agent": "vatpac-audit/1 (read-only)", "Accept-Encoding": "gzip"})
        r.raise_for_status()
        payload = r.json()
        res = analyse(payload, ring, wl)
        ts = res["update_timestamp"]
        res["cache_control"] = r.headers.get("Cache-Control")
        res["fetched_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if ts not in seen:
            seen.add(ts)
            stamp = ts.replace(":", "").replace("-", "")[:15]
            with gzip.open(out / f"raw_{stamp}.json.gz", "wt", encoding="utf-8") as f:
                json.dump(payload, f)
            with (out / "analysis.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(res) + "\n")
        print(i, ts, res["pilots_global"], res["pilots_inside"], res["controllers_kept"], flush=True)
        time.sleep(max(15.0, a.interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()
