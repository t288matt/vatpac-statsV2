"""Issue 212: every reader of a key from an env file follows one set of rules.

ops/env_key.sh serves the scripts that run with plain `sh` (and, as text, the remote reads of ops/deploy.sh); ops/env_file.py serves the
python scripts. The same table drives both, so they cannot drift apart.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
OPS = REPO / "ops"
sys.path.insert(0, str(OPS))
import env_file  # noqa: E402  (the module under test, found through ops/ as the scripts find it)

SH = shutil.which("sh")

# (file text, key, expected value); one row per rule
CASES = [
    ("K=abc\n", "K", "abc"),
    ('K="abc"\n', "K", "abc"),
    ("K='abc'\n", "K", "abc"),
    ("export K=abc\n", "K", "abc"),
    ('export  K="abc"\n', "K", "abc"),
    ("K=abc\r\nL=1\r\n", "K", "abc"),                 # a file edited on Windows
    ("K=1\nK=2\n", "K", "2"),                         # the last assignment wins
    ("K=1\nK=\n", "K", ""),                           # ... even when it is blank
    ("K=\n", "K", ""),
    ("K=  spaced  \n", "K", "spaced"),
    ('K=" in quotes "\n', "K", " in quotes "),         # whitespace inside the quotes is the value
    ("K=a=b\n", "K", "a=b"),                          # only the first = splits
    ('K="unbalanced\n', "K", '"unbalanced'),
    ("K='mixed\"\n", "K", "'mixed\""),
    ("# K=abc\n", "K", ""),                           # a comment
    ("  K=abc\n", "K", ""),                           # an indented line
    ("KK=abc\n", "K", ""),                            # another key that ends the same way
    ("XK=abc\nK=ok\n", "K", "ok"),
    ("OTHER=1\n", "K", ""),
]


@pytest.mark.skipif(SH is None, reason="sh not available")
@pytest.mark.parametrize(("text", "key", "want"), CASES)
def test_env_key_shell_reader(tmp_path: Path, text: str, key: str, want: str) -> None:
    f = tmp_path / ".env"
    f.write_bytes(text.encode())
    p = subprocess.run([str(SH), "-c", f'. "{(OPS / "env_key.sh").as_posix()}"; env_key "{f.as_posix()}" {key}'], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    assert p.stdout.rstrip("\n") == want


@pytest.mark.parametrize(("text", "key", "want"), CASES)
def test_env_file_python_reader(tmp_path: Path, text: str, key: str, want: str) -> None:
    f = tmp_path / ".env"
    f.write_bytes(text.encode())
    assert env_file.read_env(f).get(key, "") == want


@pytest.mark.skipif(SH is None, reason="sh not available")
def test_a_missing_file_gives_nothing(tmp_path: Path) -> None:
    p = subprocess.run([str(SH), "-c", f'. "{(OPS / "env_key.sh").as_posix()}"; env_key "{(tmp_path / "none").as_posix()}" K'], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0 and p.stdout == ""
    with pytest.raises(OSError):
        env_file.read_env(tmp_path / "none")


def test_no_script_keeps_its_own_env_reader() -> None:
    """Fails if a script goes back to its own sed or split on the env file: the readers must be env_key / read_env."""
    for name in ("smoke.sh", "check_env.sh", "image_mode.sh"):
        text = (OPS / name).read_text(encoding="utf-8")
        assert 'env_key.sh"' in text and "env_key " in text, name
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        assert not re.search(r"sed -n [^|]*(\.env|ENV_FILE)", code), name
    deploy = (OPS / "deploy.sh").read_text(encoding="utf-8")
    assert "declare -f env_key" in deploy and "env_key .env" in deploy and "//p' .env" not in deploy
    for name in ("check_panels.py", "grafana_public.py"):
        text = (OPS / name).read_text(encoding="utf-8")
        assert "from env_file import read_env" in text and 'split("=", 1)' not in text, name


def test_check_env_reads_secrets_the_way_compose_does(tmp_path: Path) -> None:
    """A quoted or `export`ed secret is the same secret the stack gets, so check_env judges it (before: quotes counted as characters)."""
    secrets = {n: f"{n.lower()}Abcdef0123456789".replace("_", "") for n in ("POSTGRES_PASSWORD", "GRAFANA_RO_PASSWORD", "GRAFANA_ADMIN_PASSWORD", "WATCHDOG_DB_PASSWORD", "COLLECTOR_DB_PASSWORD", "BACKUP_DB_PASSWORD", "STATS_LOADER_DB_PASSWORD")}
    names = list(secrets)
    lines = [f'{names[0]}="{secrets[names[0]]}"', f"export {names[1]}={secrets[names[1]]}", f"{names[2]}='{secrets[names[2]]}'"]
    lines += [f"{n}={secrets[n]}\r" for n in names[3:]]
    f = tmp_path / ".env"
    f.write_bytes(("\n".join(lines) + "\n").encode())
    assert SH is not None
    p = subprocess.run([SH, str(OPS / "check_env.sh"), str(f)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "check_env: OK" in p.stdout, p.stderr
    f.write_bytes(f.read_bytes().replace(secrets[names[0]].encode(), b"short"))  # the unquoted value is judged, not the quotes
    p = subprocess.run([SH, str(OPS / "check_env.sh"), str(f)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 1 and f"{names[0]} is shorter" in p.stderr