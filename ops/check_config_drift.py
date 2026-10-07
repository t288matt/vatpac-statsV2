#!/usr/bin/env python3
"""Fail when collector settings cannot be set through compose/.env, or the three files disagree.

Compares (names are case-insensitive env names):
  1. fields of the Settings class in collector/src/vatpac/settings.py   (parsed with ast)
  2. keys of the collector `environment:` block in docker-compose.yml
  3. keys in .env.example

Checks:
  - every settings field is a collector environment key, unless listed in IGNORE_SETTINGS below
  - every ${VAR} a collector environment value reads is a key in .env.example (or in ENV_ONLY_OK)
  - where compose uses ${VAR:-default} and the settings default is a literal, the two defaults agree
  - every collector environment key is a settings field (typos/dead keys), unless in COMPOSE_ONLY_OK

Usage: python ops/check_config_drift.py        (needs PyYAML)
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import yaml

# Settings that are intentionally NOT passed through compose, with the reason.
IGNORE_SETTINGS: dict[str, str] = {
    "postgres_host": "superseded by DATABASE_URL, which compose always sets",
    "postgres_port": "superseded by DATABASE_URL",
    "postgres_db": "superseded by DATABASE_URL (database name comes from POSTGRES_DB)",
    "postgres_user": "superseded by DATABASE_URL (the collector uses the least-privilege role)",
    "postgres_password": "superseded by DATABASE_URL (COLLECTOR_DB_PASSWORD)",
    "polygon_path": "fixed path; the config/ directory is mounted read-only at /app/config",
    "whitelist_path": "fixed path; the config/ directory is mounted read-only at /app/config",
}
# Collector environment keys that are not Settings fields.
COMPOSE_ONLY_OK: set[str] = set()
# Variables compose reads that need not appear in .env.example.
ENV_ONLY_OK: set[str] = set()

ROOT = Path(__file__).resolve().parent.parent
VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::?[-?][^}]*)?\}")
DEFAULT_RE = re.compile(r"\$\{([A-Z0-9_]+):-([^}]*)\}")


def settings_fields(path: Path) -> dict[str, object]:
    """Return {field_name: literal default or the sentinel NO_DEFAULT} for class Settings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    out[item.target.id] = literal_default(item.value)
    return out


NO_DEFAULT = object()


def literal_default(value: ast.expr | None) -> object:
    if isinstance(value, ast.Call) and value.args:  # Field(15.0, ge=5, ...)
        value = value.args[0]
    if isinstance(value, ast.Constant):
        return value.value
    return NO_DEFAULT


def collector_env(path: Path) -> dict[str, str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    env = data["services"]["collector"]["environment"]
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


def env_example_keys(path: Path) -> set[str]:
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if m:
            keys.add(m.group(1))
    return keys


def same_default(compose_default: str, settings_default: object) -> bool:
    if isinstance(settings_default, bool):
        return compose_default.lower() == str(settings_default).lower()
    if isinstance(settings_default, (int, float)):
        try:
            return float(compose_default) == float(settings_default)
        except ValueError:
            return False
    return compose_default == str(settings_default)


def main() -> int:
    fields = settings_fields(ROOT / "collector/src/vatpac/settings.py")
    env = collector_env(ROOT / "docker-compose.yml")
    example = env_example_keys(ROOT / ".env.example")
    env_upper = {k.upper(): v for k, v in env.items()}
    errors: list[str] = []

    for name, default in fields.items():
        key = name.upper()
        if key in env_upper:
            m = DEFAULT_RE.search(env_upper[key])
            if m and default is not NO_DEFAULT and default is not None and not same_default(m.group(2), default):
                errors.append(f"{key}: compose default '{m.group(2)}' differs from settings default '{default}'")
        elif name not in IGNORE_SETTINGS:
            errors.append(f"{key}: settings field cannot be set via compose (add it to the collector environment or IGNORE_SETTINGS)")

    for key, value in env_upper.items():
        if key.lower() not in fields and key not in COMPOSE_ONLY_OK:
            errors.append(f"{key}: collector environment key is not a settings field")
        for var in VAR_RE.findall(value):
            if var not in example and var not in ENV_ONLY_OK:
                errors.append(f"{key}: reads ${{{var}}} which is missing from .env.example")

    for name in IGNORE_SETTINGS:
        if name not in fields:
            errors.append(f"IGNORE_SETTINGS lists '{name}', which is not a settings field any more")

    if errors:
        print("config drift detected:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"config drift check OK ({len(fields)} settings, {len(env)} collector env keys, {len(example)} .env.example keys)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
