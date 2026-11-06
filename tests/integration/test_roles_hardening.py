"""Role hardening: issue 95 (collector may DELETE only from poll_log) and issue 84 (re-applying roles is atomic).

The atomicity test runs the real db/roles.sql and the watchdog_ro block of ops/migrate/migrate.sh with the psql binary while
a second connection, acting as collector / watchdog_ro, keeps reading and writing. Before the fix the REVOKE and the GRANT
were separate statements, so the reader could see "permission denied" in between; now there must be none. ROLES_SQL_PATH
(and MIGRATE_SH_PATH at another migrate.sh) point the test at other copies, used to show that the old files fail this test.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from psycopg import sql as pgsql

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
NINE = ["pilot_session", "pilot_position", "controller_session", "controller_rating_event", "controller_frequency_event",
        "network_snapshot", "controller_unlisted_sighting", "reference_data_change", "app_setting"]


def _role(conn, name: str) -> bool:
    return conn.execute("SELECT 1 AS x FROM pg_roles WHERE rolname = %s", (name,)).fetchone() is not None


# ---------------------------------------------------------------- issue 95
@pytest.mark.parametrize("table", NINE)
def test_collector_cannot_delete_from_data_tables(conn, table: str) -> None:
    if not _role(conn, "collector"):
        pytest.skip("role collector not created")
    conn.execute("SELECT ensure_pilot_position_partitions(1, current_date)")
    conn.execute("SET ROLE collector")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"DELETE FROM {table} WHERE false")  # the privilege is checked before any row is looked at
    finally:
        conn.execute("RESET ROLE")


def test_collector_can_still_trim_poll_log_and_upsert(conn) -> None:
    if not _role(conn, "collector"):
        pytest.skip("role collector not created")
    conn.execute("SET ROLE collector")
    try:
        conn.execute("DELETE FROM poll_log WHERE polled_at < now() - make_interval(days => %s)", (400,))
        conn.execute("INSERT INTO app_setting (key, value) VALUES ('hardening_probe', '1') "
                     "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")
        conn.execute("UPDATE app_setting SET value = '2' WHERE key = 'hardening_probe'")
    finally:
        conn.execute("RESET ROLE")


def test_nothing_in_the_collector_source_deletes_except_the_poll_log_trim() -> None:
    """The grant is DELETE on poll_log only, so the collector code must not delete from anything else."""
    hits = []
    for f in (REPO / "collector" / "src").rglob("*.py"):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bDELETE\s+FROM\b|\bTRUNCATE\b", line, re.I) and "poll_log" not in line:
                if "truncated" in line.lower():  # prose about logon_time truncation, not SQL
                    continue
                hits.append(f"{f.name}:{n}: {line.strip()}")
    assert hits == []


# ---------------------------------------------------------------- issue 84
def _watchdog_block() -> str:
    text = Path(os.environ.get("MIGRATE_SH_PATH", REPO / "ops" / "migrate" / "migrate.sh")).read_text(encoding="utf-8")
    m = re.search(r"<<'SQL'\n(.*?)\nSQL\n", text, re.S)
    assert m, "watchdog_ro SQL block not found in migrate.sh"
    return m.group(1)


class Hammer(threading.Thread):
    """Reads and writes as `role` in a loop and records every error."""

    def __init__(self, dsn: str, role: str, statements: list[str]) -> None:
        super().__init__(daemon=True)
        self.dsn, self.role, self.statements = dsn, role, statements
        self.stop_flag = threading.Event()
        self.errors: list[str] = []
        self.loops = 0

    def run(self) -> None:
        while not self.stop_flag.is_set():
            try:
                with psycopg.connect(self.dsn, autocommit=True) as c:
                    c.execute(f"SET ROLE {self.role}")
                    while not self.stop_flag.is_set():
                        for sql in self.statements:
                            c.execute(sql)
                        self.loops += 1
            except psycopg.Error as exc:
                self.errors.append(f"{type(exc).__name__}: {str(exc).splitlines()[0]}")


def test_reapplying_roles_never_shows_permission_denied(dsn: str, conn, tmp_path: Path) -> None:
    psql = shutil.which("psql")
    if psql is None:
        pytest.skip("psql binary not available")
    for r in ("collector", "grafana_ro", "metabase_ro", "backup_ro", "watchdog_ro"):
        if not _role(conn, r):
            pytest.skip(f"role {r} not created")
    roles_sql = Path(os.environ.get("ROLES_SQL_PATH", REPO / "db" / "roles.sql"))
    wd_sql = tmp_path / "watchdog_ro.sql"
    wd_sql.write_text(_watchdog_block(), encoding="utf-8")
    # the scripts reset the passwords: remember the hashes and put them back afterwards so the other tests can still log in
    hashes = {r["rolname"]: r["rolpassword"] for r in conn.execute(
        "SELECT rolname, rolpassword FROM pg_authid WHERE rolname IN ('collector','grafana_ro','metabase_ro','backup_ro','watchdog_ro')").fetchall()}
    collector = Hammer(dsn, "collector", ["SELECT count(*) FROM pilot_session", "SELECT count(*) FROM poll_log",
                                          "INSERT INTO app_setting (key, value) VALUES ('race_probe','1') "
                                          "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"])
    watchdog = Hammer(dsn, "watchdog_ro", ["SELECT count(*) FROM poll_log", "SELECT count(*) FROM pilot_session"])
    grafana = Hammer(dsn, "grafana_ro", ["SELECT count(*) FROM region_map"])
    hammers = [collector, watchdog, grafana]
    for h in hammers:
        h.start()
    try:
        for _ in range(25):
            common = [psql, dsn, "-X", "-q", "-v", "ON_ERROR_STOP=1"]
            r1 = subprocess.run(common + ["-v", "grafana_ro_password=tmp1", "-v", "collector_password=tmp2",
                                          "-v", "backup_password=tmp3", "-v", "metabase_ro_password=tmp5", "-f", str(roles_sql)], capture_output=True, text=True, timeout=120)
            assert r1.returncode == 0, r1.stderr
            r2 = subprocess.run(common + ["-v", "wd_pw=tmp4", "-f", str(wd_sql)], capture_output=True, text=True, timeout=120)
            assert r2.returncode == 0, r2.stderr
    finally:
        for h in hammers:
            h.stop_flag.set()
        for h in hammers:
            h.join(timeout=30)
        for name, pw in hashes.items():
            if pw:
                conn.execute(pgsql.SQL("ALTER ROLE {} PASSWORD {}").format(pgsql.Identifier(name), pgsql.Literal(pw)))
        conn.execute("DELETE FROM app_setting WHERE key = 'race_probe'")
    assert all(h.loops > 50 for h in hammers), [h.loops for h in hammers]  # the readers really were running
    assert [h.errors for h in hammers] == [[], [], []]
