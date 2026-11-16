"""Alert for pilots who log on, file a plan and never get airborne (GitHub issue 331).

The rule lives in the database: the view ops_never_flew_alert has one row per logon (CID plus logon time) that is online now with a
plan filed and not airborne yet, and is either a repeat non-flier (the dashboard panel's rule, view pilot_never_flew_90d) or a
first-timer with a Y name. This module reads that view each watchdog cycle and tells the alerter about every logon it has not
alerted for yet. It never writes to the database and never imports watchdog.py.

Memory: the alerted logons are kept in the watchdog state (the status file), with the time each was alerted. A logon is forgotten
REMEMBER hours after it was alerted, unless it is still in the view (a pilot sitting at the gate for days is not alerted again
every day). The first cycle after a deploy (no saved list yet) records the current rows and sends nothing. In dry-run mode
nothing is sent: what would be sent goes to the log, and the logons are recorded as in live mode.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from messages import Checked

WARN = "warn"
NAME = "never-flew"
STATE_KEY = "never_flew_alerted"  # {"<cid>|<logon time UTC>": epoch seconds when it was alerted or first recorded}

log = logging.getLogger("watchdog.never_flew")

QUERY = ("SELECT cid, name, kind, flights_90d, logon_number, callsign, logon_time, departure, arrival, route, lat, lon "
         "FROM ops_never_flew_alert ORDER BY logon_time, cid")


@dataclass(frozen=True)
class Logon:
    cid: int
    name: str
    kind: str  # first_timer or repeat
    flights_90d: int
    logon_number: int
    callsign: str
    logon_time: datetime
    departure: str
    arrival: str
    route: str | None
    lat: float | None
    lon: float | None

    @property
    def key(self) -> str:
        return f"{self.cid}|{self.logon_time.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}"

    def facts(self) -> dict[str, Any]:
        """The values the Telegram wording needs, JSON-safe (messages._never_flew reads these and nothing else)."""
        return {"cid": self.cid, "name": self.name, "kind": self.kind, "flights_90d": self.flights_90d,
                "logon_number": self.logon_number, "callsign": self.callsign, "departure": self.departure,
                "arrival": self.arrival, "route": self.route, "lat": self.lat, "lon": self.lon,
                "logon": self.logon_time.astimezone(UTC).strftime("%d-%m-%Y %H:%M")}


def read_logons(dsn: str) -> list[Logon]:
    """The view's rows, oldest logon first. Raises psycopg.Error when the read fails; the cycle reports that as a database problem."""
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(QUERY)
        return [Logon(*r) for r in cur.fetchall()]


def due(state: dict[str, Any], rows: list[Logon], remember_s: float, now: float) -> list[Logon]:
    """The rows to alert for, oldest first; updates the saved list in state. The first call (no saved list) alerts for none."""
    first = STATE_KEY not in state
    seen: dict[str, float] = state.setdefault(STATE_KEY, {})
    current = {r.key for r in rows}
    for key in [k for k, at in seen.items() if now - at > remember_s and k not in current]:
        del seen[key]
    fresh = [r for r in rows if r.key not in seen]
    for r in fresh:
        seen[r.key] = now
    return [] if first else fresh


def run(dsn: str, state: dict[str, Any], remember_hours: float, dry_run: bool,
        notify: Callable[[str, Checked], None], now: float | None = None) -> int:
    """One cycle: read the view and alert (or, in dry-run, log) once per new logon. Returns how many logons were new and due.
    Raises psycopg.Error when the read fails, before anything is recorded."""
    rows = read_logons(dsn)
    fresh = due(state, rows, remember_hours * 3600, time.time() if now is None else now)
    for r in fresh:
        result = Checked(WARN, f"cid {r.cid} {r.kind} logon {r.key.split('|', 1)[1]} {r.departure}-{r.arrival}", **r.facts())
        if dry_run:
            log.info("DRY RUN, would alert: %s", result[1])
        else:
            notify(NAME, result)
    return len(fresh)
