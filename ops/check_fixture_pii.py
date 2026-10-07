#!/usr/bin/env python3
"""PII gate for test fixtures (SPEC section 11). Fails if tests/fixtures contains:
  - an email address,
  - a phone-like number,
  - an IPv4 address (outside the feed's `servers` list),
  - any `cid` outside 9000000-9999999,
  - any `name` that is not `Pilot NNNN` / `Controller NNNN` (an optional trailing 4-letter
    ICAO token is allowed, e.g. `Pilot 0042 YSSY`, because `home_icao` is parsed from it).
Allowlist-free by design. Subtrees under feed lookup keys (servers, facilities, ratings, ...)
hold no personal data and are skipped for the cid/name/IP rules (not for email/phone).
Usage: python ops/check_fixture_pii.py [fixtures_dir]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
ISO_TS = re.compile(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?")
PHONE = re.compile(
    r"(?<![\w.])(?:\+\d{1,3}[ .-]?)?(?:\(?\d{2,4}\)?[ .-]){2,}\d{2,4}(?![\w.])"  # 0412 345 678, +61 2 9999 9999
    r"|(?<![\w.])\+\d{8,15}(?![\w.])"  # +61412345678
    r"|(?<![\w.])\d{9,}(?![\w.])"  # long bare digit run
)
# the feed's own compact timestamp (general.update, e.g. 20261007161119): a date, not a phone number
COMPACT_TS = re.compile(r"(?<!\d)20\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])([01]\d|2[0-3])[0-5]\d[0-5]\d(?!\d)")
NAME_OK = re.compile(r"^(Pilot|Controller) \d{4}( [A-Z]{4})?$")
SKIP_KEYS = {"servers", "facilities", "ratings", "pilot_ratings", "military_ratings"}
CID_MIN, CID_MAX = 9_000_000, 9_999_999


def text_problems(s: str, where: str, skip_ip: bool) -> list[str]:
    out: list[str] = []
    if EMAIL.search(s):
        out.append(f"{where}: email address")
    scrubbed = COMPACT_TS.sub("", ISO_TS.sub("", s))
    if PHONE.search(scrubbed):
        out.append(f"{where}: phone-like number in {s[:40]!r}")
    if not skip_ip and IPV4.search(s):
        out.append(f"{where}: IPv4 address")
    return out


def walk(node: Any, where: str, skipped: bool) -> list[str]:
    out: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            here = f"{where}.{k}"
            sub_skipped = skipped or k in SKIP_KEYS
            if k == "cid" and not skipped:
                try:
                    ok = CID_MIN <= int(v) <= CID_MAX
                except (TypeError, ValueError):
                    ok = False
                if not ok:
                    out.append(f"{here}: cid {v!r} outside {CID_MIN}-{CID_MAX}")
            elif k == "name" and not skipped and not (isinstance(v, str) and NAME_OK.match(v)):
                out.append(f"{here}: name {v!r} is not 'Pilot NNNN' / 'Controller NNNN'")
            out.extend(walk(v, here, sub_skipped))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(walk(v, f"{where}[{i}]", skipped))
    elif isinstance(node, str):
        out.extend(text_problems(node, where, skipped))
    return out


def check_file(path: Path) -> list[str]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix == ".json" or path.suffix in (".jsonl", ".ndjson"):
        try:
            docs = [json.loads(line) for line in raw.splitlines() if line.strip()] if path.suffix != ".json" else [json.loads(raw)]
        except ValueError as exc:
            return [f"{path}: invalid JSON ({exc})"]
        found = [p for i, d in enumerate(docs) for p in walk(d, f"{path.name}#{i}", False)]
        # emails hidden in keys or odd places are still caught by a raw scan
        if EMAIL.search(raw) and not any("email" in p for p in found):
            found.append(f"{path.name}: email address")
        return found
    return text_problems(raw, path.name, False)


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path("tests/fixtures")
    if not root.is_dir():
        print(f"pii check: {root} does not exist; nothing to scan")
        return 0
    problems: list[str] = []
    files = [p for p in sorted(root.rglob("*")) if p.is_file()]
    for f in files:
        problems.extend(check_file(f))
    for p in problems:
        print(f"PII: {p}", file=sys.stderr)
    print(f"pii check: scanned {len(files)} file(s), {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
