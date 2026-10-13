"""docs/DATA_DICTIONARY.md must describe every table, view and function the migrations create.

Unit level, no database: the migration files are parsed for CREATE TABLE / VIEW / FUNCTION and DROP statements, and the
dictionary is searched for each surviving name in backticks. The header must also name the latest migration, so a new
migration cannot land without the dictionary being looked at. Refresh steps: docs/RUNBOOKS/data-dictionary.md.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = (REPO / "docs" / "DATA_DICTIONARY.md").read_text(encoding="utf-8")
MIGRATIONS = sorted((REPO / "db" / "migrations").glob("*.sql"))

CREATE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?P<temp>TEMP(?:ORARY)?)\s+)?(?P<kind>TABLE|VIEW|FUNCTION)\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?:public\.)?(?P<name>[a-z_][a-z0-9_]*)",
    re.I | re.M,
)
DROP = re.compile(r"^\s*DROP\s+(?P<kind>TABLE|VIEW|FUNCTION)\s+(?:IF\s+EXISTS\s+)?(?:public\.)?(?P<name>[a-z_][a-z0-9_]*)", re.I | re.M)
# ALTER TABLE|VIEW old RENAME TO new (issue 89: the old merging view became flight_legacy, flight_summary became flight_summary). Only
# the RENAME TO form: RENAME CONSTRAINT and ALTER INDEX are not objects of the dictionary.
RENAME = re.compile(
    r"^\s*ALTER\s+(?P<kind>TABLE|VIEW)\s+(?:IF\s+EXISTS\s+)?(?:public\.)?(?P<name>[a-z_][a-z0-9_]*)\s+RENAME\s+TO\s+(?P<new>[a-z_][a-z0-9_]*)",
    re.I | re.M,
)


def up_section(text: str) -> str:
    """Only the `migrate:up` part: the down steps recreate or drop objects the up step already handled."""
    return text.split("-- migrate:down")[0]


def live_objects() -> dict[str, set[str]]:
    """Objects that exist after all migrations, by kind (table, view, function)."""
    objs: dict[str, set[str]] = {"table": set(), "view": set(), "function": set()}
    for path in MIGRATIONS:
        text = up_section(path.read_text(encoding="utf-8"))
        events = [(m.start(), "create", m) for m in CREATE.finditer(text) if not m.group("temp")]
        events += [(m.start(), "drop", m) for m in DROP.finditer(text)]
        events += [(m.start(), "rename", m) for m in RENAME.finditer(text)]
        for _, action, m in sorted(events, key=lambda e: e[0]):
            kind, name = m.group("kind").lower(), m.group("name").lower()
            if action == "rename":
                if name in objs[kind]:                   # a rename of an object the parser never saw created is ignored
                    objs[kind].discard(name)
                    objs[kind].add(m.group("new").lower())
                continue
            (objs[kind].add if action == "create" else objs[kind].discard)(name)
    return objs


def test_the_parser_sees_the_known_core_objects() -> None:
    objs = live_objects()
    assert {"pilot_session", "pilot_position", "flight_summary", "controller_session"} <= objs["table"]
    assert {"flight", "flight_ext", "poll_cadence", "movements"} <= objs["view"]
    # issue 89 phase 5: the pieces' view and the legacy view are gone; the table that was built as flight_record is flight_summary now
    assert not {"flight_part", "flight_legacy"} & objs["view"]
    assert "controller_activity_summary" not in objs["view"]  # dropped by migration 90
    assert {"write_finished_flights", "icao_region"} <= objs["function"]
    assert not {"refresh_flight_summary", "refresh_flight_summary_sessions"} & objs["function"]  # dropped in phase 5


def test_every_migration_object_is_in_the_dictionary() -> None:
    missing = [
        f"{kind} {name}"
        for kind, names in live_objects().items()
        for name in sorted(names)
        if f"`{name}`" not in DOC and f"`{name}(" not in DOC and f"### {name}" not in DOC
    ]
    assert missing == [], f"docs/DATA_DICTIONARY.md does not mention: {missing}"


def test_the_header_names_the_latest_migration() -> None:
    latest = MIGRATIONS[-1].name.split("_")[0]
    header = DOC.split("\n", 4)[2]
    assert f"up to {latest}" in header, f"DATA_DICTIONARY.md header must say 'up to {latest}'; it says: {header[:120]}"
    assert f"({len(MIGRATIONS)} migrations" in DOC, f"DATA_DICTIONARY.md must state {len(MIGRATIONS)} migrations"
