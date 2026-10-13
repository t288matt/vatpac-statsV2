"""Capture, in ONE repeatable-read READ ONLY transaction on the live database, (a) CSV copies of the raw tables the
audit needs and (b) the result of each audited panel query (the panel's own rawSql with the Grafana macros replaced).
One transaction means the raw data and the panel figures describe exactly the same instant.

READ ONLY: BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; plus SET default_transaction_read_only = on; only SELECT and
COPY ... TO STDOUT are issued. Nothing is written to the database.

Usage: python capture.py OUTDIR [--window-from "2026-10-08 06:00:00+00"] [--tz Australia/Sydney] [--host vatpac-v3]
Without --window-from the panels are run over the last 200 days (everything).
"""

# ruff: noqa: E501, E731  (long literal SQL and short lambdas are clearer in a one-off audit script)
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# panel key -> (dashboard file stem, panel id)
PANELS = {
    "pilots_online_region": ("pilots-now", 2),
    "pilots_online_global": ("pilots-now", 3),
    "pilots_over_time": ("pilots-now", 4),
    "ctrl_online_now": ("atc-overview", 2),
    "ctrl_over_time": ("atc-overview", 3),
    "ctrl_logged_on_30d": ("atc-overview", 5),
    "ctrl_monthly_by_rating": ("atc-overview", 6),
    "ctrl_ctr_fss_monthly": ("atc-overview", 7),
    "ctrl_active_monthly": ("atc-overview", 8),
    "ctrl_top50": ("atc-overview", 9),
    "ctrl_shortest_avg": ("atc-overview", 10),
    "routes_international": ("pilots-traffic", 2),
    "airports_busiest": ("pilots-traffic", 6),
    "airports_quietest": ("pilots-traffic", 7),
    "routes_diverse": ("pilots-traffic", 5),
    "mgmt_monthly": ("management", 3),
    "airlines": ("pilots-traffic", 8),
    "highest_gs": ("pilots-traffic", 12),
    "busiest_days_flights": ("pilots-traffic", 13),
    "selcal": ("pilots-traffic", 15),
    "most_time_online": ("pilots-traffic", 16),
    "flights_by_region": ("pilot-origins", 3),
    "airborne_by_region": ("pilot-origins", 4),
    "active_people": ("pilot-origins", 7),
    "snapshots_missed": ("collector-health", 6),
    "rejected_values": ("collector-health", 9),
}

RAW = {
    "pilot_session": "SELECT * FROM pilot_session ORDER BY id",
    "pilot_position": "SELECT session_id, to_char(observed_at AT TIME ZONE 'utc','YYYY-MM-DD HH24:MI:SS.US') AS observed_at, lat, lon, alt_ft, gs_kt, hdg FROM pilot_position ORDER BY session_id, observed_at",
    "controller_session": "SELECT * FROM controller_session ORDER BY id",
    "network_snapshot": "SELECT * FROM network_snapshot ORDER BY observed_at",
    "poll_log": "SELECT id, polled_at, status, snapshot_ts, pilots_seen, pilots_inside, pilots_with_plan, pilots_kept, controllers_seen, controllers_kept, rejected_values FROM poll_log ORDER BY id",
    "flight_summary": "SELECT * FROM flight_summary ORDER BY session_id, start_time",
    "airline": "SELECT * FROM airline",
    "aircraft_type": "SELECT * FROM aircraft_type",
    "region_map": "SELECT * FROM region_map",
    "app_setting": "SELECT * FROM app_setting",
    "clock": "SELECT now() AS tx_now, clock_timestamp() AS wall",
}


def render(sql: str, frm: str | None) -> str:
    """Replace the Grafana macros by plain SQL. Time-series bucketing is fixed at one hour."""
    f = f"'{frm}'::timestamptz" if frm else "(now() - interval '200 days')"
    t = "(now())"
    sql = re.sub(r"\$__timeFilter\(([^)]+)\)", lambda m: f"{m.group(1)} BETWEEN {f} AND {t}", sql)
    sql = re.sub(
        r"\$__timeGroupAlias\(([^,]+),\s*\$__interval\)",
        lambda m: f'to_timestamp(floor(extract(epoch FROM {m.group(1)}) / 3600) * 3600) AS "time"',
        sql,
    )
    return sql.replace("$__timeFrom()", f).replace("$__timeTo()", t).rstrip().rstrip(";")


def panel_sql(stem: str, pid: int) -> str:
    path = next((ROOT / "grafana/dashboards").glob(f"*/{stem}.json"))
    d = json.loads(path.read_text(encoding="utf-8"))

    def walk(ps):
        for p in ps:
            if "panels" in p:
                yield from walk(p["panels"])
            else:
                yield p

    p = next(p for p in walk(d["panels"]) if p.get("id") == pid)
    return p["targets"][0]["rawSql"]


def build(frm: str | None, tz: str | None = None) -> str:
    out = ["SET default_transaction_read_only = on;", "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;"]
    if tz:  # session time zone only (a read-only SET); used to test that panels do not depend on it
        out.append(f"SET TIME ZONE '{tz}';")
    for name, sql in RAW.items():
        out += [f"\\echo ##### raw_{name}", f"COPY ({sql}) TO STDOUT WITH CSV HEADER;"]
    for key, (stem, pid) in PANELS.items():
        out += [f"\\echo ##### panel_{key}", f"COPY (\n{render(panel_sql(stem, pid), frm)}\n) TO STDOUT WITH CSV HEADER;"]
    out.append("COMMIT;")
    return "\n".join(out) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("outdir")
    ap.add_argument("--window-from")
    ap.add_argument("--tz", help="session time zone for the panel queries, e.g. Australia/Sydney (default: the role's, UTC)")
    ap.add_argument("--host", default="vatpac-v3")
    a = ap.parse_args()
    remote = (
        "cd ~/vatpac-statsV2 && docker compose exec -T db sh -c " '\'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -q -v ON_ERROR_STOP=1\''
    )
    res = subprocess.run(["ssh", a.host, remote], input=build(a.window_from, a.tz), capture_output=True, text=True)
    if res.returncode:
        raise SystemExit(res.stderr[-2000:])
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    name, buf = None, []
    for line in res.stdout.splitlines(keepends=True):
        if line.startswith("##### "):
            if name:
                (out / f"{name}.csv").write_text("".join(buf), encoding="utf-8", newline="\n")
            name, buf = line[6:].strip(), []
        elif name and line.strip() != "SET":
            buf.append(line)
    if name:
        (out / f"{name}.csv").write_text("".join(buf), encoding="utf-8", newline="\n")
    print("captured", len(list(out.glob("*.csv"))), "files into", out)


if __name__ == "__main__":
    main()
