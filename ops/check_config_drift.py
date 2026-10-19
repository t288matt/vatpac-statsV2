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
  - policy keys (POLICY_KEYS: retention periods and watchdog thresholds) are literals in docker-compose.yml, are absent from
    .env.example, and equal the default in the code that reads them (settings.py, watchdog.py, ops/backup/*.sh)

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

# Policy values: decided in an ADR, so they live in docker-compose.yml as literals (reviewed in git), never in .env.
# Add a key here when it moves; see docs/CONFIG_MAP.md, section "Where each setting lives".
POLICY_KEYS: dict[str, str] = {
    "POSITION_RETENTION_DAYS": "collector",
    "ARCHIVE_COMPACT_KEEP_DAYS": "collector",
    "ARCHIVE_FULL_KEEP_DAYS": "collector",
    "ARCHIVE_ANOMALY_KEEP_DAYS": "collector",
    "ARCHIVE_MAX_MB": "collector",
    "BACKUP_RETENTION_DAYS": "backup",
    "BACKUP_MIN_KEEP": "backup",
    "WATCHDOG_INTERVAL_SECONDS": "watchdog",
    "WATCHDOG_WARN_SECONDS": "watchdog",
    "WATCHDOG_PAGE_SECONDS": "watchdog",
    "WATCHDOG_CONFIRM_CYCLES": "watchdog",
    "WATCHDOG_DISK_PCT": "watchdog",
    "WATCHDOG_DISK_PATHS": "watchdog",
    "WATCHDOG_DB_MAX_GB": "watchdog",
    "WATCHDOG_BACKUP_MAX_HOURS": "watchdog",
    "WATCHDOG_RESTORE_TEST_MAX_DAYS": "watchdog",
    "WATCHDOG_INTEGRITY_INTERVAL_HOURS": "watchdog",
    "WATCHDOG_CROSSCHECK_INTERVAL_DAYS": "watchdog",
    "WATCHDOG_CROSSCHECK_SAMPLES": "watchdog",
}

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


def service_env(path: Path, service: str) -> dict[str, str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    env = data["services"][service]["environment"]
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


def watchdog_defaults(path: Path) -> dict[str, object]:
    """Defaults passed to env()/env_num() for a literal name in watchdog.py."""
    out: dict[str, object] = {}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("env", "env_num")
                and len(node.args) >= 2 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[1], ast.Constant)):
            out[str(node.args[0].value)] = node.args[1].value
    return out


def backup_defaults(directory: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for script in sorted(directory.glob("*.sh")):
        for name, value in re.findall(r"\$\{(BACKUP_[A-Z0-9_]+):-([^}]*)\}", script.read_text(encoding="utf-8")):
            out.setdefault(name, value)
    return out


def check_policy(errors: list[str], example: set[str], fields: dict[str, object]) -> None:
    code = {
        "collector": {n.upper(): d for n, d in fields.items()},
        "watchdog": watchdog_defaults(ROOT / "ops/watchdog/watchdog.py"),
        "backup": backup_defaults(ROOT / "ops/backup"),
    }
    for key, service in POLICY_KEYS.items():
        if key in example:
            errors.append(f"{key}: policy value must not be in .env.example (it is a literal in docker-compose.yml)")
        value = service_env(ROOT / "docker-compose.yml", service).get(key)
        if value is None:
            errors.append(f"{key}: not set in the {service} environment in docker-compose.yml")
            continue
        if "${" in value:
            errors.append(f"{key}: must be a literal in docker-compose.yml, not '{value}'")
            continue
        default = code[service].get(key)
        if default is None or default is NO_DEFAULT:
            errors.append(f"{key}: no literal default found in the {service} code to compare with")
        elif not same_default(value, default):
            errors.append(f"{key}: compose value '{value}' differs from the {service} code default '{default}'")


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

    check_policy(errors, example, fields)

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
