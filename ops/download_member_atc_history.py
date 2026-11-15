#!/usr/bin/env python3
"""Download each controller's FULL ATC session history from the public VATSIM API to local files (issue 379). Run by hand, on your PC.

For every controller (CID) this reads all pages of GET https://api.vatsim.net/v2/members/{cid}/atc (no key) and saves the sessions,
exactly as the API returned them, to <out>/<cid>.json. It writes no database and changes nothing on the server. Standard library only.

    python ops/download_member_atc_history.py --cid 1234567 --cid 7654321
    python ops/download_member_atc_history.py --cids-file cids.txt          one CID per line; blank lines and # comments ignored
    python ops/download_member_atc_history.py --from-vm                     CIDs from the VM, over ssh, read-only
    python ops/download_member_atc_history.py --from-vm --dry-run           list what would be fetched; no API call, nothing written
    python ops/download_member_atc_history.py --from-vm --spread-over 2d    all pending CIDs, calls spread over about two days
    python ops/download_member_atc_history.py --cids-file cids.txt --refresh      download again even if the file exists

Slow by design. Every API call, including the extra pages of one controller, is followed by a pause of 5 seconds on average (--sleep),
randomly varied by plus or minus 30 percent so calls never fall on a regular beat. --spread-over 90m, 6h or 2d sets the pause so the
whole run takes about that long (never below 5 seconds on average; if the period is too short the script says so and uses 5 seconds).
--limit is 25 controllers per run by default; with --spread-over and no --limit all pending controllers are downloaded (0 = no limit).
Output folder (--out, default Documents\\vatsim-atc-history\\<DD-MM-YYYY> in your home folder): <cid>.json, manifest.csv, README.txt.
Safe to stop and run again: a CID whose file exists is skipped unless --refresh. Each file is written to a temporary name and renamed.
Ctrl+C finishes the controller in progress, then stops and says how to resume. The run also STOPS (exit code 3) at the first HTTP
429 or 5xx after a short backoff; wait a while, then run the same command again.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backfill_rating_history import (  # noqa: E402
    API,
    PAGE,
    ApiNotFound,
    ApiStop,
    Fetch,
    earliest_per_rating,
    http_fetch,
    parse_time,
)

DEFAULT_PAUSE = 5.0  # seconds between API calls, on average
JITTER = 0.3  # each pause is varied by plus or minus 30 percent
WARN_BELOW = 2.0  # an explicit --sleep below this prints a warning
CID_QUERY = "select distinct cid from controller_session order by 1"
REMOTE_CMD = ("cd ~/vatpac-statsV2 && docker compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' db "
              "sh -c 'psql -X -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -At -c \"" + CID_QUERY + "\"'")
MANIFEST_FIELDS = ["cid", "sessions", "first_start", "last_start", "ratings_seen", "first_over_20min_by_rating",
                   "downloaded_utc", "status"]
README = """These files are a download of each VATSIM controller's full ATC session history from the public VATSIM API
(https://api.vatsim.net/v2/members/<cid>/atc), made by ops/download_member_atc_history.py.

<cid>.json   {cid, downloaded_utc, count, sessions}; sessions are the API's items exactly as returned.
manifest.csv one row per download: cid, sessions, first_start, last_start, ratings_seen, downloaded_utc, status (ok or not_found).
             A controller downloaded again with --refresh gets another row; the latest row is the current one.
first_start and last_start count sessions of any length. first_over_20min_by_rating is what the rating table stores: per rating,
             the start of the first session longer than 20 minutes (rating=start, separated by semicolons; empty when none).
Times are UTC. Nothing here was written to any database.
"""

Ssh = Callable[[str, str], str]  # (ssh host, remote command) -> stdout
Wait = Callable[[int], None]  # called after every API call with the number of further pages known for this controller


def ssh_run(host: str, command: str) -> str:
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, command],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"ssh failed (exit {r.returncode}): {r.stderr.strip()[:300]}")
    return r.stdout


def parse_cids(text: str) -> list[int]:
    out: list[int] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(int(line))
    return out


def parse_duration(text: str) -> float:
    """'90m', '6h', '2d' (or 's') to seconds."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", text.strip().lower())
    if not m or float(m.group(1)) <= 0:
        raise ValueError(f"bad duration {text!r}: use a number and s, m, h or d, for example 90m, 6h or 2d")
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def spread_pause(remaining_seconds: float, calls_left: int) -> float:
    """The average pause that makes the remaining calls fill the remaining time; never below DEFAULT_PAUSE."""
    return max(DEFAULT_PAUSE, remaining_seconds / max(calls_left, 1))


def jittered(pause: float, rng: random.Random) -> float:
    return pause * rng.uniform(1 - JITTER, 1 + JITTER)


def download(cid: int, fetch: Fetch, wait: Wait) -> list[dict[str, Any]]:
    """All sessions for one CID. The response's count decides when to stop; an empty page also stops."""
    items: list[dict[str, Any]] = []
    while True:
        body = fetch(API.format(cid=cid, limit=PAGE, offset=len(items)))
        page = (body.get("items") or body.get("results") or []) if isinstance(body, dict) else body
        items += page
        count = body.get("count") if isinstance(body, dict) else None
        total = count if isinstance(count, int) and not isinstance(count, bool) else None
        done = not page or (total is not None and len(items) >= total)
        extra = 0 if done or total is None else -(-(total - len(items)) // len(page))
        wait(extra)
        if done:
            return items


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def over_20(sessions: list[dict[str, Any]]) -> str:
    return ";".join(f"{r}={t.isoformat()}" for r, t in sorted(earliest_per_rating(sessions).items()))


def manifest_row(cid: int, sessions: list[dict[str, Any]], when: str, status: str) -> dict[str, Any]:
    conns = [s.get("connection_id", s) for s in sessions]
    starts = sorted(t for t in (parse_time(c.get("start")) for c in conns) if t is not None)
    ratings = sorted({c["rating"] for c in conns if isinstance(c.get("rating"), int)})
    return {"cid": cid, "sessions": len(sessions), "first_start": starts[0].isoformat() if starts else "",
            "last_start": starts[-1].isoformat() if starts else "", "ratings_seen": " ".join(map(str, ratings)),
            "first_over_20min_by_rating": over_20(sessions),
            "downloaded_utc": when, "status": status}


def upgrade_manifest(path: Path) -> None:
    """A manifest from an earlier run has no first_over_20min_by_rating column: add it, recomputed from the controller's file."""
    if not path.exists():
        return
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows or "first_over_20min_by_rating" in rows[0]:
        return
    for row in rows:
        data = path.with_name(f"{row['cid']}.json")
        try:
            sessions = json.loads(data.read_text(encoding="utf-8"))["sessions"] if data.exists() else []
        except (ValueError, KeyError, OSError):
            sessions = []
        row["first_over_20min_by_rating"] = over_20(sessions)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS, restval="")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)


def append_manifest(path: Path, row: dict[str, Any]) -> None:
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)


def main(argv: list[str], fetch: Fetch = http_fetch, ssh: Ssh = ssh_run, sleep: Callable[[float], None] = time.sleep,
         now: Callable[[], datetime] = lambda: datetime.now(UTC), rng: random.Random | None = None,
         handle_ctrl_c: bool = False) -> int:
    rng = rng or random.Random()
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--cid", type=int, action="append", default=[], help="a controller to download (repeatable)")
    ap.add_argument("--cids-file", type=Path, help="file with one CID per line; blank lines and # comments ignored")
    ap.add_argument("--from-vm", action="store_true", help="read the distinct CIDs of controller_session over ssh (read-only)")
    ap.add_argument("--ssh-host", default="vatpac-v3", help="ssh host for --from-vm (default vatpac-v3)")
    ap.add_argument("--out", type=Path, help="output folder (default Documents/vatsim-atc-history/<DD-MM-YYYY> in your home folder)")
    ap.add_argument("--sleep", type=float, default=DEFAULT_PAUSE,
                    help="average seconds between API calls, varied by +/-30 percent (default 5; below 2 prints a warning)")
    ap.add_argument("--spread-over", metavar="DURATION", help="make the whole run take about this long, e.g. 90m, 6h, 2d")
    ap.add_argument("--limit", type=int, help="CIDs to download per run (default 25, or all with --spread-over; 0 = no limit)")
    ap.add_argument("--refresh", action="store_true", help="download again even if the file exists")
    ap.add_argument("--dry-run", action="store_true", help="list what would be downloaded; no API call, nothing written")
    a = ap.parse_args(argv)
    spread = 0.0
    try:
        if a.spread_over:
            spread = parse_duration(a.spread_over)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if a.sleep < 0 or (a.limit is not None and a.limit < 0) or any(c < 1 for c in a.cid):
        print("--sleep and --limit must be 0 or more, --cid a positive number", file=sys.stderr)
        return 2
    limit = a.limit if a.limit is not None else (0 if spread else 25)
    try:
        cids = list(a.cid)
        if a.cids_file:
            cids += parse_cids(a.cids_file.read_text(encoding="utf-8"))
        if a.from_vm:
            cids += parse_cids(ssh(a.ssh_host, REMOTE_CMD))
    except (OSError, ValueError, RuntimeError) as e:
        print(f"could not read the CIDs: {e}", file=sys.stderr)
        return 2
    cids = list(dict.fromkeys(cids))
    if not cids:
        print("no CIDs given: use --cid, --cids-file or --from-vm", file=sys.stderr)
        return 2
    start = now()
    out = a.out or Path.home() / "Documents" / "vatsim-atc-history" / start.strftime("%d-%m-%Y")
    pending = [c for c in cids if a.refresh or not (out / f"{c}.json").exists()]
    skipped = len(cids) - len(pending)
    todo = pending[:limit] if limit else pending
    if spread and a.sleep != DEFAULT_PAUSE:
        print("note: --spread-over sets the pause, so --sleep is ignored")
    elif not spread and a.sleep < WARN_BELOW:
        print(f"WARNING: --sleep {a.sleep:g} is below {WARN_BELOW:g} seconds; this is fast for a public API and may be rate limited")
    if spread:
        if spread < len(todo) * DEFAULT_PAUSE:
            print(f"note: {a.spread_over} is too short for about {len(todo)} calls at {DEFAULT_PAUSE:g} seconds each; using 5 seconds")
        total = max(spread, len(todo) * DEFAULT_PAUSE)
    else:
        total = len(todo) * a.sleep
    finish = start + timedelta(seconds=total)
    print(f"{len(todo)} controllers to download ({skipped} skipped, file exists). Expected finish, at one call per controller "
          f"(extra pages add time): {finish.astimezone():%d-%m-%Y %H:%M} local, {finish:%d-%m-%Y %H:%M} UTC")
    if a.dry_run:
        for c in todo:
            print(f"{c}: would download")
        print(f"would download {len(todo)}, skipped {skipped} (file exists); nothing written")
        return 0
    out.mkdir(parents=True, exist_ok=True)
    write_atomic(out / "README.txt", README)
    upgrade_manifest(out / "manifest.csv")
    deadline = start + timedelta(seconds=total)
    stop = False
    old_handler = None
    if handle_ctrl_c:
        def on_ctrl_c(signum: int, frame: Any) -> None:
            nonlocal stop
            stop = True
            signal.signal(signal.SIGINT, signal.default_int_handler)  # a second Ctrl+C stops at once
            print("\nCtrl+C: finishing the controller in progress, then stopping.")
        old_handler = signal.signal(signal.SIGINT, on_ctrl_c)
    fetched = not_found = sessions_total = 0
    stopped = ""
    position = [0]  # index of the controller in progress, read by wait()

    def wait(extra: int) -> None:
        calls_left = len(todo) - position[0] - 1 + extra  # further calls: later controllers plus the known extra pages
        if calls_left <= 0 or (stop and extra == 0):
            return
        mean = spread_pause((deadline - now()).total_seconds(), calls_left) if spread else a.sleep
        sleep(jittered(mean, rng))

    try:
        for i, cid in enumerate(todo):
            position[0] = i
            stamp = now().strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                sessions = download(cid, fetch, wait)
            except ApiNotFound:
                append_manifest(out / "manifest.csv", manifest_row(cid, [], stamp, "not_found"))
                print(f"{cid}: not found in the API")
                not_found += 1
                wait(0)  # the 404 was an API call too
            except ApiStop as e:
                stopped = str(e)
                break
            else:
                write_atomic(out / f"{cid}.json", json.dumps(
                    {"cid": cid, "downloaded_utc": stamp, "count": len(sessions), "sessions": sessions}, indent=1))
                append_manifest(out / "manifest.csv", manifest_row(cid, sessions, stamp, "ok"))
                print(f"{cid}: {len(sessions)} sessions")
                fetched += 1
                sessions_total += len(sessions)
            if stop:
                break
    finally:
        if handle_ctrl_c and old_handler is not None:
            signal.signal(signal.SIGINT, old_handler)
    print(f"downloaded {fetched}, skipped {skipped} (file exists), not found {not_found}, failed {1 if stopped else 0}, "
          f"sessions total {sessions_total}; folder {out}")
    if stopped:
        print(f"STOPPED early: {stopped}. Wait a while, then run the same command again; finished controllers are kept and skipped.")
        return 3
    if stop:
        print("Stopped by Ctrl+C. Run the same command again to carry on; controllers already written are skipped.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], handle_ctrl_c=True))
