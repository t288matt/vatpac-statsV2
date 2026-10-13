"""Recompute the finished flights from the raw tables in plain Python and compare with flight_summary row by row.

Independent cross-check of write_finished_flights() (issue 89): no SQL logic is reused. Input: CSV exports of pilot_session,
pilot_position and flight_summary (the finished-flights table).

Rules implemented here, from docs/DEFINITIONS.md and the design (v1's rule):
  flight gate : a session makes a flight only if departure and arrival are non-empty after upper(btrim)
  one flight  : sessions of the same (cid, callsign, departure, arrival), ordered by first position, are one flight when the
                session's logon_time is no more than 120 minutes after the latest last position of the earlier sessions of that
                key; otherwise a new flight starts. A connection is never cut inside (no gap or 8 h split)
  airborne    : per session, sum over consecutive position pairs of min(gap, 40 s) where gs at the earlier sample >= 60 and
                gap <= 120 minutes; summed over the flight's sessions, in minutes rounded to 2 decimals
  compared    : only flights ending at or before the newest end in the table (a flight still open is not in it yet)
Usage: python recompute_flights.py EXPORT_DIR [PREFIX]   (files: PREFIXpilot_session.csv, PREFIXpilot_position.csv,
PREFIXflight_summary.csv; capture.py writes them with the prefix raw_, so pass raw_)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

JOIN_S, CAP_S = 120 * 60, 40


def _norm(x: object) -> str:
    return "" if pd.isna(x) else str(x).strip().upper()


def recompute(sess: pd.DataFrame, pos: pd.DataFrame) -> pd.DataFrame:
    """One row per flight: key (session_id of the first session, start_s) and the aggregates."""
    per: dict[int, dict] = {}
    for sid, g in pos.sort_values(["session_id", "observed_at"]).groupby("session_id"):
        t = g["observed_at"].astype("int64").to_numpy() / 1e9  # ns -> seconds (timestamps are UTC)
        gs = g["gs_kt"].to_numpy()
        air = 0.0
        for i in range(len(t) - 1):
            if gs[i] >= 60 and t[i + 1] - t[i] <= JOIN_S:
                air += min(t[i + 1] - t[i], CAP_S)
        per[int(sid)] = {"first": t[0], "last": t[-1], "n": len(t), "air": air, "gs": g["gs_kt"].max(), "alt": g["alt_ft"].max()}
    s = sess[sess["id"].isin(list(per))].copy()
    s["dep"], s["arr"] = s["departure"].map(_norm), s["arrival"].map(_norm)
    s = s[(s["dep"] != "") & (s["arr"] != "")]
    s["logon_s"] = s["logon_time"].map(lambda x: x.timestamp())
    s["first"] = s["id"].map(lambda i: per[int(i)]["first"])
    flights: list[list[int]] = []
    for _, grp in s.sort_values(["first", "id"]).groupby(["cid", "callsign", "dep", "arr"]):
        prev_end: float | None = None  # latest last position of ALL earlier sessions of the key
        for r in grp.itertuples():
            if prev_end is None or r.logon_s - prev_end > JOIN_S:
                flights.append([])
            flights[-1].append(int(r.id))
            last = per[int(r.id)]["last"]
            prev_end = last if prev_end is None else max(prev_end, last)
    out = []
    for ids in flights:
        ps = [per[i] for i in ids]
        out.append({
            "session_id": ids[0],
            "start_s": min(p["first"] for p in ps),
            "end_s": max(p["last"] for p in ps),
            "sample_count": sum(p["n"] for p in ps),
            "airborne_minutes": round(sum(p["air"] for p in ps) / 60.0 + 1e-9, 2),
            "max_gs": max(p["gs"] for p in ps),
            "max_alt": max(p["alt"] for p in ps),
            "n_sessions": len(ids),
        })
    cols = ["session_id", "start_s", "end_s", "sample_count", "airborne_minutes", "max_gs", "max_alt", "n_sessions"]
    return pd.DataFrame(out, columns=cols)


def main() -> None:
    d = Path(sys.argv[1])
    prefix = sys.argv[2] if len(sys.argv) > 2 else ""
    pos = pd.read_csv(d / f"{prefix}pilot_position.csv", parse_dates=["observed_at"])
    pos["observed_at"] = pos["observed_at"].dt.tz_localize("UTC")
    sess = pd.read_csv(d / f"{prefix}pilot_session.csv", parse_dates=["logon_time"])
    if sess["logon_time"].dt.tz is None:
        sess["logon_time"] = sess["logon_time"].dt.tz_localize("UTC")
    fs = pd.read_csv(d / f"{prefix}flight_summary.csv", parse_dates=["start_time", "end_time"])
    mine = recompute(sess, pos)
    fs["start_s"] = fs["start_time"].map(lambda x: x.timestamp())
    fs["end_s"] = fs["end_time"].map(lambda x: x.timestamp())
    horizon = fs["end_s"].max() if len(fs) else 0.0
    mine = mine[mine["end_s"] <= horizon]  # flights still open when the table was exported are not in it yet
    m = mine.merge(fs, on=["session_id", "start_s"], how="outer", suffixes=("_mine", "_db"), indicator=True)
    both = m[m["_merge"] == "both"]
    only_mine = m[m["_merge"] == "left_only"]
    only_db = m[m["_merge"] == "right_only"]
    bad_end = both[(both["end_s_mine"] - both["end_s_db"]).abs() > 1e-3]
    bad_n = both[both["sample_count_mine"] != both["sample_count_db"]]
    bad_air = both[(both["airborne_minutes_mine"] - both["airborne_minutes_db"].astype(float)).abs() > 1e-9]
    bad_gs = both[both["max_gs"] != both["max_gs_kt"]]
    bad_alt = both[both["max_alt"] != both["max_alt_ft"]]
    print(f"sessions with positions: {pos['session_id'].nunique()}; positions: {len(pos)}")
    print(f"independent flights: {len(mine)}; flight_summary rows: {len(fs)}")
    print(f"compared: {len(both)}; missing in flight_summary: {len(only_mine)}; extra in flight_summary: {len(only_db)}")
    print(
        f"end differs: {len(bad_end)}; sample_count differs: {len(bad_n)}; airborne differs: {len(bad_air)}; "
        f"max_gs differs: {len(bad_gs)}; max_alt differs: {len(bad_alt)}"
    )
    print(f"flights joined from more than one session: {int((mine['n_sessions'] > 1).sum())}")
    for name, frame in (("end", bad_end), ("sample_count", bad_n), ("airborne", bad_air), ("missing", only_mine), ("extra", only_db)):
        if len(frame):
            print(name, frame[["session_id"]].head(10).to_dict("records"))


if __name__ == "__main__":
    main()
