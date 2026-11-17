"""QA for issue 387: compose and configuration, from design section 4 (settings as Docker Compose variables)."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
COMPOSE = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
COMMENT = ("# Retention must be a plain number in the compose file "
           "(ops/check_config_drift.py rejects ${...} for retention periods).")


def service() -> dict:
    return yaml.safe_load(COMPOSE)["services"]["vatsim-atc-stats"]


def test_retention_is_a_plain_number_with_the_required_comment_directly_above() -> None:
    lines = COMPOSE.splitlines()
    idx = [i for i, ln in enumerate(lines) if re.match(r"^\s+STATS_RETENTION_YEARS:", ln)]
    assert len(idx) == 1
    assert re.fullmatch(r"\s+STATS_RETENTION_YEARS: \d+\s*", lines[idx[0]]), lines[idx[0]]
    assert lines[idx[0] - 1].strip() == COMMENT
    assert service()["environment"]["STATS_RETENTION_YEARS"] == 10  # 10 years (owner, 16-11-2026)


def test_the_other_settings_are_compose_variables_with_the_designed_defaults() -> None:
    env = service()["environment"]
    assert env["STATS_RUN_AT"] == "06:00"  # plain values since 15-11-2026 (owner)
    assert env["STATS_MIN_INTERVAL_SECONDS"] == 45
    assert env["STATS_PAGE_SIZE"] == 5000
    assert env["STATS_OVERLAP_DAYS"] == "${STATS_OVERLAP_DAYS:-2}"
    assert {k for k in env if k.startswith("STATS_")} == {"STATS_RUN_AT", "STATS_MIN_INTERVAL_SECONDS", "STATS_PAGE_SIZE",
                                                         "STATS_OVERLAP_DAYS", "STATS_RETENTION_YEARS"}


def test_the_service_is_named_and_hardened_as_designed() -> None:
    s = service()
    assert s["image"].endswith("vatpac-stats-loader:${VATPAC_IMAGE_TAG:-latest}")
    assert s["restart"] == "unless-stopped"
    assert s["mem_limit"] == "256m"
    assert "ports" not in s  # no published port
    assert s["cap_drop"] == ["ALL"] and s["read_only"] is True and "tmpfs" in s
    assert s["depends_on"]["migrate"]["condition"] == "service_completed_successfully"
    assert "stats_loader:" in s["environment"]["DATABASE_URL"] and "STATS_LOADER_DB_PASSWORD" in s["environment"]["DATABASE_URL"]
    assert "healthcheck" in s and "health" in " ".join(s["healthcheck"]["test"])


def test_retention_is_not_set_through_the_env_example() -> None:
    env = (REPO / ".env.example").read_text(encoding="utf-8")
    assert not re.search(r"^\s*STATS_RETENTION_YEARS\s*=", env, re.M)
    assert re.search(r"^STATS_LOADER_DB_PASSWORD=", env, re.M)


def test_the_config_drift_check_passes() -> None:
    res = subprocess.run([sys.executable, "ops/check_config_drift.py"], cwd=REPO, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.parametrize("bad", ["STATS_RETENTION_YEARS: ${STATS_RETENTION_YEARS:-10}", "STATS_RETENTION_YEARS: ${STATS_RETENTION_YEARS}"])
def test_the_config_drift_check_rejects_retention_read_from_the_environment(tmp_path: Path, bad: str) -> None:
    for rel in ("ops", "collector/src", "stats_loader/src"):
        shutil.copytree(REPO / rel, tmp_path / rel, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(REPO / ".env.example", tmp_path / ".env.example")
    text = COMPOSE.replace("STATS_RETENTION_YEARS: 10", bad)
    assert text != COMPOSE
    (tmp_path / "docker-compose.yml").write_text(text, encoding="utf-8")
    res = subprocess.run([sys.executable, "ops/check_config_drift.py"], cwd=tmp_path, capture_output=True, text=True)
    assert res.returncode != 0, res.stdout


def test_no_mode_column_or_daily_or_large_run_types_in_the_loader_its_migration_or_its_runbook() -> None:
    files = [*(REPO / "stats_loader" / "src").rglob("*.py"), REPO / "db" / "migrations" / "20261115000400_atc_stats.sql",
             REPO / "docs" / "RUNBOOKS" / "atc-stats-loader.md"]
    pat = re.compile(r"""['"](?:daily|large|small|backfill)['"]|\brun[_ ]type\b|\bmode\b|\b(?:daily|large|backfill) (?:run )?mode\b""", re.I)
    hits = [(f.name, m.group(0)) for f in files for m in pat.finditer(f.read_text(encoding="utf-8"))]
    assert not hits, hits
