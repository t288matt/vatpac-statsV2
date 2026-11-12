"""Poll-loop statistics: fold one poll's outcome into the shared Stats (and the circuit breaker)."""

from __future__ import annotations

import logging
import time
from datetime import datetime

from .health import Stats
from .settings import Settings
from .store import ApplyResult

log = logging.getLogger("vatpac")


def update_stats(st: Stats, s: Settings, status: str, result: ApplyResult | None, snap_ts: datetime | None,
                 err: str | None) -> None:
    if status == "error":
        st.consecutive_failures += 1
        log.warning("poll error", extra={"error": err, "consecutive_failures": st.consecutive_failures})
    else:
        st.consecutive_failures = 0
        if status == "ok":  # an unchanged feed is not progress (see health.py)
            st.last_ok_wall = time.time()
    if result is not None and snap_ts is not None:
        st.last_snapshot_ts = snap_ts
        for callsign in result.new_near_misses:
            log.warning("near-miss controller callsign not on whitelist", extra={"callsign": callsign})
    was_open = st.breaker_open
    st.breaker_open = st.consecutive_failures >= s.circuit_failure_threshold
    if st.breaker_open != was_open:
        log.log(logging.ERROR if st.breaker_open else logging.INFO,
                "circuit breaker %s", "opened" if st.breaker_open else "closed")
