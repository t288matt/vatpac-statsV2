"""ops/v1_import/load.sh without V1_IMPORT_PSQL (issue 279): the default command is `docker compose exec -T db sh -c 'psql ...'`.
A fake `docker` on PATH records its arguments and the SQL stream, so the default path is exercised without a database."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "tools"))
import v1_import_synth as synth  # noqa: E402

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

FAKE_DOCKER = """#!/bin/sh
printf '%s\n' "$@" > "$FAKE_ARGS"
cat > "$FAKE_STDIN"
"""


def test_the_default_command_runs_psql_as_the_container_owner_and_streams_the_script(tmp_path: Path) -> None:
    data = tmp_path / "d"
    synth.generate(data, {"2025-09": 30}, {"2025-09": 20})
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(FAKE_DOCKER, encoding="utf-8", newline="\n")
    (bindir / "docker").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "V1_IMPORT_PSQL"}
    env.update(PATH=f"{bindir.as_posix()}{os.pathsep}{env['PATH']}", FAKE_ARGS=(tmp_path / "args").as_posix(),
               FAKE_STDIN=(tmp_path / "in").as_posix())
    p = subprocess.run([str(BASH), (REPO / "ops/v1_import/load.sh").as_posix(), "--expect-db", "somedb", "--data-dir", data.as_posix()],
                       capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, p.stderr
    args = (tmp_path / "args").read_text(encoding="utf-8").splitlines()
    assert args[:5] == ["compose", "exec", "-T", "db", "sh"] and args[5] == "-c"
    assert args[6] == 'psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1'  # expanded inside the container, not here
    sql = (tmp_path / "in").read_text(encoding="utf-8")
    head = sql.splitlines()[:3]
    assert head == [r"\set ON_ERROR_STOP on", r"\set expect_db 'somedb'", r"\set is_apply false"]  # dry run unless --apply
    assert sql.count("FROM STDIN") == 3 and "ROLLBACK;" in sql
