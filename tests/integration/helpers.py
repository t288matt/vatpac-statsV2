"""Helpers shared by the integration tests (see conftest.py)."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone


def partition_for(conn, day: datetime) -> str:
    """Create the daily partition pilot_position_YYYYMMDD for the UTC day containing ``day``."""
    start = day.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    name = f"pilot_position_{start:%Y%m%d}"
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF pilot_position "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )
    return name


def at(payload: dict, seconds: int = 0, **changes) -> dict:
    """Copy of the fixture payload with update_timestamp shifted by ``seconds``."""
    out = copy.deepcopy(payload)
    base = datetime(2026, 10, 7, 16, 11, 19, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    out["general"]["update_timestamp"] = base.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    return out


def apply(conn, payload: dict, refdata, polygon_sha: str = "p" * 64, whitelist_sha: str = "w" * 64):
    from vatpac.model import parse_snapshot
    from vatpac.store import apply_snapshot

    poly, wl = refdata
    result = apply_snapshot(conn, parse_snapshot(payload), poly, wl,
                            polygon_sha=polygon_sha, whitelist_sha=whitelist_sha)
    if not conn.autocommit:
        conn.commit()
    return result


def counts(conn) -> dict[str, int]:
    tables = ["pilot_session", "pilot_position", "controller_session", "controller_rating_event",
              "controller_frequency_event", "network_snapshot"]
    return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in tables}
