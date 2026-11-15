#!/usr/bin/env python3
"""Fail when the collector's or the ATC stats loader's settings cannot be set through compose/.env, or the files disagree.

Compares (names are case-insensitive env names):
  1. fields of the Settings class in collector/src/vatpac/settings.py and in stats_loader/src/vatsim_stats/settings.py (parsed
     with ast; a loader field `page_size` is the variable STATS_PAGE_SIZE, and `database_url` is DATABASE_URL)
  2. keys of the `environment:` block of the collector and of the vatsim-atc-stats service in docker-compose.yml
  3. keys in .env.example

Checks (for each of the two services):
  - every settings field is an environment key of its service, unless listed in IGNORE_SETTINGS below
  - every ${VAR} an environment value reads is a key in .env.example
  - where compose uses ${VAR:-default} and the settings default is a literal, the two defaults agree
  - every environment key is a settings field (typos/dead keys)
  - policy keys (POLICY_KEYS: retention periods and watchdog thresholds) are literals in docker-compose.yml, are absent from
    .env.example, and equal EVERY default the code gives them (settings.py; watchdog.py and integrity.py; all of ops/backup/*.sh).
    The loader's code gives its settings no defaults on purpose (compose is the one place they are written), so
    STATS_RETENTION_YEARS is checked for being a literal and absent from .env.example only

Usage: python ops/check_config_drift.py        (needs PyYAML)
       python ops/check_config_drift.py --policy-keys   (print the policy key names, one per line; no PyYAML needed)
"""
from __future__ import annotations

import ast
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Settings that are intentionally NOT passed through compose, with the reason.
IGNORE_SETTINGS: dict[str, str] = {
    "polygon_path": "fixed path; the config/ directory is mounted read-only at /app/config",
    "whitelist_path": "fixed path; the config/ directory is mounted read-only at /app/config",
}

LOADER = "vatsim-atc-stats"  # the compose service of the ATC stats loader (stats_loader/src/vatsim_stats)
LOADER_PREFIX = "STATS_"  # a loader settings field `page_size` is the variable STATS_PAGE_SIZE
LOADER_UNPREFIXED = ("database_url",)  # the one loader field that is not a STATS_ variable
NO_CODE_DEFAULTS = (LOADER,)  # services whose code deliberately gives no setting a default

# Policy values: decided in an ADR, so they live in docker-compose.yml as literals (reviewed in git), never in .env.
# Add a key here when it moves; see docs/CONFIG_MAP.md, section "Where each setting lives".
POLICY_KEYS: dict[str, str] = {
    "POSITION_RETENTION_DAYS": "collector",
    "ARCHIVE_COMPACT_KEEP_DAYS": "collector",
    "ARCHIVE_FULL_KEEP_DAYS": "collector",
    "ARCHIVE_ANOMALY_KEEP_DAYS": "collector",
    "ARCHIVE_MAX_MB": "collector",
    "STATS_RETENTION_YEARS": LOADER,
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


def load_compose(path: Path) -> dict[str, Any]:
    import yaml  # imported here so `--policy-keys` works without PyYAML (ops/check_env.sh runs it on the VM)

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def service_env(compose: dict[str, Any], service: str) -> dict[str, str]:
    env = compose["services"][service]["environment"]
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env)
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


def env_example_keys(path: Path) -> set[str]:
    keys = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", line)
        if m:
            keys.add(m.group(1))
    return keys


def _number(node: ast.expr) -> float | None:
    """A numeric literal or a product of literals (72 * 3600.0), else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left, right = _number(node.left), _number(node.right)
        return None if left is None or right is None else left * right
    return None


def watchdog_defaults(directory: Path) -> dict[str, list[object]]:
    """Every default the watchdog code gives a key: env()/env_num() calls with literal arguments in watchdog.py, plus the
    Plan dataclass defaults in integrity.py (seconds converted back to the hours or days the setting is written in)."""
    out: dict[str, list[object]] = {}
    for node in ast.walk(ast.parse((directory / "watchdog.py").read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("env", "env_num")
                and len(node.args) >= 2 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[1], ast.Constant)):
            out.setdefault(str(node.args[0].value), []).append(node.args[1].value)
    plan_keys = {"invariants_interval_s": ("WATCHDOG_INTEGRITY_INTERVAL_HOURS", 3600.0),
                 "crosscheck_interval_s": ("WATCHDOG_CROSSCHECK_INTERVAL_DAYS", 86400.0),
                 "samples": ("WATCHDOG_CROSSCHECK_SAMPLES", 1.0)}
    for node in ast.walk(ast.parse((directory / "integrity.py").read_text(encoding="utf-8"))):
        if isinstance(node, ast.ClassDef) and node.name == "Plan":
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.target.id in plan_keys and item.value:
                    key, unit = plan_keys[item.target.id]
                    seconds = _number(item.value)
                    if seconds is not None:
                        out.setdefault(key, []).append(seconds / unit)
    return out


def backup_defaults(directory: Path) -> dict[str, list[object]]:
    """Every ${BACKUP_X:-n}, ${BACKUP_X-n} or ${BACKUP_X:=n} default in the backup scripts, from all scripts."""
    out: dict[str, list[object]] = {}
    for script in sorted(directory.glob("*.sh")):
        for name, value in re.findall(r"\$\{(BACKUP_[A-Z0-9_]+):?[-=]([^}]*)\}", script.read_text(encoding="utf-8")):
            out.setdefault(name, []).append(value)
    return out


def check_interpolation(errors: list[str], compose_files: list[Path]) -> None:
    """No compose file may read a policy value from .env, in any service or field (${KEY}, ${KEY:-n} or $KEY)."""
    for path in compose_files:
        text = path.read_text(encoding="utf-8")
        for key in POLICY_KEYS:
            if re.search(rf"\$\{{?{key}\b", text):
                errors.append(f"{key}: {path.name} reads it from .env with $ interpolation; policy values must be literals")


def loader_key(field: str) -> str:
    return field.upper() if field in LOADER_UNPREFIXED else LOADER_PREFIX + field.upper()


def check_policy(errors: list[str], example: set[str], fields: dict[str, object], compose: dict[str, Any]) -> None:
    code: dict[str, dict[str, list[object]]] = {
        "collector": {n.upper(): [d] for n, d in fields.items() if d is not NO_DEFAULT},
        LOADER: {},
        "watchdog": watchdog_defaults(ROOT / "ops/watchdog"),
        "backup": backup_defaults(ROOT / "ops/backup"),
    }
    env_by_service = {service: service_env(compose, service) for service in set(POLICY_KEYS.values())}
    for key, service in POLICY_KEYS.items():
        if key in example:
            errors.append(f"{key}: policy value must not be in .env.example (it is a literal in docker-compose.yml)")
        value = env_by_service[service].get(key)
        if value is None:
            errors.append(f"{key}: not set in the {service} environment in docker-compose.yml")
            continue
        if "${" in value:
            errors.append(f"{key}: must be a literal in docker-compose.yml, not '{value}'")
            continue
        defaults = code[service].get(key, [])
        if not defaults and service not in NO_CODE_DEFAULTS:
            errors.append(f"{key}: no literal default found in the {service} code to compare with")
        for default in defaults:
            if default is not None and not same_default(value, default):
                errors.append(f"{key}: compose value '{value}' differs from a {service} code default '{default}'")


def same_default(compose_default: str, settings_default: object) -> bool:
    if isinstance(settings_default, bool):
        return compose_default.lower() == str(settings_default).lower()
    if isinstance(settings_default, (int, float)):
        try:
            return float(compose_default) == float(settings_default)
        except ValueError:
            return False
    return compose_default == str(settings_default)


def check_service(errors: list[str], compose: dict[str, Any], example: set[str], service: str, fields: dict[str, object],
                  key_of: Callable[[str], str], ignore: dict[str, str]) -> int:
    """Settings fields against one service's environment block. Returns the number of environment keys."""
    env = {k.upper(): v for k, v in service_env(compose, service).items()}
    keys = {key_of(name): name for name in fields}
    for key, name in keys.items():
        default = fields[name]
        if key in env:
            m = DEFAULT_RE.search(env[key])
            if m and default is not NO_DEFAULT and default is not None and not same_default(m.group(2), default):
                errors.append(f"{key}: compose default '{m.group(2)}' differs from settings default '{default}'")
        elif name not in ignore:
            errors.append(f"{key}: settings field cannot be set via compose (add it to the {service} environment or IGNORE_SETTINGS)")
    for key, value in env.items():
        if key not in keys:
            errors.append(f"{key}: {service} environment key is not a settings field")
        for var in VAR_RE.findall(value):
            if var not in example:
                errors.append(f"{key}: reads ${{{var}}} which is missing from .env.example")
    for name in ignore:
        if name not in fields:
            errors.append(f"IGNORE_SETTINGS lists '{name}', which is not a settings field any more")
    return len(env)


def main() -> int:
    if "--policy-keys" in sys.argv[1:]:
        print("\n".join(POLICY_KEYS))
        return 0

    fields = settings_fields(ROOT / "collector/src/vatpac/settings.py")
    loader_fields = settings_fields(ROOT / "stats_loader/src/vatsim_stats/settings.py")
    compose = load_compose(ROOT / "docker-compose.yml")
    example = env_example_keys(ROOT / ".env.example")
    errors: list[str] = []

    n_env = check_service(errors, compose, example, "collector", fields, str.upper, IGNORE_SETTINGS)
    n_loader = check_service(errors, compose, example, LOADER, loader_fields, loader_key, {})

    check_policy(errors, example, fields, compose)
    check_interpolation(errors, [p for p in (ROOT / "docker-compose.yml", ROOT / "docker-compose.dev.yml") if p.exists()])

    if errors:
        print("config drift detected:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"config drift check OK ({len(fields)} collector settings, {n_env} collector env keys, {len(loader_fields)} loader settings, "
          f"{n_loader} loader env keys, {len(example)} .env.example keys)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
