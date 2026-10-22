"""One reader of an env file for the python ops scripts (issue 212). The rules are those of ops/env_key.sh, so every reader agrees:
a line is NAME=value in column 0 (optionally after `export `); comments and indented lines are ignored; the last assignment wins;
the whitespace around the value and a trailing CR are dropped; one pair of matching surrounding quotes is dropped. Standard library only.
"""
from __future__ import annotations

import re
from pathlib import Path

_LINE = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)")


def parse_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def read_env(path: Path) -> dict[str, str]:
    """Every NAME=value of the file; raises OSError when the file cannot be read."""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _LINE.fullmatch(line)
        if m:
            out[m.group(1)] = parse_value(m.group(2))
    return out
