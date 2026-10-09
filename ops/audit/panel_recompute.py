"""Recompute the figures shown by a sample of Grafana panels from the RAW tables, in plain Python, and compare them with the
panel's own SQL result (both taken from the same repeatable-read transaction by capture.py). No panel SQL and no view logic
is reused: flights come from recompute_flights.recompute (own gap / span / airborne code), controller sessions are merged
here, lookups (airline, region, aircraft type) are read as plain tables.

Usage: python panel_recompute.py CAPTURE_DIR [--window-from "2026-10-08 06:00:00+00"]   (same value as given to capture.py)
Exit code 0 when every panel agrees, 1 otherwise. Prints a markdown table.
"""

# ruff: noqa: E501, E731  (long literal SQL and short lambdas are clearer in a one-off audit script)
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

import pandas as pd
from recompute_flights import recompute

HOME = ("Y",)


def utc(s):
    return pd.to_datetime(s, utc=True, format="mixed")


def load(d: Path) -> dict[str, pd.DataFrame]:
    t = {p.stem[4:]: pd.read_csv(p, keep_default_na=False, na_values=[""]) for p in d.glob("raw_*.csv")}
    for k, cols in (
        ("pilot_session", ["logon_time", "first_seen", "last_seen"]),
        ("controller_session", ["logon_time", "first_seen", "last_seen"]),
        ("network_snapshot", ["observed_at"]),
        ("poll_log", ["polled_at", "snapshot_ts"]),
        ("flight_summary", ["start_time", "end_time", "computed_at"]),
        ("clock", ["tx_now", "wall"]),
    ):
        for c in cols:
            t[k][c] = utc(t[k][c])
    t["pilot_position"]["observed_at"] = utc(t["pilot_position"]["observed_at"].astype(str) + "+00")
    return t


def btrim(s) -> str:
    return "" if s is None or (isinstance(s, float) and math.isnan(s)) else str(s).strip(" \t\r\n")


def build_flights(t) -> tuple[pd.DataFrame, pd.Timestamp]:
    """Flights as the flight view should show them, from raw positions, as at the last flight_summary refresh."""
    fsum = t["flight_summary"]
    cutoff = fsum["computed_at"].max()
    pos = t["pilot_position"]
    # flight_summary is refreshed every 5 minutes and does not yet hold samples committed after its last run, so each
    # session is cut at the last sample the summary knows about; everything else is recomputed here from raw positions.
    known_to = pos["session_id"].map(fsum.groupby("session_id")["end_time"].max())
    pos = pos[known_to.notna() & (pos["observed_at"] <= known_to)]
    f = recompute(pos)
    ps = t["pilot_session"].set_index("id")
    f["start"] = pd.to_datetime(f["start_s"], unit="s", utc=True)
    f["end"] = pd.to_datetime(f["end_s"], unit="s", utc=True)
    f = f.join(
        ps[["cid", "callsign", "name", "home_icao", "server", "departure", "arrival", "aircraft_icao", "remarks", "logon_time"]],
        on="session_id",
    )
    f["dep"] = f["departure"].map(lambda x: btrim(x).upper())
    f["arr"] = f["arrival"].map(lambda x: btrim(x).upper())
    f = f[(f["dep"] != "") & (f["arr"] != "")].copy()  # the flight gate
    f["acft"] = f["aircraft_icao"].map(lambda x: btrim(x).upper() or None)
    f["airborne"] = f["airborne_minutes"]
    return f, cutoff


def is_home(code) -> bool:
    return any(btrim(code).upper().startswith(h) for h in HOME)


def region_of(code, regions: dict[str, str]) -> str:
    c = btrim(code).upper()
    best = [(len(p), r) for p, r in regions.items() if c.startswith(p)]
    return max(best)[1] if best else "Unknown"


def rank_group(r) -> str:
    return {1: "OBS", 2: "S1", 3: "S2", 4: "S3", 5: "C1", 6: "C2", 7: "C3"}.get(r, "I1+" if r is not None and r >= 8 else "Unknown")


def merge_controllers(cs: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (cid, cal), g in cs.sort_values(["first_seen", "id"]).groupby(["cid", "callsign"]):
        cur = None
        for r in g.itertuples():
            if cur is None or (r.first_seen - cur["end"]).total_seconds() > 300:
                if cur:
                    rows.append(cur)
                cur = {
                    "cid": cid,
                    "callsign": cal,
                    "start": r.logon_time,
                    "end": r.last_seen,
                    "rating": r.rating,
                    "name": r.name,
                    "last_for_attr": r.last_seen,
                }
            else:
                cur["start"] = min(cur["start"], r.logon_time)
                cur["end"] = max(cur["end"], r.last_seen)
                if r.last_seen >= cur["last_for_attr"]:
                    cur["rating"], cur["name"], cur["last_for_attr"] = r.rating, r.name, r.last_seen
        rows.append(cur)
    m = pd.DataFrame(rows)
    m["minutes"] = (m["end"] - m["start"]).dt.total_seconds() / 60.0
    m["rank"] = m["rating"].map(rank_group)
    return m


def same(a: pd.DataFrame, b: pd.DataFrame, tol=1e-6) -> bool:
    if list(a.shape) != list(b.shape):
        return False
    for ca, cb in zip(a.columns, b.columns, strict=True):
        for x, y in zip(a[ca], b[cb], strict=True):
            if isinstance(x, (int, float)) and isinstance(y, (int, float)) and not (pd.isna(x) or pd.isna(y)):
                if abs(float(x) - float(y)) > tol:
                    return False
            elif str(x) != str(y) and not (pd.isna(x) and pd.isna(y)):
                return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("capdir")
    ap.add_argument("--window-from")
    a = ap.parse_args()
    d = Path(a.capdir)
    t = load(d)
    now = t["clock"]["tx_now"].iloc[0]
    frm = utc(a.window_from) if a.window_from else now - pd.Timedelta(days=200)
    to = now
    P = {p.stem[6:]: pd.read_csv(p, keep_default_na=False, na_values=[""]) for p in d.glob("panel_*.csv")}
    flights, cutoff = build_flights(t)
    regions = dict(zip(t["region_map"]["prefix"], t["region_map"]["region"], strict=True))
    airline = dict(zip(t["airline"]["prefix"], t["airline"]["name"], strict=True))
    acft = t["aircraft_type"].set_index("icao")
    ctl = merge_controllers(t["controller_session"])
    ns = t["network_snapshot"].sort_values("observed_at")
    pos = t["pilot_position"]
    results: list[tuple[str, str, str, bool]] = []

    def rec(key: str, shown: str, mine: str, ok: bool) -> None:
        results.append((key, shown, mine, ok))

    inw = lambda s: (s >= frm) & (s <= to)  # noqa: E731
    F = flights[inw(flights["end"])]

    # 1-3 online now
    last_ts = ns["observed_at"].max()
    mine_region = int((pos["observed_at"] == last_ts).sum())
    rec(
        "pilots_online_region",
        str(P["pilots_online_region"].iloc[0, 0]),
        str(mine_region),
        int(P["pilots_online_region"].iloc[0, 0]) == mine_region,
    )
    pl = t["poll_log"]
    pl_ok = pl[pl["status"] == "ok"].drop_duplicates("snapshot_ts")
    mine_global = int(pl_ok.loc[pl_ok["snapshot_ts"] == last_ts, "pilots_seen"].iloc[0])
    rec(
        "pilots_online_global",
        str(P["pilots_online_global"].iloc[0, 0]),
        f"{mine_global} (poll_log.pilots_seen at newest snapshot)",
        int(P["pilots_online_global"].iloc[0, 0]) == mine_global,
    )
    cs = t["controller_session"]
    mine_ctl = int((cs["last_seen"] == last_ts).sum())
    rec("ctrl_online_now", str(P["ctrl_online_now"].iloc[0, 0]), str(mine_ctl), int(P["ctrl_online_now"].iloc[0, 0]) == mine_ctl)

    # 4 hourly averages
    per_ts = pos.groupby("observed_at").size().reindex(ns["observed_at"], fill_value=0)
    hourly = per_ts.groupby(per_ts.index.floor("h")).mean()
    gl = pl_ok.set_index("snapshot_ts")["pilots_seen"]
    gl = gl[(gl.index >= frm) & (gl.index <= to)]
    hourly_g = gl.groupby(gl.index.floor("h")).mean()
    p4 = P["pilots_over_time"]
    mine4 = pd.DataFrame({"t": hourly.index, "a": hourly.values, "b": hourly_g.reindex(hourly.index).values})
    mine4 = mine4[(mine4["t"] + pd.Timedelta(hours=1) > frm) & (mine4["t"] <= to)]
    rec(
        "pilots_over_time",
        f"{len(p4)} hourly rows, mean in-region {p4['In region'].mean():.4f}",
        f"{len(mine4)} rows, mean {mine4['a'].mean():.4f}",
        same(
            mine4[["t", "a", "b"]].assign(t=mine4["t"].astype(str)).reset_index(drop=True),
            p4.assign(time=p4["time"].astype(str)).iloc[:, :3].assign(time=lambda x: utc(x["time"]).astype(str)),
            1e-6,
        ),
    )
    cov = pd.Series({ts: int(((cs["first_seen"] <= ts) & (cs["last_seen"] >= ts)).sum()) for ts in ns["observed_at"]})
    chour = cov.groupby(cov.index.floor("h")).mean()
    p5 = P["ctrl_over_time"]
    chour = chour[(chour.index + pd.Timedelta(hours=1) > frm) & (chour.index <= to)]
    m5 = pd.DataFrame({"t": chour.index.astype(str), "c": chour.values})
    rec(
        "ctrl_over_time",
        f"{len(p5)} hourly rows, mean {p5.iloc[:, 1].mean():.4f}",
        f"{len(m5)} rows, mean {m5['c'].mean():.4f}",
        same(m5, p5.assign(time=utc(p5["time"]).astype(str)), 1e-6),
    )

    # controllers (merged sessions) in the window by session_start
    C = ctl[inw(ctl["start"])]
    groups = ["S1", "S2", "S3", "C1", "C3", "I1+"]
    pc = C.groupby(["cid", "rank"])["minutes"].sum()
    pc = pc[pc > 60].reset_index().groupby("rank")["cid"].count()
    mine6 = pd.DataFrame({"rank_group": groups, "controllers": [int(pc.get(g, 0)) for g in groups]})
    rec(
        "ctrl_logged_on_30d",
        "; ".join(f"{r.rank_group}={r.controllers}" for r in P["ctrl_logged_on_30d"].itertuples()),
        "; ".join(f"{r.rank_group}={r.controllers}" for r in mine6.itertuples()),
        same(mine6, P["ctrl_logged_on_30d"]),
    )

    # monthly panels (all data is in the current month, so one row per month in range)
    months = sorted({s.strftime("%Y-%m") for s in ctl["start"]})
    months = [
        m
        for m in months
        if pd.Timestamp(m + "-01", tz="UTC") <= to
        and pd.Timestamp(m + "-01", tz="UTC") + pd.offsets.MonthBegin(1) > frm.floor("D").replace(day=1)
    ]
    rows_r, rows_c, rows_a = [], [], []
    for m in months:
        ms = pd.Timestamp(m + "-01", tz="UTC")
        me = ms + pd.offsets.MonthBegin(1)
        inm = ctl[(ctl["start"] >= ms) & (ctl["start"] < me)]
        g = inm.groupby(["cid", "rating"])["minutes"].sum()
        g = g[g > 60].reset_index()
        g["rg"] = g["rating"].map(rank_group)
        rows_r.append([ms.strftime("%Y-%m-%d")] + [int(g[g["rg"] == x]["cid"].nunique()) for x in groups])
        w = ctl[(ctl["start"] >= me - pd.Timedelta(days=30)) & (ctl["start"] < me)]
        rows_c.append(
            [ms.strftime("%Y-%m-%d"), int(w[(w["minutes"] > 60) & w["callsign"].str.contains(r"_(?:CTR|FSS)$")]["cid"].nunique())]
        )
        rows_a.append([ms.strftime("%Y-%m-%d"), int(w[w["minutes"] > 30]["cid"].nunique())])
    for key, rows, cols in (
        ("ctrl_monthly_by_rating", rows_r, ["time"] + groups),
        ("ctrl_ctr_fss_monthly", rows_c, ["time", "c"]),
        ("ctrl_active_monthly", rows_a, ["time", "c"]),
    ):
        mine = pd.DataFrame(rows, columns=cols)
        shown = P[key].copy()
        shown["time"] = utc(shown["time"]).dt.strftime("%Y-%m-%d")
        rec(key, "; ".join(map(str, shown.values.tolist())), "; ".join(map(str, mine.values.tolist())), same(mine, shown))

    # top 50 service providers and shortest average session
    CC = C[C["minutes"] > 0]
    top = CC.sort_values("start", ascending=False).groupby("cid").agg(name=("name", "first"), minutes=("minutes", "sum"))
    top["hours"] = (top["minutes"] / 60.0).round(1)
    top = top.sort_values("minutes", ascending=False).head(50)
    p9 = P["ctrl_top50"]
    ok9 = (
        len(top) == len(p9)
        and all(abs(x - y) < 1e-9 for x, y in zip(sorted(top["hours"]), sorted(p9.iloc[:, 1]), strict=True))
        and sorted(map(str, top["name"])) == sorted(map(str, p9.iloc[:, 0]))
    )
    rec(
        "ctrl_top50",
        f"{len(p9)} rows, top {p9.iloc[0, 0]} {p9.iloc[0, 1]} h, sum {p9.iloc[:, 1].sum():.1f} h",
        f"{len(top)} rows, top {top.iloc[0]['name']} {top.iloc[0]['hours']} h, sum {top['hours'].sum():.1f} h",
        ok9,
    )
    sh = C.groupby("cid").agg(n=("minutes", "size"), avg=("minutes", "mean"))
    sh = sh[sh["n"] >= 3]
    rec("ctrl_shortest_avg", f"{len(P['ctrl_shortest_avg'])} rows", f"{len(sh)} rows", len(sh) == len(P["ctrl_shortest_avg"]))

    # pilot panels
    fl = F.copy()
    fl["dep_home"] = fl["dep"].map(is_home)
    fl["arr_home"] = fl["arr"].map(is_home)
    fl["zzzz"] = (fl["dep"] == "ZZZZ") | (fl["arr"] == "ZZZZ")
    r2 = fl[(fl["dep"] != fl["arr"]) & ~fl["zzzz"] & ~(fl["dep_home"] & fl["arr_home"])]
    g2 = r2.groupby(["dep", "arr"]).agg(flights=("cid", "size"), pilots=("cid", "nunique")).reset_index()
    g2["route"] = g2["dep"] + "-" + g2["arr"]
    g2 = g2.sort_values(["flights", "route"], ascending=[False, True]).head(10)
    mine2 = g2[["route", "dep", "arr", "flights", "pilots"]]
    rec(
        "routes_international",
        "; ".join(f"{r.route} {r.flights}/{r.pilots}" for r in P["routes_international"].head(3).itertuples()) + " ...",
        "; ".join(f"{r.route} {r.flights}/{r.pilots}" for r in mine2.head(3).itertuples()) + " ...",
        same(mine2, P["routes_international"]),
    )

    mv = pd.concat(
        [
            flights.assign(airport=flights["dep"], mt="departure", time=flights["start"])[
                flights["dep"].map(is_home) & ~((flights["dep"] == "ZZZZ") | (flights["arr"] == "ZZZZ"))
            ],
            flights.assign(airport=flights["arr"], mt="arrival", time=flights["end"])[
                flights["arr"].map(is_home) & ~((flights["dep"] == "ZZZZ") | (flights["arr"] == "ZZZZ"))
            ],
        ]
    )
    mv = mv[inw(mv["time"])]
    g6 = mv.groupby("airport").agg(
        departures=("mt", lambda s: int((s == "departure").sum())), arrivals=("mt", lambda s: int((s == "arrival").sum()))
    )
    g6["tot"] = g6["departures"] + g6["arrivals"]
    g6 = g6.reset_index().sort_values(["tot", "airport"], ascending=[False, True]).head(10)
    rec(
        "airports_busiest",
        "; ".join(f"{r.airport} {r.departures}/{r.arrivals}" for r in P["airports_busiest"].head(4).itertuples()) + " ...",
        "; ".join(f"{r.airport} {r.departures}/{r.arrivals}" for r in g6.head(4).itertuples()) + " ...",
        same(g6[["airport", "departures", "arrivals"]], P["airports_busiest"]),
    )

    g7 = mv.groupby("airport").agg(
        departures=("mt", lambda s: int((s == "departure").sum())), arrivals=("mt", lambda s: int((s == "arrival").sum()))
    )
    g7["total_movements"] = g7["departures"] + g7["arrivals"]
    g7 = g7.reset_index().sort_values(["total_movements", "airport"]).head(20)
    p7q = P["airports_quietest"]
    rec(
        "airports_quietest",
        f"{len(p7q)} rows; first {p7q.iloc[0, 0]} {p7q.iloc[0, 3]}; last {p7q.iloc[-1, 0]} {p7q.iloc[-1, 3]}",
        f"{len(g7)} rows; first {g7.iloc[0]['airport']} {g7.iloc[0]['total_movements']}; last {g7.iloc[-1]['airport']} {g7.iloc[-1]['total_movements']}",
        # airports tied on the same total can be cut differently at row 20, so compare the totals and the airports above the cut
        sorted(p7q["total_movements"]) == sorted(g7["total_movements"])
        and set(p7q[p7q["total_movements"] < p7q["total_movements"].max()]["airport"])
        == set(g7[g7["total_movements"] < g7["total_movements"].max()]["airport"]),
    )

    dv = fl[(((fl["end"] - fl["start"]).dt.total_seconds() / 60.0) > 60) & ~fl["zzzz"]].copy()
    dv["route"] = dv["dep"] + "-" + dv["arr"]
    g5 = dv.groupby("cid").agg(unique_routes=("route", "nunique"), total_flights=("route", "size")).reset_index()
    g5 = g5.sort_values(["unique_routes", "total_flights"], ascending=False).head(10)
    p5 = P["routes_diverse"]
    rec(
        "routes_diverse",
        "; ".join(f"{r.unique_routes}/{r.total_flights}" for r in p5.head(5).itertuples()),
        "; ".join(f"{r.unique_routes}/{r.total_flights}" for r in g5.head(5).itertuples()),
        sorted(zip(p5["unique_routes"], p5["total_flights"], strict=True))
        == sorted(zip(g5["unique_routes"], g5["total_flights"], strict=True)),
    )

    # management: unique pilots (all, and with a trailing four-letter home-airport name starting with Y) per month of logon, and controllers
    allf = flights.copy()
    allf["month"] = allf["logon_time"].dt.strftime("%Y-%m-01")
    allf["y"] = allf["name"].map(lambda n: isinstance(n, str) and len(n) >= 4 and n[-4] == "Y")
    pm = allf.groupby("month").agg(total=("cid", "nunique"))
    pm["y"] = allf[allf["y"]].groupby("month")["cid"].nunique()
    cm = ctl.assign(month=ctl["start"].dt.strftime("%Y-%m-01")).groupby("month")["cid"].nunique()
    mine_m = pd.DataFrame({"y": pm["y"].fillna(0).astype(int), "total": pm["total"], "atc": cm}).fillna(0).astype(int).reset_index()
    p3m = P["mgmt_monthly"].copy()
    p3m["month"] = utc(p3m["month"]).dt.strftime("%Y-%m-01")
    rec(
        "mgmt_monthly",
        "; ".join(f"{r.month} Y={r.unique_pilots_with_y} all={r.total_unique_pilots} atc={r.total_unique_atc}" for r in p3m.itertuples()),
        "; ".join(f"{r.month} Y={r.y} all={r.total} atc={r.atc}" for r in mine_m.itertuples()),
        same(mine_m[["month", "y", "total", "atc"]], p3m[["month", "unique_pilots_with_y", "total_unique_pilots", "total_unique_atc"]]),
    )

    al = fl[fl["callsign"].str.match(r"^[A-Z]{3}[0-9]")].copy()
    al["prefix"] = al["callsign"].str[:3]
    al["airline"] = al["prefix"].map(lambda p: airline.get(p, p))
    g8 = al.groupby(["prefix", "airline"]).agg(flights=("cid", "size"), pilots=("cid", "nunique")).reset_index()
    g8 = g8.sort_values(["flights", "pilots"], ascending=False)
    top8 = g8.head(10)
    p8 = P["airlines"]
    ok8 = sorted(zip(top8["flights"], top8["pilots"], strict=True)) == sorted(zip(p8["flights"], p8["pilots"], strict=True))
    tie_ok = ok8 and set(top8["airline"]) ^ set(p8["airline"]) <= set(g8[g8["flights"] == top8["flights"].min()]["airline"])
    rec(
        "airlines",
        "; ".join(f"{r.airline} {r.flights}/{r.pilots}" for r in p8.head(3).itertuples()) + " ...",
        "; ".join(f"{r.airline} {r.flights}/{r.pilots}" for r in top8.head(3).itertuples()) + " ...",
        bool(ok8 and tie_ok),
    )

    fl["region"] = fl["home_icao"].map(lambda c: region_of(c, regions))
    g3 = fl.groupby("region").size().sort_values(ascending=False)
    p3 = P["flights_by_region"]
    rec(
        "flights_by_region",
        "; ".join(f"{r.region} {r.flights}" for r in p3.head(4).itertuples()) + f" ... total {p3['flights'].sum()}",
        "; ".join(f"{k} {v}" for k, v in g3.head(4).items()) + f" ... total {g3.sum()}",
        dict(zip(p3["region"], p3["flights"], strict=True)) == g3.to_dict(),
    )
    from decimal import ROUND_HALF_UP, Decimal

    ab = (
        fl[fl["airborne"] > 0]
        .groupby("region")["airborne"]
        .apply(lambda s: float((sum(Decimal(str(round(x, 2))) for x in s) / Decimal(60)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)))
    )
    p4b = P["airborne_by_region"]
    rec(
        "airborne_by_region",
        "; ".join(f"{r.region} {r.hours}" for r in p4b.head(3).itertuples()) + f" ... total {p4b['hours'].sum():.1f}",
        "; ".join(f"{k} {v}" for k, v in ab.sort_values(ascending=False).head(3).items()) + f" ... total {ab.sum():.1f}",
        all(abs(ab.get(k, -1) - v) < 1e-9 for k, v in zip(p4b["region"], p4b["hours"], strict=True)) and len(ab) == len(p4b),
    )

    fs_ = flights[(flights["start"] >= frm) & (flights["start"] <= to)]
    g13 = fs_.groupby(fs_["start"].dt.strftime("%Y-%m-%d")).size().sort_values(ascending=False).head(5)
    p13 = P["busiest_days_flights"]
    rec(
        "busiest_days_flights",
        "; ".join(f"{r.flight_date} {r.total_flights}" for r in p13.itertuples()),
        "; ".join(f"{k} {v}" for k, v in g13.items()),
        dict(zip(p13["flight_date"].astype(str), p13["total_flights"], strict=True)) == g13.to_dict(),
    )

    rm = fl[fl["remarks"].map(lambda x: btrim(x) != "")]
    mine_selcal = round(100.0 * rm["remarks"].str.contains("SEL/", case=False, regex=False).sum() / len(rm), 2) if len(rm) else None
    rec("selcal", str(P["selcal"].iloc[0, 0]), str(mine_selcal), abs(float(P["selcal"].iloc[0, 0]) - mine_selcal) < 1e-9)

    # most time online: name has 'Y' as 4th-from-last character, airborne > 30 min, ended within 7 days of now
    mt = flights[flights["name"].map(lambda n: isinstance(n, str) and len(n) >= 4 and n[-4] == "Y")]
    mt = mt[(mt["airborne"] > 30) & (mt["end"] >= now - pd.Timedelta(days=7))]
    g16 = mt.groupby(["cid", "name"])["airborne"].sum().reset_index()
    g16["h"] = g16["airborne"].map(lambda x: math.floor(x / 60.0 + 0.5))
    g16 = g16.sort_values(["airborne", "cid"], ascending=[False, True]).head(10)
    p16 = P["most_time_online"]
    rec(
        "most_time_online",
        "; ".join(f"{r.cid}:{r.total_enroute_hours}" for r in p16.head(4).itertuples()) + " ...",
        "; ".join(f"{r.cid}:{r.h}" for r in g16.head(4).itertuples()) + " ...",
        list(zip(p16["cid"], p16["total_enroute_hours"], strict=True)) == list(zip(g16["cid"], g16["h"], strict=True)),
    )

    # highest groundspeeds (last-7-days window is the panel window)
    excl = {
        "E135",
        "E145",
        "E170",
        "E175",
        "E190",
        "E195",
        "F18H",
        "VF35",
        "BE90",
        "GLF7",
        "FAL7",
        "FAL8",
        "FAL9",
        "FAL0",
        "AT42",
        "DH8Q",
        "PC21",
        "EMB1",
        "EMB2",
        "EMB3",
        "EMB4",
        "EMB5",
    }
    h = F.copy()
    h["cat"] = h["acft"].map(lambda c: acft["category"].get(c) if c in acft.index else None)
    h["mil"] = h["acft"].map(lambda c: str(acft["is_military"].get(c)) == "t" if c in acft.index else False)
    h = h[(h["max_gs"] > 100) & (h["max_gs"] <= 750) & (h["max_alt"] > 1000) & h["acft"].notna()]
    h = h[~h["mil"] & ~h["cat"].fillna("").isin(["Supersonic", "Business jet", "Turboprop"]) & ~h["acft"].isin(excl)]
    h = h[~h["acft"].str.match(r"^(MIG|MIR|F-|SU-|TU-)")]
    h = h.sort_values(["callsign", "max_gs", "end"], ascending=[True, False, False]).groupby("callsign").head(1)
    h = h.sort_values("max_gs", ascending=False).head(20)
    p12 = P["highest_gs"]
    panel_rows = set(zip(p12["callsign"], p12["Peak groundspeed (kt)"], p12["Highest altitude (ft)"], strict=True))
    mine_rows = set(zip(h["callsign"], h["max_gs"], h["max_alt"], strict=True))
    # LIMIT 20 on a sort by speed alone: rows tied with the 20th speed may be cut either way, so only those may differ
    cut_speed = min(p12["Peak groundspeed (kt)"]) if len(p12) else None
    ok12 = len(p12) == len(h) and all(r[1] == cut_speed for r in panel_rows ^ mine_rows)
    rec(
        "highest_gs",
        "; ".join(f"{r.callsign} {r[6]}" for r in p12.head(3).itertuples()) + " ...",
        "; ".join(f"{r.callsign} {r.max_gs}" for r in h.head(3).itertuples()) + " ...",
        ok12,
    )

    # active pilots and controllers
    ap_ = F[F["airborne"] >= 30]["cid"].nunique()
    cw = ctl[(inw(ctl["start"]) | inw(ctl["end"])) & (ctl["minutes"] >= 30)]
    label = {1: "Obs", 2: "S1", 3: "S2", 4: "S3", 5: "C1", 6: "C2", 7: "C3", 8: "I1", 9: "I2", 10: "I3"}
    lab = cw.assign(l=cw["rating"].map(lambda r: label.get(r, "Unknown"))).groupby("l").agg(n=("cid", "nunique"), r=("rating", "min"))
    mine7 = [("Pilots", ap_)] + [(k, int(v.n)) for k, v in lab.sort_values("r").iterrows()]
    p7 = P["active_people"]
    rec(
        "active_people",
        "; ".join(f"{r.label}={r.people}" for r in p7.itertuples()),
        "; ".join(f"{k}={v}" for k, v in mine7),
        list(zip(p7["label"], p7["people"], strict=True)) == mine7,
    )

    # monitoring
    tx = now
    ok24 = pl_ok[pl_ok["snapshot_ts"] >= tx - pd.Timedelta(hours=24)]
    go_live = ns["observed_at"].min()
    expected = math.floor((tx - max(tx - pd.Timedelta(hours=24), go_live)).total_seconds() / 15)
    missed = max(0.0, round(100.0 * (1 - len(ok24) / expected), 2))
    rec(
        "snapshots_missed",
        f"{P['snapshots_missed'].iloc[0, 0]} %",
        f"{missed} % ({len(ok24)} of {expected} expected)",
        abs(float(P["snapshots_missed"].iloc[0, 0]) - missed) < 0.011,
    )
    nulls = int(pos["gs_kt"].isna().sum() + pos["alt_ft"].isna().sum())
    rec(
        "rejected_values",
        str(P["rejected_values"].iloc[0, 0]),
        f"{nulls} (NULL gs/alt in pilot_position)",
        int(P["rejected_values"].iloc[0, 0]) == nulls,
    )

    print(
        f"Capture transaction time {now:%d-%m-%Y %H:%M:%S} UTC; flight_summary refresh cutoff {cutoff:%H:%M:%S} UTC; "
        f"window from {frm:%d-%m-%Y %H:%M} to now\n"
    )
    print("| Panel | Displayed (panel SQL) | Independent recompute | Agree |\n|---|---|---|---|")
    for k, s, m, ok in results:
        print(f"| {k} | {s} | {m} | {'YES' if ok else 'NO'} |")
    bad = [r for r in results if not r[3]]
    print(f"\npanels compared: {len(results)}; agree: {len(results) - len(bad)}; disagree: {len(bad)}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    re  # noqa: B018 (re is imported for parity with the other audit scripts)
    main()
