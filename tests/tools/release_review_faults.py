"""Fault-injection and replay harness for the collector release review (08-10-2026).

Runs the real Collector (poll_once, archive, store) against a throwaway Postgres and an in-process fake feed server.
Not part of the pytest suite: run it by hand inside the collector image, for example

  python tests/tools/release_review_faults.py all
  python tests/tools/release_review_faults.py sigterm

Environment: DATABASE_URL (a migrated THROWAWAY database whose name ends in _test or _fault), PYTHONPATH=collector/src,
ARCHIVE_DIR (optional, default a fresh temporary directory). It TRUNCATEs the data tables, so it refuses any other name.
"""
from __future__ import annotations

import asyncio
import copy
import gzip
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from aiohttp import web
from psycopg_pool import ConnectionPool

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "collector" / "src"))

from vatpac.feed import FeedClient  # noqa: E402
from vatpac.filters import load_reference  # noqa: E402
from vatpac.main import Collector  # noqa: E402
from vatpac.settings import Settings  # noqa: E402

DSN = os.environ["DATABASE_URL"]
assert re.search(r"/\w+_(test|fault)$", DSN), "refusing: database name must end in _test or _fault"
FIX = json.loads((REPO / "tests/fixtures/vatsim-data.sample.json").read_text())
PORT = int(os.environ.get("FAKE_FEED_PORT", "18765"))
RESULTS: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, "PASS" if ok else "FAIL", detail))
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}", flush=True)


def payload(ts: datetime, fractional: bool = True, scale: int = 1) -> bytes:
    """The fixture stamped ts. fractional: logon_time carries .1234567; scale: clone the pilots to make a big feed."""
    p = copy.deepcopy(FIX)
    p["general"]["update_timestamp"] = ts.strftime("%Y-%m-%dT%H:%M:%S.1434968Z")
    base = list(p["pilots"])  # a copy: appending to the list being iterated never ends
    for k in range(1, scale):
        for q in base:
            c = copy.deepcopy(q)
            c["cid"] = q["cid"] + 1_000_000 * k
            c["callsign"] = f"{q['callsign']}{k}"
            c["flight_plan"] = dict(c.get("flight_plan") or {}, remarks="x" * 600)
            p["pilots"].append(c)
    text = json.dumps(p)
    tail = ".1234567Z" if fractional else "Z"
    text = re.sub(r'("logon_time": "[^"]*?)(\.\d+)?Z"', lambda m: m.group(1) + tail + '"', text)
    return text.encode()


class Fake:
    def __init__(self) -> None:
        self.body = b"{}"
        self.delay = 0.0
        self.hits = 0
        self.runner: web.AppRunner | None = None

    async def start(self) -> None:
        async def feed(_: web.Request) -> web.Response:
            self.hits += 1
            if self.delay:
                await asyncio.sleep(self.delay)
            return web.Response(body=self.body, content_type="application/json")

        app = web.Application()
        app.add_routes([web.get("/feed", feed)])
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", PORT).start()

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()


def settings(**kw: object) -> Settings:
    env = dict(DATABASE_URL=DSN, VATSIM_FEED_URL=f"http://127.0.0.1:{PORT}/feed", ARCHIVE_ENABLED="true",
               ARCHIVE_DIR=os.environ.get("ARCHIVE_DIR") or tempfile.mkdtemp(prefix="vp-rev-arc-"), HEALTH_PORT="18080",
               HTTP_TIMEOUT_SECONDS="5", POLL_INTERVAL_SECONDS="15")
    env.update({k.upper(): str(v) for k, v in kw.items()})
    os.environ.update(env)
    return Settings(_env_file=None)  # type: ignore[call-arg]


def reset() -> None:
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("TRUNCATE pilot_position, pilot_session, controller_rating_event, controller_frequency_event, "
                  "controller_session, network_snapshot, poll_log, controller_unlisted_sighting, reference_data_change "
                  "RESTART IDENTITY CASCADE")


def q(sql: str, *a: object) -> list[tuple]:
    with psycopg.connect(DSN, autocommit=True) as c:
        return c.execute(sql, a).fetchall()


async def boot(s: Settings) -> Collector:
    ref = load_reference(REPO / "config/australian_airspace_polygon.json", REPO / "config/controller_callsigns_list.txt")
    pool = ConnectionPool(s.dsn, min_size=1, max_size=4, open=False, kwargs={"autocommit": True})
    pool.open(wait=True)
    col = Collector(s, ref, pool, FeedClient(s))
    await col.maintain_once()
    await col.load_last_ts()
    return col


async def drain(col: Collector) -> None:
    if col.archive._pool is not None:
        await asyncio.to_thread(lambda: col.archive._pool.submit(lambda: None).result(timeout=60))  # type: ignore[union-attr]


def files(root: str) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file())


def rss_mb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS"):
            return int(line.split()[1]) / 1024
    return 0.0


NOW = datetime.now(UTC).replace(microsecond=0)
BASE = NOW - timedelta(hours=3)


async def s_precision() -> None:
    reset()
    fake, s = Fake(), settings()
    await fake.start()
    col = await boot(s)
    try:
        for i in range(3):
            fake.body = payload(BASE + timedelta(seconds=15 * i), fractional=True)
            await col.poll_once()
        a = q("SELECT (SELECT count(*) FROM pilot_session), (SELECT count(*) FROM controller_session)")[0]
        for i in range(3, 6):
            fake.body = payload(BASE + timedelta(seconds=15 * i), fractional=False)
            await col.poll_once()
        b = q("SELECT (SELECT count(*) FROM pilot_session), (SELECT count(*) FROM controller_session)")[0]
        await drain(col)
        arc = [f for f in files(s.archive_dir) if "precision" in f]
        record("04:08 pattern one session each", a == b and a[0] > 0, f"before {a} after {b}")
        record("04:08 precision anomaly archived", len(arc) >= 1, str(arc))
    finally:
        await fake.stop()
        col.pool.close()


async def s_future() -> None:
    reset()
    fake, s = Fake(), settings()
    await fake.start()
    col = await boot(s)
    try:
        fake.body = payload(BASE)
        st = [(await col.poll_once()).status]
        fake.body = payload(NOW + timedelta(minutes=10))  # 10 minutes ahead of the clock
        st.append((await col.poll_once()).status)
        fake.body = payload(NOW + timedelta(seconds=119))  # inside the bound
        st.append((await col.poll_once()).status)
        fake.body = payload(BASE + timedelta(seconds=15))  # genuine but older than the 119 s one -> skipped by design
        st.append((await col.poll_once()).status)
        await drain(col)
        record("future stamp rejected as error", st[1] == "error", str(st))
        arc = [f for f in files(s.archive_dir) if f.startswith("anomaly")]
        record("future-stamp error poll archived as anomaly", any("poll_error" in f for f in arc), str(arc))
        record("ts_future reason reachable from main", any("ts_future" in f for f in arc),
               "archive's ts_future reason never fires: poll_once raises before snap_ts is set (tag is poll_error)")
        # recovery: a fresh collector after the 119 s snapshot was stored; real stamps follow
        record("stamp inside bound accepted (ok), then older genuine skipped", st[2] == "ok" and st[3] == "skipped_unchanged", str(st))
        # clock 3 minutes slow == feed 180 s ahead: every poll fails
        fails = []
        for i in range(3):
            fake.body = payload(NOW + timedelta(seconds=180 + 15 * i))
            fails.append((await col.poll_once()).status)
        record("clock 3 min slow: every poll errors (total outage until clock fixed)", fails == ["error"] * 3, str(fails))
        # clock 3 minutes fast == feed 180 s behind: accepted
        reset()
        col2 = await boot(s)
        fake.body = payload(NOW - timedelta(seconds=180))
        r = (await col2.poll_once()).status
        record("clock 3 min fast: accepted", r == "ok", r)
        col2.pool.close()
    finally:
        await fake.stop()
        col.pool.close()


async def s_future_row_startup() -> None:
    reset()
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("INSERT INTO network_snapshot(observed_at,pilots_in_region,controllers_in_region,pilots_global,controllers_global)"
                  " VALUES (now()+interval '1 day',1,1,1,1)")
    fake, s = Fake(), settings()
    await fake.start()
    col = await boot(s)
    try:
        record("startup ignores a future-dated row", col.last_ts is None, f"last_ts={col.last_ts}")
        fake.body = payload(BASE)
        r = (await col.poll_once()).status
        record("poll after startup with future row is ok", r == "ok", r)
    finally:
        await fake.stop()
        col.pool.close()
    reset()
    col = await boot(s)
    record("startup with empty database", col.last_ts is None, str(col.last_ts))
    col.pool.close()


async def s_restart() -> None:
    reset()
    fake, s = Fake(), settings()
    await fake.start()
    col = await boot(s)
    fake.body = payload(BASE)
    r1 = (await col.poll_once()).status
    col.pool.close()
    col2 = await boot(s)  # restart
    r2 = (await col2.poll_once()).status  # same snapshot replayed
    n = q("SELECT status, count(*) FROM poll_log GROUP BY 1 ORDER BY 1")
    record("restart replay is skipped, not ok", (r1, r2) == ("ok", "skipped_unchanged"), f"{r1},{r2} {n} last_ts={col2.last_ts}")
    await fake.stop()
    col2.pool.close()


async def s_unwritable(kind: str) -> None:
    reset()
    if kind == "readonly":
        d = tempfile.mkdtemp(prefix="vp-rev-ro-")
        os.chmod(d, 0o555)
        arc = d
    elif kind == "blocked":  # a regular file where the directory should be
        f = tempfile.NamedTemporaryFile(prefix="vp-rev-blk-", delete=False)
        arc = f.name
    else:  # full: a tiny tmpfs mounted by the caller at /full
        arc = "/full"
    fake, s = Fake(), settings(archive_dir=arc)
    await fake.start()
    col = await boot(s)
    st = []
    t0 = time.monotonic()
    for i in range(20):
        fake.body = payload(BASE + timedelta(seconds=15 * i), scale=3 if kind == "full" else 1)
        st.append((await col.poll_once()).status)
    wall = time.monotonic() - t0
    await drain(col)
    info = col.archive.info()
    record(f"archive {kind}: 20 polls all ok", st == ["ok"] * 20, f"{wall:.2f}s errors={info['errors']} files={info['files_written']} dropped={info['dropped']}")
    record(f"archive {kind}: errors counted", info["errors"] >= 1 or kind == "readonly" and os.geteuid() == 0, str(info))
    await fake.stop()
    col.pool.close()


async def s_slow() -> None:
    reset()
    import vatpac.archive as arc_mod

    real = arc_mod.write_gz

    def slow(path: Path, data: bytes) -> int:
        time.sleep(6.0)  # a disk that takes six seconds per file
        return real(path, data)

    arc_mod.write_gz = slow  # type: ignore[assignment]
    fake, s = Fake(), settings()
    await fake.start()
    col = await boot(s)
    lag = 0.0

    async def monitor() -> None:
        nonlocal lag
        while True:
            t = time.monotonic()
            await asyncio.sleep(0.02)
            lag = max(lag, time.monotonic() - t - 0.02)

    mon = asyncio.create_task(monitor())
    durs = []
    for i in range(20):
        fake.body = payload(BASE + timedelta(seconds=15 * i))
        t0 = time.monotonic()
        await col.poll_once()
        durs.append(time.monotonic() - t0)
    mon.cancel()
    info = col.archive.info()
    record("slow disk (6 s/file): poll never delayed", max(durs) < 1.0 and lag < 0.5,
           f"max poll {max(durs)*1000:.0f} ms, median {sorted(durs)[10]*1000:.0f} ms, max loop lag {lag*1000:.0f} ms, dropped={info['dropped']}")
    arc_mod.write_gz = real  # type: ignore[assignment]
    await drain(col)
    await fake.stop()
    col.pool.close()


async def s_memory(n: int = 500, scale: int = 28) -> None:
    reset()
    fake, s = Fake(), settings(archive_compact_every_s=60, archive_full_every_s=300)
    await fake.start()
    col = await boot(s)
    fake.body = payload(BASE, scale=scale)
    size = len(fake.body)
    durs, lag = [], 0.0

    async def monitor() -> None:
        nonlocal lag
        while True:
            t = time.monotonic()
            await asyncio.sleep(0.02)
            lag = max(lag, time.monotonic() - t - 0.02)

    mon = asyncio.create_task(monitor())
    r0 = None
    for i in range(n):
        fake.body = payload(BASE + timedelta(seconds=15 * i), fractional=(i % 40 < 20), scale=scale) if i % 10 == 0 else fake.body.replace(
            re.search(rb'"update_timestamp": "([^"]+)"', fake.body).group(1),  # type: ignore[union-attr]
            (BASE + timedelta(seconds=15 * i)).strftime("%Y-%m-%dT%H:%M:%S.1434968Z").encode())
        t0 = time.monotonic()
        await col.poll_once()
        durs.append(time.monotonic() - t0)
        if i == 49:
            await drain(col)
            r0 = rss_mb()
    await drain(col)
    mon.cancel()
    r1 = rss_mb()
    info = col.archive.info()
    durs.sort()
    record(f"memory over {n} polls of a {size/1e6:.2f} MB feed", (r1 - (r0 or 0)) < 60,
           f"RSS at poll 50 {r0:.0f} MB, at end {r1:.0f} MB; poll p50 {durs[n//2]*1000:.0f} ms p99 {durs[int(n*.99)]*1000:.0f} ms "
           f"max {durs[-1]*1000:.0f} ms; loop lag max {lag*1000:.0f} ms; archive {info['files_written']} files {info['size_bytes']/1e6:.1f} MB dropped {info['dropped']} errors {info['errors']}")
    await fake.stop()
    col.pool.close()


def s_sigterm() -> None:
    """Run the real entrypoint with a 6 s write, send SIGTERM mid-write, measure exit time and leftovers."""
    reset()
    arc = tempfile.mkdtemp(prefix="vp-rev-sig-")
    wrapper = Path(tempfile.mkdtemp()) / "slow_main.py"
    wrapper.write_text(
        "import sys, time\nimport vatpac.archive as a\nreal = a.write_gz\n"
        "def slow(p, d):\n    print('WRITE_START', flush=True)\n    time.sleep(6)\n    return real(p, d)\n"
        "a.write_gz = slow\nfrom vatpac.main import main\nraise SystemExit(main())\n")
    feed_script = Path(tempfile.mkdtemp()) / "feed.py"
    feed_script.write_text(
        "import sys\nfrom aiohttp import web\nbody = open(sys.argv[1], 'rb').read()\n"
        "async def h(_):\n    return web.Response(body=body, content_type='application/json')\n"
        "app = web.Application(); app.add_routes([web.get('/feed', h)]); web.run_app(app, host='127.0.0.1', port=int(sys.argv[2]), print=None)\n")
    body = Path(tempfile.mkdtemp()) / "b.json"
    body.write_bytes(payload(BASE))
    fp = subprocess.Popen([sys.executable, str(feed_script), str(body), str(PORT + 1)])
    time.sleep(1.5)
    env = dict(os.environ, DATABASE_URL=DSN, VATSIM_FEED_URL=f"http://127.0.0.1:{PORT + 1}/feed", VATSIM_STATUS_URL="http://127.0.0.1:1/x",
               ARCHIVE_DIR=arc, ARCHIVE_ENABLED="true", HEALTH_PORT="18081", POLICY="x",
               POLYGON_PATH=str(REPO / "config/australian_airspace_polygon.json"),
               WHITELIST_PATH=str(REPO / "config/controller_callsigns_list.txt"))
    p = subprocess.Popen([sys.executable, str(wrapper)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t_start = time.monotonic()
    seen = False
    out = []
    assert p.stdout
    while time.monotonic() - t_start < 40:
        line = p.stdout.readline()
        out.append(line)
        if "WRITE_START" in line:
            seen = True
            break
    time.sleep(1.0)
    t_sig = time.monotonic()
    p.send_signal(signal.SIGTERM)
    try:
        rc = p.wait(timeout=40)
    except subprocess.TimeoutExpired:
        p.kill()
        rc = -999
    took = time.monotonic() - t_sig
    left = files(arc)
    tmp = [f for f in left if f.endswith(".tmp")]
    fp.terminate()
    record("SIGTERM during an archive write", seen and rc in (0,) and took < 10,
           f"write started={seen} rc={rc} exit after {took:.1f}s (6 s write in flight); files={left}; partial .tmp={tmp}")


async def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    steps = {"precision": s_precision, "future": s_future, "futurerow": s_future_row_startup, "restart": s_restart,
             "readonly": lambda: s_unwritable("readonly"), "blocked": lambda: s_unwritable("blocked"),
             "full": lambda: s_unwritable("full"), "slow": s_slow, "memory": s_memory}
    if which == "sigterm":
        s_sigterm()
        return
    for name, fn in steps.items():
        if which in ("all", name) and not (which == "all" and name in ("full",)):
            await fn()
    if which == "all":
        s_sigterm()


if __name__ == "__main__":
    asyncio.run(main())
