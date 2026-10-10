"""Issue #98: measure what the partition drop does to a collector-like writer, for the busy-partition cases.

Usage (throwaway database whose name ends in _test, migrations applied, capped container, never the live database):
    python measure_partition_drop.py <superuser dsn> <function name> <label>
The function is called like the collector does (one `SELECT fn(retention, today)` per call, repeated until it drops nothing;
the previous version drops everything in its first call). Cases: 4 partitions due with 0, 1, 2, 3 of the oldest busy
(a reader holding ACCESS SHARE on them), and 48 due with one long reader (40 s) on the 20th.
Prints one line per case: dropped, call seconds, writer inserts, errors, inserts that waited over 0.5 s, worst wait.
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import psycopg

dsn, fn, label = sys.argv[1], sys.argv[2], sys.argv[3]
if not psycopg.conninfo.conninfo_to_dict(dsn)["dbname"].endswith("_test"):
    sys.exit("refusing: database name must end in _test")
TODAY = datetime(2026, 10, 7, tzinfo=UTC)


def build(adm: psycopg.Connection, due: int) -> list[str]:
    adm.execute("TRUNCATE pilot_position, pilot_session, flight_summary, network_snapshot RESTART IDENTITY CASCADE")
    for r in adm.execute("SELECT inhrelid::regclass::text AS p FROM pg_inherits WHERE inhparent = 'pilot_position'::regclass").fetchall():
        adm.execute(f"DROP TABLE {r[0]}")
    names = []
    for off in range(300, 300 - due, -1):
        d = (TODAY - timedelta(days=off)).date()
        name = f"pilot_position_{d:%Y%m%d}"
        adm.execute(f"CREATE TABLE {name} PARTITION OF pilot_position FOR VALUES FROM ('{d} 00:00:00+00') TO ('{d + timedelta(days=1)} 00:00:00+00')")
        adm.execute(
            "INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen, departure, arrival) "
            "SELECT %s + k, 'M' || %s || k, %s::timestamptz + make_interval(mins => k), %s::timestamptz + make_interval(mins => k), "
            "%s::timestamptz + make_interval(mins => k + 50), 'YSSY', 'YMML' FROM generate_series(1, 200) k",
            (9_000_000 + off * 1000, off, f"{d} 01:00:00+00", f"{d} 01:00:00+00", f"{d} 01:00:00+00"))
        adm.execute(
            "INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) "
            "SELECT s.first_seen + make_interval(mins => n), s.id, 5000, 250 FROM pilot_session s, generate_series(0, 49) n "
            "WHERE s.first_seen >= %s::timestamptz AND s.first_seen < %s::timestamptz", (f"{d} 00:00:00+00", f"{d + timedelta(days=1)} 00:00:00+00"))
        names.append(name)
    adm.execute("SELECT ensure_pilot_position_partitions(1)")
    adm.execute("INSERT INTO pilot_session (cid, callsign, logon_time, first_seen, last_seen) VALUES (9999999, 'LIVE1', date_trunc('second', now()), now(), now())")
    adm.execute("ANALYZE")
    return sorted(names)


def run_case(admin_dsn: str, due: int, busy_idx: list[int], hold_s: float | None) -> str:
    with psycopg.connect(admin_dsn, autocommit=True) as adm:
        names = build(adm, due)
        live = adm.execute("SELECT id FROM pilot_session WHERE cid = 9999999").fetchone()[0]
    stop = threading.Event()
    stats = {"n": 0, "err": 0, "slow": 0, "worst": 0.0}

    def writer() -> None:
        with psycopg.connect(admin_dsn, autocommit=True, options="-c lock_timeout=5000") as c:
            while not stop.is_set():
                t0 = time.time()
                try:
                    c.execute("INSERT INTO pilot_position (observed_at, session_id, alt_ft, gs_kt) VALUES (clock_timestamp(), %s, 5000, 250)", (live,))
                    stats["n"] += 1
                except psycopg.Error:
                    stats["err"] += 1
                w = time.time() - t0
                stats["worst"] = max(stats["worst"], w)
                stats["slow"] += w > 0.5
                stop.wait(0.05)

    readers = []
    for i in busy_idx:
        r = psycopg.connect(admin_dsn, autocommit=False)
        r.execute(f"SELECT count(*) FROM {names[i]}")
        readers.append(r)
        if hold_s:
            threading.Timer(hold_s, r.rollback).start()
    th = threading.Thread(target=writer, daemon=True)
    th.start()
    time.sleep(0.5)
    dropped, t0 = 0, time.time()
    with psycopg.connect(admin_dsn.replace("postgres:x@", "collector:cpw@"), autocommit=True,
                         options="-c lock_timeout=5000 -c statement_timeout=900000") as m:
        for _ in range(60):
            n = m.execute(f"SELECT {fn}(180, %s::date) AS n", (TODAY.date() - timedelta(days=300 - due - 180),)).fetchone()[0]
            dropped += n
            if n == 0 or fn.endswith("_v1") or label.startswith("before"):
                break
    secs = time.time() - t0
    stop.set()
    th.join(5)
    for r in readers:
        if not hold_s:
            r.rollback()
    time.sleep(hold_s + 1 if hold_s else 0)
    for r in readers:
        r.close()
    return (f"[{label}] due={due} busy={len(busy_idx)}{' long' if hold_s else ''}: dropped={dropped} call_s={secs:.1f} "
            f"writer_inserts={stats['n']} errors={stats['err']} waits>0.5s={stats['slow']} worst_wait={stats['worst']:.2f}s")


for k in (0, 1, 2, 3):
    print(run_case(dsn, 4, list(range(k)), None), flush=True)
print(run_case(dsn, 48, [19], 40.0), flush=True)
