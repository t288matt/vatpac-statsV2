#!/usr/bin/env python3
"""Size budget gate (SPEC sections 7 and 11): collector package under 1800 lines, no module over 300.

Counts physical lines of every .py file under collector/src/vatpac (blank and comment lines
included, so the budget cannot be met by stripping whitespace). Exit 1 on breach.
The ATC stats loader (issue 387) has its own budget: python ops/check_size_budget.py stats_loader/src/vatsim_stats 700
(594 lines when set, plus about 15 percent; the module limit is the same 300).
Usage: python ops/check_size_budget.py [package_dir [package_limit]]   (no arguments: the collector and its limit)
"""
from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_LIMIT = 1800  # ADR-014, 022, 027: raised 900 -> 1300 -> 1400 -> 1700 -> 1800 as required behaviour landed (1698 on 10-10-2026)
MODULE_LIMIT = 300


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path("collector/src/vatpac")
    limit = int(argv[2]) if len(argv) > 2 else PACKAGE_LIMIT
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
    print(f"{total:5d}  TOTAL (limit {limit})")
    if total >= limit:
        print(f"size budget: package is {total} lines, limit is under {limit}", file=sys.stderr)
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
