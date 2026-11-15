"""Independent QA for the --preflight service-list fix in ops/deploy.sh (issue 387).

Specification: --preflight checks health only for the services the VM's own compose file defines today (`docker compose config
--services` on the VM); a service new in the commit being deployed is not "missing"; every service the VM does run must be
healthy; an unreadable VM list FAILS; --verify and --status keep using the full list and do not read the VM's list.
The VM is the fake from tests/test_deploy_script.py (stub ssh, docker, gh, git; nothing real is touched).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_deploy_script import Fake, line_for

OLD = ["db", "collector", "grafana", "backup", "watchdog", "migrate"]  # the VM's services before the loader is deployed
ALL_FIVE = ["db", "collector", "grafana", "backup", "watchdog"]
LOADER = "vatsim-atc-stats"


def _inspect(running: list[str], bad: dict[str, str] | None = None) -> str:
    """One inspect line per running service: name|started|restarts|health. `bad` maps a service to its health."""
    bad = bad or {}
    rows = [f"/vatpac-{s}-1|2026-10-09T15:42:07.7Z|0|{bad.get(s, 'healthy')}" for s in running]
    return "\n".join(rows) + "\n/vatpac-migrate-1|2026-10-09T19:13:22.4Z|0|exited\n"


def vm(tmp_path: Path, services: list[str], running: list[str], bad: dict[str, str] | None = None) -> Fake:
    f = Fake(tmp_path)
    f.write("services.txt", "\n".join(services) + "\n")
    f.write("inspect.txt", _inspect(running, bad))
    return f


def pre(f: Fake) -> tuple[int, str]:
    return f.run("--preflight", "f893585")


def healthy_line(out: str) -> str:
    return line_for(out, "containers_healthy")


# ---- the defect: a service new in the commit is not missing --------------------------------------------------------
def test_loader_not_defined_on_the_vm_and_not_running_passes(tmp_path: Path) -> None:
    f = vm(tmp_path, OLD, ALL_FIVE)
    rc, out = pre(f)
    assert healthy_line(out).startswith("PASS"), out
    assert LOADER not in healthy_line(out)


def test_the_vm_list_is_read_from_the_vm_with_compose_config_services(tmp_path: Path) -> None:
    f = vm(tmp_path, OLD, ALL_FIVE)
    pre(f)
    assert "compose config --services" in (f.dir / "docker.log").read_text()


def test_loader_defined_on_the_vm_and_healthy_passes_and_is_checked(tmp_path: Path) -> None:
    f = vm(tmp_path, [*OLD, LOADER], [*ALL_FIVE, LOADER])
    _, out = pre(f)
    assert healthy_line(out).startswith("PASS") and LOADER in healthy_line(out), out


def test_loader_defined_on_the_vm_but_missing_fails(tmp_path: Path) -> None:
    f = vm(tmp_path, [*OLD, LOADER], ALL_FIVE)
    _, out = pre(f)
    assert healthy_line(out).startswith("FAIL") and f"{LOADER}=missing" in healthy_line(out), out


def test_loader_defined_on_the_vm_but_unhealthy_fails(tmp_path: Path) -> None:
    f = vm(tmp_path, [*OLD, LOADER], [*ALL_FIVE, LOADER], {LOADER: "unhealthy"})
    _, out = pre(f)
    assert healthy_line(out).startswith("FAIL") and f"{LOADER}=unhealthy" in healthy_line(out), out


# ---- what the fix must not weaken --------------------------------------------------------------------------------
@pytest.mark.parametrize("svc", ALL_FIVE)
def test_an_existing_service_that_is_unhealthy_fails(tmp_path: Path, svc: str) -> None:
    f = vm(tmp_path, OLD, ALL_FIVE, {svc: "unhealthy"})
    _, out = pre(f)
    assert healthy_line(out).startswith("FAIL") and f"{svc}=unhealthy" in healthy_line(out), out


@pytest.mark.parametrize("svc", ALL_FIVE)
def test_an_existing_service_that_is_missing_fails(tmp_path: Path, svc: str) -> None:
    f = vm(tmp_path, OLD, [s for s in ALL_FIVE if s != svc])
    _, out = pre(f)
    assert healthy_line(out).startswith("FAIL") and f"{svc}=missing" in healthy_line(out), out


@pytest.mark.parametrize("health", ["starting", "exited", ""])
def test_any_state_other_than_healthy_fails(tmp_path: Path, health: str) -> None:
    f = vm(tmp_path, OLD, ALL_FIVE, {"grafana": health})
    _, out = pre(f)
    assert healthy_line(out).startswith("FAIL"), out


def test_an_unreadable_vm_service_list_fails_and_never_passes_silently(tmp_path: Path) -> None:
    f = vm(tmp_path, OLD, ALL_FIVE)
    f.brk("services")  # `docker compose config --services` exits non-zero without output
    rc, out = pre(f)
    assert healthy_line(out).startswith("FAIL"), out
    assert rc != 0


@pytest.mark.parametrize("text", ["", "\n", "migrate\n", "not-a-service\n"])
def test_an_empty_or_useless_vm_list_fails(tmp_path: Path, text: str) -> None:
    f = vm(tmp_path, OLD, ALL_FIVE)
    f.write("services.txt", text)
    _, out = pre(f)
    assert healthy_line(out).startswith("FAIL"), out


def test_an_unreadable_container_list_still_fails(tmp_path: Path) -> None:
    f = vm(tmp_path, OLD, ALL_FIVE)
    f.brk("inspect")
    _, out = pre(f)
    assert healthy_line(out).startswith("FAIL"), out


def test_a_service_the_vm_does_not_define_is_not_checked_even_if_unhealthy_elsewhere(tmp_path: Path) -> None:
    """A container of an undefined service is irrelevant to the preflight verdict (the list comes from the VM's compose file)."""
    f = vm(tmp_path, OLD, [*ALL_FIVE, LOADER], {LOADER: "unhealthy"})
    _, out = pre(f)
    assert healthy_line(out).startswith("PASS") and LOADER not in healthy_line(out), out


def test_the_overall_preflight_is_ok_when_only_the_loader_is_new(tmp_path: Path) -> None:
    f = vm(tmp_path, OLD, ALL_FIVE)
    rc, out = pre(f)
    assert "FAIL" not in "\n".join(ln for ln in out.splitlines() if "containers_healthy" in ln), out
    # the restart check must not trip over the absent loader either
    assert not any(ln.startswith("FAIL") and "restart_counts" in ln for ln in out.splitlines()), out


# ---- --verify and --status keep the full list and do not read the VM's list --------------------------------------
def test_verify_does_not_read_the_vm_service_list_and_gives_the_same_answer_for_any_list(tmp_path: Path) -> None:
    results = []
    for i, services in enumerate((OLD, [*OLD, LOADER], [])):
        f = vm(tmp_path / str(i), services, [*ALL_FIVE, LOADER])
        rc, out = f.run("--verify")
        assert "config --services" not in (f.dir / "docker.log").read_text() if (f.dir / "docker.log").exists() else True
        assert "containers_healthy" not in out
        results.append((rc, out))
    assert results[0] == results[1] == results[2]


def test_status_does_not_read_the_vm_service_list_and_shows_every_container(tmp_path: Path) -> None:
    f = vm(tmp_path, OLD, [*ALL_FIVE, LOADER])
    rc, out = f.run("--status")
    assert rc == 0
    assert f"vatpac-{LOADER}-1" in out and "vatpac-db-1" in out
    log = (f.dir / "docker.log").read_text() if (f.dir / "docker.log").exists() else ""
    assert "config --services" not in log


def test_an_unreadable_vm_list_fails_even_when_every_service_including_the_loader_is_healthy(tmp_path: Path) -> None:
    """The failure must come from the unreadable list, not from a missing container (no silent fallback to the full list)."""
    f = vm(tmp_path, [*OLD, LOADER], [*ALL_FIVE, LOADER])
    f.brk("services")
    rc, out = pre(f)
    assert healthy_line(out).startswith("FAIL"), out
    assert rc != 0
