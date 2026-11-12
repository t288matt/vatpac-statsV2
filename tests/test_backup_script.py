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
  big) echo "GOODDUMP" > "$out"; head -c "${FAKE_PAD:-0}" /dev/zero >> "$out" ;;
  nodata) echo "GOODDUMP NODATA" > "$out" ;;
  hang) echo partial > "$out"; exec sleep 30 ;;
esac
"""
FAKE_PG_RESTORE = """#!/bin/sh
# usage: pg_restore -l FILE ; succeeds only for a good fake dump
grep -q GOODDUMP "$2" || { echo "pg_restore: error: unsupported version in file header" >&2; exit 1; }
echo "1; 0 1 SCHEMA - public owner"
grep -q NODATA "$2" && exit 0
for t in poll_log pilot_session network_snapshot app_setting pilot_position_20261006; do echo "2; 0 2 TABLE DATA public $t owner"; done
"""


def _run(tmp_path: Path, mode: str, remote: bool = True, marker_is_dir: bool = False, old: int = 3, old_pad: int = 0, **extra_env: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir, backups = tmp_path / "bin", tmp_path / "backups"
    bin_dir.mkdir(parents=True, exist_ok=True)
    backups.mkdir(exist_ok=True)
    for name, body in (("pg_dump", FAKE_PG_DUMP), ("pg_restore", FAKE_PG_RESTORE)):
        f = bin_dir / name
        f.write_text(body, newline="\n")
        f.chmod(0o755)
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", BACKUP_DIR=str(backups),
               PGDATABASE="vatpac", FAKE_DUMP=mode, BACKUP_RETENTION_DAYS="14")
    env["BACKUP_REMOTE"] = f'touch "{backups}/remote_called"' if remote else ""
    env.update(extra_env)
    if marker_is_dir:
        (backups / ".last_success").mkdir()
    # old dumps 20200101 (oldest) .. all more than 14 days old; old_pad makes each one that many bytes bigger
    for i in range(old):
        f = backups / f"vatpac_2020010{i + 1}T000000Z.dump"
        f.write_text("GOODDUMP" + "x" * old_pad)
        aged = time.time() - (32 - i) * 86400
        os.utime(f, (aged, aged))
    assert BASH is not None
    r = subprocess.run([BASH, str(SCRIPT), "once"], env=env, capture_output=True, text=True, timeout=60)
    return r, backups


def _new_dumps(backups: Path) -> list[Path]:
    return [p for p in backups.glob("*.dump") if "2020010" not in p.name]


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


def test_a_marker_that_cannot_be_written_does_not_skip_the_remote_copy_or_rotation(tmp_path: Path) -> None:
    r, backups = _run(tmp_path, "good", marker_is_dir=True)
    assert "WARNING: could not write .last_success" in r.stdout
    assert len(_new_dumps(backups)) == 1                              # the good dump is kept
    assert (backups / "remote_called").exists() and (backups / ".last_remote_success").exists()
    assert not (backups / "vatpac_20200101T000000Z.dump").exists()     # rotation still ran
    assert (backups / ".last_success").is_dir()                       # and success was not claimed by writing a marker


def test_a_hung_pg_dump_times_out_as_a_failed_backup(tmp_path: Path) -> None:
    t0 = time.time()
    r, backups = _run(tmp_path, "hang", BACKUP_DUMP_TIMEOUT_S="1")
    assert time.time() - t0 < 25
    assert r.returncode != 0 and "timed out after 1s" in r.stdout
    assert not _new_dumps(backups) and not list(backups.glob("*.partial"))
    assert not (backups / ".last_success").exists() and not (backups / "remote_called").exists()
    assert (backups / "vatpac_20200101T000000Z.dump").exists()         # nothing rotated after a failure


def test_a_tiny_dump_is_rejected_and_moved_aside(tmp_path: Path) -> None:
    r, backups = _run(tmp_path, "good", old_pad=5000)             # previous dumps ~5 kB, new one 9 bytes
    assert r.returncode != 0
    assert "FAILED content check" in r.stdout and "under 50%" in r.stdout
    assert not _new_dumps(backups)
    assert len(list(backups.glob("*.dump.rejected"))) == 1        # moved aside, not deleted
    assert not (backups / ".last_success").exists() and not (backups / "remote_called").exists()
    assert len(list(backups.glob("vatpac_2020*.dump"))) == 3      # nothing rotated


def test_the_first_ever_dump_needs_the_minimum_size(tmp_path: Path) -> None:
    r, backups = _run(tmp_path, "good", old=0, BACKUP_MIN_FIRST_BYTES="1000")
    assert r.returncode != 0 and "first dump is" in r.stdout
    assert len(list(backups.glob("*.dump.rejected"))) == 1 and not _new_dumps(backups)
    r, backups = _run(tmp_path / "ok", "good", old=0, BACKUP_MIN_FIRST_BYTES="5")
    assert r.returncode == 0 and len(_new_dumps(backups)) == 1


def test_a_dump_with_no_key_table_data_is_rejected(tmp_path: Path) -> None:
    r, backups = _run(tmp_path, "nodata")
    assert r.returncode != 0
    assert "no table data for poll_log" in r.stdout
    assert not _new_dumps(backups) and len(list(backups.glob("*.dump.rejected"))) == 1
    assert not (backups / ".last_success").exists() and len(list(backups.glob("vatpac_2020*.dump"))) == 3


def test_rotation_deletes_only_the_oldest_and_never_below_the_minimum(tmp_path: Path) -> None:
    r, backups = _run(tmp_path, "good", BACKUP_MIN_KEEP="3")      # 3 old + 1 new: only one may go
    assert r.returncode == 0, r.stdout
    assert not (backups / "vatpac_20200101T000000Z.dump").exists()
    assert (backups / "vatpac_20200102T000000Z.dump").exists() and (backups / "vatpac_20200103T000000Z.dump").exists()
    assert "rotation kept" in r.stdout
    r, backups = _run(tmp_path / "high", "good", BACKUP_MIN_KEEP="10")
    assert r.returncode == 0 and len(list(backups.glob("*.dump"))) == 4, "nothing may be deleted below the minimum"


def test_a_rejected_dump_is_never_counted_as_good(tmp_path: Path) -> None:
    # 2 old good dumps + 2 rejected files + 1 new good dump = 3 good: rotation must keep all three, and the (huge)
    # rejected files must not serve as the size baseline for the new dump.
    bin_backups = tmp_path / "backups"
    bin_backups.mkdir()
    for n in ("20190101", "20190102"):
        f = bin_backups / f"vatpac_{n}T000000Z.dump.rejected"
        f.write_text("x" * 100000)
        aged = time.time() - 40 * 86400
        os.utime(f, (aged, aged))
    r, backups = _run(tmp_path, "good", old=2, BACKUP_MIN_KEEP="3")
    assert r.returncode == 0, r.stdout
    assert len(list(backups.glob("*.dump"))) == 3, "the two old good dumps must survive: only 3 good dumps exist"


def test_a_dump_of_exactly_half_the_previous_size_is_kept_and_one_byte_less_is_rejected(tmp_path: Path) -> None:
    """Issue 342, mutant W14 (`-gt` instead of `-ge`): the new dump is 9 bytes. A previous dump of 18 bytes makes the minimum
    (50 percent) exactly 9, which is enough; one of 20 bytes makes it 10, which is not."""
    r, backups = _run(tmp_path / "equal", "good", old_pad=10)
    assert r.returncode == 0 and len(_new_dumps(backups)) == 1, r.stdout + r.stderr
    r, backups = _run(tmp_path / "under", "good", old_pad=12)
    assert r.returncode != 0 and "under 50%" in r.stdout and not _new_dumps(backups)
