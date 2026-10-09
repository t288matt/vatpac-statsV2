"""Issue 86: the grafana_ro write check in ops/smoke.sh must be unable to change data, and must still catch a wrong grant.

The check functions are cut out of smoke.sh (between the ro-check markers) and run with `dc` replaced by a function that runs the
same `psql` command line locally against the throwaway database, so the real statements and the real grep are exercised.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration

SMOKE = Path(__file__).resolve().parents[2] / "ops" / "smoke.sh"

FAKE_DC = r'''
dc() {
  # dc exec -T -e PGPASSWORD=pw db sh -c CMD sh ARG   ->   run CMD locally, as the compose service would
  PGPASSWORD="${4#PGPASSWORD=}" sh -c "$8" sh "${10}"
}
'''


def _run_check(ro_dsn: str) -> int:
    text = SMOKE.read_text(encoding="utf-8")
    block = text[text.index("# ro-check-begin"): text.index("# ro-check-end")]
    u = urlsplit(ro_dsn)
    env = dict(os.environ, POSTGRES_DB=u.path.lstrip("/"), PGPORT=str(u.port or 5432), RO_PW=u.password or "")
    script = FAKE_DC + "\n" + block + "\nro_denied_count\n"
    r = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return int(r.stdout.strip().splitlines()[-1])


@pytest.fixture()
def ro_dsn() -> str:
    dsn = os.environ.get("VATPAC_TEST_GRAFANA_RO_DSN")
    if not dsn:
        pytest.skip("VATPAC_TEST_GRAFANA_RO_DSN not set")
    if shutil.which("psql") is None:
        pytest.skip("psql binary not available")
    return dsn


def test_a_correctly_locked_down_role_passes_the_check(conn, ro_dsn: str) -> None:
    assert _run_check(ro_dsn) == 3


def test_a_wrongly_granted_role_fails_the_check_and_nothing_changes(conn, ro_dsn: str) -> None:
    conn.execute("INSERT INTO poll_log (status, polled_at) VALUES ('ok', now())")
    before = conn.execute("SELECT count(*) AS n FROM poll_log").fetchone()["n"]
    conn.execute("GRANT INSERT, DELETE ON poll_log TO grafana_ro")
    conn.execute("GRANT CREATE ON SCHEMA public TO grafana_ro")
    try:
        assert _run_check(ro_dsn) == 0                    # nothing was denied: the check fails
        assert conn.execute("SELECT count(*) AS n FROM poll_log").fetchone()["n"] == before
        assert conn.execute("SELECT to_regclass('public.smoke_should_fail') AS t").fetchone()["t"] is None
    finally:
        conn.execute("REVOKE INSERT, DELETE ON poll_log FROM grafana_ro")
        conn.execute("REVOKE CREATE ON SCHEMA public FROM grafana_ro")
    assert _run_check(ro_dsn) == 3                        # and passes again once the grants are gone


def test_the_old_form_would_have_deleted_rows(conn, ro_dsn: str) -> None:
    """Why the change matters: with DELETE wrongly granted, the old unqualified statement empties the table."""
    conn.execute("INSERT INTO poll_log (status, polled_at) VALUES ('ok', now())")
    conn.execute("GRANT DELETE ON poll_log TO grafana_ro")
    try:
        with psycopg.connect(ro_dsn, autocommit=True) as c:
            c.execute("SET default_transaction_read_only = off")
            c.execute("DELETE FROM poll_log")
        assert conn.execute("SELECT count(*) AS n FROM poll_log").fetchone()["n"] == 0
    finally:
        conn.execute("REVOKE DELETE ON poll_log FROM grafana_ro")
