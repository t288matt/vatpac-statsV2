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
        st.polls_error += 1
        st.consecutive_failures += 1
        log.warning("poll error", extra={"error": err, "consecutive_failures": st.consecutive_failures})
    else:
        st.polls_skipped += status == "skipped_unchanged"
        st.polls_ok += status == "ok"
        st.consecutive_failures = 0
        if status == "ok":  # an unchanged feed is not progress (see health.py)
            st.last_ok_wall = time.time()
    if result is not None and snap_ts is not None:
        st.last_snapshot_ts = snap_ts
        st.positions_written += result.positions_inserted
        st.rejected_values += result.rejected_values
        for name in ("pilots_seen", "pilots_inside", "pilots_kept", "controllers_seen", "controllers_kept"):
            setattr(st, name, getattr(result, name))
    was_open = st.breaker_open
    st.breaker_open = st.consecutive_failures >= s.circuit_failure_threshold
    if st.breaker_open != was_open:
        log.log(logging.ERROR if st.breaker_open else logging.INFO,
                "circuit breaker %s", "opened" if st.breaker_open else "closed")
