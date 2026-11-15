"""Independent QA support for issue 387 (written from the design, not from the builders' tests).

A throwaway Postgres container (name starts with vpprev-) is reached through `docker exec ... psql`, so no local psql is needed.
Environment: QA_PG_CONTAINER (default vpprev-qa387-db), QA_PG_PORT (default 55387), QA_PG_SUPER_PW (default qa-super-pw).
Without the container the database tests skip, unless QA_STRICT=1 (then they fail).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CONTAINER = os.environ.get("QA_PG_CONTAINER", "vpprev-qa387-db")
PORT = os.environ.get("QA_PG_PORT", "55387")
SUPER_PW = os.environ.get("QA_PG_SUPER_PW", "qa-super-pw")
PASSWORDS = {"grafana_ro": "qa-grafana-pw", "collector": "qa-collector-pw", "backup": "qa-backup-pw",
             "watchdog": "qa-watchdog-pw", "stats_loader": "qa-stats-pw"}
NEW_MIGRATION = "20261115000400_atc_stats.sql"


def dsn(db: str, user: str = "postgres") -> str:
    pw = SUPER_PW if user == "postgres" else {"stats_loader": PASSWORDS["stats_loader"], "grafana_ro": PASSWORDS["grafana_ro"],
                                              "watchdog_ro": PASSWORDS["watchdog"]}[user]
    return f"postgresql://{user}:{pw}@127.0.0.1:{PORT}/{db}"


def psql(db: str, sql: str, **variables: str) -> None:
    args = ["docker", "exec", "-i", CONTAINER, "psql", "-U", "postgres", "-d", db, "-X", "-q", "-v", "ON_ERROR_STOP=1"]
    for k, v in variables.items():
        args += ["-v", f"{k}={v}"]
    res = subprocess.run([*args, "-f", "-"], input=sql, text=True, capture_output=True, encoding="utf-8")
    if res.returncode:
        raise RuntimeError(f"psql failed: {res.stderr[:500]}")


def up_part(path: Path) -> str:
    return path.read_text(encoding="utf-8").split("-- migrate:down")[0]


def migrations() -> list[Path]:
    return sorted((REPO / "db" / "migrations").glob("*.sql"))


def apply_roles(db: str) -> None:
    psql(db, (REPO / "db" / "roles.sql").read_text(encoding="utf-8"),
         grafana_ro_password=PASSWORDS["grafana_ro"], collector_password=PASSWORDS["collector"],
         backup_password=PASSWORDS["backup"], watchdog_password=PASSWORDS["watchdog"],
         stats_loader_password=PASSWORDS["stats_loader"])


def build_db(db: str, *, stop_before_new: bool = False) -> None:
    """A blank database, every migration in order (optionally all but the ATC stats one), then roles.sql."""
    psql("postgres", f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE); CREATE DATABASE "{db}";')
    for path in migrations():
        if stop_before_new and path.name == NEW_MIGRATION:
            continue
        psql(db, up_part(path))
    if not stop_before_new:
        apply_roles(db)


def docker_ready() -> bool:
    res = subprocess.run(["docker", "exec", CONTAINER, "pg_isready", "-U", "postgres"], capture_output=True)
    return res.returncode == 0


def need_db() -> None:
    if not docker_ready():
        if os.environ.get("QA_STRICT") == "1":
            pytest.fail("the QA throwaway Postgres is not reachable")
        pytest.skip("the QA throwaway Postgres is not reachable")
