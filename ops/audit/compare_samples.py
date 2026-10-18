"""Compare the audit's independent per-snapshot results (analysis.jsonl from feed_sampler.py) with what the live
database stored for the same update_timestamp. READ-ONLY: one SELECT-only SQL session per run, sent over ssh to
`psql` inside the db container, with default_transaction_read_only = on.

Usage: python compare_samples.py SAMPLE_DIR [--host vatpac-v3] [--out report.json]

Checks per snapshot timestamp:
  network_snapshot : pilots_global, controllers_global, pilots_in_region, controllers_in_region
  poll_log (ok)    : pilots_seen, pilots_inside, pilots_with_plan, pilots_kept, controllers_seen, controllers_kept,
                     rejected_values
  pilots           : exact set of (cid, callsign, logon_time) stored at observed_at = ts; per pilot lat, lon, gs, alt, hdg
  controllers      : each whitelisted controller present in the feed has a session spanning ts with the right
                     logon_time; rating and frequency taken from the event tables as at ts; no unexpected sessions whose
                     last_seen == ts
"""

from __future__ import annotations

import argparse
import gzip
import json
import subprocess
from datetime import datetime
from pathlib import Path

from independent import analyse, load_ring, load_whitelist


def iso(s: str) -> datetime:
    import re

    s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", "+00:00"))
    return datetime.fromisoformat(s)


def build_sql(stamps: list[str]) -> str:
    vals = ",".join(f"('{s}'::timestamptz)" for s in stamps)
    return f"""SET default_transaction_read_only = on;
\\pset tuples_only on
\\pset format unaligned
WITH ts(t) AS (VALUES {vals})
SELECT json_build_object(
 'ts', to_char(t at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
 'ns', (SELECT row_to_json(n) FROM network_snapshot n WHERE n.observed_at = t),
 'polls', (SELECT coalesce(json_agg(row_to_json(p)), '[]') FROM (SELECT status, pilots_seen, pilots_inside, pilots_with_plan,
          pilots_kept, controllers_seen, controllers_kept, rejected_values FROM poll_log WHERE snapshot_ts = t) p),
 'pilots', (SELECT coalesce(json_agg(json_build_object('cid', s.cid, 'callsign', s.callsign,
          'logon', to_char(s.logon_time at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS.US'), 'lat', pp.lat, 'lon', pp.lon,
          'gs', pp.gs_kt, 'alt', pp.alt_ft, 'hdg', pp.hdg, 'name', s.name, 'home_icao', s.home_icao, 'server', s.server,
          'departure', s.departure, 'arrival', s.arrival, 'aircraft_icao', s.aircraft_icao, 'aircraft_faa', s.aircraft_faa,
          'flight_rules', s.flight_rules, 'route', s.route, 'remarks', s.remarks, 'planned_alt', s.planned_alt,
          'deptime', s.deptime, 'pilot_rating', s.pilot_rating, 'military_rating', s.military_rating,
          'plan_changed_at', s.plan_changed_at, 'last_seen', s.last_seen)), '[]')
          FROM pilot_position pp JOIN pilot_session s ON s.id = pp.session_id WHERE pp.observed_at = t),
 'ctrls', (SELECT coalesce(json_agg(json_build_object('cid', c.cid, 'callsign', c.callsign,
          'logon', to_char(c.logon_time at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
          'last_seen_is_ts', c.last_seen = t,
          'rating_at_ts', (SELECT e.rating FROM controller_rating_event e WHERE e.session_id = c.id AND e.observed_at <= t
                           ORDER BY e.observed_at DESC LIMIT 1),
          'freq_at_ts', (SELECT e.frequency_hz FROM controller_frequency_event e WHERE e.session_id = c.id AND e.observed_at <= t
                           ORDER BY e.observed_at DESC LIMIT 1))), '[]')
          FROM controller_session c WHERE c.first_seen <= t AND c.last_seen >= t)
) FROM ts ORDER BY t;
"""


def run_sql(sql: str, host: str) -> list[dict]:
    remote = (
        "cd ~/vatpac-statsV2 && docker compose exec -T db sh -c " '\'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -v ON_ERROR_STOP=1\''
    )
    out = subprocess.run(["ssh", host, remote], input=sql, capture_output=True, text=True, check=True).stdout
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def key(cid, callsign, logon) -> tuple:
    return (int(cid), callsign, iso(logon).replace(tzinfo=None))  # both sides are UTC


def compare(mine: dict, db: dict) -> list[tuple[str, str]]:
    """Return a list of (difference type, detail) for one snapshot; empty means everything matched."""
    diffs: list[tuple[str, str]] = []
    ns = db["ns"]
    if ns is None:
        return [("snapshot_missing", "no network_snapshot row")]
    for mk, dk in (  # pilots_in_region is the pilots stored, so it also differs when the collector rejected a record
        ("pilots_global", "pilots_global"),
        ("controllers_global", "controllers_global"),
        ("pilots_inside", "pilots_in_region"),
        ("controllers_kept", "controllers_in_region"),
    ):
        if mine[mk] != ns[dk]:
            diffs.append(("network_snapshot_count", f"{dk}: independent {mine[mk]} stored {ns[dk]}"))
    ok = [p for p in db["polls"] if p["status"] == "ok"]
    if not ok:
        diffs.append(("poll_log_missing", "no ok poll_log row"))
    for p in ok[:1]:
        for mk, dk in (
            ("pilots_global", "pilots_seen"),
            ("pilots_inside", "pilots_inside"),
            ("pilots_inside", "pilots_kept"),
            ("pilots_with_plan", "pilots_with_plan"),
            ("controllers_global", "controllers_seen"),
            ("controllers_kept", "controllers_kept"),
            ("rejected_values", "rejected_values"),
        ):
            if mine[mk] != p[dk]:
                diffs.append(("poll_log_count", f"{dk}: independent {mine[mk]} stored {p[dk]}"))
    mp = {key(k["cid"], k["callsign"], k["logon_time"]): k for k in mine["kept_pilots"]}
    dp = {key(k["cid"], k["callsign"], k["logon"]): k for k in db["pilots"]}
    for k in sorted(set(mp) - set(dp), key=str):
        diffs.append(("pilot_missing_in_db", f"{k[0]} {k[1]} should be stored"))
    for k in sorted(set(dp) - set(mp), key=str):
        diffs.append(("pilot_extra_in_db", f"{k[0]} {k[1]} stored but not kept by the independent rule"))
    for k in set(mp) & set(dp):
        a, b = mp[k], dp[k]
        for fa, fb in (("lat", "lat"), ("lon", "lon"), ("gs", "gs"), ("alt", "alt"), ("hdg", "hdg")):
            va, vb = a[fa], b[fb]
            if va is None and vb is None:
                continue
            if va is None or vb is None or abs(float(va) - float(vb)) > 1e-9:
                diffs.append(("pilot_field", f"{k[1]} {fa}: feed/capped {va} stored {vb}"))
        ts = iso(mine["update_timestamp"])
        later = b.get("last_seen") and iso(b["last_seen"]) > ts  # the session was seen again after this snapshot
        pairs = [("name", a["name"], b["name"]), ("departure", a["dep"], b["departure"]), ("arrival", a["arr"], b["arrival"])]
        pairs += [
            (f, a["plan"][f], b[f])
            for f in (
                "aircraft_icao",
                "aircraft_faa",
                "flight_rules",
                "route",
                "remarks",
                "planned_alt",
                "deptime",
                "server",
                "pilot_rating",
                "military_rating",
                "home_icao",
            )
        ]
        for f, va, vb in pairs:
            if va != vb:
                # a later snapshot may legitimately have changed the stored value (latest non-null wins)
                kind = "pilot_attr_changed_later" if later else "pilot_attr"
                diffs.append((kind, f"{k[1]} {f}: feed {str(va)[:40]!r} stored {str(vb)[:40]!r}"))
    mc = {key(c["cid"], c["callsign"], c["logon_time"]): c for c in mine["kept_controllers"]}
    dc = {key(c["cid"], c["callsign"], c["logon"]): c for c in db["ctrls"]}
    for k in sorted(set(mc) - set(dc), key=str):
        diffs.append(("controller_missing_in_db", f"{k[1]} should be stored"))
    for k in sorted(set(dc) - set(mc), key=str):
        if dc[k]["last_seen_is_ts"]:
            diffs.append(("controller_extra_in_db", f"{k[1]} last_seen == ts but absent from independent kept set"))
    for k in set(mc) & set(dc):
        a, b = mc[k], dc[k]
        if a["rating"] != b["rating_at_ts"]:
            diffs.append(("controller_rating", f"{k[1]}: feed {a['rating']} stored event {b['rating_at_ts']}"))
        if a["freq_hz"] != b["freq_at_ts"]:
            diffs.append(("controller_frequency", f"{k[1]}: independent {a['freq_hz']} stored event {b['freq_at_ts']}"))
    return diffs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("sample_dir")
    ap.add_argument("--host", default="vatpac-v3")
    ap.add_argument("--out")
    ap.add_argument("--markdown", help="write the per-snapshot table as markdown to this file")
    a = ap.parse_args()
    root = Path(__file__).resolve().parents[2]
    ring = load_ring(root / "config/australian_airspace_polygon.json")
    wl = load_whitelist(root / "config/controller_callsigns_list.txt")
    rows = []
    for f in sorted(Path(a.sample_dir).glob("raw_*.json.gz")):  # the independent analysis is recomputed from the raw payloads
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            rows.append(analyse(json.load(fh), ring, wl))
    stamps = [iso(r["update_timestamp"]).isoformat() for r in rows]
    db = {d["ts"]: d for d in run_sql(build_sql(stamps), host=a.host)}
    report, total = [], 0
    for r in rows:
        key_ts = iso(r["update_timestamp"]).strftime("%Y-%m-%dT%H:%M:%S.%f")
        d = db.get(key_ts)
        diffs = compare(r, d) if d else [("snapshot_missing", "no row returned")]
        total += len(diffs)
        report.append(
            {
                "ts": r["update_timestamp"],
                "pilots_global": r["pilots_global"],
                "inside": r["pilots_inside"],
                "ctrl_kept": r["controllers_kept"],
                "ctrl_global": r["controllers_global"],
                "stored_in_region": d["ns"]["pilots_in_region"] if d and d["ns"] else None,
                "stored_ctrl": d["ns"]["controllers_in_region"] if d and d["ns"] else None,
                "n_diffs": len(diffs),
                "diffs": diffs,
            }
        )
        print(
            r["update_timestamp"],
            r["pilots_global"],
            r["pilots_inside"],
            report[-1]["stored_in_region"],
            r["controllers_kept"],
            report[-1]["stored_ctrl"],
            "OK" if not diffs else diffs[:3],
        )
    by_type: dict[str, int] = {}
    for x in report:
        for kind, _ in x["diffs"]:
            by_type[kind] = by_type.get(kind, 0) + 1
    print(
        f"snapshots compared: {len(rows)}; snapshots with differences: {sum(1 for x in report if x['n_diffs'])}; "
        f"total differences: {total}; by type: {by_type}"
    )
    if a.markdown:
        lines = [
            "| Snapshot (UTC) | Feed pilots | Independent in-region | Stored | Independent controllers | Stored "
            "| Unexplained differences | Plan amended later |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for x in report:
            hard = [d for d in x["diffs"] if d[0] != "pilot_attr_changed_later"]
            later = len(x["diffs"]) - len(hard)
            lines.append(
                f"| {x['ts'][:19].replace('T', ' ')} | {x['pilots_global']} | {x['inside']} | {x['stored_in_region']} | "
                f"{x['ctrl_kept']} | {x['stored_ctrl']} | {len(hard)} | {later} |"
            )
        Path(a.markdown).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    if a.out:
        Path(a.out).write_text(json.dumps(report, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
