#!/usr/bin/env python3
"""Local only: compare the tools installed on this machine with the versions .github/workflows/ci.yml pins (issue 241).

  python ops/check_tool_versions.py

A different ruff, mypy, sqlglot or actionlint can pass here and fail in CI (or the reverse). Exit 1 on a difference, with the
pip command that fixes it. PyYAML is reported but does not fail (a newer one reads the same files). The Python version is
reported, not enforced.
"""
from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

CI = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
WARN_ONLY = {"pyyaml"}


def pins(text: str) -> dict[str, str]:
    """name -> version for every `name==version` in ci.yml, with the ${RUFF_VERSION} and ${MYPY_VERSION} variables filled in."""
    env = {m.group(1): m.group(2) for m in re.finditer(r'^\s+([A-Z_]+_VERSION): "([^"]+)"', text, re.M)}
    found: dict[str, str] = {}
    for m in re.finditer(r'"?([A-Za-z0-9_.-]+)==(\$\{(\w+)\}|[0-9][^\s"]*)"?', text):
        version = env.get(m.group(3), "") if m.group(3) else m.group(2)
        if version:
            found[m.group(1).lower()] = version
    return found


def main() -> int:
    wanted = pins(CI.read_text(encoding="utf-8"))
    bad: list[str] = []
    for name, want in sorted(wanted.items()):
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            have = "not installed"
        if have == want:
            continue
        print(f"{'warning' if name in WARN_ONLY else 'DIFFERENT'}: {name} is {have}, ci.yml pins {want}")
        if name not in WARN_ONLY:
            bad.append(f"{name}=={want}")
    print(f"python {sys.version.split()[0]} (CI uses the version in setup-python)")
    if bad:
        print("fix: pip install " + " ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
