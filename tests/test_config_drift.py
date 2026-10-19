"""ops/check_config_drift.py: the policy-value rules (ADR-034) fire on deliberately broken copies of the real files.

Each test copies the files the check reads into a temp tree, breaks one thing, and expects a named error. The first test
proves the untouched copy passes, so every failure below is caused by its own edit."""
from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]
FILES = [
    "docker-compose.yml",
    ".env.example",
    "collector/src/vatpac/settings.py",
    "ops/watchdog/watchdog.py",
    "ops/watchdog/integrity.py",
]


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_config_drift_under_test", REPO / "ops" / "check_config_drift.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for name in FILES:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / name, tmp_path / name)
    (tmp_path / "ops" / "backup").mkdir(parents=True, exist_ok=True)
    for script in (REPO / "ops" / "backup").glob("*.sh"):
        shutil.copy(script, tmp_path / "ops" / "backup" / script.name)
    return tmp_path


def run(tree: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    module = load()
    module.ROOT = tree
    code = module.main()
    return code, capsys.readouterr().out


def edit(tree: Path, name: str, old: str, new: str) -> None:
    path = tree / name
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not found in {name}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_untouched_copy_passes(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run(tree, capsys)
    assert code == 0, out


def test_compose_reading_a_policy_value_from_env_fails(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    edit(tree, "docker-compose.yml", "POSITION_RETENTION_DAYS: 180", "POSITION_RETENTION_DAYS: ${POSITION_RETENTION_DAYS:-180}")
    code, out = run(tree, capsys)
    assert code == 1 and "POSITION_RETENTION_DAYS: must be a literal" in out


def test_policy_key_back_in_env_example_fails(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tree / ".env.example").write_text((tree / ".env.example").read_text(encoding="utf-8") + "\nWATCHDOG_DB_MAX_GB=20\n", encoding="utf-8")
    code, out = run(tree, capsys)
    assert code == 1 and "WATCHDOG_DB_MAX_GB: policy value must not be in .env.example" in out


def test_policy_key_missing_from_compose_fails(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    edit(tree, "docker-compose.yml", "      WATCHDOG_DB_MAX_GB: 5\n", "")
    code, out = run(tree, capsys)
    assert code == 1 and "WATCHDOG_DB_MAX_GB: not set in the watchdog environment" in out


@pytest.mark.parametrize(("file", "old", "new", "expect"), [
    ("collector/src/vatpac/settings.py", "Field(180,", "Field(90,", "POSITION_RETENTION_DAYS: compose value '180' differs"),
    ("ops/watchdog/watchdog.py", '"WATCHDOG_DB_MAX_GB", 5)', '"WATCHDOG_DB_MAX_GB", 20)', "WATCHDOG_DB_MAX_GB: compose value '5' differs"),
    ("ops/watchdog/integrity.py", "samples: int = 24", "samples: int = 99", "WATCHDOG_CROSSCHECK_SAMPLES: compose value '24' differs"),
    ("ops/watchdog/integrity.py", "invariants_interval_s: float = 72 * 3600.0", "invariants_interval_s: float = 48 * 3600.0",
     "WATCHDOG_INTEGRITY_INTERVAL_HOURS: compose value '72' differs"),
])
def test_code_default_that_differs_from_compose_fails(
    tree: Path, capsys: pytest.CaptureFixture[str], file: str, old: str, new: str, expect: str,
) -> None:
    edit(tree, file, old, new)
    code, out = run(tree, capsys)
    assert code == 1 and expect in out


@pytest.mark.parametrize("line", [
    'X="${BACKUP_MIN_KEEP:-5}"',   # a second script with another default (the first match alone would pass)
    'X="${BACKUP_MIN_KEEP-5}"',    # no-colon form
    'X="${BACKUP_MIN_KEEP:=5}"',   # assignment form
])
def test_any_backup_script_default_that_differs_fails(tree: Path, capsys: pytest.CaptureFixture[str], line: str) -> None:
    script = tree / "ops" / "backup" / "restore-test.sh"
    script.write_text(script.read_text(encoding="utf-8") + "\n" + line + "\n", encoding="utf-8")
    code, out = run(tree, capsys)
    assert code == 1 and "BACKUP_MIN_KEEP: compose value '3' differs from a backup code default '5'" in out
