"""Fail if any commit message, or the pull request title or body, carries a Claude attribution (working agreement, section 10).

  python ops/check_no_claude_attribution.py            scans every commit reachable from HEAD, plus PR_TITLE and PR_BODY if set

The harness can add `Co-Authored-By: Claude ...` or a "Generated with Claude Code" footer on its own, and that made `claude`
appear in GitHub's contributors list (591 commits, removed on 10-11-2026). Exit 0 when clean, 1 when a match is found.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

# One line each. Case does not matter. Dependabot and human co-authors are not matched.
PATTERNS = (
    re.compile(r"^\s*co-authored-by:.*(\bclaude\b|anthropic)", re.IGNORECASE),
    re.compile(r"noreply@anthropic\.com", re.IGNORECASE),
    re.compile(r"(generated|created|made)\s+(with|by)\s+.{0,8}\bclaude\b", re.IGNORECASE),
    re.compile(r"claude\.com/claude-code", re.IGNORECASE),
)


def find_attributions(text: str) -> list[str]:
    """The lines of `text` that are a Claude attribution."""
    return [ln.strip() for ln in text.splitlines() if any(p.search(ln) for p in PATTERNS)]


def main() -> int:
    log = subprocess.run(
        ["git", "log", "--format=%x00%H%n%B", "HEAD"], capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout
    bad: list[str] = []
    for entry in log.split("\x00"):
        if not entry.strip():
            continue
        sha, _, body = entry.partition("\n")
        bad += [f"commit {sha[:7]}: {ln}" for ln in find_attributions(body)]
    for label in ("PR_TITLE", "PR_BODY"):
        bad += [f"{label}: {ln}" for ln in find_attributions(os.environ.get(label, ""))]
    if bad:
        print("Claude attribution found (working agreement section 10 forbids it):")
        print("\n".join(f"  {b}" for b in bad))
        return 1
    print("No Claude attribution in any commit message or in the pull request text.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
