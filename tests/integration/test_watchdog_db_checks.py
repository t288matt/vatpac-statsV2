"""Issue #55: the watchdog's database checks (ops/watchdog/watchdog.py check_db) against a real database.

The checks run as the owner here (they need only SELECT); the least-privilege role is tested in test_watchdog_flap.py."""
from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("watchdog_under_test_dbchecks", REPO / "ops" / "watchdog" / "watchdog.py")
assert spec and spec.loader
wd = importlib.util.module_from_spec(spec)
sys.modules["watchdog_under_test_dbchecks"] = wd
spec.loader.exec_module(wd)


@pytest.fixture()
def cfg(dsn: str):
    c = wd.Config()
    c.db_dsn = dsn
    return c


def poll(conn, status: str, age_s: float) -> None:
    conn.execute("INSERT INTO poll_log (status, polled_at) VALUES (%s, now() - make_interval(secs => %s))", (status, age_s))


def snapshot(conn, age_s: float) -> None:
    conn.execute("INSERT INTO network_snapshot VALUES (now() - make_interval(secs => %s),1,1,1,1)", (age_s,))


def test_an_empty_database_warns_that_nothing_has_arrived_yet(conn, cfg) -> None:
    got = wd.check_db(cfg)
    assert got["database"] == (wd.OK, "reachable")
    assert got["poll_log_ok_age"] == (wd.WARN, "no newest ok poll yet")
    assert got["network_snapshot_age"] == (wd.WARN, "no newest snapshot yet")
    assert got["_near_miss"] == (wd.OK, "") and got["identity_flap"][0] == wd.OK


@pytest.mark.parametrize("age,level", [(5, wd.OK), (90, wd.WARN), (400, wd.CRIT)])
def test_poll_and_snapshot_age_levels(conn, cfg, age: float, level: str) -> None:
    poll(conn, "ok", age)
    snapshot(conn, age)
    got = wd.check_db(cfg)
    assert got["poll_log_ok_age"][0] == level and got["network_snapshot_age"][0] == level


def test_only_ok_polls_count_for_the_poll_age(conn, cfg) -> None:
    poll(conn, "ok", 400)
    poll(conn, "error", 1)
    poll(conn, "skipped_unchanged", 1)
    assert wd.check_db(cfg)["poll_log_ok_age"][0] == wd.CRIT


def test_database_size_limit(conn, cfg) -> None:
    assert wd.check_db(cfg)["db_size"][0] == wd.OK
    cfg.db_max_gb = 0.0001                                    # about 100 KB: the schema alone is bigger
    level, detail = wd.check_db(cfg)["db_size"]
    assert level == wd.WARN and "limit 0.0001" in detail


def test_near_miss_callsigns_are_listed_oldest_first(conn, cfg) -> None:
    t = datetime(2026, 10, 7, 16, 0, tzinfo=UTC)
    for i, cs in enumerate(["ML_XYZ_CTR", "AD_XYZ_TWR"]):
        conn.execute("INSERT INTO controller_unlisted_sighting (callsign, cid, first_seen, last_seen) VALUES (%s,1,%s,%s)",
                     (cs, t + timedelta(minutes=i), t + timedelta(minutes=i)))
    assert wd.check_db(cfg)["_near_miss"] == (wd.OK, "ML_XYZ_CTR,AD_XYZ_TWR")


def test_an_unreachable_database_is_critical(cfg) -> None:
    # Linux refuses a closed local port at once. Windows retries the SYN for about 2 s before reporting the refusal, which races
    # connect_timeout and gives ConnectionTimeout instead of OperationalError; there a missing socket directory fails instantly.
    host = "/nonexistent-socket-dir" if sys.platform == "win32" else "127.0.0.1 port=1"
    cfg.db_dsn = f"host={host} dbname=x user=x connect_timeout=2"
    got = wd.check_db(cfg)
    assert got == {"database": (wd.CRIT, "query/connect failed (OperationalError)")}


def test_the_newest_row_decides_the_age_not_the_oldest(conn, cfg) -> None:
    for age in (4000, 400, 5):
        poll(conn, "ok", age)
        snapshot(conn, age)
    got = wd.check_db(cfg)
    assert got["poll_log_ok_age"][0] == wd.OK and got["network_snapshot_age"][0] == wd.OK
