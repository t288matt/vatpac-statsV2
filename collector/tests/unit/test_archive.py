"""Rolling raw-feed archive (vatpac.archive): layout, schedule, anomaly triggers, retention, failure isolation."""
from __future__ import annotations

import gzip
import json
import os
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from vatpac.archive import PILOT_KEYS, Archive, compact, fraction_share
from vatpac.health import Stats, health_body
from vatpac.model import parse_snapshot
from vatpac.settings import Settings

T0 = datetime.now(UTC).replace(microsecond=0, second=0, minute=0) - timedelta(hours=2)  # a recent whole hour
FRACTION = b".1234567Z"


def raw_at(payload: dict[str, Any], ts: datetime, fractional: bool = True) -> bytes:
    """The fixture feed stamped ts, with every logon_time given a fractional second (or none)."""
    body = dict(payload, general=dict(payload["general"], update_timestamp=ts.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")))
    text = json.dumps(body)
    tail = FRACTION.decode() if fractional else "Z"
    text = re.sub(r'("logon_time": "[^"]*?)(\.\d+)?Z"', lambda m: m.group(1) + tail + '"', text)
    return text.encode()


def make(tmp_path: Path, **kw: Any) -> tuple[Archive, Stats]:
    kw.setdefault("archive_enabled", True)  # conftest turns the archive off for every other test
    settings = Settings(_env_file=None, archive_dir=str(tmp_path / "arc"), **kw)  # type: ignore[call-arg]
    stats = Stats()
    return Archive(settings, stats), stats


def files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def feed(a: Archive, raw: bytes | None, ts: datetime | None, status: str = "ok") -> None:
    a._process(raw, ts, status, 200, None)  # the worker body, run synchronously


def read(path: Path) -> Any:
    return json.loads(gzip.decompress(path.read_bytes()))


def test_compact_keeps_audit_fields_and_parses_as_a_feed(fixture_payload: dict[str, Any]) -> None:
    rec = compact(fixture_payload)
    assert set(rec) == {"general", "pilots", "controllers"}
    assert len(rec["pilots"]) == len(fixture_payload["pilots"])
    assert set(rec["pilots"][0]) <= set(PILOT_KEYS)
    assert "name" not in rec["pilots"][0] and "flight_plan" not in rec["pilots"][0]
    snap = parse_snapshot(rec)  # the collector's own parser accepts a compact record
    assert snap.pilots_global == len(fixture_payload["pilots"]) and snap.malformed == 0


def test_compact_survives_odd_input() -> None:
    assert compact({"general": None, "pilots": None, "controllers": ["x", {"cid": 1}]}) == {
        "general": {"update_timestamp": None}, "pilots": [], "controllers": [{"cid": 1}]}


def test_fraction_share(fixture_payload: dict[str, Any]) -> None:
    n, frac = fraction_share(raw_at(fixture_payload, T0, fractional=True))
    assert n == 90 and frac == 1.0
    assert fraction_share(raw_at(fixture_payload, T0, fractional=False)) == (90, 0.0)
    assert fraction_share(b"{}") == (0, 0.0)


def test_schedule_layout_and_atomic_files(tmp_path: Path, fixture_payload: dict[str, Any]) -> None:
    a, _ = make(tmp_path, archive_compact_every_s=300, archive_full_every_s=3600)
    for i in range(0, 80):  # 80 polls, 15 s apart = 20 minutes
        ts = T0 + timedelta(seconds=15 * i)
        feed(a, raw_at(fixture_payload, ts), ts)
    names = files(tmp_path / "arc")
    day = f"{T0:%Y%m%d}"
    assert [n for n in names if n.startswith("full/")] == [f"full/{day}/{T0:%H%M%S}.json.gz"]
    assert len([n for n in names if n.startswith("compact/")]) == 4  # slots 0, 5, 10 and 15 minutes
    assert not [n for n in names if "/." in n or n.endswith(".tmp")]  # no temporary file is left behind
    assert not [n for n in names if n.startswith("anomaly/")]  # a steady feed is not an anomaly
    first = read(tmp_path / "arc" / names[0])
    assert first["general"]["update_timestamp"].startswith(T0.strftime("%Y-%m-%dT%H:%M:%S"))
    assert a.files == len(names) and a.size == sum((tmp_path / "arc" / n).stat().st_size for n in names)


def test_unchanged_feed_and_other_statuses_write_nothing(tmp_path: Path, fixture_payload: dict[str, Any]) -> None:
    a, _ = make(tmp_path)
    raw = raw_at(fixture_payload, T0)
    feed(a, raw, T0)
    n = a.files
    feed(a, raw, T0, "skipped_unchanged")
    feed(a, None, None, "skipped_unchanged")
    assert a.files == n


def test_full_file_is_the_raw_payload_byte_for_byte(tmp_path: Path, fixture_payload: dict[str, Any]) -> None:
    a, _ = make(tmp_path)
    raw = raw_at(fixture_payload, T0)
    feed(a, raw, T0)
    full = next((tmp_path / "arc" / "full").rglob("*.json.gz"))
    assert gzip.decompress(full.read_bytes()) == raw


@pytest.mark.parametrize("delta,reason", [(66, "gap"), (-20, "ts_older")])
def test_timestamp_anomalies(tmp_path: Path, fixture_payload: dict[str, Any], delta: int, reason: str) -> None:
    a, _ = make(tmp_path)
    feed(a, raw_at(fixture_payload, T0), T0)
    t1 = T0 + timedelta(seconds=delta)
    feed(a, raw_at(fixture_payload, t1), t1, "ok" if delta > 0 else "skipped_unchanged")
    kept = [n for n in files(tmp_path / "arc") if n.startswith("anomaly/")]
    assert sorted(k.split("-", 1)[1] for k in kept) == sorted([f"{reason}-prev.json.gz", f"{reason}.json.gz"])
    assert a.anomalies == 1


def test_a_normal_missed_tick_is_not_an_anomaly(tmp_path: Path, fixture_payload: dict[str, Any]) -> None:
    a, _ = make(tmp_path)
    feed(a, raw_at(fixture_payload, T0), T0)
    t1 = T0 + timedelta(seconds=30)
    feed(a, raw_at(fixture_payload, t1), t1)
    assert a.anomalies == 0


def test_future_timestamp_is_an_anomaly_and_not_a_baseline(tmp_path: Path, fixture_payload: dict[str, Any]) -> None:
    a, _ = make(tmp_path)
    feed(a, raw_at(fixture_payload, T0), T0)
    future = datetime.now(UTC) + timedelta(days=3)
    feed(a, raw_at(fixture_payload, future), future, "ok")
    assert any(n.endswith("ts_future.json.gz") for n in files(tmp_path / "arc"))
    t1 = T0 + timedelta(seconds=15)  # the poisoned timestamp must not make the next real one look like a gap
    feed(a, raw_at(fixture_payload, t1), t1)
    assert a.anomalies == 1


def test_precision_change_is_an_anomaly_with_the_payload_before_it(tmp_path: Path,
                                                                   fixture_payload: dict[str, Any]) -> None:
    """Issue 15: known sessions suddenly arrive with whole-second logon_time."""
    a, _ = make(tmp_path)
    before, after = raw_at(fixture_payload, T0, True), raw_at(fixture_payload, T0 + timedelta(seconds=15), False)
    feed(a, before, T0)
    feed(a, after, T0 + timedelta(seconds=15))
    kept = [n for n in files(tmp_path / "arc") if n.startswith("anomaly/")]
    assert [k.split("-", 1)[1] for k in kept] == ["precision-prev.json.gz", "precision.json.gz"]
    root = tmp_path / "arc"
    assert gzip.decompress((root / kept[0]).read_bytes()) == before
    assert gzip.decompress((root / kept[1]).read_bytes()) == after


def test_precision_rise_or_small_change_is_quiet(tmp_path: Path, fixture_payload: dict[str, Any]) -> None:
    a, _ = make(tmp_path)
    feed(a, raw_at(fixture_payload, T0, False), T0)
    feed(a, raw_at(fixture_payload, T0 + timedelta(seconds=15), True), T0 + timedelta(seconds=15))
    assert a.anomalies == 0
    small = fixture_payload["pilots"][:5]  # five values do not make a trend: under the minimum count
    tiny = dict(fixture_payload, pilots=small, controllers=[], atis=[])
    b, _ = make(tmp_path / "b")
    feed(b, raw_at(tiny, T0, True), T0)
    feed(b, raw_at(tiny, T0 + timedelta(seconds=15), False), T0 + timedelta(seconds=15))
    assert b.anomalies == 0


def test_poll_error_without_payload_keeps_a_small_note_and_is_rate_limited(tmp_path: Path) -> None:
    a, _ = make(tmp_path)
    a._process(None, None, "error", 503, "HTTP 503")
    a._process(None, None, "error", 503, "HTTP 503")  # within 5 minutes: not written again
    kept = [n for n in files(tmp_path / "arc") if n.startswith("anomaly/")]
    assert len(kept) == 1 and kept[0].endswith("poll_error.json.gz")
    note = read(tmp_path / "arc" / kept[0])
    assert note["http_status"] == 503 and note["reasons"] == ["poll_error"]


def test_poll_error_with_a_payload_keeps_it(tmp_path: Path, fixture_payload: dict[str, Any]) -> None:
    a, _ = make(tmp_path)
    raw = raw_at(fixture_payload, T0)
    a._process(raw, T0, "error", 200, "apply timed out")
    assert any(n.endswith("poll_error.json.gz") for n in files(tmp_path / "arc"))


def old(path: Path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_retention_prunes_each_class_by_its_own_age(tmp_path: Path, fixture_payload: dict[str, Any]) -> None:
    a, _ = make(tmp_path, archive_compact_keep_days=30, archive_full_keep_days=7, archive_anomaly_keep_days=90)
    root = tmp_path / "arc"
    for kind in ("compact", "full", "anomaly"):
        for days in (1.0, 8.0, 31.0, 91.0):
            p = root / kind / f"d{days:g}" / "x.json.gz"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
            old(p, days)
    (root / "compact" / "d1" / ".stale.json.gz.tmp").write_bytes(b"x")
    old(root / "compact" / "d1" / ".stale.json.gz.tmp", 1)
    a._prune()
    left = files(root)
    assert left == ["anomaly/d1/x.json.gz", "anomaly/d31/x.json.gz", "anomaly/d8/x.json.gz", "compact/d1/x.json.gz",
                    "compact/d8/x.json.gz", "full/d1/x.json.gz"]
    assert not (root / "full" / "d8").exists() and not (root / "compact" / "d31").exists()  # emptied folders go
    assert a.size == len(left)


def test_hard_cap_deletes_oldest_first(tmp_path: Path) -> None:
    a, _ = make(tmp_path, archive_max_mb=1)
    root = tmp_path / "arc"
    for i in range(6):
        p = root / "compact" / "20260101" / f"{i}.json.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(300_000))
        old(p, 0.5 - i / 100)  # file 0 is the oldest
    a._prune()
    left = [n.rsplit("/", 1)[1] for n in files(root)]
    assert left == ["3.json.gz", "4.json.gz", "5.json.gz"] and a.size <= 1_048_576


def test_unwritable_directory_is_counted_and_never_raises(tmp_path: Path, fixture_payload: dict[str, Any],
                                                          caplog: pytest.LogCaptureFixture) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    a, _ = make(tmp_path)
    a.root = blocker / "arc"  # a path below a regular file can never be created
    for i in range(3):
        ts = T0 + timedelta(seconds=15 * i)
        a.submit(raw_at(fixture_payload, ts), ts, "ok", 200, None)
        deadline = time.time() + 5
        while a._busy and time.time() < deadline:
            time.sleep(0.01)
    assert a.errors >= 1 and a.files == 0
    assert len([r for r in caplog.records if "archive failed" in r.message]) == 1  # logged once, not on every poll


def test_submit_never_blocks_and_drops_when_busy(tmp_path: Path, fixture_payload: dict[str, Any]) -> None:
    a, _ = make(tmp_path)
    gate = threading.Event()
    real = a._process
    a._process = lambda *args: gate.wait(5) and real(*args)  # type: ignore[method-assign,assignment]
    raw = raw_at(fixture_payload, T0)
    t0 = time.perf_counter()
    a.submit(raw, T0, "ok", 200, None)
    a.submit(raw, T0, "ok", 200, None)  # the worker is busy: dropped, counted
    assert time.perf_counter() - t0 < 0.05 and a.dropped == 1
    gate.set()
    deadline = time.time() + 5
    while a._busy and time.time() < deadline:
        time.sleep(0.01)
    assert a.files >= 1


def test_disabled_archive_does_nothing(tmp_path: Path, fixture_payload: dict[str, Any]) -> None:
    a, stats = make(tmp_path, archive_enabled=False)
    a.submit(raw_at(fixture_payload, T0), T0, "ok", 200, None)
    assert not (tmp_path / "arc").exists() and a._pool is None
    assert health_body(stats)["archive"]["enabled"] is False


def test_health_exposes_the_archive(tmp_path: Path, fixture_payload: dict[str, Any]) -> None:
    a, stats = make(tmp_path)
    assert health_body(stats)["archive"]["last_write"] is None  # nothing written yet
    feed(a, raw_at(fixture_payload, T0), T0)
    body = health_body(stats)["archive"]
    assert body["enabled"] and body["files_written"] == 2 and body["size_bytes"] > 0 and body["last_write"]
    assert body["compact_every_s"] == 300 and body["last_write_age_seconds"] < 5


def test_submit_swallows_a_dead_worker(tmp_path: Path, fixture_payload: dict[str, Any]) -> None:
    a, _ = make(tmp_path)
    assert a._pool is not None
    a._pool.shutdown()
    a.submit(raw_at(fixture_payload, T0), T0, "ok", 200, None)  # RuntimeError inside, never visible to the poll loop
    assert a.errors == 1 and not a._busy


def test_feed_client_keeps_the_raw_body_of_the_last_200_only(tmp_path: Path) -> None:
    import asyncio

    import httpx

    from vatpac.feed import FeedClient

    responses = iter([httpx.Response(200, content=b'{"a": 1}'), httpx.Response(304),
                      httpx.Response(200, content=b"[1]")])
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: next(responses)))
    feed = FeedClient(Settings(_env_file=None), client, min_gap_s=0)  # type: ignore[call-arg]

    async def go() -> list[bytes | None]:
        seen = []
        for _ in range(2):
            await feed.fetch()
            seen.append(feed.last_raw)
        with pytest.raises(Exception, match="not a JSON object"):
            await feed.fetch()
        seen.append(feed.last_raw)  # a body that is not a feed is still kept: the archive keeps bad payloads
        return seen

    assert asyncio.run(go()) == [b'{"a": 1}', None, b"[1]"]


@pytest.mark.parametrize(("bound", "expect"), [(30, True), (120, False)])
def test_ts_future_follows_feed_max_future_seconds(tmp_path: Path, fixture_payload: dict[str, Any], bound: int,
                                                   expect: bool) -> None:
    a, _ = make(tmp_path, feed_max_future_seconds=bound)
    ts = (datetime.now(UTC) + timedelta(seconds=60)).replace(microsecond=0)
    a._process(raw_at(fixture_payload, ts), ts, "error", 200, "ahead of the clock")
    assert any("ts_future" in n for n in files(tmp_path / "arc")) is expect


def test_shutdown_stops_between_the_compact_and_the_full_write(tmp_path: Path, fixture_payload: dict[str, Any]) -> None:
    """Issue 69: SIGTERM waited for the whole in-flight job; now the flag is checked between the writes."""
    a, _ = make(tmp_path)
    real = a._put
    a._put = lambda *args: (real(*args), a.stopping.set())  # type: ignore[method-assign,assignment]  # stop arrives mid-job
    feed(a, raw_at(fixture_payload, T0), T0)
    assert files(tmp_path / "arc") == [f"compact/{T0:%Y%m%d}/{T0:%H%M%S}.json.gz"]  # no full file
    a.submit(raw_at(fixture_payload, T0 + timedelta(seconds=30)), T0 + timedelta(seconds=30), "ok", 200, None)
    a._pool.shutdown(wait=True)  # type: ignore[union-attr]
    assert a.files == 1  # a job submitted after the stop did nothing
