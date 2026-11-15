"""ops/check_config_drift.py: the policy-value rules (ADR-034) fire on deliberately broken copies of the real files.

Each test copies the files the check reads into a temp tree, breaks one thing, and expects a named error. The first test
proves the untouched copy passes, so every failure below is caused by its own edit."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]
FILES = [
    "docker-compose.yml",
    ".env.example",
    "collector/src/vatpac/settings.py",
    "stats_loader/src/vatsim_stats/settings.py",
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


def test_loader_retention_read_from_env_fails(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    edit(tree, "docker-compose.yml", "STATS_RETENTION_YEARS: 3", "STATS_RETENTION_YEARS: ${STATS_RETENTION_YEARS:-3}")
    code, out = run(tree, capsys)
    assert code == 1 and "STATS_RETENTION_YEARS: must be a literal" in out


def test_loader_retention_back_in_env_example_fails(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tree / ".env.example"
    path.write_text(path.read_text(encoding="utf-8") + "\nSTATS_RETENTION_YEARS=15\n", encoding="utf-8")
    code, out = run(tree, capsys)
    assert code == 1 and "STATS_RETENTION_YEARS: policy value must not be in .env.example" in out


def test_loader_setting_missing_from_compose_fails(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    edit(tree, "docker-compose.yml", "      STATS_OVERLAP_DAYS: ${STATS_OVERLAP_DAYS:-2}\n", "")
    code, out = run(tree, capsys)
    assert code == 1 and "STATS_OVERLAP_DAYS: settings field cannot be set via compose" in out


def test_loader_environment_key_that_is_not_a_setting_fails(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    line = "      STATS_OVERLAP_DAYS: ${STATS_OVERLAP_DAYS:-2}\n"
    edit(tree, "docker-compose.yml", line, line + "      STATS_TYPO: ${STATS_TYPO:-1}\n")
    code, out = run(tree, capsys)
    assert code == 1 and "STATS_TYPO: vatsim-atc-stats environment key is not a settings field" in out


def test_loader_variable_missing_from_env_example_fails(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    edit(tree, ".env.example", "STATS_PAGE_SIZE=5000\n", "")
    code, out = run(tree, capsys)
    assert code == 1 and "STATS_PAGE_SIZE: reads ${STATS_PAGE_SIZE} which is missing from .env.example" in out


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


@pytest.mark.parametrize("line", [
    "export WATCHDOG_DB_MAX_GB=20",
    "  WATCHDOG_DB_MAX_GB = 20",
])
def test_policy_key_in_env_example_in_export_or_indented_form_fails(tree: Path, capsys: pytest.CaptureFixture[str], line: str) -> None:
    path = tree / ".env.example"
    path.write_text(path.read_text(encoding="utf-8") + "\n" + line + "\n", encoding="utf-8")
    code, out = run(tree, capsys)
    assert code == 1 and "WATCHDOG_DB_MAX_GB: policy value must not be in .env.example" in out


def test_another_field_interpolating_a_policy_key_fails(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    line = "      WATCHDOG_DB_MAX_GB: 5\n"
    edit(tree, "docker-compose.yml", line, line + "      EXTRA: ${WATCHDOG_DB_MAX_GB:-9}\n")
    code, out = run(tree, capsys)
    assert code == 1 and "WATCHDOG_DB_MAX_GB: docker-compose.yml reads it from .env with $ interpolation" in out


def test_dev_compose_file_interpolating_a_policy_key_fails(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dev = "services:\n  collector:\n    environment:\n      X: $POSITION_RETENTION_DAYS\n"
    (tree / "docker-compose.dev.yml").write_text(dev, encoding="utf-8")
    code, out = run(tree, capsys)
    assert code == 1 and "POSITION_RETENTION_DAYS: docker-compose.dev.yml reads it from .env" in out


def test_policy_keys_option_prints_the_names(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    module = load()
    monkeypatch.setattr("sys.argv", ["check_config_drift.py", "--policy-keys"])
    assert module.main() == 0
    names = capsys.readouterr().out.split()
    assert names == list(module.POLICY_KEYS) and len(names) == 20 and "STATS_RETENTION_YEARS" in names


# ------------------------------------------------------------------ ops/check_env.sh warnings
SECRETS = ("POSTGRES_PASSWORD", "GRAFANA_RO_PASSWORD", "GRAFANA_ADMIN_PASSWORD", "WATCHDOG_DB_PASSWORD",
           "COLLECTOR_DB_PASSWORD", "BACKUP_DB_PASSWORD", "STATS_LOADER_DB_PASSWORD")
needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")


def check_env(env_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", (REPO / "ops" / "check_env.sh").as_posix(), env_file.as_posix()],
                          capture_output=True, text=True, timeout=60, check=False)


def write_env(path: Path, extra: str = "") -> Path:
    lines = [f"{name}=Secret{index}abcdefghijklmnop" for index, name in enumerate(SECRETS)]
    path.write_text("\n".join(lines) + "\n" + extra, encoding="utf-8")
    return path


@needs_sh
def test_check_env_warns_about_a_moved_key_without_printing_its_value(tmp_path: Path) -> None:
    result = check_env(write_env(tmp_path / ".env", "POSITION_RETENTION_DAYS=4242\nWATCHDOG_DB_MAX_GB=20\nPOLL_INTERVAL_SECONDS=30\n"))
    assert result.returncode == 0, result.stderr
    assert "WARNING POSITION_RETENTION_DAYS is in" in result.stderr and "WARNING WATCHDOG_DB_MAX_GB is in" in result.stderr
    assert "POLL_INTERVAL_SECONDS" not in result.stderr  # still a legitimate .env setting until the second PR
    assert "4242" not in result.stdout + result.stderr


@needs_sh
def test_check_env_is_quiet_for_a_clean_env_file(tmp_path: Path) -> None:
    result = check_env(write_env(tmp_path / ".env"))
    assert result.returncode == 0 and "WARNING" not in result.stderr and "NOTE" not in result.stderr


@needs_sh
def test_check_env_notes_an_override_file_next_to_the_env_file(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.override.yml").write_text("services: {}\n", encoding="utf-8")
    result = check_env(write_env(tmp_path / ".env"))
    assert result.returncode == 0 and "NOTE docker-compose.override.yml is present" in result.stderr
