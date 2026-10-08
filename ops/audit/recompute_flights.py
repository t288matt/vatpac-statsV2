"""Recompute flights from the raw tables (CSV exports of pilot_session, pilot_position, flight_summary) in plain
Python and compare with flight_summary row by row. No SQL logic is reused.

Definitions implemented here from SPEC section 5 / docs/DEFINITIONS.md:
  gap split   : consecutive samples more than 120 minutes apart start a new gap segment
  span split  : within a gap segment, part = max(ceil((t - segment_start) / 8 h) - 1, 0)   (DEFINITIONS "bucket" form)
  exact span  : alternative reading of SPEC ("a span of more than 8 hours starts a new session"): a new flight starts at
                the first sample later than 8 h after the current flight's first sample (reported for information)
  airborne    : sum over consecutive sample pairs (any segment) of min(gap, 40 s) where gs at the earlier sample >= 60
                and gap <= 120 minutes; in minutes, rounded to 2 decimals
Usage: python recompute_flights.py EXPORT_DIR   (files: pilot_position.csv, flight_summary.csv; capture.py writes them as
raw_pilot_position.csv / raw_flight_summary.csv, pass --prefix raw_)
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd

GAP_MIN, SPAN_S, CAP_S = 120 * 60, 8 * 3600, 40


def recompute(pos: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for sid, g in pos.sort_values(["session_id", "observed_at"]).groupby("session_id"):
        t = g["observed_at"].astype("int64").to_numpy() / 1e9  # ns -> seconds (timestamps are UTC)
        gs = g["gs_kt"].to_numpy()
        alt = g["alt_ft"].to_numpy()
        seg = 0
        seg0 = t[0]
        parts: dict[tuple[int, int], dict] = {}
        for i in range(len(t)):
            if i == 0 or t[i] - t[i - 1] > GAP_MIN:
                seg += 1
                seg0 = t[i]
            part = max(math.ceil((t[i] - seg0) / SPAN_S - 1e-12) - 1, 0)
            air = 0.0
            if i + 1 < len(t) and gs[i] >= 60 and t[i + 1] - t[i] <= GAP_MIN:
                air = min(t[i + 1] - t[i], CAP_S)
            p = parts.setdefault((seg, part), {"start": t[i], "end": t[i], "n": 0, "air": 0.0, "gs": -1, "alt": -(10**9)})
            p["end"] = t[i]
            p["n"] += 1
            p["air"] += air
            p["gs"] = max(p["gs"], gs[i])
            p["alt"] = max(p["alt"], alt[i])
        for (s, pt), p in parts.items():
            rows.append(
                {
                    "session_id": sid,
                    "gap_segment": s,
                    "span_part": pt,
                    "start_s": p["start"],
                    "end_s": p["end"],
                    "sample_count": p["n"],
                    "airborne_minutes": round(p["air"] / 60.0 + 1e-9, 2),
                    "max_gs": p["gs"],
                    "max_alt": p["alt"],
                }
            )
    return pd.DataFrame(rows)


def exact_span_count(pos: pd.DataFrame) -> int:
    """Number of flights if the 8 h rule restarted at the first sample after 8 h from the current flight start."""
    n = 0
    for _, g in pos.sort_values(["session_id", "observed_at"]).groupby("session_id"):
        t = g["observed_at"].astype("int64").to_numpy() / 1e9
        start = None
        for i in range(len(t)):
            if i == 0 or t[i] - t[i - 1] > GAP_MIN or t[i] - start > SPAN_S:
                start = t[i]
                n += 1
    return n


def main() -> None:
    d = Path(sys.argv[1])
    prefix = sys.argv[2] if len(sys.argv) > 2 else ""
    pos = pd.read_csv(d / f"{prefix}pilot_position.csv", parse_dates=["observed_at"])
    pos["observed_at"] = pos["observed_at"].dt.tz_localize("UTC")
    fs = pd.read_csv(d / f"{prefix}flight_summary.csv", parse_dates=["start_time", "end_time", "computed_at"])
    # flight_summary is refreshed every 5 minutes: cut each session at the last sample the summary knows about (its
    # latest end_time), so that lag is not reported as a difference. Everything else is compared exactly.
    known_to = pos["session_id"].map(fs.groupby("session_id")["end_time"].max())
    pos_c = pos[known_to.notna() & (pos["observed_at"] <= known_to)]
    mine = recompute(pos_c)
    fs["start_s"] = fs["start_time"].map(lambda x: x.timestamp())
    fs["end_s"] = fs["end_time"].map(lambda x: x.timestamp())
    settled = fs
    m = mine.merge(settled, on=["session_id", "gap_segment", "span_part"], how="outer", suffixes=("_mine", "_db"), indicator=True)
    both = m[m["_merge"] == "both"]
    only_mine = m[m["_merge"] == "left_only"]
    only_db = m[m["_merge"] == "right_only"]
    tol = 1e-3
    bad_start = both[(both["start_s_mine"] - both["start_s_db"]).abs() > tol]
    bad_end = both[(both["end_s_mine"] - both["end_s_db"]).abs() > tol]
    bad_n = both[both["sample_count_mine"] != both["sample_count_db"]]
    bad_air = both[(both["airborne_minutes_mine"] - both["airborne_minutes_db"].astype(float)).abs() > 1e-9]
    bad_gs = both[both["max_gs"] != both["max_gs_kt"]]
    bad_alt = both[both["max_alt"] != both["max_alt_ft"]]
    print(f"sessions with positions: {pos['session_id'].nunique()}; positions: {len(pos)}")
    print(f"independent flight parts: {len(mine)}; flight_summary rows: {len(fs)}; settled (comparable) rows: {len(settled)}")
    print(
        f"compared: {len(both)}; missing in flight_summary: {len(only_mine[only_mine['session_id'].isin(settled['session_id'])])}; "
        f"extra in flight_summary: {len(only_db)}"
    )
    print(
        f"start differs: {len(bad_start)}; end differs: {len(bad_end)}; sample_count differs: {len(bad_n)}; "
        f"airborne differs: {len(bad_air)}; max_gs differs: {len(bad_gs)}; max_alt differs: {len(bad_alt)}"
    )
    print(
        f"total airborne hours independent {both['airborne_minutes_mine'].sum() / 60:.2f} stored "
        f"{both['airborne_minutes_db'].astype(float).sum() / 60:.2f}"
    )
    multi = mine.groupby("session_id").size()
    print(
        f"sessions split into more than one part: {int((multi > 1).sum())}; parts with span_part > 0: "
        f"{int((mine['span_part'] > 0).sum())}; exact-8h-rule flight count {exact_span_count(pos_c)} vs bucket count {len(mine)}"
    )
    for name, frame in (("start", bad_start), ("end", bad_end), ("sample_count", bad_n), ("airborne", bad_air)):
        if len(frame):
            print(name, frame[["session_id", "gap_segment", "span_part"]].head(10).to_dict("records"))


if __name__ == "__main__":
    main()
