#!/usr/bin/env python3
"""Static checks for the provisioned Grafana dashboards (standard library only).

Usage: python grafana/validate_dashboards.py

Checks every JSON file under grafana/dashboards: it parses, panels (other than text panels) use the
`vatpac` datasource and have rawSql, SQL reads only allow-listed views and lookups (no base tables),
no hard-coded dates, no DISTINCT ON, panel ids and dashboard uids are unique. If `sqlglot` is
installed (optional) every query is also parsed with the Postgres dialect after Grafana macros are
substituted, and db/migrations/*.sql are parsed too (informational: PostgreSQL is the authority).

NOTHING IS EXECUTED AGAINST POSTGRES. This checks shape and syntax only.
Exit status 0 when there are no errors, 1 otherwise.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DASHBOARDS = HERE / "dashboards"

# Objects a panel may read, derived by scanning the SQL files: every view created in db/migrations
# (the single source of truth: there is no separate grafana/views.sql), plus the lookup tables (SPEC section 6) that db/migrations creates.
# Base tables (pilot_session, poll_log ...) are deliberately NOT allowed.
ROOT = HERE.parent
LOOKUPS = {"controller_rating", "facility", "pilot_rating", "airline", "aircraft_type", "region_map"}
VIEW_RE = re.compile(r"(?i)create\s+(?:or\s+replace\s+)?(?:materialized\s+)?view\s+(?:if\s+not\s+exists\s+)?(\w+)")
TABLE_RE = re.compile(r"(?i)create\s+table\s+(?:if\s+not\s+exists\s+)?(\w+)")
MIGRATION_FILES = sorted((ROOT / "db" / "migrations").glob("*.sql"))
MIGRATION_TEXT = "\n".join(f.read_text(encoding="utf-8") for f in MIGRATION_FILES)
MIGRATION_VIEWS = set(VIEW_RE.findall(MIGRATION_TEXT))
ALLOWED = MIGRATION_VIEWS | (LOOKUPS & set(TABLE_RE.findall(MIGRATION_TEXT)))
SKIP_RELATIONS = {"generate_series", "unnest", "lateral"}  # functions / keywords after FROM or JOIN
# Views that appear in the migration history but no longer exist, and that no panel may read (issue 89 phase 3 refused them while they
# were kept for comparison; phase 5 dropped them). The migrations still create them, so they are in ALLOWED unless named here.
# Base tables are refused already (they are not in ALLOWED); the message below is the specific one.
NEVER_FOR_PANELS = {
    "flight_legacy": "the old merging view, dropped in issue 89 phase 5; read flight instead",
    "flight_part": "the per-session pieces view, dropped in issue 89 phase 5; read flight instead",
}

MACROS = [
    (re.compile(r"\$__timeFrom\(\)"), "'2026-01-01T00:00:00Z'"),
    (re.compile(r"\$__timeTo\(\)"), "'2026-02-01T00:00:00Z'"),
    (re.compile(r"\$__timeFilter\(\s*([^)]+?)\s*\)"), r"\1 BETWEEN '2026-01-01T00:00:00Z' AND '2026-02-01T00:00:00Z'"),
    (re.compile(r"\$__timeGroupAlias\(\s*([^,]+?)\s*,[^)]*\)"), r"date_trunc('minute', \1) AS time"),
    (re.compile(r"\$\{[A-Za-z_]+:sqlstring\}"), "'X'"),
]


def substitute_macros(sql: str) -> str:
    for pattern, repl in MACROS:
        sql = pattern.sub(repl, sql)
    return sql


def strip_noise(sql: str) -> str:
    """Remove comments and string literals so relation names can be found by regex."""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r'"[^"]*"', '""', sql)
    return sql


def relations(sql: str) -> set[str]:
    clean = strip_noise(substitute_macros(sql))
    # EXTRACT(field FROM expr) is not a relation reference
    clean = re.sub(r"(?i)\bextract\s*\(\s*\w+\s+from\b", "extract(x,", clean)
    ctes = {m.group(1).lower() for m in re.finditer(r"(?i)(?:\bwith|,)\s*([a-z_]\w*)\s*(?:\([^)]*\))?\s+as\s*\(", clean)}
    found = set()
    for m in re.finditer(r"(?i)\b(?:from|join)\s+([a-z_][\w.]*)(\s*\()?", clean):
        name, is_call = m.group(1).lower(), m.group(2)
        if is_call or name in SKIP_RELATIONS or name in ctes:
            continue
        found.add(name)
    # comma-joined relations after FROM are not used in these dashboards; flag the common shape
    return found


def sql_items(dash: dict):
    """Yield (label, sql, is_panel) for every query in a dashboard."""
    for var in dash.get("templating", {}).get("list", []):
        if var.get("type") == "query":
            yield f"variable {var.get('name')}", var.get("query", ""), False
    for p in dash.get("panels", []):
        if p.get("type") == "text":
            continue
        for t in p.get("targets", []):
            yield f"panel {p.get('id')} '{p.get('title')}'", t.get("rawSql", ""), True


def main() -> int:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        import sqlglot  # type: ignore
    except ImportError:
        sqlglot = None
        warnings.append("sqlglot not installed: SQL syntax was NOT parsed (only structural checks ran)")

    files = sorted(DASHBOARDS.rglob("*.json"))
    if not files:
        errors.append(f"no dashboard files under {DASHBOARDS}")
    uids: dict[str, str] = {}
    n_panels = n_text = n_queries = n_parsed = 0
    used: set[str] = set()
    folders: dict[str, int] = {}

    for path in files:
        rel = path.relative_to(HERE).as_posix()
        try:
            dash = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"{rel}: does not parse: {exc}")
            continue
        folders[path.parent.name] = folders.get(path.parent.name, 0) + 1
        uid = dash.get("uid")
        if not uid:
            errors.append(f"{rel}: missing uid")
        elif uid in uids:
            errors.append(f"{rel}: uid {uid} also used by {uids[uid]}")
        else:
            uids[uid] = rel
        if not dash.get("title"):
            errors.append(f"{rel}: missing title")
        if dash.get("timezone") != "utc":
            errors.append(f"{rel}: dashboard timezone must be 'utc'")
        if not any(p.get("type") == "text" and "${go_live}" in p.get("options", {}).get("content", "")
                   for p in dash.get("panels", [])):
            errors.append(f"{rel}: missing the 'Live data since ${{go_live}}' banner text panel")

        ids: set[int] = set()
        for p in dash.get("panels", []):
            pid = p.get("id")
            if pid in ids:
                errors.append(f"{rel}: duplicate panel id {pid}")
            ids.add(pid)
            if p.get("type") == "text":
                n_text += 1
                continue
            n_panels += 1
            label = f"{rel}: panel {pid} '{p.get('title')}'"
            if (p.get("datasource") or {}).get("uid") != "vatpac":
                errors.append(f"{label}: datasource is not vatpac")
            targets = p.get("targets") or []
            if not targets:
                errors.append(f"{label}: no targets")
            for t in targets:
                if (t.get("datasource") or {}).get("uid") != "vatpac":
                    errors.append(f"{label}: target datasource is not vatpac")
                if not str(t.get("rawSql", "")).strip():
                    errors.append(f"{label}: empty rawSql")
            if not p.get("description"):
                warnings.append(f"{label}: no description")

        for label, sql, is_panel in sql_items(dash):
            n_queries += 1
            full = f"{rel}: {label}"
            for rel_name in relations(sql):
                used.add(rel_name)
                if rel_name in NEVER_FOR_PANELS:
                    errors.append(f"{full}: reads '{rel_name}', which no panel may read ({NEVER_FOR_PANELS[rel_name]})")
                elif rel_name not in ALLOWED:
                    errors.append(f"{full}: reads '{rel_name}', which is not an allow-listed view or lookup")
            noise = strip_noise(sql)
            if re.search(r"(?i)\bdistinct\s+on\b", noise):
                errors.append(f"{full}: uses DISTINCT ON")
            if re.search(r"'\d{4}-\d{2}-\d{2}", re.sub(r"--[^\n]*", " ", sql)):
                errors.append(f"{full}: hard-coded date literal")
            if is_panel and re.search(r"(?i)\bnow\(\)", noise) is None and "$__time" not in sql \
                    and "network_history" not in noise and "monitor_freshness" not in noise \
                    and "controller_online_now" not in noise and "pilot_online_now" not in noise \
                    and "pilot_latest" not in noise \
                    and "data_freshness" not in noise and "poll_history" not in noise:
                warnings.append(f"{full}: no time range, now() or live view")
            if sqlglot is not None:
                try:
                    sqlglot.parse_one(substitute_macros(sql), read="postgres")
                    n_parsed += 1
                except Exception as exc:  # sqlglot raises ParseError and TokenError
                    errors.append(f"{full}: SQL does not parse: {str(exc).splitlines()[0]}")

    # db/migrations/*.sql: lookups exist, and (informational) every file parses
    for missing in sorted(set(LOOKUPS) - ALLOWED):
        errors.append(f"lookup table {missing} is not created by db/migrations")
    if not MIGRATION_FILES:
        errors.append("db/migrations has no .sql files")
    if sqlglot is not None:
        for f in MIGRATION_FILES:
            body = re.sub(r"(?is)\$(\w*)\$.*?\$\1\$", "'body'", f.read_text(encoding="utf-8"))  # dollar-quoted bodies
            try:
                n_parsed += len([st for st in sqlglot.parse(body, read="postgres") if st is not None])
            except Exception as exc:
                warnings.append(f"{f.name}: sqlglot could not parse it ({str(exc).splitlines()[0]}); PostgreSQL decides")

    print(f"dashboards: {len(files)}  (folders: {', '.join(f'{k}={v}' for k, v in sorted(folders.items()))})")
    print(f"panels with queries: {n_panels}   text panels: {n_text}   queries checked: {n_queries}")
    print(f"sql statements parsed with sqlglot (postgres): {n_parsed}" if sqlglot else "sql parsing: skipped")
    print(f"relations read by panels: {', '.join(sorted(used))}")
    for w in warnings:
        print("WARNING:", w)
    for e in errors:
        print("ERROR:", e)
    print("RESULT:", "FAIL" if errors else "OK", f"({len(errors)} errors, {len(warnings)} warnings)")
    print("Note: nothing was executed against PostgreSQL.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
