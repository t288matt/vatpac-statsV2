"""ops/backup/backup.sh with a fake pg_dump and pg_restore on PATH (issue 81).

Covers: pg_dump failing, pg_dump producing junk that pg_restore -l rejects (nothing kept, no success marker, no off-box
call, older dumps NOT rotated), and the good path (dump kept, marker written, remote hook run, old dumps rotated).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "backup" / "backup.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

FAKE_PG_DUMP = """#!/bin/sh
# usage: pg_dump -Fc --no-password -f FILE
while [ $# -gt 0 ]; do [ "$1" = "-f" ] && out="$2"; shift; done
case "$FAKE_DUMP" in
  fail) echo "pg_dump: error: connection refused" >&2; exit 1 ;;
  junk) echo "this is not a dump" > "$out" ;;
  good) echo "GOODDUMP" > "$out" ;;
esac
"""
FAKE_PG_RESTORE = """#!/bin/sh
# usage: pg_restore -l FILE ; succeeds only for a good fake dump
grep -q GOODDUMP "$2" || { echo "pg_restore: error: unsupported version in file header" >&2; exit 1; }
"""


def _run(tmp_path: Path, mode: str, remote: bool = True) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir, backups = tmp_path / "bin", tmp_path / "backups"
    bin_dir.mkdir(exist_ok=True)
    backups.mkdir(exist_ok=True)
    for name, body in (("pg_dump", FAKE_PG_DUMP), ("pg_restore", FAKE_PG_RESTORE)):
        f = bin_dir / name
        f.write_text(body, newline="\n")
        f.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", BACKUP_DIR=str(backups),
               PGDATABASE="vatpac", FAKE_DUMP=mode, BACKUP_RETENTION_DAYS="14")
    env["BACKUP_REMOTE"] = f'touch "{backups}/remote_called"' if remote else ""
    old = backups / "vatpac_20200101T000000Z.dump"
    old.write_text("GOODDUMP")
    month_ago = time.time() - 30 * 86400
    os.utime(old, (month_ago, month_ago))
    assert BASH is not None
    r = subprocess.run([BASH, str(SCRIPT), "once"], env=env, capture_output=True, text=True, timeout=60)
    return r, backups


def _new_dumps(backups: Path) -> list[Path]:
    return [p for p in backups.glob("*.dump") if "20200101" not in p.name]


def test_pg_dump_failure_keeps_nothing_and_rotates_nothing(tmp_path: Path) -> None:
    r, backups = _run(tmp_path, "fail")
    assert r.returncode != 0
    assert "FAILED pg_dump" in r.stdout
    assert not _new_dumps(backups) and not list(backups.glob("*.partial"))
    assert not (backups / ".last_success").exists() and not (backups / "remote_called").exists()
    assert (backups / "vatpac_20200101T000000Z.dump").exists(), "older dumps must not be rotated after a failed backup"


def test_junk_dump_is_rejected_even_when_called_through_or(tmp_path: Path) -> None:
    """The defect: pg_restore -l failing used to be ignored, so the junk was renamed to .dump and marked a success."""
    r, backups = _run(tmp_path, "junk")
    assert r.returncode != 0
    assert "FAILED validation" in r.stdout
    assert not _new_dumps(backups) and not list(backups.glob("*.partial"))
    assert not (backups / ".last_success").exists()
    assert not (backups / "remote_called").exists()
    assert (backups / "vatpac_20200101T000000Z.dump").exists()


def test_junk_dump_in_the_loop_style_or_call_also_stops(tmp_path: Path) -> None:
    """Same as above but calling take_backup as `take_backup || log ...`, the form that disables set -e in bash."""
    bin_dir, backups = tmp_path / "bin", tmp_path / "backups"
    bin_dir.mkdir()
    backups.mkdir()
    for name, body in (("pg_dump", FAKE_PG_DUMP), ("pg_restore", FAKE_PG_RESTORE)):
        (bin_dir / name).write_text(body, newline="\n")
        (bin_dir / name).chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", BACKUP_DIR=str(backups),
               PGDATABASE="vatpac", FAKE_DUMP="junk", BACKUP_REMOTE=f'touch "{backups}/remote_called"')
    # source only the functions (everything above the final `case`), then call the way the loop does
    text = SCRIPT.read_text()
    funcs = text[: text.index('case "${1:-loop}"')]
    harness = tmp_path / "h.sh"
    harness.write_text(funcs + '\ntake_backup || echo "caller saw failure"\n', newline="\n")
    assert BASH is not None
    r = subprocess.run([BASH, str(harness)], env=env, capture_output=True, text=True, timeout=60)
    assert "caller saw failure" in r.stdout
    assert not list(backups.glob("*.dump")) and not (backups / ".last_success").exists()
    assert not (backups / "remote_called").exists()


def test_good_path(tmp_path: Path) -> None:
    r, backups = _run(tmp_path, "good")
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(_new_dumps(backups)) == 1
    assert (backups / ".last_success").exists()
    assert (backups / "remote_called").exists() and (backups / ".last_remote_success").exists()
    assert not (backups / "vatpac_20200101T000000Z.dump").exists(), "old dump should be rotated after a good backup"
