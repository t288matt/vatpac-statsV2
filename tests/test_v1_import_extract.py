"""ops/v1_import/extract.sh (issue 279, ADR-035) with a fake `ssh` on PATH.

Checks what the script sends to v1: only SELECTs on flight_summaries and controller_summaries, every session read-only with a 30 s
timeout, never one of the big or forbidden tables; and what it leaves behind: frozen files with a checksum that match, and
nothing at all (no checksum file) when the connection fails part-way or when an extract already exists."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "ops" / "v1_import" / "extract.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

FAKE_SSH = """#!/bin/sh
# records the remote command (arguments) and the SQL (stdin); answers like psql --csv
echo "ARGS: $*" >> "$FAKE_LOG"
sql="$(cat)"
printf 'SQL: %s\\n' "$sql" >> "$FAKE_LOG"
[ "$FAKE_SSH" = "fail" ] && { echo "ssh: connect to host failed" >&2; exit 255; }
case "$sql" in
  *"count(*)"*) printf 'count\\n3\\n' ;;
  *"FROM flight_summaries"*)
    [ "$FAKE_SSH" = "cut" ] && { printf 'id,cid\\n1,2\\n'; echo "psql: error: canceling statement due to statement timeout" >&2; exit 1; }
    printf 'id,cid,callsign\\n1,10,QFA1\\n2,11,VOZ2\\n3,12,JST3\\n' ;;
  *"FROM controller_summaries"*) printf 'id,cid,callsign\\n1,20,ML_APP\\n2,21,SY_TWR\\n3,22,BN_APP\\n' ;;
  *) echo "unexpected query" >&2; exit 1 ;;
esac
"""


def run(tmp_path: Path, out: Path, mode: str = "ok") -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "ssh").write_text(FAKE_SSH, encoding="utf-8", newline="\n")
    (bindir / "ssh").chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir.as_posix()}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": (tmp_path / "log.txt").as_posix(),
           "FAKE_SSH": mode, "V1_SSH_HOST": "v1host"}
    return subprocess.run([str(BASH), SCRIPT.as_posix(), out.as_posix()], capture_output=True, text=True, env=env, timeout=60)


def test_it_reads_only_the_two_summary_tables_read_only_and_writes_a_matching_checksum(tmp_path: Path) -> None:
    out = tmp_path / "x"
    p = run(tmp_path, out)
    assert p.returncode == 0, p.stderr
    log = (tmp_path / "log.txt").read_text(encoding="utf-8")
    sqls = [ln[5:] for ln in log.splitlines() if ln.startswith("SQL: ")]
    assert len(sqls) == 4 and all("default_transaction_read_only = on" in s and "statement_timeout = '30s'" in s for s in sqls)
    tables = set(re.findall(r"\bFROM\s+(\w+)", log))
    assert tables == {"flight_summaries", "controller_summaries"}
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|COPY|GRANT)\b", "\n".join(sqls), re.I)
    assert all("completion_time < '2026-04-01" in s or "session_start_time < '2026-04-01" in s for s in sqls if "FROM" in s)
    assert all("v1host" in ln and "vatsim_postgres" in ln and "--csv" in ln for ln in log.splitlines() if ln.startswith("ARGS: "))
    assert (out / "EXPECTED_COUNTS").read_text(encoding="utf-8") == "flights=3\ncontrollers=3\n"
    for line in (out / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = re.fullmatch(r"([0-9a-f]{64}) [ *](.+)", line).groups()  # type: ignore[union-attr]  # text or binary marker
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    assert not list(out.glob("*.part"))
    assert "QFA1" not in p.stdout  # it prints sizes and checksums, never a row


def test_a_finished_extract_is_frozen_and_is_not_overwritten(tmp_path: Path) -> None:
    out = tmp_path / "x"
    assert run(tmp_path, out).returncode == 0
    before = (out / "v1_flights.csv").read_bytes()
    (tmp_path / "log.txt").unlink()
    p = run(tmp_path, out)
    assert p.returncode == 2 and "frozen" in p.stderr
    assert not (tmp_path / "log.txt").exists()  # v1 was not even contacted
    assert (out / "v1_flights.csv").read_bytes() == before


@pytest.mark.parametrize("mode", ["fail", "cut"])
def test_a_failed_or_cut_off_read_leaves_no_checksum_and_no_data_file(tmp_path: Path, mode: str) -> None:
    out = tmp_path / "x"
    p = run(tmp_path, out, mode)
    assert p.returncode != 0
    assert not (out / "SHA256SUMS").exists() and not (out / "v1_flights.csv").exists()
    assert not list(out.glob("*.part")) and not (out / "EXPECTED_COUNTS").exists()  # no half-written extract is left


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not meaningful on Windows")
def test_the_extract_is_readable_by_its_owner_only(tmp_path: Path) -> None:
    out = tmp_path / "x"
    assert run(tmp_path, out).returncode == 0
    assert (out.stat().st_mode & 0o777) == 0o700
    for f in out.iterdir():
        assert (f.stat().st_mode & 0o777) == 0o600, f.name
