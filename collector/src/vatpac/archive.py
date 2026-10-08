"""Rolling raw-feed archive (issue 20, audit section 6.2). Runbook: docs/RUNBOOKS/feed-archive.md.

Layout under ARCHIVE_DIR (one gzipped file per write, written to a temporary name and renamed, never appended):
compact/YYYYMMDD/HHMMSS.json.gz  every ARCHIVE_COMPACT_EVERY_S: all pilots and controllers, audit fields only, in the
                                 feed's own key names so the file parses with vatpac.model.parse_snapshot
full/YYYYMMDD/HHMMSS.json.gz     every ARCHIVE_FULL_EVERY_S: the raw payload exactly as received
anomaly/YYYYMMDD/HHMMSS-<reasons>.json.gz (+ -prev: the payload before it)  reasons: poll_error, gap, ts_older,
                                 ts_future, precision (the share of logon_time values with a fraction dropped).
The archive can never block or fail a poll: submit() only queues work for one worker thread, which runs after the
snapshot is stored; any error is counted, logged once and swallowed. Counters appear in /health and /metrics.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .health import Stats
from .settings import Settings

log = logging.getLogger(__name__)
FUTURE_S, ANOMALY_REPEAT_S, STALE_TMP_S = 120.0, 300.0, 600.0
PRECISION_DROP, PRECISION_MIN_VALUES = 0.2, 50  # a fall of 20 points or more over at least 50 logon_time values
_LOGON_RE = re.compile(rb'"logon_time"\s*:\s*"([^"]*)"')
PILOT_KEYS = ("cid", "callsign", "logon_time", "latitude", "longitude", "groundspeed", "altitude")
CONTROLLER_KEYS = ("cid", "callsign", "logon_time", "frequency", "rating", "facility")


def compact(payload: dict[str, Any]) -> dict[str, Any]:
    """The audit record: timestamp, every pilot and controller with only the audit fields."""
    def pick(rows: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
        return [{k: r[k] for k in keys if k in r} for r in rows or [] if isinstance(r, dict)]

    general = payload.get("general")
    stamp = general.get("update_timestamp") if isinstance(general, dict) else None
    return {"general": {"update_timestamp": stamp}, "pilots": pick(payload.get("pilots"), PILOT_KEYS),
            "controllers": pick(payload.get("controllers"), CONTROLLER_KEYS)}


def fraction_share(raw: bytes) -> tuple[int, float]:
    """(number of logon_time values, share of them that carry a fractional second)."""
    values = _LOGON_RE.findall(raw)
    return len(values), (sum(b"." in v for v in values) / len(values) if values else 0.0)


def write_gz(path: Path, data: bytes) -> int:
    """Write data gzipped (level 6, no embedded time) to a temporary name, then rename. Returns the file size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    blob, tmp = gzip.compress(data, 6, mtime=0), path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(blob)
    os.replace(tmp, path)
    return len(blob)


class Archive:
    def __init__(self, settings: Settings, stats: Stats) -> None:
        self.s, self.root, self.enabled = settings, Path(settings.archive_dir), settings.archive_enabled
        self.started = time.time()
        self.last_write: float | None = None
        self.size = self.files = self.errors = self.dropped = self.anomalies = 0
        self._busy = self._failing = False
        self._last_ts: datetime | None = None  # the state below is touched by the worker thread only
        self._share: tuple[int, float] | None = None
        self._prev: bytes | None = None
        self._slot: dict[str, int] = {}
        self._anomaly_at: dict[str, float] = {}
        self._last_prune = 0.0
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="archive") if self.enabled else None
        stats.archive_info = self.info

    def info(self) -> dict[str, Any]:
        last = self.last_write
        return {"enabled": self.enabled, "size_bytes": self.size, "files_written": self.files, "errors": self.errors,
                "dropped": self.dropped, "anomalies": self.anomalies,
                "last_write": datetime.fromtimestamp(last, UTC).isoformat() if last else None,
                "last_write_age_seconds": time.time() - (last or self.started),  # since start while nothing is written
                "compact_every_s": self.s.archive_compact_every_s}

    def submit(self, raw: bytes | None, snap_ts: datetime | None, status: str, http: int | None,
               err: str | None) -> None:
        """Called by the poll loop after the snapshot is stored. Never blocks, never raises."""
        try:
            if self._pool is None or (raw is None and status != "error"):
                return
            if self._busy:
                self.dropped += 1
                return
            self._busy = True
            self._pool.submit(self._run, raw, snap_ts, status, http, err)
        except Exception:
            self._busy = False
            self.errors += 1

    def _run(self, raw: bytes | None, snap_ts: datetime | None, status: str, http: int | None, err: str | None) -> None:
        try:
            self._process(raw, snap_ts, status, http, err)
            if self._failing:
                self._failing = False
                log.info("archive writes recovered")
        except Exception as exc:  # the archive must never affect polling
            self.errors += 1
            if not self._failing:
                self._failing = True
                log.error("archive failed, polling continues", extra={"error": f"{type(exc).__name__}: {exc}"})
        finally:
            self._busy = False

    def _put(self, path: Path, data: bytes) -> None:
        self.size += write_gz(path, data)
        self.files += 1
        self.last_write = time.time()

    def _reasons(self, raw: bytes | None, snap_ts: datetime | None, status: str) -> tuple[list[str], bool]:
        """Anomaly reasons for this poll, and whether the payload may become the new baseline."""
        out = ["poll_error"] if status == "error" else []
        if raw is None or snap_ts is None:
            return out, False
        if snap_ts.timestamp() - time.time() > FUTURE_S:
            return [*out, "ts_future"], False
        last = self._last_ts
        if last is not None:
            gap = (snap_ts - last).total_seconds()
            if gap < 0:
                return [*out, "ts_older"], False
            if gap > self.s.archive_gap_s:
                out.append("gap")
        n, share = fraction_share(raw)
        if self._share and n >= PRECISION_MIN_VALUES and self._share[0] >= PRECISION_MIN_VALUES \
                and self._share[1] - share >= PRECISION_DROP:
            out.append("precision")
        return out, True

    def _process(self, raw: bytes | None, snap_ts: datetime | None, status: str, http: int | None,
                 err: str | None) -> None:
        s = self.s
        if status != "error" and raw is not None and snap_ts is not None and snap_ts == self._last_ts:
            return  # the same snapshot again (an unchanged feed)
        reasons, baseline = self._reasons(raw, snap_ts, status)
        if reasons:
            self._anomaly(reasons, raw, snap_ts, http, err)
        if status == "ok" and raw is not None and snap_ts is not None:
            epoch = snap_ts.timestamp()
            if self._slot.get("compact") != int(epoch // s.archive_compact_every_s):
                body = json.dumps(compact(json.loads(raw)), separators=(",", ":")).encode()
                self._put(self.root / "compact" / f"{snap_ts:%Y%m%d}" / f"{snap_ts:%H%M%S}.json.gz", body)
                self._slot["compact"] = int(epoch // s.archive_compact_every_s)
            if self._slot.get("full") != int(epoch // s.archive_full_every_s):
                self._put(self.root / "full" / f"{snap_ts:%Y%m%d}" / f"{snap_ts:%H%M%S}.json.gz", raw)
                self._slot["full"] = int(epoch // s.archive_full_every_s)
        if baseline and raw is not None and snap_ts is not None:
            self._last_ts, self._share, self._prev = snap_ts, fraction_share(raw), raw
        # prune every two compact intervals (10 minutes by default) or at once when over the cap
        if time.time() - self._last_prune > 2 * s.archive_compact_every_s or self.size > s.archive_max_mb * 1_048_576:
            self._prune()

    def _anomaly(self, reasons: list[str], raw: bytes | None, snap_ts: datetime | None, http: int | None,
                 err: str | None) -> None:
        tag, now = "+".join(reasons), time.time()
        if now - self._anomaly_at.get(tag, 0.0) < ANOMALY_REPEAT_S:
            return  # one file per reason per 5 minutes, so an outage cannot flood the disk
        self._anomaly_at[tag] = now
        when = snap_ts or datetime.fromtimestamp(now, UTC)
        base = self.root / "anomaly" / f"{when:%Y%m%d}" / f"{when:%H%M%S}-{tag}"
        if raw is not None:
            self._put(base.with_name(base.name + ".json.gz"), raw)
            if self._prev is not None and reasons != ["poll_error"]:
                self._put(base.with_name(base.name + "-prev.json.gz"), self._prev)
        else:
            meta = {"reasons": reasons, "at": when.isoformat(), "http_status": http, "error": err}
            self._put(base.with_name(base.name + ".json.gz"), json.dumps(meta).encode())
        self.anomalies += 1
        log.warning("archive anomaly kept", extra={"reasons": tag})

    def _prune(self) -> None:
        """Delete files past their retention, then the oldest files while over the hard cap; recount the size."""
        now, self._last_prune = time.time(), time.time()
        keep = {"compact": self.s.archive_compact_keep_days, "full": self.s.archive_full_keep_days,
                "anomaly": self.s.archive_anomaly_keep_days}
        files: list[tuple[float, int, Path]] = []
        for kind, days in keep.items():
            for path in (self.root / kind).glob("*/*"):
                try:
                    st = path.stat()
                    limit = STALE_TMP_S if path.name.startswith(".") else days * 86400
                    if now - st.st_mtime > limit:
                        path.unlink()
                    elif not path.name.startswith("."):
                        files.append((st.st_mtime, st.st_size, path))
                except OSError:
                    continue
        total, cap = sum(f[1] for f in files), self.s.archive_max_mb * 1_048_576
        for _, size, path in sorted(files):
            if total <= cap:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                continue
        for day in self.root.glob("*/*"):
            if day.is_dir() and not any(day.iterdir()):
                day.rmdir()
        self.size = total
