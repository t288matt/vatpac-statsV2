"""End-to-end replay of REAL raw feed payloads through the collector's own parse + apply code into a THROWAWAY database,
then comparison with the audit's independent results (analysis.jsonl from feed_sampler.py). Also injects poison records and
stale / future timestamps. Run it ONLY against a scratch database (it TRUNCATEs); it refuses to run unless the database name
ends in "t30b" or "_test".

Run inside a short-lived container from the collector image (the code under test), with the host network, e.g.
  docker run --rm --network host -e PGPASSWORD=... -v /tmp/vp-t30b:/work:ro --entrypoint python \
     vatpac-stats-collector:latest /work/audit/throwaway/replay_check.py --port 32769 --db t30b --samples /work/samples --config /work/config
"""

# ruff: noqa: E501, E731  (long literal SQL and short lambdas are clearer in a one-off audit script)
from __future__ import annotations

import argparse
import asyncio
import copy
import gzip
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from vatpac.feed import FetchResult
from vatpac.filters import load_reference
from vatpac.main import Collector
from vatpac.model import parse_snapshot
from vatpac.settings import Settings
from vatpac.store import apply_snapshot

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)


def ts_key(s: str) -> str:
    return re.sub(r"(\.\d{6})\d+", r"\1", s).replace("Z", "+00:00")


def dt(s: str) -> datetime:
    return datetime.fromisoformat(ts_key(s))


def wipe(conn) -> None:
    conn.execute(
        "TRUNCATE pilot_position, pilot_session, controller_session, controller_rating_event, controller_frequency_event, "
        "network_snapshot, poll_log RESTART IDENTITY CASCADE"
    )


def signature(conn) -> dict:
    q = lambda s: conn.execute(s).fetchall()  # noqa: E731
    return {
        "pilot_session": q(
            "SELECT cid, callsign, logon_time, first_seen, last_seen, departure, arrival, max_gs_kt, max_alt_ft FROM pilot_session ORDER BY 1,2,3"
        ),
        "pilot_position": q(
            "SELECT s.cid, s.callsign, p.observed_at, p.lat, p.lon, p.alt_ft, p.gs_kt, p.hdg FROM pilot_position p JOIN pilot_session s ON s.id=p.session_id ORDER BY 1,2,3"
        ),
        "controller_session": q(
            "SELECT cid, callsign, logon_time, first_seen, last_seen, rating, frequency_hz FROM controller_session ORDER BY 1,2,3"
        ),
        "events": q(
            "SELECT c.cid, c.callsign, 'r' k, e.observed_at, e.rating::bigint v FROM controller_rating_event e JOIN controller_session c ON c.id=e.session_id UNION ALL SELECT c.cid, c.callsign, 'f', e.observed_at, e.frequency_hz FROM controller_frequency_event e JOIN controller_session c ON c.id=e.session_id ORDER BY 1,2,3,4"
        ),
        "network_snapshot": q("SELECT * FROM network_snapshot ORDER BY 1"),
    }


def apply(conn, payload, ref):
    return apply_snapshot(
        conn, parse_snapshot(payload), ref.polygon, ref.whitelist, polygon_sha=ref.polygon_sha, whitelist_sha=ref.whitelist_sha
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--samples", required=True)
    ap.add_argument("--config", required=True)
    a = ap.parse_args()
    if not (a.db.endswith("t30b") or a.db.endswith("_test")):
        raise SystemExit("refusing: not a scratch database")
    dsn = f"host=127.0.0.1 port={a.port} dbname={a.db} user=postgres password={os.environ['PGPASSWORD']}"
    ref = load_reference(Path(a.config) / "australian_airspace_polygon.json", Path(a.config) / "controller_callsigns_list.txt")
    samples = sorted(Path(a.samples).glob("raw_*.json.gz"))
    analysis = {dt(r["update_timestamp"]): r for r in map(json.loads, (Path(a.samples) / "analysis.jsonl").read_text().splitlines())}
    payloads = []
    for p in samples:
        with gzip.open(p, "rt") as fh:
            payloads.append(json.load(fh))
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    wipe(conn)

    # 1. real payloads, in order, compared one by one with the independent analysis
    print(f"== 1. replay of {len(payloads)} real payloads")
    bad = 0
    for pl in payloads:
        ts = dt(pl["general"]["update_timestamp"])
        mine = analysis[ts]
        res = apply(conn, pl, ref)
        ns = conn.execute("SELECT * FROM network_snapshot WHERE observed_at=%s", (ts,)).fetchone()
        got_p = {
            (r["cid"], r["callsign"])
            for r in conn.execute(
                "SELECT s.cid, s.callsign FROM pilot_position p JOIN pilot_session s ON s.id=p.session_id WHERE p.observed_at=%s", (ts,)
            )
        }
        exp_p = {(k["cid"], k["callsign"]) for k in mine["kept_pilots"]}
        exp_c = {(k["cid"], k["callsign"]) for k in mine["kept_controllers"]}
        got_c = {(r["cid"], r["callsign"]) for r in conn.execute("SELECT cid, callsign FROM controller_session WHERE last_seen=%s", (ts,))}
        ok = (
            ns["pilots_in_region"] == mine["pilots_inside"]
            and ns["controllers_in_region"] == mine["controllers_kept"]
            and ns["pilots_global"] == mine["pilots_global"]
            and ns["controllers_global"] == mine["controllers_global"]
            and got_p == exp_p
            and got_c == exp_c
            and res.pilots_with_plan == mine["pilots_with_plan"]
            and res.rejected_values == mine["rejected_values"]
        )
        bad += not ok
    check("real payloads reproduce the independent per-pilot / per-controller result", bad == 0, f"{len(payloads)} snapshots, {bad} differ")
    fwd = signature(conn)

    # 2. idempotence: replay everything again, nothing may change
    inserted = sum(apply(conn, pl, ref).positions_inserted for pl in payloads)
    check("replaying the same snapshots inserts zero positions", inserted == 0, f"inserted={inserted}")
    check("replay leaves every table identical", signature(conn) == fwd)
    last = payloads[-1]
    before_x = conn.execute("SELECT count(*) n, sum(xmin::text::bigint) x FROM pilot_session").fetchone()
    apply(conn, last, ref)  # the newest snapshot again: no row may be rewritten
    after_x = conn.execute("SELECT count(*) n, sum(xmin::text::bigint) x FROM pilot_session").fetchone()
    check("re-delivering the newest snapshot does not rewrite pilot_session rows (xmin unchanged)", before_x == after_x)

    # 3. order independence: reversed order; list what differs (plan fields are 'latest delivered non-null wins')
    wipe(conn)
    for pl in reversed(payloads):
        apply(conn, pl, ref)
    rev = signature(conn)
    diff_tables = [k for k in fwd if fwd[k] != rev[k]]
    check(
        "reverse-order replay gives identical sessions, positions, controller sessions and snapshots",
        not [k for k in diff_tables if k != "events"],
        f"tables that differ: {diff_tables}",
    )
    print("INFO  controller rating / frequency event tables are order dependent:", "differ" if "events" in diff_tables else "identical")
    for k in diff_tables:
        a_, b_ = fwd[k], rev[k]
        changed = [(x, y) for x, y in zip(a_, b_, strict=False) if x != y]
        print(
            f"INFO  {k}: {len(changed)} rows differ, e.g. {[(x.get('callsign'), {c: (x[c], y[c]) for c in x if x[c] != y[c]}) for x, y in changed[:3]]}"
        )
    wipe(conn)

    # 4. poison and edge records inside a real payload
    print("== 4. poison records")
    base = copy.deepcopy(payloads[-1])
    t0 = dt(base["general"]["update_timestamp"])
    logon = "2026-10-08T10:00:00.1234567Z"

    def pilot(cs, lat=-33.9, lon=151.2, cid=9100001, **kw):
        p = {
            "cid": cid,
            "name": "Audit Pilot YSSY",
            "callsign": cs,
            "server": "AUDIT",
            "pilot_rating": 0,
            "military_rating": 0,
            "latitude": lat,
            "longitude": lon,
            "altitude": 5000,
            "groundspeed": 250,
            "transponder": "2000",
            "heading": 90,
            "qnh_i_hg": 29.9,
            "qnh_mb": 1013,
            "flight_plan": {
                "flight_rules": "I",
                "aircraft_short": "B738",
                "departure": "YSSY",
                "arrival": "YMML",
                "altitude": "35000",
                "route": "DCT",
                "remarks": "SEL/ABCD",
            },
            "logon_time": logon,
            "last_updated": "2026-10-08T12:00:00Z",
        }
        p.update(kw)
        return p

    def ctrl(cs, freq="124.200", cid=9200001, **kw):
        c = {
            "cid": cid,
            "name": "Audit Ctrl",
            "callsign": cs,
            "frequency": freq,
            "facility": 4,
            "rating": 3,
            "server": "AUDIT",
            "visual_range": 50,
            "text_atis": None,
            "last_updated": "2026-10-08T12:00:00Z",
            "logon_time": logon,
        }
        c.update(kw)
        return c

    cases = {
        "ok": pilot("AUD001"),
        "ok_duplicate_in_same_snapshot": pilot("AUD001"),
        "reconnect_new_logon": pilot("AUD001", logon_time="2026-10-08T10:05:00Z"),
        "lat_string": pilot("AUD002", lat="abc"),
        "lat_null": pilot("AUD003", lat=None, lon=None),
        "lat_nan": pilot("AUD004", lat=float("nan")),
        "on_vertex": pilot("AUD005", lat=-6.0, lon=75.0),
        "outside_nz": pilot("AUD006", lat=-37.0, lon=174.8),
        "alt_int32_overflow": pilot("AUD007", altitude=3_000_000_000),
        "gs_701": pilot("AUD008", groundspeed=701),
        "gs_negative": pilot("AUD009", groundspeed=-5),
        "alt_65001": pilot("AUD010", altitude=65001),
        "hdg_400": pilot("AUD011", heading=400),
        "empty_callsign": pilot(""),
        "nul_in_callsign": pilot("AUD\x0012"),
        "surrogate_name": pilot("AUD013", name="Bad \ud800 name"),
        "bad_logon": pilot("AUD014", logon_time="garbage"),
        "missing_logon": {k: v for k, v in pilot("AUD015").items() if k != "logon_time"},
        "cid_string_number": pilot("AUD016", cid="9100016"),
        "cid_string_junk": pilot("AUD017", cid="x"),
        "no_flight_plan": pilot("AUD018", flight_plan=None),
        "blank_dep_arr": pilot("AUD019", flight_plan={"departure": "  ", "arrival": "\t"}),
        "huge_text": pilot("AUD020", name="N" * 100000),
    }
    pl = copy.deepcopy(base)
    pl["pilots"] = pl["pilots"] + list(cases.values())
    pl["controllers"] = pl["controllers"] + [
        ctrl("SY_TWR", freq="199.998", cid=9200001),
        ctrl("MK_TWR", freq="abc", cid=9200002),
        ctrl("BN_GND", freq="", cid=9200003),
        ctrl("SY_OBS", cid=9200004),
        ctrl("SY_NEW", cid=9200005),
        ctrl("SY_ATIS", cid=9200006),
        ctrl("sy_twr", cid=9200007),
        ctrl("SY_TWR ", cid=9200008),
        ctrl("ZZ_NEW", cid=9200009),
        ctrl("", cid=9200010),
        ctrl("SY_GND", cid=None),
    ]
    res = apply(conn, json.loads(json.dumps(pl, allow_nan=True)), ref)
    names = {r["callsign"] for r in conn.execute("SELECT callsign FROM pilot_session")}
    n = lambda cs: cs in names  # noqa: E731
    check(
        "poison: duplicate record in one snapshot stores one session and one position",
        conn.execute(
            "SELECT count(*) n FROM pilot_session WHERE callsign='AUD001' AND logon_time='2026-10-08T10:00:00.123456Z'"
        ).fetchone()["n"]
        == 1
        and conn.execute(
            "SELECT count(*) n FROM pilot_position p JOIN pilot_session s ON s.id=p.session_id WHERE s.callsign='AUD001' AND s.logon_time='2026-10-08T10:00:00.123456Z'"
        ).fetchone()["n"]
        == 1,
    )
    check(
        "poison: same callsign with a new logon_time is a second session",
        conn.execute("SELECT count(*) n FROM pilot_session WHERE callsign='AUD001'").fetchone()["n"] == 2,
    )
    row = lambda cs: conn.execute(
        "SELECT p.* FROM pilot_position p JOIN pilot_session s ON s.id=p.session_id WHERE s.callsign=%s", (cs,)
    ).fetchone()  # noqa: E731
    r = row("AUD002")
    print(
        "INFO  pilot with latitude 'abc' (unparsable):",
        "stored with NULL position (passes the no-position rule)" if r and r["lat"] is None else "not stored",
    )
    check("poison: unparsable latitude is not silently stored with a position", r is None or r["lat"] is None)
    r = row("AUD003")
    check("no-position pilot passes the filter (v1 rule) and is stored with NULL lat/lon", r is not None and r["lat"] is None)
    check("NaN latitude is not stored", not n("AUD004"))
    check("point exactly on a polygon vertex is excluded", not n("AUD005"))
    check("pilot outside the polygon (NZ) is not stored", not n("AUD006"))
    r = row("AUD007")
    print("INFO  altitude 3e9 (beyond int32):", f"stored alt_ft={r['alt_ft']}" if r else "not stored")
    check("altitude beyond int32 does not drop the pilot, altitude is NULL", r is not None and r["alt_ft"] is None)
    check("gs 701 -> NULL", row("AUD008")["gs_kt"] is None)
    check("gs -5 -> NULL", row("AUD009")["gs_kt"] is None)
    check("alt 65001 -> NULL", row("AUD010")["alt_ft"] is None)
    check("heading 400 -> NULL, pilot kept", row("AUD011") is not None and row("AUD011")["hdg"] is None)
    check("empty callsign skipped", conn.execute("SELECT count(*) n FROM pilot_session WHERE callsign=''").fetchone()["n"] == 0)
    check("NUL in callsign removed, pilot kept as AUD12", n("AUD12"))
    check("lone surrogate in name does not abort the snapshot", n("AUD013"))
    check("bad logon_time skipped", not n("AUD014"))
    check("missing logon_time skipped", not n("AUD015"))
    check("cid given as numeric string is accepted", n("AUD016"))
    check("non-numeric cid skipped", not n("AUD017"))
    check(
        "no flight plan: pilot stored with NULL plan",
        n("AUD018") and conn.execute("SELECT departure FROM pilot_session WHERE callsign='AUD018'").fetchone()["departure"] is None,
    )
    check(
        "blank departure/arrival stored as NULL (flight gate fails)",
        conn.execute("SELECT departure, arrival FROM pilot_session WHERE callsign='AUD019'").fetchone()
        == {"departure": None, "arrival": None},
    )
    r = conn.execute("SELECT length(name) n FROM pilot_session WHERE callsign='AUD020'").fetchone()
    print("INFO  100 000 character name: stored length", r["n"] if r else "not stored")
    ctl = {r["callsign"]: r for r in conn.execute("SELECT * FROM controller_session WHERE cid >= 9200000 AND cid <> 9200008")}
    ctl_all = list(conn.execute("SELECT * FROM controller_session WHERE cid >= 9200000"))
    check("199.998 stored as NULL frequency", ctl.get("SY_TWR") and ctl["SY_TWR"]["frequency_hz"] is None)
    check("junk and empty frequency stored as NULL", ctl["MK_TWR"]["frequency_hz"] is None and ctl["BN_GND"]["frequency_hz"] is None)
    check("controller with trailing space in callsign is matched after stripping", any(r["cid"] == 9200008 for r in ctl_all))
    check(
        "_OBS, unlisted, ATIS-style and lower-case callsigns are not stored",
        not ({"SY_OBS", "SY_NEW", "SY_ATIS", "sy_twr", "ZZ_NEW"} & set(ctl)),
    )
    check("controller with null cid / empty callsign skipped", not any(r["cid"] is None for r in ctl.values()) and "" not in ctl)
    ns = conn.execute("SELECT * FROM network_snapshot WHERE observed_at=%s", (t0,)).fetchone()
    print(
        f"INFO  network_snapshot after poison: pilots_in_region={ns['pilots_in_region']} pilots_global={ns['pilots_global']} (raw array length {len(pl['pilots'])}); "
        f"poll counts: seen={res.pilots_seen} inside={res.pilots_inside} kept={res.pilots_kept} rejected={res.rejected_values}"
    )
    check("pilots_global counts every raw array entry, including malformed ones", ns["pilots_global"] == len(pl["pilots"]))

    # 5. stale / future / duplicate timestamps through the real poll loop step (Collector.poll_once) with a stub feed
    print("== 5. timestamps through poll_once")
    wipe(conn)
    from psycopg_pool import ConnectionPool

    class Stub:
        def __init__(self) -> None:
            self.payload = None

        async def fetch(self):
            return FetchResult(self.payload, 200, 15.0)

        async def discover(self):
            return ""

    async def scenario() -> list[tuple[str, str]]:
        settings = Settings(database_url=dsn)
        pool = ConnectionPool(dsn, min_size=1, max_size=2, open=False, kwargs={"autocommit": True})
        pool.open(wait=True)
        stub = Stub()
        col = Collector(settings, ref, pool, stub)  # type: ignore[arg-type]
        out = []

        async def step(label: str, ts: datetime | None, raw: str | None = None, drop: bool = False) -> None:
            p = copy.deepcopy(payloads[1])
            if drop:
                del p["general"]["update_timestamp"]
            elif raw is not None:
                p["general"]["update_timestamp"] = raw
            elif ts is not None:
                p["general"]["update_timestamp"] = ts.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
            stub.payload = p
            o = await col.poll_once()
            out.append((label, o.status))

        base_ts = dt(payloads[1]["general"]["update_timestamp"])
        await step("T0 real", base_ts)
        await step("T0 again (duplicate delivery)", base_ts)
        await step("T0 - 1 h (stale)", base_ts - timedelta(hours=1))
        await step("T0 + 15 s (next real update)", base_ts + timedelta(seconds=15))
        await step("T0 + 3 days (clock-skewed / poisoned timestamp)", base_ts + timedelta(days=3))
        await step("T0 + 30 s (the next genuine update)", base_ts + timedelta(seconds=30))
        await step("T0 + 45 s (another genuine update)", base_ts + timedelta(seconds=45))
        await step("timestamp text 'garbage'", None, raw="garbage")
        await step("timestamp missing", None, drop=True)
        pool.close()
        return out

    for label, status in asyncio.run(scenario()):
        print(f"  poll_once: {label:55s} -> {status}")
    n_ok = conn.execute("SELECT count(*) n FROM poll_log WHERE status='ok'").fetchone()["n"]
    n_ns = conn.execute("SELECT count(*) n FROM network_snapshot").fetchone()["n"]
    print(f"INFO  poll_log ok rows {n_ok}, network_snapshot rows {n_ns}")
    print("FAILED CHECKS:", FAILS or "none")
    raise SystemExit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
