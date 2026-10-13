"""ops/deploy.sh (issue 104, trimmed): pure logic, and the whole script against FAKE ssh, docker, gh, git and friends.

Nothing here touches the VM. The fake `ssh` runs the remote command locally inside a fake home directory, with stub
`docker`, `timedatectl`, `df`, `date` ... first on PATH. Each stub reads its answer from a file under the fake folder,
and FAKE_BREAK lists the stubs that must fail without output (a check that cannot run must count as FAIL).
"""
from __future__ import annotations

import calendar
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "ops" / "deploy.sh"
BASH = shutil.which("bash")
pytestmark = [pytest.mark.skipif(BASH is None, reason="bash not available"), pytest.mark.slow_shell]

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
[ "$1" = "-C" ] && shift 2
case "$1" in
  fetch) exit 0 ;;
  rev-parse) for a; do last="$a"; done
    case "$last" in 0000000*) exit 1 ;; origin/main) echo "$FAKE_GIT_TIP"; exit 0 ;; *) echo "$FAKE_GIT_FULL"; exit 0 ;; esac ;;
  merge-base) exit "${FAKE_ANCESTOR_RC:-0}" ;;
  rev-list) echo 3 ;;
  log) echo "$*" >> "$FAKE_DIR/gitlog.args"; [ -f "$FAKE_DIR/gitlog.txt" ] && cat "$FAKE_DIR/gitlog.txt" ;;
esac
""",
    "gh": r"""#!/bin/sh
echo "$*" >> "$FAKE_DIR/gh.log"
case ",$FAKE_BREAK," in *,gh,*) exit 1 ;; esac
[ "$1" = issue ] && { case ",$FAKE_BREAK," in *,ghissues,*) exit 1 ;; esac; cat "$FAKE_DIR/issues.tsv"; exit 0; }
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

GAPS = "snapshot gaps over 5 minutes (last 3 days)"
PACK = [ln for ln in (REPO / "ops" / "audit" / "invariants.sql").read_text(encoding="utf-8").splitlines() if re.search(r"SELECT +'[^']+'( +AS +check_name)? *, *count\(", ln)]
NAMES = [re.search(r"SELECT +'([^']+)'", ln).group(1) for ln in PACK]  # type: ignore[union-attr]


def _inspect(restarts: str = "0", health: str = "healthy") -> str:
    rows = [f"/vatpac-{s}-1|2026-10-09T15:42:07.7Z|{restarts if s == 'collector' else '0'}|{health if s == 'db' else 'healthy'}"
            for s in ("backup", "collector", "db", "grafana", "watchdog")]
    rows.append("/vatpac-migrate-1|2026-10-09T19:13:22.4Z|0|exited")
    return "\n".join(rows) + "\n"


class Fake:
    """A fake PC + VM with healthy defaults; a test changes one thing at a time."""

    def __init__(self, root: Path) -> None:
        self.dir, self.home, self.bin = root / "fake", root / "home", root / "bin"
        for d in (self.dir, self.bin, self.home / "vatpac-statsV2" / "ops"):
            d.mkdir(parents=True)
        for name, body in STUBS.items():
            f = self.bin / name
            f.write_text(body, newline="\n")
            f.chmod(0o755)
        (self.home / "vatpac-statsV2" / "ops" / "smoke.sh").write_text('# ro-check-begin\ncat "$FAKE_DIR/smoke.txt"\n', newline="\n")
        self.env = dict(os.environ, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}", FAKE_DIR=str(self.dir),
                        FAKE_HOME=str(self.home), FAKE_NOW=str(NOW), FAKE_REAL_DATE=shutil.which("date") or "date",
                        FAKE_GIT_FULL=FULL, FAKE_GIT_TIP=FULL, DEPLOY_GH=str(self.bin / "gh"),
                        DEPLOY_MEMINFO=str(self.dir / "meminfo"), DEPLOY_LOADAVG=str(self.dir / "loadavg"),
                        DEPLOY_EXPECTED_FILE=str(self.dir / "expected.txt"), FAKE_BREAK="", DEPLOY_SSH="fakehost", USER="tester")
        w = self.write
        w("ci.txt", "ci completed success\n")
        w("timedatectl.txt", "System clock synchronized: yes\n              NTP service: active\n")
        w("meminfo", "MemTotal: 8000000 kB\nMemFree: 500000 kB\nMemAvailable: 6700000 kB\n")
        w("loadavg", "0.40 0.50 0.60 1/200 12345\n")
        w("df.txt", "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda2 206099468 22795560 174375024 12% /\n")
        w("inspect.txt", _inspect())
        w("psa.txt", "vatpac-db-1\nvatpac-collector-1\nvatpac-migrate-1\n")
        w("dump.txt", f"{NOW - 3600}.7457417320 5363183 vatpac_20261010T020000Z.dump\nvalid\n")
        w("smoke.txt", "PASS  container healthy: db\nPASS  grafana\nSMOKE PASS\n")
        w("panels.txt", "grafana health: HTTP 200 ok\npanel queries: 79 with rows, 5 empty, 0 errors\n")
        w("gap.txt", "30|25|20|10-10 14:59:30\n")
        w("health.txt", "200 True 0 266\n")
        w("flap.txt", "pilot session pairs|0|\ncontroller session pairs|0|\nfractional|0|\n")
        self.invariants()
        w("wdlog.txt", "2026-10-10 INFO invariant sweep: ok\n")
        w("expected.txt", "")

    def invariants(self, overrides: dict[str, str] | None = None, rows: list[str] | None = None) -> None:
        o = overrides or {}
        self.write("invariants.txt", "".join(f"{n}|{o.get(n, '0')}|\n" for n in (rows if rows is not None else NAMES)))

    def write(self, name: str, text: str) -> None:
        (self.dir / name).write_text(text, newline="\n")

    def set(self, **env: str) -> None:
        self.env.update(env)

    def brk(self, *names: str) -> None:
        self.env["FAKE_BREAK"] = ",".join(names)

    def run(self, *args: str) -> tuple[int, str]:
        p = subprocess.run([str(BASH), str(SCRIPT), *args], capture_output=True, text=True, env=self.env, cwd=REPO, timeout=120)
        return p.returncode, p.stdout + p.stderr

    def ssh_log(self) -> str:
        f = self.dir / "ssh.log"
        return f.read_text() if f.exists() else ""


def line_for(out: str, check: str) -> str:
    for ln in out.splitlines():
        parts = ln.split(None, 2)
        if len(parts) >= 2 and parts[1] == check:
            return ln
    raise AssertionError(f"no line for {check} in:\n{out}")


def _at(hh: int, mm: int, day: int = 11) -> str:
    return str(calendar.timegm((2026, 10, day, hh, mm, 0)))


def _expected(f: Fake, line: str) -> None:
    f.write("expected.txt", line + "\n")


# (mode, check, expected status, mutation); every check fails alone, once
SCENARIOS = [
    ("pre", "commit_on_main", "FAIL", lambda f: f.set(FAKE_ANCESTOR_RC="1")),
    ("pre", "ci_green", "FAIL", lambda f: f.write("ci.txt", "ci completed success\nother completed failure\n")),
    ("pre", "ci_green", "FAIL", lambda f: f.brk("gh")),
    ("pre", "vm_reachable", "FAIL", lambda f: f.set(FAKE_SSH_DOWN="1")),
    ("pre", "vm_clock", "FAIL", lambda f: f.write("timedatectl.txt", "System clock synchronized: no\n              NTP service: active\n")),
    ("pre", "vm_clock", "FAIL", lambda f: f.brk("timedatectl")),
    ("pre", "mem_available", "FAIL", lambda f: f.write("meminfo", "MemFree: 7000000 kB\nMemAvailable: 2900000 kB\n")),
    ("pre", "mem_available", "PASS", lambda f: f.write("meminfo", "MemFree: 200000 kB\nMemAvailable: 6800000 kB\n")),  # free column ignored
    ("pre", "load", "FAIL", lambda f: f.write("loadavg", "3.20 1.00 1.00 1/2 3\n")),
    ("pre", "disk_free", "FAIL", lambda f: f.write("df.txt", "h\n/dev/sda2 100 90 10 95% /\n")),
    ("pre", "containers_healthy", "FAIL", lambda f: f.write("inspect.txt", _inspect(health="unhealthy"))),
    ("pre", "restart_counts", "FAIL", lambda f: f.write("inspect.txt", _inspect(restarts="1"))),
    ("pre", "backup_window", "FAIL", lambda f: f.set(FAKE_NOW=_at(2, 0))),
    ("pre", "foreign_containers", "FAIL", lambda f: f.write("psa.txt", "vatpac-db-1\nreplayfuzz-pg\n")),
    ("pre", "recent_dump", "FAIL", lambda f: f.write("dump.txt", f"{NOW - 30 * 3600} 5363183 old.dump\nvalid\n")),
    ("pre", "recent_dump", "FAIL", lambda f: f.write("dump.txt", f"{NOW - 3600} 5363183 bad.dump\ninvalid\n")),
    ("pre", "recent_dump", "FAIL", lambda f: f.write("dump.txt", f"{NOW + 7200} 5363183 future.dump\nvalid\n")),
    ("pre", "recent_dump", "WARN", lambda f: f.write("dump.txt", f"{NOW - 13 * 3600} 5363183 d.dump\nvalid\n")),
    ("pre", "recent_dump", "FAIL", lambda f: f.brk("dump")),
    ("ver", "smoke", "FAIL", lambda f: f.write("smoke.txt", "FAIL  x\nSMOKE FAIL (1 failed)\n")),
    ("ver", "smoke", "FAIL", lambda f: (f.home / "vatpac-statsV2" / "ops" / "smoke.sh").write_text("cat x\n")),  # old unguarded smoke is never run
    ("ver", "panels", "FAIL", lambda f: f.write("panels.txt", "panel queries: 70 with rows, 5 empty, 2 errors\n")),
    ("ver", "panels", "FAIL", lambda f: f.brk("panels")),
    ("ver", "restart_counts", "FAIL", lambda f: f.write("inspect.txt", _inspect(restarts="2"))),
    ("ver", "polling_gap", "WARN", lambda f: f.write("gap.txt", "125|25|20|10-10 14:50:00\n")),
    ("ver", "polling_gap", "FAIL", lambda f: f.write("gap.txt", "301|25|20|t\n")),
    ("ver", "polling_gap", "FAIL", lambda f: f.write("gap.txt", "30|91|20|t\n")),
    ("ver", "polling_gap", "FAIL", lambda f: f.brk("gap")),
    ("ver", "health", "FAIL", lambda f: f.write("health.txt", "200 True 0 601\n")),
    ("ver", "health", "FAIL", lambda f: f.write("health.txt", "200 False 0 5\n")),
    ("ver", "health", "FAIL", lambda f: f.write("health.txt", "200 True 3 5\n")),
    ("ver", "health", "FAIL", lambda f: f.brk("health")),
    ("ver", "identity_flap", "FAIL", lambda f: f.write("flap.txt", "a|0|\nb|2|x\nc|0|\n")),
    ("ver", "invariant", "FAIL", lambda f: f.invariants({NAMES[1]: "1"})),
    ("ver", "invariants", "FAIL", lambda f: f.invariants(rows=NAMES[:1])),  # a short pack is not a pass
    ("ver", "invariant", "FAIL", lambda f: f.write("invariants.txt", "".join(f"{n}|0|\n" for n in NAMES[1:]) + f"{NAMES[0]}||\n")),  # NULL count
    ("ver", "invariants", "FAIL", lambda f: f.brk("invariants")),
    ("ver", "watchdog_log", "FAIL", lambda f: f.write("wdlog.txt", "Traceback (most recent call last):\n")),
    ("ver", "watchdog_log", "FAIL", lambda f: f.brk("logs")),
    # expected-warning entries
    ("ver", "invariant", "WARN", lambda f: (f.invariants({GAPS: "1"}), _expected(f, f"{GAPS}|1|10-10-2026 15:01|x"))),
    ("ver", "invariant", "FAIL", lambda f: (f.invariants({GAPS: "500"}), _expected(f, f"{GAPS}|1|10-10-2026 15:01|x"))),  # above the max count
    ("ver", "invariant", "FAIL", lambda f: (f.invariants({GAPS: "1"}), _expected(f, f"{GAPS}|1|10-10-2026 14:59|x"))),  # expired
    ("ver", "invariant", "FAIL", lambda f: (f.invariants({NAMES[1]: "1"}), _expected(f, f"{GAPS}|1|31-12-2099 00:00|x"))),  # another check's entry
]
GOOD = {"pre": ["--preflight", "f893585"], "ver": ["--verify"]}


def _scenario(arg: tuple[int, tuple]) -> str:  # type: ignore[type-arg]
    i, (mode, check, want, mutate) = arg
    with tempfile.TemporaryDirectory() as tmp:
        f = Fake(Path(tmp))
        mutate(f)
        rc, out = f.run(*GOOD[mode])
        try:
            got = line_for(out, check).split()[0]
        except AssertionError:
            got = "MISSING"
        has_fail = any(ln.split()[:1] == ["FAIL"] for ln in out.splitlines())
        if got != want or (rc == 1) != has_fail:
            return f"#{i} {mode} {check}: wanted {want}, got {got}, rc={rc}\n{out}"
    return ""


def test_every_check_passes_when_healthy_and_fails_alone() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        f = Fake(Path(tmp))
        rc, out = f.run(*GOOD["pre"])
        assert rc == 0 and "RESULT   preflight OK" in out, out
        assert "1 h old" in line_for(out, "recent_dump")
        rc, out = f.run(*GOOD["ver"])
        assert rc == 0 and "RESULT   verify OK" in out, out
        assert len(NAMES) == 22 and "22 invariants checked" in line_for(out, "invariants")
    with ThreadPoolExecutor(16) as pool:
        bad = [r for r in pool.map(_scenario, list(enumerate(SCENARIOS))) if r]
    assert not bad, "\n".join(bad)


def test_usage_and_hostile_arguments(tmp_path: Path) -> None:
    f = Fake(tmp_path)
    assert f.run("--preflight")[0] == 2
    assert f.run("--preflight", "not-a-sha; rm -rf /")[0] == 2
    assert f.run("--mark", "x; rm -rf /")[0] == 2
    assert f.run()[0] == 2


# ------------------------------------------------------------------ pure logic, one bash process

def test_pure_logic(tmp_path: Path) -> None:
    def ep(*a: int) -> int:
        return calendar.timegm(a)  # type: ignore[arg-type]

    f = tmp_path / "exp.txt"
    f.write_text("# c\n\nchk a|2|12-10-2026 15:42|r\nchk b|2|bad date|x\nchk c|x|12-10-2026 15:42|r\n", newline="\n")
    ok, ex = ep(2026, 10, 12, 15, 41, 59), ep(2026, 10, 12, 15, 42, 0)
    fp = f.as_posix()
    yn = " && echo y || echo n"
    calls = [
        "mem_ok 3000" + yn, "mem_ok 2999" + yn, 'mem_ok ""' + yn, "mem_ok 12x" + yn,
        f"in_backup_window {ep(2026, 10, 11, 1, 49, 0)}" + yn, f"in_backup_window {ep(2026, 10, 11, 1, 50, 0)}" + yn,
        f"in_backup_window {ep(2026, 10, 11, 2, 30, 0)}" + yn, f"in_backup_window {ep(2026, 10, 11, 2, 31, 0)}" + yn,
        f"in_backup_window {ep(2026, 10, 11, 4, 5, 0)} 04:00" + yn, f"in_backup_window {ep(2026, 10, 11, 2, 0, 0)} 04:00" + yn,
        f"in_backup_window {ep(2026, 10, 11, 23, 55, 0)} 00:05" + yn,
        'dmy_to_epoch "12-10-2026 15:42"', 'dmy_to_epoch "2026-10-12 15:42" || echo bad',
        "classify_gap 30 25", "classify_gap 90 25", "classify_gap 300 25", "classify_gap 301 25", "classify_gap 30 90", 'classify_gap "" 5',
        f'expected_warning_status "chk a" {ok} "{fp}"', f'expected_warning_status "chk a" {ex} "{fp}"',
        f'expected_warning_status "chk b" {ok} "{fp}"', f'expected_warning_status "chk c" {ok} "{fp}"', f'expected_warning_status "chk" {ok} "{fp}"',
        f'classify_invariant "chk a" 2 {ok} "{fp}"', f'classify_invariant "chk a" 3 {ok} "{fp}"', f'classify_invariant "chk a" 1 {ex} "{fp}"',
        f'classify_invariant "chk a" 0 {ex} "{fp}"', f'classify_invariant "chk a" "" {ok} "{fp}"',
    ]
    e = dict(os.environ, DEPLOY_SOURCE_ONLY="1")
    p = subprocess.run([str(BASH), "-c", f'. "{SCRIPT.as_posix()}"; ' + "; ".join(calls)], capture_output=True, text=True, env=e, timeout=60)
    got = p.stdout.split("\n")
    assert got[:11] == ["y", "n", "n", "n", "n", "y", "y", "n", "y", "n", "y"], got
    assert got[11:13] == [str(ep(2026, 10, 12, 15, 42, 0)), "bad"]
    assert got[13:19] == ["PASS", "WARN", "WARN", "FAIL", "FAIL", "BAD"], got
    assert got[19:24] == ["valid 2 12-10-2026 15:42", "expired 2 12-10-2026 15:42", "malformed", "malformed", "none"], got
    assert got[24].startswith("WARN|chk a = 2 (expected, at most 2")
    assert got[25].startswith("FAIL|chk a = 3 (above the expected maximum of 2")
    assert got[26].startswith("FAIL|") and "expired" in got[26]
    assert got[27] == "PASS|" and got[28].startswith("FAIL|")


def test_shipped_expected_warnings_file_is_well_formed() -> None:
    sql = (REPO / "ops" / "audit" / "invariants.sql").read_text(encoding="utf-8")
    seen = 0
    for ln in (REPO / "ops" / "deploy_expected_warnings.txt").read_text(encoding="utf-8").splitlines():
        if not ln.strip() or ln.startswith("#"):
            continue
        name, mx, exp, reason = ln.split("|")
        assert name and reason and mx.isdigit() and re.fullmatch(r"\d{2}-\d{2}-\d{4} \d{2}:\d{2}", exp), ln
        assert f"'{name}'" in sql, name
        seen += 1
    assert seen == 2


# ------------------------------------------------------------------ status, mark, safety

def test_status_and_mark(tmp_path: Path) -> None:
    f = Fake(tmp_path)
    rc, out = f.run("--status")
    assert rc == 0 and "no marker" in out and "vatpac-collector-1" in out and "vatpac_20261010T020000Z.dump, 1 h old" in out
    tree = f.home / "vatpac-statsV2"
    before = {p.relative_to(tree).as_posix() for p in tree.rglob("*")}
    rc, out = f.run("--mark", "f893585", "--allow-silent-issues")  # the first ever mark has no previous marker to compare with
    assert rc == 0, out
    assert {p.relative_to(tree).as_posix() for p in tree.rglob("*")} - before == {".deploy", ".deploy/release", ".deploy/release.log"}
    assert (tree / ".deploy" / "release").read_text() == f"commit={FULL}\nmarked_utc=2026-10-10T15:00:00Z\nmarked_by=tester\n"
    assert "no marker" not in f.run("--status")[1]
    f.set(FAKE_GIT_FULL="a" * 40)
    f.run("--mark", "aaaaaaa", "--allow-silent-issues")
    assert (tree / ".deploy" / "release").read_text().startswith("commit=" + "a" * 40)
    assert (tree / ".deploy" / "release.log").read_text().count("commit=") == 2


def test_mark_refusals_and_failed_log_keeps_the_old_marker(tmp_path: Path) -> None:
    f = Fake(tmp_path)
    tree = f.home / "vatpac-statsV2"
    assert f.run("--mark", "0000000")[0] == 1  # unknown commit
    f.set(FAKE_ANCESTOR_RC="1")
    rc, out = f.run("--mark", "f893585")  # not on origin/main
    assert rc == 1 and "not on origin/main" in out and not (tree / ".deploy").exists()
    f.set(FAKE_ANCESTOR_RC="0")
    assert f.run("--mark", "f893585", "--allow-silent-issues")[0] == 0
    old = (tree / ".deploy" / "release").read_text()
    (tree / ".deploy" / "release.log").unlink()
    (tree / ".deploy" / "release.log").mkdir()  # the log append will fail
    f.set(FAKE_GIT_FULL="b" * 40)
    rc, out = f.run("--mark", "bbbbbbb", "--allow-silent-issues")
    assert rc == 1 and "unchanged" in out and (tree / ".deploy" / "release").read_text() == old
    f.set(FAKE_SSH_DOWN="1")
    assert f.run("--mark", "f893585")[0] == 1
    assert f.run("--status")[0] == 1


FORBIDDEN = re.compile(r"docker (run|rm|stop|start|restart|build|kill|volume|system|image)|compose (up|down|restart|build|stop|start|rm|run|kill)|sudo|\btar\b|\brm\b|roles\.sql|\.env\b|printenv|\benv\b|TELEGRAM")


def test_read_only_modes_write_nothing_and_run_nothing_forbidden(tmp_path: Path) -> None:
    f = Fake(tmp_path)
    before = sorted(str(p.relative_to(f.home)) for p in f.home.rglob("*"))
    for mode in (["--preflight", "f893585"], ["--verify"], ["--status"]):
        f.run(*mode)
    assert sorted(str(p.relative_to(f.home)) for p in f.home.rglob("*")) == before
    f.run("--mark", "f893585", "--allow-silent-issues")
    log = f.ssh_log() + (f.dir / "docker.log").read_text()
    one_read = "sed -n 's/^BACKUP_TIME_UTC=//p' .env"  # the one deliberate read of .env: a single key
    assert one_read in log
    log = log.replace(one_read, "")
    assert not FORBIDDEN.search(log), FORBIDDEN.search(log)
    assert "ServerAliveInterval=10" in log and "--commit" in (f.dir / "gh.log").read_text()
    code = "\n".join(ln for ln in SCRIPT.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"compose (up|down|restart|build|stop)|docker run|sudo|roles\.sql", code)


def test_ci_checks_deploy_sh_with_bash_not_sh() -> None:
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "ops/backup/*.sh ops/deploy.sh ops/stale_issues.sh; do bash -n" in ci
    assert '"$f" = ops/deploy.sh ] || [ "$f" = ops/stale_issues.sh ] || sh -n' in ci


# ------------------------------------------------------------------ silent-issue guard (issue 104)

STALE = REPO / "ops" / "stale_issues.sh"
PREV = "c" * 40


def _commit(sha: str, when: str, msg: str) -> str:
    return f"\x01{sha}\x1f{when}\x1f{msg}\n"


def _guard_fake(tmp_path: Path, commits: list[str], issues: list[tuple[int, str, str]]) -> Fake:
    f = Fake(tmp_path)
    f.write("gitlog.txt", "".join(commits))
    f.write("issues.tsv", "".join(f"{n}\t{t}\t{c}\n" for n, t, c in issues))
    tree = f.home / "vatpac-statsV2" / ".deploy"
    tree.mkdir()
    (tree / "release").write_text(f"commit={PREV}\nmarked_utc=2026-10-09T10:00:00Z\nmarked_by=x\n", newline="\n")
    return f


def _marker(f: Fake) -> str:
    return (f.home / "vatpac-statsV2" / ".deploy" / "release").read_text()


COMMITS = [
    _commit("a" * 40, "2026-10-10T10:00:00+10:00", "Fix thing (#93)\n\nRefs #94"),  # 00:00Z
    _commit("b" * 40, "2026-10-10T12:00:00+00:00", "Docs for issues 95, 96 and 97"),
    _commit("d" * 40, "2026-10-10T13:00:00+00:00", "Merge pull request #139 from x/dev"),
]


def test_guard_refuses_silent_issues_and_lists_them(tmp_path: Path) -> None:
    issues = [(93, "ninety-three", "2026-10-10T01:00:00Z"),   # commented after the commit: fine
              (94, "ninety-four", "2026-10-09T23:00:00Z"),    # comment older than the commit: silent
              (95, "ninety-five", ""),                        # no comment at all: silent
              (96, "ninety-six", "2026-10-10T12:00:00Z"),     # same second as the commit: not newer, silent
              (97, "ninety-seven", "2026-10-10T12:00:01Z"),   # newer: fine
              (50, "unrelated", "")]                          # not named in any commit
    f = _guard_fake(tmp_path, COMMITS, issues)
    rc, out = f.run("--mark", "f893585")
    assert rc == 1 and "nothing written" in out, out
    assert all(x in out for x in ("#94", "#95", "#96", "ninety-five", "no comments"))
    assert not any(x in out for x in ("#93 ", "#97", "#50", "#139"))
    assert PREV in _marker(f)  # untouched
    assert f"{PREV}..{FULL}" in (f.dir / "gitlog.args").read_text()  # the range starts at the previous marker


def test_guard_allows_with_warning_and_passes_when_all_commented(tmp_path: Path) -> None:
    f = _guard_fake(tmp_path, COMMITS, [(94, "x", "2026-10-09T00:00:00Z")])
    rc, out = f.run("--mark", "f893585", "--allow-silent-issues")
    assert rc == 0 and "WARN" in out and "#94" in out and FULL in _marker(f)
    f = _guard_fake(tmp_path / "ok", COMMITS, [(n, "t", "2026-10-11T00:00:00Z") for n in (93, 94, 95, 96, 97)])
    rc, out = f.run("--mark", "f893585")
    assert rc == 0 and "PASS" in out and FULL in _marker(f)
    f = _guard_fake(tmp_path / "none", [_commit("e" * 40, "2026-10-10T10:00:00Z", "Tidy")], [])
    assert f.run("--mark", "f893585")[0] == 0


def test_guard_fails_closed(tmp_path: Path) -> None:
    f = _guard_fake(tmp_path, COMMITS, [])
    f.brk("ghissues")
    rc, out = f.run("--mark", "f893585")
    assert rc == 1 and "could not run" in out and PREV in _marker(f)
    rc, out = f.run("--mark", "f893585", "--allow-silent-issues")
    assert rc == 0 and "WARN" in out and FULL in _marker(f)
    f = Fake(tmp_path / "nomarker")  # no previous marker to take a range from
    rc, out = f.run("--mark", "f893585")
    assert rc == 1 and "no previous marker" in out and not (f.home / "vatpac-statsV2" / ".deploy").exists()
    assert f.run("--mark", "f893585", "--bogus")[0] == 2


def test_stale_issues_script_table_and_exit_codes(tmp_path: Path) -> None:
    f = _guard_fake(tmp_path, COMMITS, [(94, "ninety-four", "2026-10-09T23:00:00Z"), (97, "ok", "2026-10-11T00:00:00Z")])
    f.set(GH_BIN=str(f.bin / "gh"), REPO="o/r")

    def run(*a: str) -> tuple[int, str]:
        p = subprocess.run([str(BASH), str(STALE), *a], capture_output=True, text=True, env=f.env, cwd=REPO, timeout=60)
        return p.returncode, p.stdout + p.stderr

    rc, out = run("--since", "f893585")
    assert rc == 1 and "#94" in out and "ninety-four" in out and "2026-10-09T23:00:00Z" in out and "#97" not in out
    assert "aaaaaaaaaa" in out and "2026-10-10T10:00:00+10:00" in out
    assert run("--range", "x..y")[0] == 1
    f.write("issues.tsv", "94\tninety-four\t2026-10-11T00:00:00Z\n")
    assert run("--since", "2026-10-01")[0] == 0
    f.brk("ghissues")
    rc, out = run("--since", "2026-10-01")
    assert rc == 2 and "gh could not list" in out
    assert run("--since")[0] == 2 and run("--nope")[0] == 2
    f.brk()
    f.write("gitlog.txt", "")
    assert run("--since", "2026-10-01") == (0, "no issue numbers in the commits checked\n")
