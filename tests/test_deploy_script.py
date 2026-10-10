"""ops/deploy.sh (issue 104, trimmed): pure logic, and the whole script against FAKE ssh, docker, gh, git and friends.

Nothing here touches the VM. The fake `ssh` runs the remote command locally inside a fake home directory, with stub
`docker`, `timedatectl`, `df`, `date` ... first on PATH. Each stub reads its answer from a file under the fake folder,
and FAKE_BREAK lists the stubs that must fail without output (a check that cannot run must count as FAIL).
"""
from __future__ import annotations

import calendar
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "ops" / "deploy.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash not available")

NOW = calendar.timegm((2026, 10, 10, 15, 0, 0))  # 15:00 UTC, outside the backup window
FULL = "f893585d9dc8af4426883fd1dbaffc80e728a3a1"

STUBS = {
    "ssh": r"""#!/bin/sh
echo "$*" >> "$FAKE_DIR/ssh.log"
[ -n "$FAKE_SSH_DOWN" ] && exit 255
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -n) shift ;; *) break ;; esac; done
shift   # the host
HOME="$FAKE_HOME" exec sh -c "$1"
""",
    "git": r"""#!/bin/sh
case "$1" in
  fetch) exit 0 ;;
  rev-parse) for a; do last="$a"; done
    case "$last" in 0000000*) exit 1 ;; origin/main) echo "$FAKE_GIT_TIP"; exit 0 ;; *) echo "$FAKE_GIT_FULL"; exit 0 ;; esac ;;
  merge-base) exit "${FAKE_ANCESTOR_RC:-0}" ;;
  rev-list) echo 3 ;;
esac
""",
    "gh": r"""#!/bin/sh
echo "$*" >> "$FAKE_DIR/gh.log"
case ",$FAKE_BREAK," in *,gh,*) exit 1 ;; esac
cat "$FAKE_DIR/ci.txt"
""",
    "date": r"""#!/bin/sh
if [ "$*" = "-u +%s" ] && [ -n "$FAKE_NOW" ]; then echo "$FAKE_NOW"; exit 0; fi
exec "$FAKE_REAL_DATE" "$@"
""",
    "timedatectl": r"""#!/bin/sh
case ",$FAKE_BREAK," in *,timedatectl,*) exit 1 ;; esac
cat "$FAKE_DIR/timedatectl.txt"
""",
    "df": r"""#!/bin/sh
case ",$FAKE_BREAK," in *,df,*) exit 1 ;; esac
cat "$FAKE_DIR/df.txt"
""",
    "python3": r"""#!/bin/sh
case ",$FAKE_BREAK," in *,panels,*) exit 1 ;; esac
cat "$FAKE_DIR/panels.txt"; exit "${FAKE_PANELS_RC:-0}"
""",
    "docker": r"""#!/bin/sh
echo "docker $*" >> "$FAKE_DIR/docker.log"
brk() { case ",$FAKE_BREAK," in *,"$1",*) return 0 ;; esac; return 1; }
case "$*" in
  "compose ps -aq") echo c1; echo c2 ;;
  inspect*) brk inspect && exit 1; cat "$FAKE_DIR/inspect.txt" ;;
  "ps -a --format"*) brk dockerps && exit 1; cat "$FAKE_DIR/psa.txt" ;;
  *"exec -T db"*) in="$(cat)"
    case "$in" in
      *"BEGIN ISOLATION"*) brk invariants && exit 1; cat "$FAKE_DIR/invariants.txt" ;;
      *"pilot session pairs"*) brk flap && exit 1; cat "$FAKE_DIR/flap.txt" ;;
      *poll_log*) brk gap && exit 1; cat "$FAKE_DIR/gap.txt" ;;
    esac ;;
  *"exec -T backup"*) brk dump && exit 1; cat "$FAKE_DIR/dump.txt" ;;
  *"exec -T collector"*) brk health && exit 1; cat "$FAKE_DIR/health.txt" ;;
  "compose logs"*) brk logs && exit 1; cat "$FAKE_DIR/wdlog.txt"; exit "${FAKE_LOGS_RC:-0}" ;;
esac
""",
}

INVARIANT_NAMES = ["snapshot rows without an ok poll_log row", "in-region count above global count", "snapshot gaps over 5 minutes (last 3 days)"]
GAPS = "snapshot gaps over 5 minutes (last 3 days)"


def _inspect(restarts: str = "0", health: str = "healthy") -> str:
    rows = [f"/vatpac-{s}-1|2026-10-09T15:42:07.7Z|{restarts if s == 'collector' else '0'}|{health if s == 'db' else 'healthy'}"
            for s in ("backup", "collector", "db", "grafana", "watchdog")]
    rows.append("/vatpac-migrate-1|2026-10-09T19:13:22.4Z|0|exited")
    return "\n".join(rows) + "\n"


class Fake:
    """A fake PC + VM in tmp_path with healthy defaults; tests change one thing at a time."""

    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path / "fake"
        self.home = tmp_path / "home"
        self.bin = tmp_path / "bin"
        for d in (self.dir, self.bin, self.home / "vatpac-statsV2" / "ops"):
            d.mkdir(parents=True)
        for name, body in STUBS.items():
            f = self.bin / name
            f.write_text(body, newline="\n")
            f.chmod(0o755)
        smoke = self.home / "vatpac-statsV2" / "ops" / "smoke.sh"
        smoke.write_text('cat "$FAKE_DIR/smoke.txt"\n', newline="\n")
        self.env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}", FAKE_DIR=str(self.dir),
                        FAKE_HOME=str(self.home), FAKE_NOW=str(NOW), FAKE_REAL_DATE=shutil.which("date") or "date",
                        FAKE_GIT_FULL=FULL, FAKE_GIT_TIP=FULL, DEPLOY_GH=str(self.bin / "gh"),
                        DEPLOY_MEMINFO=str(self.dir / "meminfo"), DEPLOY_EXPECTED_FILE=str(self.dir / "expected.txt"),
                        FAKE_BREAK="", DEPLOY_SSH="fakehost", USER="tester")
        w = self.write
        w("ci.txt", "ci completed success\n")
        w("timedatectl.txt", "System clock synchronized: yes\n              NTP service: active\n")
        w("meminfo", "MemTotal: 8000000 kB\nMemFree: 500000 kB\nMemAvailable: 6700000 kB\n")
        w("df.txt", "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda2 206099468 22795560 174375024 12% /\n")
        w("inspect.txt", _inspect())
        w("psa.txt", "vatpac-db-1\nvatpac-collector-1\nvatpac-migrate-1\n")
        w("dump.txt", f"{NOW - 13 * 3600}.7457417320 5363183 vatpac_20261010T020000Z.dump\nvalid\n")
        w("smoke.txt", "PASS  container healthy: db\nPASS  grafana\nSMOKE PASS\n")
        w("panels.txt", "grafana health: HTTP 200 ok\npanel queries: 79 with rows, 5 empty, 0 errors\n")
        w("gap.txt", "30|25|20\n")
        w("health.txt", '200\n{"status": "ok", "archive": {"enabled": true, "size_bytes": 1, "files_written": 3, "errors": 0, "dropped": 0, "last_write_age_seconds": 266.5}}\n')
        w("flap.txt", "pilot session pairs|0|\ncontroller session pairs|0|\nfractional|0|\n")
        w("invariants.txt", "".join(f"{n}|0|\n" for n in INVARIANT_NAMES))
        w("wdlog.txt", "2026-10-10 INFO invariant sweep: ok\n2026-10-10 INFO cycle ok\n")
        w("expected.txt", "")

    def write(self, name: str, text: str) -> None:
        (self.dir / name).write_text(text, newline="\n")

    def set(self, **env: str) -> None:
        self.env.update(env)

    def run(self, *args: str) -> tuple[int, str]:
        p = subprocess.run([str(BASH), str(SCRIPT), *args], capture_output=True, text=True, env=self.env, cwd=REPO, timeout=120)
        return p.returncode, p.stdout + p.stderr

    def ssh_log(self) -> str:
        f = self.dir / "ssh.log"
        return f.read_text() if f.exists() else ""


@pytest.fixture
def fake(tmp_path: Path) -> Fake:
    return Fake(tmp_path)


def line_for(out: str, check: str) -> str:
    for ln in out.splitlines():
        parts = ln.split(None, 2)
        if len(parts) >= 2 and parts[1] == check:
            return ln
    raise AssertionError(f"no line for {check} in:\n{out}")


def status_of(out: str, check: str) -> str:
    return line_for(out, check).split()[0]


def fn(call: str, **env: str) -> str:
    """Run one pure function from deploy.sh in a bash that only sources it."""
    e = dict(os.environ, DEPLOY_SOURCE_ONLY="1", **env)
    p = subprocess.run([str(BASH), "-c", f'. "{SCRIPT.as_posix()}"; {call}'], capture_output=True, text=True, env=e, timeout=30)
    return (p.stdout.strip() + f" rc={p.returncode}").strip()


# ------------------------------------------------------------------ pure logic

@pytest.mark.parametrize(("mb", "ok"), [("3000", True), ("2999", False), ("6700", True), ("", False), ("abc", False), ("12x", False)])
def test_mem_ok_threshold(mb: str, ok: bool) -> None:
    assert fn(f'mem_ok "{mb}"').endswith("rc=0" if ok else "rc=1")


@pytest.mark.parametrize(("hh", "mm", "inside"), [(1, 49, False), (1, 50, True), (2, 0, True), (2, 30, True), (2, 31, False), (15, 0, False), (0, 0, False)])
def test_backup_window_edges(hh: int, mm: int, inside: bool) -> None:
    ep = calendar.timegm((2026, 10, 10, hh, mm, 0))
    assert fn(f"in_backup_window {ep}").endswith("rc=0" if inside else "rc=1")


def test_backup_window_applies_on_any_day() -> None:
    assert fn(f"in_backup_window {calendar.timegm((2026, 12, 25, 2, 10, 0))}").endswith("rc=0")


def test_dmy_to_epoch_and_malformed() -> None:
    assert fn('dmy_to_epoch "12-10-2026 15:42"') == f"{calendar.timegm((2026, 10, 12, 15, 42, 0))} rc=0"
    assert fn('dmy_to_epoch "2026-10-12 15:42"').endswith("rc=1")
    assert fn('dmy_to_epoch "12-10-2026"').endswith("rc=1")


@pytest.mark.parametrize(("gap", "age", "ok"), [("30", "25", True), ("89", "89", True), ("90", "10", False), ("30", "90", False), ("", "10", False), ("-1", "10", False)])
def test_gap_ok(gap: str, age: str, ok: bool) -> None:
    assert fn(f'gap_ok "{gap}" "{age}"').endswith("rc=0" if ok else "rc=1")


def test_expected_warning_status(tmp_path: Path) -> None:
    f = tmp_path / "exp.txt"
    f.write_text("# comment\n\nchk a|12-10-2026 15:42|reason\nchk b|bad date|x\n", newline="\n")
    before, after = calendar.timegm((2026, 10, 12, 15, 41, 59)), calendar.timegm((2026, 10, 12, 15, 42, 0))
    assert fn(f'expected_warning_status "chk a" {before} "{f.as_posix()}"') == "valid 12-10-2026 15:42 rc=0"
    assert fn(f'expected_warning_status "chk a" {after} "{f.as_posix()}"') == "expired 12-10-2026 15:42 rc=0"
    assert fn(f'expected_warning_status "chk b" {before} "{f.as_posix()}"') == "malformed bad date rc=0"
    assert fn(f'expected_warning_status "chk" {before} "{f.as_posix()}"') == "none rc=0"  # exact name only
    assert fn(f'expected_warning_status "chk a" {before} "{tmp_path.as_posix()}/missing.txt"') == "none rc=0"


def test_classify_invariant(tmp_path: Path) -> None:
    f = tmp_path / "exp.txt"
    f.write_text("named check|12-10-2026 15:42|r\n", newline="\n")
    before = calendar.timegm((2026, 10, 12, 0, 0, 0))
    after = calendar.timegm((2026, 10, 13, 0, 0, 0))
    c = lambda name, n, now: fn(f'classify_invariant "{name}" "{n}" {now} "{f.as_posix()}"')  # noqa: E731
    assert c("named check", 0, before).startswith("PASS|")
    assert c("named check", 2, before).startswith("WARN|named check = 2 (expected until 12-10-2026 15:42 UTC)")
    assert c("named check", 2, after).startswith("FAIL|") and "expired" in c("named check", 2, after)
    assert c("other check", 1, before).startswith("FAIL|other check = 1")
    assert c("named check", "x", before).startswith("FAIL|")


# ------------------------------------------------------------------ preflight

def test_preflight_all_good(fake: Fake) -> None:
    rc, out = fake.run("--preflight", "f893585")
    assert rc == 0, out
    assert "RESULT   preflight OK" in out
    for check in ("commit_on_main", "ci_green", "vm_clock", "mem_available", "disk_free", "containers_healthy", "restart_counts",
                  "backup_window", "foreign_containers", "recent_dump", "not_mid_deploy"):
        assert status_of(out, check) == "PASS", out
    assert "13 h old" in line_for(out, "recent_dump") and "5363183 bytes" in line_for(out, "recent_dump")
    log = (fake.dir / "gh.log").read_text()
    assert "--repo t288matt/vatpac-statsV2" in log and "--branch main" in log


def test_preflight_needs_a_commit(fake: Fake) -> None:
    rc, out = fake.run("--preflight")
    assert rc == 2 and "usage" in out
    rc, out = fake.run("--preflight", "not-a-sha; rm -rf /")
    assert rc == 2


def _break(fake: Fake, *names: str) -> None:
    fake.set(FAKE_BREAK=",".join(names))


FAILING_PREFLIGHT = [
    ("commit_on_main", lambda f: f.set(FAKE_ANCESTOR_RC="1")),
    ("commit_on_main", lambda f: f.set(FAKE_GIT_FULL="")),
    ("ci_green", lambda f: f.write("ci.txt", "ci completed failure\n")),
    ("ci_green", lambda f: f.write("ci.txt", "ci in_progress \n")),
    ("ci_green", lambda f: f.write("ci.txt", "ci completed success\nother completed failure\n")),
    ("ci_green", lambda f: f.write("ci.txt", "")),
    ("ci_green", lambda f: _break(f, "gh")),
    ("vm_reachable", lambda f: f.set(FAKE_SSH_DOWN="1")),
    ("vm_clock", lambda f: f.write("timedatectl.txt", "System clock synchronized: no\n              NTP service: active\n")),
    ("vm_clock", lambda f: f.write("timedatectl.txt", "System clock synchronized: yes\n              NTP service: inactive\n")),
    ("vm_clock", lambda f: _break(f, "timedatectl")),
    ("mem_available", lambda f: f.write("meminfo", "MemFree: 7000000 kB\nMemAvailable: 2900000 kB\n")),
    ("mem_available", lambda f: f.write("meminfo", "MemFree: 7000000 kB\n")),
    ("disk_free", lambda f: f.write("df.txt", "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda2 100 90 10 95% /\n")),
    ("disk_free", lambda f: _break(f, "df")),
    ("containers_healthy", lambda f: f.write("inspect.txt", _inspect(health="unhealthy"))),
    ("containers_healthy", lambda f: f.write("inspect.txt", "/vatpac-db-1|x|0|healthy\n")),
    ("containers_healthy", lambda f: _break(f, "inspect")),
    ("restart_counts", lambda f: f.write("inspect.txt", _inspect(restarts="1"))),
    ("backup_window", lambda f: f.set(FAKE_NOW=str(calendar.timegm((2026, 10, 11, 2, 0, 0))))),
    ("foreign_containers", lambda f: f.write("psa.txt", "vatpac-db-1\nreplayfuzz-pg\n")),
    ("foreign_containers", lambda f: _break(f, "dockerps")),
    ("recent_dump", lambda f: f.write("dump.txt", f"{NOW - 30 * 3600} 5363183 old.dump\nvalid\n")),
    ("recent_dump", lambda f: f.write("dump.txt", f"{NOW - 3600} 5363183 bad.dump\ninvalid\n")),
    ("recent_dump", lambda f: f.write("dump.txt", f"{NOW - 3600} 1234 tiny.dump\nvalid\n")),
    ("recent_dump", lambda f: f.write("dump.txt", "\ninvalid\n")),
    ("recent_dump", lambda f: _break(f, "dump")),
    ("not_mid_deploy", lambda f: (f.home / "vatpac-statsV2" / ".deploy").mkdir() or (f.home / "vatpac-statsV2" / ".deploy" / "lock").write_text("deploy in progress")),
]


@pytest.mark.parametrize(("check", "mutate"), FAILING_PREFLIGHT, ids=[f"{c}-{i}" for i, (c, _) in enumerate(FAILING_PREFLIGHT)])
def test_each_preflight_check_failing_gives_exit_1(fake: Fake, check: str, mutate) -> None:  # type: ignore[no-untyped-def]
    mutate(fake)
    rc, out = fake.run("--preflight", "f893585")
    assert rc == 1, out
    assert status_of(out, check) == "FAIL", out
    assert "RESULT   preflight FAILED" in out


def test_memavailable_not_the_free_column(fake: Fake) -> None:
    # little "free" but plenty available (the real situation on 10-10-2026): PASS
    fake.write("meminfo", "MemFree: 200000 kB\nMemAvailable: 6800000 kB\n")
    rc, out = fake.run("--preflight", "f893585")
    assert rc == 0 and "6640 MB" in line_for(out, "mem_available")
    # plenty "free" but little available: FAIL
    fake.write("meminfo", "MemFree: 7000000 kB\nMemAvailable: 1000000 kB\n")
    rc, out = fake.run("--preflight", "f893585")
    assert rc == 1 and status_of(out, "mem_available") == "FAIL"


def test_backup_window_refusal_names_the_window(fake: Fake) -> None:
    fake.set(FAKE_NOW=str(calendar.timegm((2026, 10, 11, 1, 55, 0))))
    rc, out = fake.run("--preflight", "f893585")
    assert rc == 1 and "inside 01:50 to 02:30 UTC" in line_for(out, "backup_window")


def test_unreachable_vm_fails_every_vm_check_without_hanging(fake: Fake) -> None:
    fake.set(FAKE_SSH_DOWN="1")
    rc, out = fake.run("--preflight", "f893585")
    assert rc == 1
    for check in ("vm_clock", "mem_available", "disk_free", "containers_healthy", "backup_window", "recent_dump", "not_mid_deploy"):
        assert status_of(out, check) == "FAIL", out
    assert fake.ssh_log().count("\n") == 1  # one probe, then no more attempts


# ------------------------------------------------------------------ verify

def test_verify_all_good(fake: Fake) -> None:
    rc, out = fake.run("--verify")
    assert rc == 0, out
    for check in ("smoke", "panels", "restart_counts", "collector_started", "polling_gap", "health", "identity_flap", "invariants", "watchdog_log"):
        assert status_of(out, check) == "PASS", out
    assert "SUMMARY" in out and "RESULT   verify OK" in out
    assert "2026-10-09T15:42:07 UTC" in line_for(out, "collector_started")


FAILING_VERIFY = [
    ("smoke", lambda f: f.write("smoke.txt", "PASS  a\nFAIL  collector /health returns 200\nSMOKE FAIL (1 failed)\n")),
    ("smoke", lambda f: f.write("smoke.txt", "")),
    ("panels", lambda f: f.write("panels.txt", "ERROR  x\npanel queries: 70 with rows, 5 empty, 2 errors\n")),
    ("panels", lambda f: f.write("panels.txt", "")),
    ("panels", lambda f: _break(f, "panels")),
    ("restart_counts", lambda f: f.write("inspect.txt", _inspect(restarts="2"))),
    ("restart_counts", lambda f: _break(f, "inspect")),
    ("collector_started", lambda f: f.write("inspect.txt", "/vatpac-db-1|x|0|healthy\n")),
    ("polling_gap", lambda f: f.write("gap.txt", "120|25|20\n")),
    ("polling_gap", lambda f: f.write("gap.txt", "30|200|20\n")),
    ("polling_gap", lambda f: f.write("gap.txt", "30|25|2\n")),
    ("polling_gap", lambda f: _break(f, "gap")),
    ("health", lambda f: f.write("health.txt", "500\n")),
    ("health", lambda f: f.write("health.txt", '200\n{"archive": {"enabled": false, "errors": 0, "last_write_age_seconds": 1}}\n')),
    ("health", lambda f: f.write("health.txt", '200\n{"archive": {"enabled": true, "errors": 3, "last_write_age_seconds": 1}}\n')),
    ("health", lambda f: _break(f, "health")),
    ("identity_flap", lambda f: f.write("flap.txt", "a|0|\nb|2|2026-10-10\nc|0|\n")),
    ("identity_flap", lambda f: f.write("flap.txt", "a|0|\nb|0|\n")),
    ("identity_flap", lambda f: _break(f, "flap")),
    ("invariant", lambda f: f.write("invariants.txt", "in-region count above global count|1|\n")),
    ("invariants", lambda f: f.write("invariants.txt", "")),
    ("invariants", lambda f: _break(f, "invariants")),
    ("watchdog_log", lambda f: f.write("wdlog.txt", "ok\nTraceback (most recent call last):\n")),
    ("watchdog_log", lambda f: f.write("wdlog.txt", "ERROR could not format the alert in plain words\n")),
    ("watchdog_log", lambda f: _break(f, "logs")),
    ("watchdog_log", lambda f: f.set(FAKE_LOGS_RC="1")),
]


@pytest.mark.parametrize(("check", "mutate"), FAILING_VERIFY, ids=[f"{c}-{i}" for i, (c, _) in enumerate(FAILING_VERIFY)])
def test_each_verify_check_failing_gives_exit_1(fake: Fake, check: str, mutate) -> None:  # type: ignore[no-untyped-def]
    mutate(fake)
    rc, out = fake.run("--verify")
    assert rc == 1, out
    assert status_of(out, check) == "FAIL", out
    assert "RESULT   verify FAILED" in out


def test_expected_warning_is_warn_not_fail_until_it_expires(fake: Fake) -> None:
    fake.write("invariants.txt", f"{GAPS}|1|00:06:00\n")
    fake.write("expected.txt", f"{GAPS}|10-10-2026 15:01|power cut\n")  # one minute after FAKE_NOW
    rc, out = fake.run("--verify")
    assert rc == 0, out
    assert status_of(out, "invariant") == "WARN" and "expected until 10-10-2026 15:01 UTC" in line_for(out, "invariant")
    assert "1 WARN, 0 FAIL" in out


def test_expired_expected_warning_is_a_fail(fake: Fake) -> None:
    fake.write("invariants.txt", f"{GAPS}|1|00:06:00\n")
    fake.write("expected.txt", f"{GAPS}|10-10-2026 14:59|power cut\n")  # one minute before FAKE_NOW
    rc, out = fake.run("--verify")
    assert rc == 1
    assert status_of(out, "invariant") == "FAIL" and "expired" in line_for(out, "invariant")


def test_expected_warning_for_another_check_does_not_hide_this_one(fake: Fake) -> None:
    fake.write("invariants.txt", "in-region count above global count|1|\n")
    fake.write("expected.txt", f"{GAPS}|31-12-2099 00:00|x\n")
    rc, _ = fake.run("--verify")
    assert rc == 1


def test_shipped_expected_warnings_file_is_well_formed() -> None:
    for ln in (REPO / "ops" / "deploy_expected_warnings.txt").read_text(encoding="utf-8").splitlines():
        if not ln.strip() or ln.startswith("#"):
            continue
        name, exp, reason = ln.split("|")
        assert name and reason and re.fullmatch(r"\d{2}-\d{2}-\d{4} \d{2}:\d{2}", exp), ln
        # the name must be a real invariant in the pack, or the entry is dead text
        assert f"'{name}'" in (REPO / "ops" / "audit" / "invariants.sql").read_text(encoding="utf-8"), name


# ------------------------------------------------------------------ status and mark

def test_status_shows_marker_containers_and_dump(fake: Fake) -> None:
    rc, out = fake.run("--status")
    assert rc == 0
    assert "no marker" in out and "vatpac-collector-1" in out and "vatpac_20261010T020000Z.dump, 13 h old" in out
    (fake.home / "vatpac-statsV2" / ".deploy").mkdir()
    (fake.home / "vatpac-statsV2" / ".deploy" / "release").write_text("commit=abc\nmarked_utc=x\nmarked_by=y\n")
    _, out = fake.run("--status")
    assert "commit=abc" in out and "no marker" not in out


def test_mark_writes_only_the_marker(fake: Fake) -> None:
    tree = fake.home / "vatpac-statsV2"
    before = {p.relative_to(tree) for p in tree.rglob("*")}
    rc, out = fake.run("--mark", "f893585")
    assert rc == 0, out
    after = {p.relative_to(tree) for p in tree.rglob("*")}
    assert {p.as_posix() for p in after - before} == {".deploy", ".deploy/release", ".deploy/release.log"}
    text = (tree / ".deploy" / "release").read_text()
    assert text == f"commit={FULL}\nmarked_utc=2026-10-10T15:00:00Z\nmarked_by=tester\n"
    assert not re.search(r"password|token|secret", text, re.I)


def test_mark_refuses_an_unknown_commit_or_unreadable_vm(fake: Fake) -> None:
    rc, _ = fake.run("--mark", "0000000")
    assert rc == 1 and not (fake.home / "vatpac-statsV2" / ".deploy").exists()
    rc, _ = fake.run("--mark", "x; rm -rf /")
    assert rc == 2
    fake.set(FAKE_SSH_DOWN="1")
    rc, _ = fake.run("--mark", "f893585")
    assert rc == 1


def test_mark_twice_keeps_a_log_and_the_latest_marker(fake: Fake) -> None:
    fake.run("--mark", "f893585")
    fake.set(FAKE_GIT_FULL="a" * 40)
    fake.run("--mark", "aaaaaaa")
    tree = fake.home / "vatpac-statsV2" / ".deploy"
    assert (tree / "release").read_text().startswith("commit=" + "a" * 40)
    assert (tree / "release.log").read_text().count("commit=") == 2


# ------------------------------------------------------------------ safety: what the script is allowed to run on the VM

FORBIDDEN = re.compile(r"docker (run|rm|stop|start|restart|build|kill|volume|system|image)|compose (up|down|restart|build|stop|start|rm|run|kill)|sudo|\btar\b|\brm\b|roles\.sql|\.env\b|printenv|\benv\b|TELEGRAM")


@pytest.mark.parametrize("mode", [["--preflight", "f893585"], ["--verify"], ["--status"], ["--mark", "f893585"]])
def test_no_forbidden_command_reaches_the_vm(fake: Fake, mode: list[str]) -> None:
    fake.run(*mode)
    dlog = fake.dir / "docker.log"
    log = fake.ssh_log() + (dlog.read_text() if dlog.exists() else "")
    assert log and not FORBIDDEN.search(log), FORBIDDEN.search(log)


@pytest.mark.parametrize("mode", [["--preflight", "f893585"], ["--verify"], ["--status"]])
def test_read_only_modes_write_nothing_on_the_vm(fake: Fake, mode: list[str]) -> None:
    tree = fake.home
    before = sorted(str(p.relative_to(tree)) for p in tree.rglob("*"))
    fake.run(*mode)
    assert sorted(str(p.relative_to(tree)) for p in tree.rglob("*")) == before


def test_script_text_has_no_apply_paths() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"compose (up|down|restart|build|stop)|docker run|sudo|roles\.sql", code)
