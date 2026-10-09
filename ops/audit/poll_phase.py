"""Explain the missing 15-second ticks: for every snapshot gap print the store lag (polled_at - snapshot_ts) and how it changed.

Input: the raw_poll_log.csv written by capture.py. A lag that drops by 15.0 s exactly at a 30 s gap means the poll phase
wrapped relative to the feed's publication (the poll loop runs at the feed's own period).

Usage: python poll_phase.py CAPTURE_DIR
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd


def main() -> None:
    pl = pd.read_csv(Path(sys.argv[1]) / "raw_poll_log.csv", parse_dates=["polled_at", "snapshot_ts"])
    ok = pl[pl["status"] == "ok"].drop_duplicates("snapshot_ts").sort_values("snapshot_ts").reset_index(drop=True)
    ok["lag"] = (ok["polled_at"] - ok["snapshot_ts"]).dt.total_seconds()
    ok["gap"] = ok["snapshot_ts"].diff().dt.total_seconds()
    ok["lag_change"] = ok["lag"].diff()
    gaps = ok[ok["gap"] > 16]
    print(gaps[["snapshot_ts", "gap", "lag", "lag_change"]].round(1).to_string())
    thirty = gaps[(gaps["gap"] - 30).abs() < 0.5]
    wrapped = thirty[(thirty["lag_change"] + 15).abs() < 0.5]
    print(
        f"stored snapshots {len(ok)}; gaps over 16 s: {len(gaps)}; of 30 s gaps {len(thirty)}, lag fell by 15.0 s (+-0.5) in {len(wrapped)}"
    )
    print(f"store lag min {ok['lag'].min():.1f} s, median {ok['lag'].median():.1f} s, max {ok['lag'].max():.1f} s")


if __name__ == "__main__":
    main()
