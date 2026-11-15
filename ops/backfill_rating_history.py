#!/usr/bin/env python3
"""Load each controller's full VATSIM rating history into controller_rating_first_seen (issue 379). Run by hand, on the VM.

For every controller in our data that has no row in controller_rating_backfill, this reads the member's ATC history from the public
VATSIM API (no key) and stores, per rating, the start of the first session longer than 20 minutes. The rating rows and the marker row
are written in ONE transaction, so a member is either fully loaded or not at all.
Safe to stop and run again: members with a marker are skipped.

    python3 ops/backfill_rating_history.py --dry-run          show what would be written; writes nothing
    python3 ops/backfill_rating_history.py                    load up to 25 controllers (the default --limit)
    python3 ops/backfill_rating_history.py --limit 100 --sleep 2
    python3 ops/backfill_rating_history.py --cid 1234567      one controller (reloaded even if already marked)

Gentle on the API: one request at a time, a pause (--sleep, default 1.5 s) after every request, at most --limit controllers per run, and the
run STOPS at the first HTTP 429 or 5xx after a short backoff (it honours Retry-After).
Nothing is lost: finished controllers are already saved.
Database access: like ops/backfill_flights.sh, through `docker compose exec -T db psql` as the
database owner (POSTGRES_USER and POSTGRES_DB are
read inside the container), so run it from ~/vatpac-statsV2 on the VM; no password or key is read, printed or stored. Standard library only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

API = "https://api.vatsim.net/v2/members/{cid}/atc?limit={limit}&offset={offset}"
PAGE = 500
MIN_SESSION_SECONDS = 20 * 60  # a session must be LONGER than 20 minutes
RETRY_WAITS = (10.0, 30.0)  # backoff before giving up on a 429 or 5xx
PSQL = ["docker", "compose", "exec", "-T", "db", "sh", "-c",
        'psql -X -q -tA -1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -f -']

Fetch = Callable[[str], Any]  # url -> parsed JSON; raises ApiStop on 429 or 5xx, ApiNotFound on 404
Runner = Callable[[str], str]  # SQL in -> psql output


class ApiStop(Exception):
    """The API asked us to slow down or failed (HTTP 429 or 5xx): stop the whole run."""


class ApiNotFound(Exception):
    """HTTP 404: the API has no such member."""


def http_fetch(url: str, sleep: Callable[[float], None] = time.sleep) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "vatpac-stats-backfill", "Accept": "application/json"})
    for attempt in range(len(RETRY_WAITS) + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise ApiNotFound(url) from e
            if e.code != 429 and e.code < 500:
                raise
            if attempt == len(RETRY_WAITS):
                raise ApiStop(f"HTTP {e.code}") from e
            retry_after = e.headers.get("Retry-After", "") if e.headers else ""
            sleep(max(RETRY_WAITS[attempt], float(retry_after)) if retry_after.isdigit() else RETRY_WAITS[attempt])
        except (urllib.error.URLError, TimeoutError) as e:
            raise ApiStop(f"network error: {e}") from e
    raise ApiStop("unreachable")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def earliest_per_rating(items: list[dict[str, Any]]) -> dict[int, datetime]:
    """The earliest start per rating among sessions longer than 20 minutes (a session with no end is ignored)."""
    out: dict[int, datetime] = {}
    for item in items:
        c = item.get("connection_id", item)
        rating = c.get("rating")
        start, end = parse_time(c.get("start")), parse_time(c.get("end"))
        if not isinstance(rating, int) or isinstance(rating, bool) or start is None or end is None:
            continue
        if (end - start).total_seconds() > MIN_SESSION_SECONDS and (rating not in out or start < out[rating]):
            out[rating] = start
    return out


def fetch_history(cid: int, fetch: Fetch, sleep: Callable[[float], None], pause: float) -> dict[int, datetime]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        body = fetch(API.format(cid=cid, limit=PAGE, offset=offset))
        page = (body.get("items") or body.get("results") or []) if isinstance(body, dict) else body
        items += page
        sleep(pause)
        if len(page) < PAGE:
            return earliest_per_rating(items)
        offset += PAGE


def write_sql(cid: int, rows: dict[int, datetime]) -> str:
    """The rating rows, then the marker; psql -1 runs them as one transaction."""
    lines = [f"INSERT INTO controller_rating_first_seen (cid, rating, first_session_start) VALUES ({cid}, {r}, '{t.isoformat()}') "
             f"ON CONFLICT (cid, rating) DO UPDATE SET first_session_start = EXCLUDED.first_session_start;"
             for r, t in sorted(rows.items())]
    lines.append(f"INSERT INTO controller_rating_backfill (cid) VALUES ({cid}) ON CONFLICT (cid) DO UPDATE SET backfilled_at = now();")
    return "\n".join(lines) + "\n"


def psql(sql: str) -> str:
    r = subprocess.run(PSQL, input=sql, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"psql failed (exit {r.returncode}): {r.stderr.strip()[:500]}")
    return r.stdout


def pending_cids(run: Runner, limit: int) -> list[int]:
    sql = ("SELECT cid FROM (SELECT DISTINCT cid FROM controller_session) s "
           "WHERE NOT EXISTS (SELECT 1 FROM controller_rating_backfill b WHERE b.cid = s.cid) "
           f"ORDER BY cid LIMIT {int(limit)};\n")
    return [int(x) for x in run(sql).split()]


def main(argv: list[str], fetch: Fetch = http_fetch, run: Runner = psql, sleep: Callable[[float], None] = time.sleep) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--sleep", type=float, default=1.5, help="seconds to wait after every API request (default 1.5)")
    ap.add_argument("--limit", type=int, default=25, help="controllers per run (default 25)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be written; write nothing")
    ap.add_argument("--cid", type=int, help="load this one controller only")
    a = ap.parse_args(argv)
    if a.sleep < 0 or a.limit < 1 or (a.cid is not None and a.cid < 1):
        print("--sleep must be 0 or more, --limit 1 or more, --cid a positive number", file=sys.stderr)
        return 2
    cids = [a.cid] if a.cid else pending_cids(run, a.limit)
    done = empty = 0
    stopped = ""
    for cid in cids:
        try:
            rows = fetch_history(cid, fetch, sleep, a.sleep)
        except ApiNotFound:
            rows = {}
            print(f"{cid}: not found in the API; marked with no ratings")
        except ApiStop as e:
            stopped = str(e)
            break
        shown = ", ".join(f"{r}:{t:%d-%m-%Y}" for r, t in sorted(rows.items())) or "none"
        print(f"{cid}: {shown}" + ("  (dry run, not written)" if a.dry_run else ""))
        if not a.dry_run:
            run(write_sql(cid, rows))
        done += 1
        empty += not rows
    print(f"{'would load' if a.dry_run else 'loaded'} {done} of {len(cids)} controllers ({empty} with no long session)"
          + (f"; STOPPED early: {stopped}. Wait a while, then run again." if stopped else ""))
    return 3 if stopped else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
