"""Quantify the effect of the 08-10-2026 04:08:27 UTC feed event, when the feed returned logon_time without its fractional
seconds for sessions that were already online, so every such session was stored a second time (the session identity is
(cid, callsign, logon_time)).

Method (raw data from capture.py): treat two pilot sessions of the same cid + callsign whose logon_time differs by less
than 1 second as ONE session, recompute flights with the audit's own code, and compare flight, movement and airborne figures
with the stored, split version.

Usage: python logon_precision_impact.py CAPTURE_DIR
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from panel_recompute import build_flights, is_home, load


def main() -> None:
    t = load(Path(sys.argv[1]))
    ps = t["pilot_session"]

    def canon_map(rule) -> dict[int, int]:
        """Map each session id to the lowest id of the group it would belong to under ``rule(prev_row, row)``."""
        canon: dict[int, int] = {}
        for (_cid, _cs), g in ps.sort_values(["first_seen", "id"]).groupby(["cid", "callsign"]):
            prev, rep = None, None
            for r in g.itertuples():
                if prev is not None and rule(prev, r):
                    canon[r.id] = rep
                else:
                    rep = r.id
                    canon[r.id] = r.id
                prev = r if prev is None else (r if r.last_seen > prev.last_seen else prev)
        return canon

    precision_rule = lambda a, b: abs((a.logon_time - b.logon_time).total_seconds()) < 1  # noqa: E731
    reconnect_rule = lambda a, b: (b.first_seen - a.last_seen).total_seconds() <= 300  # noqa: E731
    canon = canon_map(precision_rule)
    dup = {k: v for k, v in canon.items() if k != v}
    print(f"pilot sessions: {len(ps)}; second copies created by the precision change: {len(dup)}")
    stored, _ = build_flights(t)

    def merged_flights(cmap: dict[int, int]) -> pd.DataFrame:
        t2 = {k: v.copy() for k, v in t.items()}
        t2["pilot_position"]["session_id"] = t2["pilot_position"]["session_id"].map(lambda i: cmap.get(i, i))
        t2["flight_summary"]["session_id"] = t2["flight_summary"]["session_id"].map(lambda i: cmap.get(i, i))
        return build_flights(t2)[0]

    merged = merged_flights(canon)

    def movements(f: pd.DataFrame) -> tuple[int, int]:
        ok = ~((f["dep"] == "ZZZZ") | (f["arr"] == "ZZZZ"))
        return int((f["dep"].map(is_home) & ok).sum()), int((f["arr"].map(is_home) & ok).sum())

    sd, sa = movements(stored)
    md, ma = movements(merged)
    print(f"flights (gate passed): stored {len(stored)}  merged {len(merged)}  difference {len(stored) - len(merged)}")
    print(f"home departures: stored {sd} merged {md} (+{sd - md}); home arrivals: stored {sa} merged {ma} (+{sa - ma})")
    print(f"airborne hours: stored {stored['airborne'].sum() / 60:.2f}  merged {merged['airborne'].sum() / 60:.2f}")
    for name, f in (("stored", stored), ("merged", merged)):
        r = f[(f["dep"] != f["arr"]) & ~(f["dep"] == "ZZZZ") & ~(f["arr"] == "ZZZZ") & ~(f["dep"].map(is_home) & f["arr"].map(is_home))]
        g = r.groupby(["dep", "arr"]).agg(n=("cid", "size")).sort_values("n", ascending=False).head(5)
        print(name, "top international routes:", "; ".join(f"{a}-{b} {n}" for (a, b), n in g["n"].items()))
        ap = pd.concat([f[f["dep"].map(is_home)]["dep"], f[f["arr"].map(is_home)]["arr"]]).value_counts().head(5)
        print(name, "busiest airports (movements):", "; ".join(f"{k} {v}" for k, v in ap.items()))
    # secondary question: how many flights would remain if a pilot reconnecting within 5 minutes were one flight
    # (the rule controllers already get). Both effects combined, then the reconnect effect alone.
    rc = canon_map(lambda a, b: precision_rule(a, b) or reconnect_rule(a, b))
    f_all = merged_flights(rc)
    n_groups = len(set(rc.values()))
    print(f"if reconnects within 5 minutes were merged too: sessions {len(ps)} -> {n_groups}; gated flights {len(stored)} -> {len(f_all)}")
    print("spurious movement events by hour of start_time (stored):")
    spurious = stored[stored["session_id"].isin(dup)]
    print(spurious["start"].dt.strftime("%H:%M:%S").value_counts().head(3).to_dict())


if __name__ == "__main__":
    main()
