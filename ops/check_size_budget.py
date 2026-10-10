#!/usr/bin/env python3
"""Size budget gate (SPEC sections 7 and 11): collector package under 1800 lines, no module over 300.

Counts physical lines of every .py file under collector/src/vatpac (blank and comment lines
included, so the budget cannot be met by stripping whitespace). Exit 1 on breach.
Usage: python ops/check_size_budget.py [package_dir]
"""
from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_LIMIT = 1800  # ADR-014, 022, 027: raised 900 -> 1300 -> 1400 -> 1700 -> 1800 as required behaviour landed (1698 on 10-10-2026)
MODULE_LIMIT = 300


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path("collector/src/vatpac")
    files = sorted(root.rglob("*.py"))
    if not files:
        print(f"size budget: no Python files under {root}", file=sys.stderr)
        return 1
    counts = {f: len(f.read_text(encoding="utf-8").splitlines()) for f in files}
    total = sum(counts.values())
    failed = False
    for f, n in counts.items():
        flag = ""
        if n > MODULE_LIMIT:
            flag, failed = f"  OVER {MODULE_LIMIT}", True
        print(f"{n:5d}  {f.as_posix()}{flag}")
    print(f"{total:5d}  TOTAL (limit {PACKAGE_LIMIT})")
    if total >= PACKAGE_LIMIT:
        print(f"size budget: package is {total} lines, limit is under {PACKAGE_LIMIT}", file=sys.stderr)
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
