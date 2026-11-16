"""Independent QA support for issue 387 (written from the design, not from the builders' tests).

SQL files run through a local `psql` when there is one, otherwise through `docker exec ... psql` in the container.
Environment: QA_DB_DSN (a superuser DSN of an existing server; databases vpqa_* are created and dropped on it), or else the
QA_PG_CONTAINER (default vpprev-qa387-db), QA_PG_PORT (55387) and QA_PG_SUPER_PW (qa-super-pw) of a throwaway container.
Without either the database tests skip, unless QA_STRICT=1 (then they fail).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import psycopg
import pytest

REPO = Path(__file__).resolve().parents[1]
CONTAINER = os.environ.get("QA_PG_CONTAINER", "vpprev-qa387-db")
PORT = os.environ.get("QA_PG_PORT", "55387")
SUPER_PW = os.environ.get("QA_PG_SUPER_PW", "qa-super-pw")
# QA_DB_DSN (a superuser DSN, any database) makes the tests use that existing server and build their own vpqa_* databases on it.
EXTERNAL = os.environ.get("QA_DB_DSN", "").strip()
# Role passwords. roles.sql resets them cluster-wide, so on a shared server (QA_DB_DSN) set the variables to the passwords the
# other tests already use; otherwise the defaults below are used on the throwaway container.
PASSWORDS = {"grafana_ro": os.environ.get("GRAFANA_RO_PASSWORD", "qa-grafana-pw"),
             "collector": os.environ.get("COLLECTOR_DB_PASSWORD", "qa-collector-pw"),
             "backup": os.environ.get("BACKUP_DB_PASSWORD", "qa-backup-pw"),
             "watchdog": os.environ.get("WATCHDOG_DB_PASSWORD", "qa-watchdog-pw"),
             "stats_loader": os.environ.get("STATS_LOADER_DB_PASSWORD", "qa-stats-pw")}
NEW_MIGRATION = "20261115000400_atc_stats.sql"
DB_PREFIX = "vpqa_"
_created: list[str] = []


def _super() -> urllib.parse.SplitResult:
    return urllib.parse.urlsplit(EXTERNAL or f"postgresql://postgres:{SUPER_PW}@127.0.0.1:{PORT}/postgres")


def dbname(name: str) -> str:
    """Throwaway database names always start with vpqa_ (the tests ask for qa_x_test)."""
    return name if name.startswith(DB_PREFIX) or name == "postgres" else DB_PREFIX + name.removeprefix("qa_")


def dsn(db: str, user: str = "postgres") -> str:
    u = _super()
    host = f"{u.hostname}:{u.port}" if u.port else str(u.hostname)
    if user == "postgres":
        creds = f"{urllib.parse.quote(u.username or 'postgres')}:{urllib.parse.quote(u.password or '')}"
    else:
        pw = {"stats_loader": PASSWORDS["stats_loader"], "grafana_ro": PASSWORDS["grafana_ro"], "watchdog_ro": PASSWORDS["watchdog"],
              "collector": PASSWORDS["collector"]}[user]
        creds = f"{user}:{urllib.parse.quote(pw)}"
    return f"postgresql://{creds}@{host}/{dbname(db)}{'?' + u.query if u.query else ''}"


def psql(db: str, sql: str, **variables: str) -> None:
    u = _super()
    flags = ["-X", "-q", "-v", "ON_ERROR_STOP=1"]
    for k, v in variables.items():
        flags += ["-v", f"{k}={v}"]
    if shutil.which("psql"):
        args = ["psql", "-h", str(u.hostname), "-p", str(u.port or 5432), "-U", u.username or "postgres", "-d", dbname(db), *flags]
        env = {**os.environ, "PGPASSWORD": u.password or SUPER_PW}
    else:  # no local psql (a developer PC): run it inside the container
        args = ["docker", "exec", "-i", CONTAINER, "psql", "-U", u.username or "postgres", "-d", dbname(db), *flags]
        env = None
    res = subprocess.run([*args, "-f", "-"], input=sql, text=True, capture_output=True, encoding="utf-8", env=env)
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
    """A blank database, every migration in order (optionally only those before the ATC stats one: the later ones, such as the
    promotions view and its index, read atc_stats_session and cannot apply without it), then roles.sql."""
    db = dbname(db)
    psql("postgres", f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE); CREATE DATABASE "{db}";')
    # the non-UTC condition must not depend on the server's default: set it on our own database
    psql("postgres", f"ALTER DATABASE \"{db}\" SET timezone TO 'Pacific/Auckland';")
    _created.append(db)
    for path in migrations():
        if stop_before_new and path.name >= NEW_MIGRATION:
            continue
        psql(db, up_part(path))
    if not stop_before_new:
        apply_roles(db)


import atexit  # noqa: E402


def drop_created() -> None:
    for db in _created:
        psql("postgres", f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE);')
    _created.clear()


def db_ready() -> bool:
    try:
        with psycopg.connect(dsn("postgres"), connect_timeout=5):
            return True
    except psycopg.Error:
        return False


def need_db() -> None:
    if not db_ready():
        if os.environ.get("QA_STRICT") == "1":
            pytest.fail("no QA database is reachable (set QA_DB_DSN, or start the vpprev- container)")
        pytest.skip("no QA database is reachable")


atexit.register(drop_created)
