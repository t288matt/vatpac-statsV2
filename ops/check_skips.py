#!/usr/bin/env python3
"""Fail when the unit job skipped a different number of tests than expected (issue 343).

A skipped test is a test that did not run, and a green result hides it. The expected number is kept here, in one place:
  python ops/check_skips.py JUNIT_XML [--expected N]     counts <skipped> in a pytest --junitxml file, exits 1 on a difference

EXPECTED_UNIT_SKIPS is what the CI unit job (Linux, bash and sh present) skips: the 23 watchdog auto-restart tests that are
switched off on purpose (issue 232, owner decision 11-11-2026: leave them), plus the golden-file writer in
tests/test_watchdog_alert_wording.py, which runs only when WRITE_GOLDEN=1 is set (issue 199).
Changing the number is a deliberate act that a reviewer sees in the diff.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET

EXPECTED_UNIT_SKIPS = 24


def count_skipped(xml_text: str) -> int:
    root = ET.fromstring(xml_text)
    return sum(1 for case in root.iter("testcase") if case.find("skipped") is not None)


def main(argv: list[str]) -> int:
    args = argv[1:]
    expected = EXPECTED_UNIT_SKIPS
    if len(args) == 3 and args[1] == "--expected" and args[2].isdigit():
        expected = int(args[2])
        args = args[:1]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        with open(args[0], encoding="utf-8") as fh:
            got = count_skipped(fh.read())
    except (OSError, ET.ParseError) as exc:
        print(f"check_skips: cannot read {args[0]}: {exc}", file=sys.stderr)
        return 1
    if got != expected:
        print(f"check_skips: FAIL: {got} tests were skipped, {expected} expected (ops/check_skips.py). "
              "A new skip hides a test; a lower number means a skipped test was switched back on: update the number on purpose.")
        return 1
    print(f"check_skips: ok, {got} skipped as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
