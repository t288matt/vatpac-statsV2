"""Issue 342, mutants W07 and W16: the long-running services restart by themselves; only the one-shot migrate does not.
A service whose policy slipped to "no" would stay down after a crash or a host reboot until someone noticed."""
from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
SERVICES = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
ONE_SHOT = {"migrate"}


def test_every_long_running_service_restarts_unless_stopped() -> None:
    assert set(SERVICES) >= {"db", "collector", "grafana", "backup", "watchdog", "migrate"}
    for name, svc in SERVICES.items():
        if name not in ONE_SHOT:
            assert svc.get("restart") == "unless-stopped", name


def test_the_migrate_job_runs_once_and_is_not_restarted() -> None:
    assert SERVICES["migrate"].get("restart") == "no"
