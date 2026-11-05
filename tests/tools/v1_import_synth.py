"""Synthetic stand-ins for the frozen v1 CSV files (issue 279, ADR-035), the reference mapping, and a row-by-row comparison.

Never real data: names, CIDs and times are invented. The CSV layout is exactly what ops/v1_import/extract.sh writes
(`psql --csv`: header, empty field = NULL, timestamps `YYYY-MM-DD HH:MM:SS[.ffffff]+00`, jsonb as JSON text).

    python tests/tools/v1_import_synth.py generate OUT_DIR [--full]      write the files (--full: the real monthly counts)
    python tests/tools/v1_import_synth.py compare DSN OUT_DIR [--limit N]  compare loaded rows with the reference mapping

The reference mapping (`expected_flight`, `expected_controller`) is written from the owner's field map and the collector's own
text helpers (vatpac.model), not from the SQL, so a mistake in either shows as a difference.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "collector" / "src"))
from vatpac import model  # noqa: E402

FLIGHT_COLUMNS = ["id", "cid", "callsign", "departure", "arrival", "logon_time", "name", "server", "flight_rules", "route", "deptime",
                  "aircraft_type", "aircraft_faa", "planned_altitude", "pilot_rating", "military_rating", "time_online_minutes",
                  "total_enroute_time_minutes", "completion_time"]
CONTROLLER_COLUMNS = ["id", "cid", "callsign", "name", "session_start_time", "session_end_time", "rating", "facility", "server",
                      "frequencies_used"]
# The live counts of 05-11-2026 (ADR-035): flights by completion month, controller sessions by start month.
FULL_FLIGHTS = {"2025-08": 2801, "2025-09": 10284, "2025-10": 11450, "2025-11": 10540, "2025-12": 12456, "2026-01": 13268,
                "2026-02": 10841, "2026-03": 1892}
FULL_CONTROLLERS = {"2025-08": 277, "2025-09": 1674, "2025-10": 1716, "2025-11": 1674, "2025-12": 1711, "2026-01": 1979,
                    "2026-02": 1497, "2026-03": 742}
SMALL_FLIGHTS = {m: 40 for m in FULL_FLIGHTS}
SMALL_CONTROLLERS = {m: 30 for m in FULL_CONTROLLERS}
EDGE_ID = 900_000_001  # edge-case rows carry ids from here; random rows use ids below
AIRPORTS = ["YSSY", "YMML", "YBBN", "YPPH", "YPAD", "NZAA", "WSSS", "KLAX", "EGLL", "YSCB", "ZZZZ"]


def utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0, us: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, us, tzinfo=UTC)


def _window(month: str, kind: str) -> tuple[datetime, datetime]:
    y, m = int(month[:4]), int(month[5:])
    start = utc(y, m, 1)
    end = utc(y + (m == 12), m % 12 + 1, 1)
    if month == "2025-08":
        start = utc(2025, 8, 24)
    if month == "2026-03":
        end = utc(2026, 3, 7) if kind == "flight" else utc(2026, 3, 16)
    return start, end


def _flight(i: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": i, "cid": 1_000_000 + i, "callsign": f"QFA{i % 900:03d}", "departure": "YSSY", "arrival": "YMML",
        "logon_time": utc(2025, 9, 10, 8, 0, 0), "name": "Test Pilot YSSY", "server": "AUSTRALIA", "flight_rules": "I",
        "route": "DCT", "deptime": "0900", "aircraft_type": "B738", "aircraft_faa": "B738/L", "planned_altitude": "35000",
        "pilot_rating": 0, "military_rating": 0, "time_online_minutes": 90, "total_enroute_time_minutes": 80,
        "completion_time": utc(2025, 9, 10, 10, 0, 0)}
    base.update(kw)
    return base


def edge_flights() -> list[dict[str, Any]]:
    """Hand-made rows for the awkward cases; all complete in 09-2025."""
    t = EDGE_ID
    done = utc(2025, 9, 15, 12, 30, 15, 250_000)
    cjk = "漢" * 300  # 900 UTF-8 bytes
    rows = [
        _flight(t, departure=" ysy ", arrival="\tymml ", completion_time=done),
        _flight(t + 1, logon_time=utc(2025, 9, 15, 10, 0, 7, 987_654), completion_time=done),
        _flight(t + 2, route="", completion_time=done),
        _flight(t + 3, route="   ", completion_time=done),
        _flight(t + 4, time_online_minutes=0, total_enroute_time_minutes=55, completion_time=done),
        _flight(t + 5, time_online_minutes=60, total_enroute_time_minutes=75, completion_time=done),
        _flight(t + 6, time_online_minutes=60, total_enroute_time_minutes=0, completion_time=done),
        _flight(t + 7, name="Mason Rank YPPH", completion_time=done),
        _flight(t + 8, name="1673355 NZNR", completion_time=done),
        _flight(t + 9, name="Solo", completion_time=done),
        _flight(t + 10, name="Alice YPPH  ", completion_time=done),
        _flight(t + 11, name="Bob YPPHX", completion_time=done),
        _flight(t + 12, name="Cy yPPH", completion_time=done),
        _flight(t + 13, name="YPPH", completion_time=done),
        _flight(t + 14, route="DCT " * 1500, name="N" * 3000, completion_time=done),
        _flight(t + 15, callsign=cjk, departure=cjk, arrival="é" * 400, completion_time=done),
        _flight(t + 16, route="DCT\tYSSY\nYMML\x07 END\r\n", server="AUS\x01TRALIA", completion_time=done),
        _flight(t + 17, aircraft_type=" b738 ", aircraft_faa="H/B744/L", planned_altitude="FL350", completion_time=done),
        _flight(t + 18, pilot_rating=None, military_rating=None, flight_rules=None, deptime=None, name=None, completion_time=done),
        _flight(t + 19, total_enroute_time_minutes=None, completion_time=done),
        _flight(t + 20, callsign="  VOZ12  ", completion_time=done),
        _flight(t + 21, aircraft_type="", aircraft_faa="", completion_time=done),
        _flight(t + 22, completion_time=utc(2025, 9, 15, 0, 0, 0)),  # ends at midnight
    ]
    return rows


def _ctrl(i: int, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": i, "cid": 1_500_000 + i, "callsign": "ML_APP", "name": "Test Controller", "session_start_time": utc(2025, 9, 20, 10, 0, 0),
        "session_end_time": utc(2025, 9, 20, 12, 0, 0), "rating": 5, "facility": 5, "server": "AUSTRALIA",
        "frequencies_used": ["120.500"]}
    base.update(kw)
    return base


def edge_controllers() -> list[dict[str, Any]]:
    t = EDGE_ID
    s = utc(2025, 9, 20, 10, 0, 0)
    return [
        _ctrl(t, frequencies_used=["199.998"]),
        _ctrl(t + 1, frequencies_used=["199.998", "118.100"], cid=1_600_001),
        _ctrl(t + 2, frequencies_used=[], cid=1_600_002),
        _ctrl(t + 3, frequencies_used=["124.2"], cid=1_600_003),
        _ctrl(t + 4, frequencies_used=["124.20000", "199.998"], cid=1_600_004),
        _ctrl(t + 5, frequencies_used=["199.99800", "120.5", "121.7"], cid=1_600_005),
        # overlapping sessions of one controller and callsign: both are loaded (controller_session_merged joins them later)
        _ctrl(t + 6, cid=1_600_010, session_start_time=s, session_end_time=s + timedelta(hours=2)),
        _ctrl(t + 7, cid=1_600_010, session_start_time=s + timedelta(hours=1, seconds=30, microseconds=500_000),
              session_end_time=s + timedelta(hours=3)),
        _ctrl(t + 8, cid=1_600_011, session_start_time=utc(2025, 9, 21, 1, 2, 10, 999_999),
              session_end_time=utc(2025, 9, 21, 2, 2, 10, 500_000)),
        _ctrl(t + 9, cid=1_600_012, name="N" * 3000, server="AUS\x01TRALIA", callsign="SY_TWR"),
        _ctrl(t + 10, cid=1_600_013, name=None, rating=None, facility=None, server=None, frequencies_used=["121.700"]),
        _ctrl(t + 11, cid=1_600_014, frequencies_used=["0.000", "junk", "132.050"]),
        _ctrl(t + 12, cid=1_600_015, facility=0, rating=2),
    ]


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S") + (f".{v.microsecond:06d}".rstrip("0") if v.microsecond else "") + "+00"
    if isinstance(v, list):
        return json.dumps(v)
    return str(v)


def write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(columns)
        for r in rows:
            w.writerow([_fmt(r[c]) for c in columns])


def whitelist_callsigns(path: Path | None = None) -> list[str]:
    p = path or REPO / "config" / "controller_callsigns_list.txt"
    return sorted({ln.split(",")[0].strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")})


def generate(out: Path, flights: dict[str, int], controllers: dict[str, int], seed: int = 279,
             edge: bool = True) -> dict[str, list[dict[str, Any]]]:
    """Write v1_flights.csv, v1_controllers.csv, EXPECTED_COUNTS and SHA256SUMS to `out`; the edge rows count towards the
    monthly totals (they sit in 09-2025). Returns the rows."""
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    f_edge, c_edge = (edge_flights(), edge_controllers()) if edge else ([], [])
    calls = whitelist_callsigns()
    f_rows: list[dict[str, Any]] = []
    c_rows: list[dict[str, Any]] = []
    next_id = 1000
    for month, n in flights.items():
        lo, hi = _window(month, "flight")
        edge_here = [r for r in f_edge if r["completion_time"].strftime("%Y-%m") == month]
        f_rows += edge_here
        for _ in range(n - len(edge_here)):
            next_id += rng.randint(1, 3)
            done = lo + timedelta(seconds=rng.randrange(int((hi - lo).total_seconds())), microseconds=rng.choice([0, 0, 123_456]))
            online = rng.choice([0, 1, 3, 12, 45, 90, 150, 400])
            dep, arr = rng.sample(AIRPORTS, 2)
            f_rows.append(_flight(
                next_id, cid=rng.randint(800_000, 1_900_000), callsign=f"{rng.choice(['QFA', 'VOZ', 'JST', 'RXA'])}{rng.randint(1, 999)}",
                departure=dep.lower() if rng.random() < 0.03 else dep, arrival=f" {arr}" if rng.random() < 0.03 else arr,
                logon_time=done - timedelta(minutes=online + rng.randint(0, 20), seconds=rng.randint(0, 59)),
                name=rng.choice([f"Pilot {next_id} {dep}", f"Pilot {next_id}", f"{next_id} {arr}", None]),
                route=rng.choice(["", "DCT", "BIK H65 MNZ"]), time_online_minutes=online,
                total_enroute_time_minutes=rng.choice([0, online, online + rng.randint(1, 60), max(0, online - 5)]),
                aircraft_type=rng.choice(["B738", "A388", "c172", " A320"]), completion_time=done))
    next_id = 500_000
    for month, n in controllers.items():
        lo, hi = _window(month, "controller")
        edge_here = [r for r in c_edge if r["session_start_time"].strftime("%Y-%m") == month]
        c_rows += edge_here
        used: set[tuple[int, str, datetime]] = {(r["cid"], r["callsign"], r["session_start_time"].replace(microsecond=0)) for r in c_edge}
        for _ in range(n - len(edge_here)):
            next_id += rng.randint(1, 3)
            while True:
                cid, cs = rng.randint(800_000, 1_900_000), rng.choice(calls)
                start = lo + timedelta(seconds=rng.randrange(int((hi - lo).total_seconds())), microseconds=rng.choice([0, 250_000]))
                if (cid, cs, start.replace(microsecond=0)) not in used:
                    used.add((cid, cs, start.replace(microsecond=0)))
                    break
            c_rows.append(_ctrl(next_id, cid=cid, callsign=cs, session_start_time=start,
                                session_end_time=start + timedelta(minutes=rng.randint(1, 360), seconds=rng.randint(0, 59)),
                                frequencies_used=rng.choice([["199.998"], ["118.100", "199.998"], ["199.998", "125.250"], ["124.200"]]),
                                rating=rng.choice([2, 3, 4, 5, 7, 8, 10]), facility=rng.randint(0, 6)))
    write_files(out, f_rows, c_rows)
    return {"flights": f_rows, "controllers": c_rows}


def write_sums(out: Path) -> None:
    names = ("v1_flights.csv", "v1_controllers.csv", "EXPECTED_COUNTS")
    sums = "".join(f"{hashlib.sha256((out / n).read_bytes()).hexdigest()}  {n}\n" for n in names)
    (out / "SHA256SUMS").write_text(sums, encoding="utf-8", newline="\n")


def write_files(out: Path, f_rows: list[dict[str, Any]], c_rows: list[dict[str, Any]]) -> None:
    """The four frozen files for the given rows (EXPECTED_COUNTS = the number of rows given)."""
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "v1_flights.csv", FLIGHT_COLUMNS, f_rows)
    write_csv(out / "v1_controllers.csv", CONTROLLER_COLUMNS, c_rows)
    (out / "EXPECTED_COUNTS").write_text(f"flights={len(f_rows)}\ncontrollers={len(c_rows)}\n", encoding="utf-8", newline="\n")
    write_sums(out)


# ------------------------------------------------------------------------------------------------- reading the files back
def _ts(s: str) -> datetime | None:
    return datetime.fromisoformat(s.replace("+00", "+00:00")) if s else None


def _int(s: str) -> int | None:
    return int(s) if s else None


def read_rows(out: Path) -> dict[str, list[dict[str, Any]]]:
    """Parse the CSV files like COPY does (empty = NULL), independently of the generator's in-memory rows."""
    def rd(name: str) -> list[dict[str, str]]:
        with (out / name).open(encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))
    ints_f = ("id", "cid", "pilot_rating", "military_rating", "time_online_minutes", "total_enroute_time_minutes")
    f = [{**{k: (v or None) for k, v in r.items()}, **{k: _int(r[k]) for k in ints_f},
          **{k: _ts(r[k]) for k in ("logon_time", "completion_time")}} for r in rd("v1_flights.csv")]
    ints_c = ("id", "cid", "rating", "facility")
    c = [{**{k: (v or None) for k, v in r.items()}, **{k: _int(r[k]) for k in ints_c},
          **{k: _ts(r[k]) for k in ("session_start_time", "session_end_time")}, "frequencies_used": json.loads(r["frequencies_used"])}
         for r in rd("v1_controllers.csv")]
    return {"flights": f, "controllers": c}


# ------------------------------------------------------------------------------------------------- the reference mapping
def _text(v: str | None) -> str | None:
    s = model._s(v)  # the collector's rule: tab/newline to space, control characters out, blank to None
    return None if s is None else s[: model.MAX_TEXT]


def _indexed(s: str | None) -> str | None:
    if s is not None and len(s.encode("utf-8")) > model.MAX_INDEXED_BYTES:
        s = s.encode("utf-8")[: model.MAX_INDEXED_BYTES].decode("utf-8", "ignore")
    return s


def _plan_airport(v: str | None) -> str | None:
    s = _text(v)
    return None if s is None else _indexed(s.strip(" \t\r\n").upper()) or None


def expected_flight(r: dict[str, Any]) -> dict[str, Any]:
    """The flight_summary row ADR-035 says v1 row `r` becomes."""
    online, enroute = r["time_online_minutes"], r["total_enroute_time_minutes"]
    name = _text(r["name"])
    aircraft = _text(r["aircraft_type"])
    callsign = _text(r["callsign"])
    return {
        "session_id": -r["id"], "session_ids": [-r["id"]], "cid": r["cid"],
        "callsign": _indexed(callsign.strip()) if callsign else None,
        "departure": _plan_airport(r["departure"]), "arrival": _plan_airport(r["arrival"]),
        "logon_time": r["logon_time"].replace(microsecond=0), "name": name, "home_icao": model.home_icao(name),
        "server": _text(r["server"]), "flight_rules": _text(r["flight_rules"]), "route": _text(r["route"]), "deptime": _text(r["deptime"]),
        "aircraft_icao": aircraft.strip().upper() if aircraft else None, "aircraft_faa": _text(r["aircraft_faa"]),
        "planned_alt": _text(r["planned_altitude"]), "pilot_rating": r["pilot_rating"], "military_rating": r["military_rating"],
        "end_time": r["completion_time"], "start_time": r["completion_time"] - timedelta(minutes=online),
        "airborne_minutes": None if enroute is None else min(enroute, online),
        "max_gs_kt": None, "max_alt_ft": None, "sample_count": 0, "remarks": None}


def first_frequency_hz(freqs: list[Any]) -> int | None:
    for f in freqs:
        hz = model.parse_frequency_hz(str(f))  # None for 199.998, zero and junk
        if hz is not None:
            return int(hz)
    return None


def expected_controller(r: dict[str, Any]) -> dict[str, Any]:
    callsign = _text(r["callsign"])
    return {
        "id": -r["id"], "cid": r["cid"], "callsign": _indexed(callsign.strip()) if callsign else None,
        "logon_time": r["session_start_time"].replace(microsecond=0), "first_seen": r["session_start_time"].replace(microsecond=0),
        "last_seen": r["session_end_time"], "name": _text(r["name"]), "rating": r["rating"], "facility": r["facility"],
        "server": _text(r["server"]), "frequency_hz": first_frequency_hz(r["frequencies_used"]), "visual_range": None}


def compare(conn: Any, rows: dict[str, list[dict[str, Any]]], limit: int | None = None) -> list[str]:
    """Differences between the loaded rows and the reference mapping, for every row (or `limit` random ones plus every edge row)."""
    out: list[str] = []
    for kind, table, key, expect in (("flights", "flight_summary", "session_id", expected_flight),
                                     ("controllers", "controller_session", "id", expected_controller)):
        picked = rows[kind]
        if limit and len(picked) > limit:
            picked = [r for r in picked if r["id"] >= EDGE_ID] + random.Random(1).sample([r for r in picked if r["id"] < EDGE_ID], limit)
        for r in picked:
            want = expect(r)
            got = conn.execute(f"SELECT * FROM {table} WHERE {key} = %s", (want[key],)).fetchone()  # noqa: S608
            if got is None:
                out.append(f"{kind} v1 id {r['id']}: not loaded")
                continue
            for col, w in want.items():
                g = got[col]
                if col == "airborne_minutes" and g is not None:
                    g = int(g)
                if g != w:
                    out.append(f"{kind} v1 id {r['id']} column {col}: loaded {g!r:.80} expected {w!r:.80}")
            if kind == "flights" and (got["written_at"] is None):
                out.append(f"{kind} v1 id {r['id']}: written_at not set")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("out", type=Path)
    g.add_argument("--full", action="store_true", help="the real monthly counts (73,532 flights, 11,270 controller sessions)")
    c = sub.add_parser("compare")
    c.add_argument("dsn")
    c.add_argument("out", type=Path)
    c.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    if a.cmd == "generate":
        rows = generate(a.out, FULL_FLIGHTS if a.full else SMALL_FLIGHTS, FULL_CONTROLLERS if a.full else SMALL_CONTROLLERS)
        print(f"wrote {len(rows['flights'])} flights and {len(rows['controllers'])} controller sessions to {a.out}")
        return 0
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(a.dsn, row_factory=dict_row) as conn:
        diffs = compare(conn, read_rows(a.out), a.limit)
    print("\n".join(diffs[:40]) or "no differences")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
