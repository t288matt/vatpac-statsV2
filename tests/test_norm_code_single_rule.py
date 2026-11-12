"""Issue #207: the ICAO text rule is written once, as the database function norm_code().

Fails when a file outside the allowed few spells the rule out again (upper(btrim(...)), NULLIF(upper(...)), upper(trim(...))), so a new
copy cannot creep back in. Also checks that a migration building an index CONCURRENTLY runs outside a transaction with one statement.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COPY = re.compile(r"upper\s*\(\s*(?:b|l|r)?trim\s*\(|NULLIF\s*\(\s*upper\s*\(", re.IGNORECASE)

# Migrations up to the one that introduces norm_code are history and are never edited. The migration that switches everything over
# (20261112000150) names the old text on purpose, to find and replace it. Every later migration must call norm_code().
LAST_HISTORIC_MIGRATION = "20261112000100"
SWITCH_MIGRATION = "20261112000150"
# Files that hold the old rule on purpose: tests that keep the old text as the thing to compare against.
ALLOWED = {
    "tests/integration/test_norm_code.py",                          # the old military panel text, to show the same rows
    "tests/integration/test_pilot_latest_one_row_per_flight.py",    # the old pilot_latest definition, to compare one row per flight
    "tests/test_norm_code_single_rule.py",
}
SCANNED = ("ops", "grafana/dashboards", "collector/src", "tests/integration", "tests/scale", "tests/tools", "docs")
SUFFIXES = {".sql", ".sh", ".py", ".json", ".md"}


def _files() -> list[Path]:
    out: list[Path] = []
    for top in SCANNED:
        out += [p for p in (REPO / top).rglob("*") if p.is_file() and p.suffix in SUFFIXES and ".venv" not in p.parts]
    return out


def test_no_file_outside_the_allowed_few_spells_the_rule_out() -> None:
    bad = []
    for p in _files():
        rel = p.relative_to(REPO).as_posix()
        if rel in ALLOWED or rel.startswith("docs/reference/"):       # docs/reference holds archived v1 text
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if COPY.search(text):
            bad.append(rel)
    assert bad == [], f"use norm_code() instead of spelling the rule out: {bad}"


def test_migrations_after_the_function_call_it_instead_of_spelling_the_rule_out() -> None:
    bad = []
    for p in sorted((REPO / "db" / "migrations").glob("*.sql")):
        number = p.name.split("_", 1)[0]
        if number > LAST_HISTORIC_MIGRATION and number != SWITCH_MIGRATION and COPY.search(p.read_text(encoding="utf-8")):
            bad.append(p.name)
    assert bad == []


def test_concurrent_index_migrations_run_outside_a_transaction_with_one_statement() -> None:
    for p in sorted((REPO / "db" / "migrations").glob("*.sql")):
        text = p.read_text(encoding="utf-8")
        up = text.split("-- migrate:down")[0]
        code = "\n".join(line for line in up.splitlines() if not line.lstrip().startswith("--"))
        if re.search(r"\b(?:CREATE|DROP)\s+(?:UNIQUE\s+)?INDEX\s+CONCURRENTLY\b", code, re.IGNORECASE):
            assert text.startswith("-- migrate:up transaction:false"), p.name
            assert code.count(";") == 1, f"{p.name}: dbmate sends one file as one command; one CONCURRENTLY statement per file"
