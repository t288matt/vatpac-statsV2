"""ops/deploy.sh (issue 104, trimmed): pure logic, and the whole script against FAKE ssh, docker, git and friends; ops/stale_issues.sh against a fake gh.

Nothing here touches the VM. The fake `ssh` runs the remote command locally inside a fake home directory, with stub
`docker`, `date` ... first on PATH. Each stub reads its answer from a file under the fake folder,
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

import integrity  # noqa: E402  (tests/conftest.py puts ops/watchdog on sys.path)
import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "ops" / "deploy.sh"
BASH = shutil.which("bash")
pytestmark = [pytest.mark.skipif(BASH is None, reason="bash not available"), pytest.mark.slow_shell]

NOW = calendar.timegm((2026, 10, 10, 15, 0, 0))  # 15:00 UTC
FULL = "f893585d9dc8af4426883fd1dbaffc80e728a3a1"

STUBS = {
    "ssh": r"""#!/bin/sh
echo "$*" >> "$FAKE_DIR/ssh.log"
[ -n "$FAKE_SSH_DOWN" ] && exit 255
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -n) shift ;; *) break ;; esac; done
shift   # the host
HOME="$FAKE_HOME" FAKE_ON_VM=1 exec sh -c "$1"
""",
    "git": r"""#!/bin/sh
[ "$1" = "-C" ] && shift 2
case "$1" in
  fetch) exit 0 ;;
  status) [ -n "$FAKE_GIT_STATUS_FAIL" ] && exit 128; [ -n "$FAKE_DIRTY" ] && printf '%s\n' "$FAKE_DIRTY"; exit 0 ;;
  rev-parse) for a; do last="$a"; done
    case "$last" in HEAD) if [ -n "$FAKE_ON_VM" ]; then echo "${FAKE_VM_HEAD:-$FAKE_GIT_FULL}"; else echo "${FAKE_LOCAL_HEAD:-$FAKE_GIT_FULL}"; fi; exit 0 ;; *) echo "$FAKE_GIT_FULL"; exit 0 ;; esac ;;
  log) echo "$*" >> "$FAKE_DIR/gitlog.args"; [ -f "$FAKE_DIR/gitlog.txt" ] && cat "$FAKE_DIR/gitlog.txt" ;;
esac
""",
    "gh": r"""#!/bin/sh
echo "$*" >> "$FAKE_DIR/gh.log"
[ "$1" = issue ] && { case ",$FAKE_BREAK," in *,ghissues,*) exit 1 ;; esac; cat "$FAKE_DIR/issues.tsv"; exit 0; }
exit 0
""",
    "date": r"""#!/bin/sh
if [ "$*" = "-u +%s" ] && [ -n "$FAKE_NOW" ]; then echo "$FAKE_NOW"; exit 0; fi
exec "$FAKE_REAL_DATE" "$@"
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
  "compose config --services") brk services && exit 1; cat "$FAKE_DIR/services.txt" ;;
  inspect*) brk inspect && exit 1; cat "$FAKE_DIR/inspect.txt" ;;
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
NAMES = integrity.invariant_names((REPO / "ops" / "audit" / "invariants.sql").read_text(encoding="utf-8"))


def _inspect(restarts: str = "0", health: str = "healthy", loader_health: str = "healthy") -> str:
    rows = [f"/vatpac-{s}-1|2026-10-09T15:42:07.7Z|{restarts if s == 'collector' else '0'}|"
            f"{health if s == 'db' else loader_health if s == 'vatsim-atc-stats' else 'healthy'}"
            for s in ("backup", "collector", "db", "grafana", "watchdog", "vatsim-atc-stats")]
    rows.append("/vatpac-migrate-1|2026-10-09T19:13:22.4Z|0|exited")
    return "\n".join(rows) + "\n"


PFX = "ghcr.io/t288matt/"
TAG7 = "sha-" + FULL[:7]


def _inspect_registry(tag: str = "latest", rev: str = FULL, only: str = "") -> str:
    """Six-field inspect lines: the five registry-image services carry an image and a revision label (only: the one image name that
    differs). The loader's compose service is vatsim-atc-stats and its image is vatpac-stats-loader (issue 387)."""
    rows = []
    image_of = {"vatsim-atc-stats": "loader"}
    for svc in ("backup", "collector", "db", "grafana", "watchdog", "migrate", "vatsim-atc-stats"):
        s = image_of.get(svc, svc)
        base = f"/vatpac-{svc}-1|2026-10-09T15:42:07.7Z|0|{'exited' if s == 'migrate' else 'healthy'}"
        if s in ("backup", "collector", "watchdog", "migrate", "loader"):
            odd = only == s
            base += f"|{PFX}vatpac-stats-{s}:{tag if odd else 'latest'}|{rev if odd else FULL}"
        else:
            base += f"|third/party-{s}:1|"
        rows.append(base)
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
                        FAKE_GIT_FULL=FULL,
                        DEPLOY_EXPECTED_FILE=str(self.dir / "expected.txt"), FAKE_BREAK="", DEPLOY_SSH="fakehost", USER="tester")
        w = self.write
        w("inspect.txt", _inspect())
        # the VM's compose services today: the stats loader is not among them before its first deploy
        w("services.txt", "db\ncollector\ngrafana\nbackup\nwatchdog\nmigrate\n")
        w("dump.txt", f"{NOW - 3600}.7457417320 5363183 vatpac_20261010T020000Z.dump\nvalid\n")
        w("smoke.txt", "PASS  container healthy: db\nPASS  grafana\nSMOKE PASS\n")
        w("panels.txt", "grafana health: HTTP 200 ok\npanel queries: 79 with rows, 5 empty, 0 errors\n")
        w("gap.txt", "30|25|20|10-10 14:59:30\n")
        w("health.txt", "200 True 0 266\n")
        w("flap.txt", "pilot session pairs, same cid+callsign, logon_time within 2 s (identity flap)|0|\ncontroller session pairs, same cid+callsign, logon_time within 2 s (identity flap)|0|\n")  # name|count|first_seen, as ops/audit/identity_flap.sql prints it
        self.invariants()
        w("wdlog.txt", "2026-10-10 INFO invariant sweep: ok\n")
        w("expected.txt", "")

    def invariants(self, overrides: dict[str, str] | None = None, rows: list[str] | None = None) -> None:
        o = overrides or {}
        self.write("invariants.txt", "".join(f"{n}|{o.get(n, '0')}|\n" for n in (rows if rows is not None else NAMES)))

    def registry(self, prefix: str = PFX, tag: str = "") -> None:
        """Registry mode as production runs it: the prefix alone (images follow latest); a tag line is only a pin."""
        self.dotenv(f"VATPAC_IMAGE_PREFIX={prefix}" + chr(10) + (f"VATPAC_IMAGE_TAG={tag}" + chr(10) if tag else ""))
        self.write("inspect.txt", _inspect_registry())

    def dotenv(self, text: str) -> None:
        (self.home / "vatpac-statsV2" / ".env").write_text(text, newline="\n")

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


def _expected(f: Fake, line: str) -> None:
    f.write("expected.txt", line + "\n")


# (check, expected status, mutation); every check fails alone, once
SCENARIOS = [
    ("smoke", "FAIL", lambda f: f.write("smoke.txt", "FAIL  x\nSMOKE FAIL (1 failed)\n")),
    ("smoke", "FAIL", lambda f: (f.home / "vatpac-statsV2" / "ops" / "smoke.sh").write_text("cat x\n")),  # old unguarded smoke is never run
    ("panels", "FAIL", lambda f: f.write("panels.txt", "panel queries: 70 with rows, 5 empty, 2 errors\n")),
    ("panels", "FAIL", lambda f: f.brk("panels")),
    ("restart_counts", "FAIL", lambda f: f.write("inspect.txt", _inspect(restarts="2"))),
    ("polling_gap", "WARN", lambda f: f.write("gap.txt", "125|25|20|10-10 14:50:00\n")),
    ("polling_gap", "FAIL", lambda f: f.write("gap.txt", "301|25|20|t\n")),
    ("polling_gap", "FAIL", lambda f: f.write("gap.txt", "30|91|20|t\n")),
    ("polling_gap", "FAIL", lambda f: f.brk("gap")),
    ("health", "FAIL", lambda f: f.write("health.txt", "200 True 0 601\n")),
    ("health", "FAIL", lambda f: f.write("health.txt", "200 False 0 5\n")),
    ("health", "FAIL", lambda f: f.write("health.txt", "200 True 3 5\n")),
    ("health", "FAIL", lambda f: f.brk("health")),
    ("identity_flap", "FAIL", lambda f: f.write("flap.txt", "pilot session pairs, same cid+callsign, logon_time within 2 s (identity flap)|0|\ncontroller session pairs, same cid+callsign, logon_time within 2 s (identity flap)|2|2026-10-10 14:59:30+00\n")),
    ("invariant", "FAIL", lambda f: f.invariants({NAMES[1]: "1"})),
    ("invariants", "FAIL", lambda f: f.invariants(rows=NAMES[:1])),  # a short pack is not a pass
    ("invariant", "FAIL", lambda f: f.write("invariants.txt", "".join(f"{n}|0|\n" for n in NAMES[1:]) + f"{NAMES[0]}||\n")),  # NULL count
    ("invariants", "FAIL", lambda f: f.brk("invariants")),
    ("watchdog_log", "FAIL", lambda f: f.write("wdlog.txt", "Traceback (most recent call last):\n")),
    ("watchdog_log", "FAIL", lambda f: f.brk("logs")),
    # expected-warning entries
    ("invariant", "WARN", lambda f: (f.invariants({GAPS: "1"}), _expected(f, f"{GAPS}|1|10-10-2026 15:01|x"))),
    ("invariant", "FAIL", lambda f: (f.invariants({GAPS: "500"}), _expected(f, f"{GAPS}|1|10-10-2026 15:01|x"))),  # above the max count
    ("invariant", "FAIL", lambda f: (f.invariants({GAPS: "1"}), _expected(f, f"{GAPS}|1|10-10-2026 14:59|x"))),  # expired
    ("invariant", "FAIL", lambda f: (f.invariants({NAMES[1]: "1"}), _expected(f, f"{GAPS}|1|31-12-2099 00:00|x"))),  # another check's entry
]
GOOD = ["--verify"]


def _scenario(arg: tuple[int, tuple]) -> str:  # type: ignore[type-arg]
    i, (check, want, mutate) = arg
    with tempfile.TemporaryDirectory() as tmp:
        f = Fake(Path(tmp))
        mutate(f)
        rc, out = f.run(*GOOD)
        try:
            got = line_for(out, check).split()[0]
        except AssertionError:
            got = "MISSING"
        has_fail = any(ln.split()[:1] == ["FAIL"] for ln in out.splitlines())
        if got != want or (rc == 1) != has_fail:
            return f"#{i} {check}: wanted {want}, got {got}, rc={rc}\n{out}"
    return ""


def test_every_check_passes_when_healthy_and_fails_alone() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        f = Fake(Path(tmp))
        rc, out = f.run(*GOOD)
        assert rc == 0 and "RESULT   verify OK" in out, out
        assert len(NAMES) == 19 and "19 invariants checked" in line_for(out, "invariants")
    with ThreadPoolExecutor(16) as pool:
        bad = [r for r in pool.map(_scenario, list(enumerate(SCENARIOS))) if r]
    assert not bad, "\n".join(bad)


def test_verify_refuses_a_checkout_that_is_not_at_the_vm_commit(tmp_path: Path) -> None:
    other = "a" * 40
    f = Fake(tmp_path / "stale")  # local checkout at another commit than the VM checkout
    f.set(FAKE_LOCAL_HEAD=other)
    rc, out = f.run("--verify")
    ln = line_for(out, "checkout_current")
    assert rc == 1 and ln.startswith("FAIL") and other[:10] in ln and FULL[:10] in ln, out
    assert "STALE CHECKOUT" in out and "run it from a checkout of the VM's commit, for example a fresh worktree: git worktree add --detach .worktrees/verify " + FULL[:10] in out
    assert "origin/main" not in out  # main may have moved on since the release: the hint names the VM's commit
    assert "RESULT   verify FAILED" in out
    assert "invariants" not in out and "smoke" not in out  # nothing that reads the local SQL ran
    f = Fake(tmp_path / "same")  # same commit passes
    rc, out = f.run("--verify")
    assert rc == 0 and line_for(out, "checkout_current").startswith("PASS"), out
    f = Fake(tmp_path / "down")  # the VM cannot be read: vm_reachable still FAILs and the comparison does not pass either
    f.set(FAKE_SSH_DOWN="1")
    rc, out = f.run("--verify")
    assert rc == 1 and line_for(out, "vm_reachable").startswith("FAIL") and line_for(out, "checkout_current").startswith("FAIL")


def test_status_names_the_local_checkout(tmp_path: Path) -> None:
    f = Fake(tmp_path)
    f.set(FAKE_LOCAL_HEAD="a" * 40)  # status keeps working from any checkout
    rc, out = f.run("--status")
    assert rc == 0 and "local checkout is at " + "a" * 10 in out, out


def test_usage_and_removed_modes(tmp_path: Path) -> None:
    f = Fake(tmp_path)
    for flag in ("--preflight", "--mark", "--deploy-mark", "--deploy-gate"):
        rc, out = f.run(flag, "f893585")
        assert rc == 2 and "usage: bash ops/deploy.sh --verify | --status | --wait-healthy" in out, (flag, out)
    assert f.run()[0] == 2


# ------------------------------------------------------------------ pure logic, one bash process

def test_pure_logic(tmp_path: Path) -> None:
    def ep(*a: int) -> int:
        return calendar.timegm(a)  # type: ignore[arg-type]

    f = tmp_path / "exp.txt"
    f.write_text("# c\n\nchk a|2|12-10-2026 15:42|r\nchk b|2|bad date|x\nchk c|x|12-10-2026 15:42|r\n", newline="\n")
    ok, ex = ep(2026, 10, 12, 15, 41, 59), ep(2026, 10, 12, 15, 42, 0)
    fp = f.as_posix()
    calls = [
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
    assert got[:2] == [str(ep(2026, 10, 12, 15, 42, 0)), "bad"]
    assert got[2:8] == ["PASS", "WARN", "WARN", "FAIL", "FAIL", "BAD"], got
    assert got[8:13] == ["valid 2 12-10-2026 15:42", "expired 2 12-10-2026 15:42", "malformed", "malformed", "none"], got
    assert got[13].startswith("WARN|chk a = 2 (expected, at most 2")
    assert got[14].startswith("FAIL|chk a = 3 (above the expected maximum of 2")
    assert got[15].startswith("FAIL|") and "expired" in got[15]
    assert got[16] == "PASS|" and got[17].startswith("FAIL|")


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
    assert seen == 0  # both expired entries were removed; update this when a new one is added


# ------------------------------------------------------------------ status, mark, safety

def test_status(tmp_path: Path) -> None:
    f = Fake(tmp_path)
    tree = f.home / "vatpac-statsV2"
    before = {p.relative_to(tree).as_posix() for p in tree.rglob("*")}
    rc, out = f.run("--status")
    assert rc == 0 and "vatpac-collector-1" in out and "vatpac_20261010T020000Z.dump, 1 h old" in out, out
    assert "marker" not in out
    assert {p.relative_to(tree).as_posix() for p in tree.rglob("*")} == before
    f.set(FAKE_SSH_DOWN="1")
    assert f.run("--status")[0] == 1
FORBIDDEN = re.compile(r"docker (run|rm|stop|start|restart|build|kill|volume|system|image)|compose (up|down|restart|build|stop|start|rm|run|kill)|sudo|\btar\b|\brm\b|roles\.sql|\.env\b|printenv|\benv\b|TELEGRAM")


def test_read_only_modes_write_nothing_and_run_nothing_forbidden(tmp_path: Path) -> None:
    f = Fake(tmp_path)
    before = sorted(str(p.relative_to(f.home)) for p in f.home.rglob("*"))
    for mode in (["--verify"], ["--status"], ["--wait-healthy"]):
        f.run(*mode)
    assert sorted(str(p.relative_to(f.home)) for p in f.home.rglob("*")) == before
    log = f.ssh_log() + (f.dir / "docker.log").read_text()
    # The only reads of .env: two keys by name, each through env_key (ops/env_key.sh, sent to the VM as text; issue 212). The key
    # names are not secrets and the values are never printed; any other .env access still trips FORBIDDEN below.
    for key in ("VATPAC_IMAGE_PREFIX", "VATPAC_IMAGE_TAG"):
        read = f"env_key .env {key}"
        assert read in log
        log = log.replace(read, "")
    assert not FORBIDDEN.search(log), FORBIDDEN.search(log)
    assert "ServerAliveInterval=10" in log
    assert not (f.dir / "gh.log").exists()  # deploy.sh makes no gh call
    code = "\n".join(ln for ln in SCRIPT.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"compose (up|down|restart|build|stop)|docker run|sudo|roles\.sql|\bgh\b", code)

def test_ci_checks_deploy_sh_with_bash_not_sh() -> None:
    ci = (REPO / "ops" / "run_checks.sh").read_text(encoding="utf-8")   # the shell syntax check lives in the shared script (issue 241)
    assert "ops/backup/*.sh ops/deploy.sh ops/stale_issues.sh; do bash -n" in ci
    assert '"$f" = ops/deploy.sh ] || [ "$f" = ops/stale_issues.sh ] || sh -n' in ci


# ------------------------------------------------------------------ ops/stale_issues.sh (issue 104)

STALE = STALE_SH = REPO / "ops" / "stale_issues.sh"
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


COMMITS = [
    _commit("a" * 40, "2026-10-10T10:00:00+10:00", "Fix thing (#93)\n\nRefs #94"),  # 00:00Z
    _commit("b" * 40, "2026-10-10T12:00:00+00:00", "Docs for issues 95, 96 and 97"),
    _commit("d" * 40, "2026-10-10T13:00:00+00:00", "Merge pull request #139 from x/dev"),
]


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


# ------------------------------------------------------------------ registry mode (issue 173)

def test_registry_pure_logic() -> None:
    calls = ["registry_mode ''", "registry_mode ghcr.io/x/", "registry_mode ghcr.io/x/", "registry_mode ''",  # the prefix alone decides (the tag argument was ignored and is gone)
             "short_tag " + FULL + "; echo",
             f"head_matches {FULL} {FULL[:7]} && echo m1", f"head_matches {FULL} {FULL} && echo m2",
             f"head_matches {FULL} abcdef0 || echo no1", f"head_matches {FULL} {FULL[:6]} || echo short", f"head_matches {FULL} '' || echo empty2"]
    e = dict(os.environ, DEPLOY_SOURCE_ONLY="1")
    p = subprocess.run([str(BASH), "-c", f'. "{SCRIPT.as_posix()}"; ' + "; ".join(calls)], capture_output=True, text=True, env=e, timeout=60)
    assert p.stdout.split() == ["local", "registry", "registry", "local", TAG7,
                                "m1", "m2", "no1", "short", "empty2"], p.stdout + p.stderr


def test_verify_checks_image_revisions(tmp_path: Path) -> None:
    f = Fake(tmp_path / "ok")
    f.registry()
    rc, out = f.run("--verify")
    assert rc == 0 and line_for(out, "image_revisions").startswith("PASS"), out
    f = Fake(tmp_path / "local")
    assert "local build mode" in line_for(f.run("--verify")[1], "image_revisions")
    cases = {
        "revision": ({"rev": "a" * 40, "only": "backup"}, "revision label"),  # image built from commit A, checkout at B
        "nolabel": ({"rev": "", "only": "watchdog"}, "revision label"),
        "migrate": ({"rev": "a" * 40, "only": "migrate"}, "revision label"),  # the migrate image carries the migrations: it must be this commit's
        "tag": ({"tag": "sha-1234567", "only": "collector"}, "image tag"),
    }
    for name, (kw, needle) in cases.items():
        f = Fake(tmp_path / name)
        f.registry()
        f.write("inspect.txt", _inspect_registry(**kw))
        rc, out = f.run("--verify")
        ln = line_for(out, "image_revisions")
        assert rc == 1 and ln.startswith("FAIL") and needle in ln and kw["only"] in ln, out
    f = Fake(tmp_path / "envtag")  # the checkout is at f893585 but the .env names another tag
    f.registry(tag="sha-1234567")
    rc, out = f.run("--verify")
    assert rc == 1 and ".env tag is" in line_for(out, "image_revisions")


def test_status_shows_images(tmp_path: Path) -> None:
    f = Fake(tmp_path)
    f.registry()
    rc, out = f.run("--status")
    assert rc == 0 and "== images" in out, out
    assert f"/vatpac-collector-1 | {PFX}vatpac-stats-collector:latest | {FULL[:12]}" in out
    assert "image mode: registry" in out and f"checkout HEAD: {FULL[:12]}" in out
    containers = out.split("== containers")[1].split("== images")[0]
    assert all(ln.count("|") == 3 for ln in containers.splitlines()[1:] if ln.startswith("/")), containers  # the old four fields
    f = Fake(tmp_path / "local")
    assert "image mode: local" in f.run("--status")[1]


def test_image_mode_script_prefix_alone_is_registry_mode(tmp_path: Path) -> None:
    env_file = tmp_path / "e.env"
    base = {k: v for k, v in os.environ.items() if not k.startswith("VATPAC_IMAGE")}
    script = (REPO / "ops" / "image_mode.sh").as_posix()

    def mode(text: str | None, **extra: str) -> str:
        if text is not None:
            env_file.write_bytes(text.encode())
        target = env_file.as_posix() if text is not None else (tmp_path / "missing.env").as_posix()
        p = subprocess.run([str(BASH), "-c", f'sh "{script}" "{target}"'], capture_output=True, text=True, env={**base, **extra}, timeout=30)
        assert p.returncode == 0, p.stderr
        return p.stdout.strip()

    assert mode("OTHER=1\n") == "local"
    assert mode("VATPAC_IMAGE_PREFIX=ghcr.io/x/\n") == "registry"  # the prefix alone is registry mode
    assert mode("VATPAC_IMAGE_PREFIX=ghcr.io/x/\nVATPAC_IMAGE_TAG=sha-1\n") == "registry"
    assert mode("VATPAC_IMAGE_TAG=sha-1\n") == "local"  # a tag alone does not switch mode
    assert mode("VATPAC_IMAGE_PREFIX=\nVATPAC_IMAGE_TAG=\n") == "local"  # blank counts as unset
    assert mode("VATPAC_IMAGE_PREFIX=a/\nVATPAC_IMAGE_PREFIX=\n") == "local"  # last assignment wins
    assert mode("VATPAC_IMAGE_TAG=sha-1\r\nVATPAC_IMAGE_PREFIX=a/\r\n") == "registry"
    assert mode(None) == "local"  # no file, no variables
    assert mode('VATPAC_IMAGE_PREFIX="ghcr.io/x/"\n') == "registry"  # quoted value
    assert mode('VATPAC_IMAGE_PREFIX=""\n') == "local"  # empty quotes are unset
    assert mode("export VATPAC_IMAGE_PREFIX=a/\n") == "registry"  # export form
    assert mode("", VATPAC_IMAGE_PREFIX="a/") == "registry"  # the environment counts too
    assert mode("", VATPAC_IMAGE_TAG="t") == "local"
    env_file.write_bytes(b"VATPAC_IMAGE_PREFIX=a/\nVATPAC_IMAGE_TAG=sha-1\n")
    p = subprocess.run([str(BASH), "-c", f'sh "{script}" "{env_file.as_posix()}"'], capture_output=True, text=True, env=base, timeout=30)
    assert p.stdout.strip() == "registry" and "WARNING" in p.stderr and "sha-1" not in p.stderr + p.stdout  # a pin warns loudly, value not printed


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env).stdout.strip()


def _mkcommit(repo: Path, rel: str, text: str) -> str:
    f = repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"change {rel}")
    return _git(repo, "rev-parse", "HEAD")


def _pulled_images_run(tmp_path: Path, repo: Path, labels: dict[str, str], tag: str = "latest", docker_ok: bool = True) -> "subprocess.CompletedProcess[str]":
    """ops/check_pulled_images.sh in a real throwaway git repo with a stub docker: labels maps a service to its image's revision label."""
    b = tmp_path / "bin"
    b.mkdir(parents=True)
    (b / "docker").write_text(
        '#!/bin/sh\n[ -n "$FAKE_DOCKER_DOWN" ] && exit 1\ncase "$1 $2" in\n'
        f'  "compose config") for n in collector watchdog backup migrate loader; do echo "ghcr.io/x/vatpac-stats-$n:{tag}"; done ;;\n'
        '  "image inspect") for l in $(cat "$FAKE_LABELS"); do n="${l%%=*}"; case "$*" in *"vatpac-stats-$n:"*) echo "${l#*=}" ;; esac; done ;;\n'
        "esac\n", newline="\n")
    (b / "docker").chmod(0o755)
    (tmp_path / "labels").write_text("\n".join(f"{k}={v}" for k, v in labels.items()), newline="\n")
    env = dict(os.environ, PATH=f"{b}{os.pathsep}{os.environ['PATH']}", FAKE_LABELS=str(tmp_path / "labels"))
    if not docker_ok:
        env["FAKE_DOCKER_DOWN"] = "1"
    return subprocess.run([str(BASH), str(REPO / "ops" / "check_pulled_images.sh")], capture_output=True, text=True, env=env, cwd=repo, timeout=60)


def _all(rev: str, **odd: str) -> dict[str, str]:
    return {n: odd.get(n, rev) for n in ("collector", "watchdog", "backup", "migrate", "loader")}


def test_make_deploy_stops_when_pulled_images_do_not_match_the_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    released = _mkcommit(repo, "collector/src/app.py", "v1\n")
    r = _pulled_images_run(tmp_path / "t1", repo, _all(released))
    assert r.returncode == 0, r.stderr  # label equals HEAD
    r = _pulled_images_run(tmp_path / "t1s", repo, _all(released[:7]))
    assert r.returncode == 0, r.stderr  # a short label is a prefix of HEAD
    docs_head = _mkcommit(repo, "docs/note.md", "docs only\n")
    r = _pulled_images_run(tmp_path / "t2", repo, _all(released))
    assert r.returncode == 0, r.stderr  # a docs-only commit after the release is accepted
    _mkcommit(repo, "collector/src/app.py", "v2\n")
    r = _pulled_images_run(tmp_path / "t3", repo, _all(released))
    assert r.returncode == 1 and "images for this commit are not published yet: run the release workflow for" in r.stderr  # code changed since
    assert docs_head not in r.stderr
    r = _pulled_images_run(tmp_path / "t4", repo, _all("a" * 40))
    assert r.returncode == 1 and "not published yet" in r.stderr  # label is not an ancestor of HEAD
    r = _pulled_images_run(tmp_path / "t5", repo, _all(released, collector="a" * 40))
    assert r.returncode == 1  # one image odd is enough


def test_migrations_travel_only_in_the_image(tmp_path: Path) -> None:
    """Issue 233: no ./db mount, so the image must be the commit's: a migration or roles.sql change since the release label stops make deploy."""
    import yaml
    for name in ("docker-compose.yml", "docker-compose.dev.yml"):
        cfg = yaml.safe_load((REPO / name).read_text(encoding="utf-8"))
        assert "volumes" not in (cfg.get("services", {}).get("migrate") or {}), name  # nothing mounted over the baked /db
    assert "COPY db /db" in (REPO / "ops" / "migrate" / "Dockerfile").read_text(encoding="utf-8")
    assert "!db" in (REPO / "ops" / "migrate" / "Dockerfile.dockerignore").read_text(encoding="utf-8").split()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    released = _mkcommit(repo, "db/migrations/20260101000000_a.sql", "-- migrate:up\nselect 1;\n")
    for sub, rel in (("mig", "db/migrations/20260101000010_b.sql"), ("roles", "db/roles.sql")):
        _mkcommit(repo, rel, "-- migrate:up\nselect 2;\n")
        r = _pulled_images_run(tmp_path / sub, repo, _all(released))
        assert r.returncode == 1 and "not published yet" in r.stderr, (rel, r.stderr)  # the pulled image lacks this migration
    r = _pulled_images_run(tmp_path / "mig_only_label", repo, _all(_git(repo, "rev-parse", "HEAD"), migrate=released))
    assert r.returncode == 1, r.stderr  # the migrate image alone is old


def test_pulled_images_check_distinct_messages(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    head = _mkcommit(repo, "collector/src/app.py", "v1\n")
    for sub, labels in (("empty", _all(head, migrate="")), ("short", _all(head, backup=head[:6])), ("nonhex", _all(head, watchdog="zzzzzzzz"))):
        r = _pulled_images_run(tmp_path / sub, repo, labels)
        assert r.returncode == 1 and "could not read a usable revision label" in r.stderr and "not published yet" not in r.stderr, (sub, r.stderr)
    r = _pulled_images_run(tmp_path / "down", repo, _all(head), docker_ok=False)
    assert r.returncode == 1 and "could not read a usable revision label" in r.stderr
    _mkcommit(repo, "collector/src/app.py", "v2\n")
    r = _pulled_images_run(tmp_path / "pin", repo, _all(head), tag="sha-" + head[:7])
    assert r.returncode == 1 and "pins a sha- tag" in r.stderr and "Delete that line" in r.stderr
    mk = (REPO / "Makefile").read_text(encoding="utf-8")
    body = mk.split("\ndeploy:")[1].split("\n\n")[0]
    order = [body.index(x) for x in ("git merge --ff-only", "docker compose pull", "check_pulled_images.sh", "docker compose up -d --no-build")]
    assert order == sorted(order) and "--build" not in body.replace("--no-build", "")  # never up before the check, never builds


# ------------------------------------------------------------------ shared helper file (issue 215)

def _source_run(script: Path, flag: str, calls: str, **env: str) -> str:
    e = {k: v for k, v in os.environ.items() if k != "GH_BIN"}
    e.update(env, **{flag: "1"})
    p = subprocess.run([str(BASH), "-c", f'. "{script.as_posix()}"; {calls}'], capture_output=True, text=True, env=e, timeout=60)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def test_gh_bin_in_stale_issues() -> None:
    fallback = 'PATH=/nonexistent; gh_bin'  # builtins only: the function is already defined, so no gh and no other program is found
    assert _source_run(STALE_SH, "STALE_SOURCE_ONLY", "gh_bin", GH_BIN="/x/my-gh") == "/x/my-gh"
    assert _source_run(STALE_SH, "STALE_SOURCE_ONLY", fallback) == "/c/Program Files/GitHub CLI/gh.exe"  # the Windows fallback


def test_stale_issues_reads_the_marker_with_one_pattern(tmp_path: Path) -> None:
    """The release marker is read with one pattern: 7 to 40 hex characters (git resolves a short one)."""
    text = {"full": f"commit={FULL}\nmarked_utc=x\n", "short": "commit=f893585\r\nmarked_utc=x\r\n", "bad": "commit=zzzzzzz\n", "tiny": "commit=f89358\n"}
    want = {"full": FULL, "short": "f893585", "bad": "", "tiny": ""}
    for key, body in text.items():
        f = Fake(tmp_path / key)
        d = f.home / "vatpac-statsV2" / ".deploy"
        d.mkdir()
        (d / "release").write_bytes(body.encode())
        f.set(GH_BIN=str(f.bin / "gh"), REPO="o/r")
        f.write("gitlog.txt", "")
        p = subprocess.run([str(BASH), str(STALE_SH)], capture_output=True, text=True, env=f.env, cwd=REPO, timeout=60)
        out = p.stdout + p.stderr
        if want[key]:
            assert p.returncode == 0 and f"since the release marker {want[key][:10]}" in out, (key, out)
        else:
            assert p.returncode == 2 and "could not read the release marker" in out, (key, out)

# ------------------------------------------------------------------ one .env key reader (issue 212)

def test_vm_env_keys_are_read_with_quotes_export_and_crlf(tmp_path: Path) -> None:
    f = Fake(tmp_path)
    f.dotenv('export VATPAC_IMAGE_PREFIX="ghcr.io/t288matt/"\r\nVATPAC_IMAGE_TAG=\'sha-1234567\'\r\nVATPAC_IMAGE_PREFIX="ghcr.io/t288matt/"\r\n')
    f.write("inspect.txt", _inspect_registry())
    assert "image mode: registry" in f.run("--status")[1]
    f.dotenv("VATPAC_IMAGE_PREFIX=ghcr.io/t288matt/\nVATPAC_IMAGE_PREFIX=\n")  # the last assignment wins: blank means local mode
    assert "image mode: local" in f.run("--status")[1]


# ------------------------------------------------------------------ one wording of the sha- pin warning (issue 213)

def test_the_sha_pin_warning_has_one_wording_in_both_places(tmp_path: Path) -> None:
    sentence = subprocess.run([str(BASH), "-c", f'IMAGE_MODE_SOURCE_ONLY=1 . "{(REPO / "ops" / "image_mode.sh").as_posix()}"; sha_pin_warning'],
                              capture_output=True, text=True, timeout=30).stdout.strip()
    assert sentence.startswith("WARNING: VATPAC_IMAGE_TAG is a sha- pin") and "Delete that line unless this is a deliberate rollback" in sentence
    env = tmp_path / "e.env"
    env.write_text("VATPAC_IMAGE_PREFIX=ghcr.io/x/\nVATPAC_IMAGE_TAG=sha-1234567\n", newline="\n")
    p = subprocess.run([str(BASH), "-c", f'sh "{(REPO / "ops" / "image_mode.sh").as_posix()}" "{env.as_posix()}"'], capture_output=True, text=True, timeout=30)
    assert sentence in p.stderr and "1234567" not in p.stderr
    secrets = subprocess.run([str(BASH), "-c", f'sh "{(REPO / "ops" / "gen_env.sh").as_posix()}" "{(tmp_path / "g.env").as_posix()}"'], capture_output=True, text=True, timeout=30)
    assert secrets.returncode == 0, secrets.stderr
    (tmp_path / "g.env").write_text((tmp_path / "g.env").read_text(encoding="utf-8") + "VATPAC_IMAGE_TAG=sha-1234567\n", newline="\n")
    p = subprocess.run([str(BASH), "-c", f'sh "{(REPO / "ops" / "check_env.sh").as_posix()}" "{(tmp_path / "g.env").as_posix()}"'], capture_output=True, text=True, timeout=30)
    assert "check_env: " + sentence in p.stderr and "1234567" not in p.stderr


# ------------------------------------------------------------------ read once, probe once (issue 214)

def _count(f: Fake, needle: str) -> int:
    return f.ssh_log().count(needle)


def test_each_mode_reads_the_image_keys_head_and_reachability_once(tmp_path: Path) -> None:
    for mode in (["--verify"], ["--status"]):
        f = Fake(tmp_path / mode[0].strip("-"))
        f.registry()
        rc, out = f.run(*mode)
        assert rc == 0, out
        assert _count(f, "env_key .env VATPAC_IMAGE_PREFIX") <= 1 and _count(f, "env_key .env VATPAC_IMAGE_TAG") <= 1, (mode, f.ssh_log())
        assert _count(f, "git rev-parse HEAD") <= 1 and _count(f, "echo reachable") <= 1, (mode, f.ssh_log())
    f = Fake(tmp_path / "verify-counts")
    f.registry()
    f.run("--verify")
    assert _count(f, "env_key .env VATPAC_IMAGE_PREFIX") == 1 and _count(f, "git rev-parse HEAD") == 1 and _count(f, "echo reachable") == 1


def test_read_only_output_is_unchanged_by_the_caching(tmp_path: Path) -> None:
    f = Fake(tmp_path)
    f.registry()
    out = f.run("--status")[1]
    assert "image mode: registry (only the two image keys of the VM's .env are read); checkout HEAD: " + FULL[:12] in out
    out = f.run("--verify")[1]
    assert "registry mode: collector watchdog backup migrate loader run latest, built from the checkout " + FULL[:10] in line_for(out, "image_revisions")


def test_verify_refuses_uncommitted_changes_under_ops(tmp_path: Path) -> None:
    f = Fake(tmp_path / "clean")
    rc, out = f.run("--verify")
    assert rc == 0 and line_for(out, "checkout_clean").startswith("PASS"), out
    f = Fake(tmp_path / "dirty")  # a modified tracked file and untracked files, four in all: at most three are named
    f.set(FAKE_DIRTY=" M ops/audit/invariants.sql\n?? ops/new1.sql\n?? ops/new2.sql\n?? ops/new3.sql")
    rc, out = f.run("--verify")
    ln = line_for(out, "checkout_clean")
    assert rc == 1 and ln.startswith("FAIL") and "ops/audit/invariants.sql" in ln and "ops/new2.sql" in ln and "new3" not in ln, out
    assert "commit, stash or use a clean worktree at the VM's commit" in ln and "invariants " not in out.split("checkout_clean")[1]
    f.set(FAKE_DIRTY="?? ops/scratch.sh")  # untracked alone is enough
    assert f.run("--verify")[0] == 1
    f.set(FAKE_DIRTY="", FAKE_GIT_STATUS_FAIL="1")  # git cannot answer: a FAIL with a clear message, not a crash
    rc, out = f.run("--verify")
    assert rc == 1 and "cannot read the git status" in line_for(out, "checkout_clean")
    f.set(FAKE_DIRTY=" M ops/x.sh", FAKE_GIT_STATUS_FAIL="")  # status works from a dirty checkout
    assert f.run("--status")[0] == 0


# ---------------------------------------------------------------- make deploy on the VM


def test_wait_healthy_passes_then_refuses_when_a_service_stays_unhealthy(tmp_path: Path) -> None:
    f = Fake(tmp_path)
    rc, out = f.run("--wait-healthy")
    assert rc == 0 and line_for(out, "containers_healthy").startswith("PASS"), out
    f.write("inspect.txt", _inspect(health="starting"))
    f.set(DEPLOY_WAIT_S="0")
    rc, out = f.run("--wait-healthy")
    assert rc == 1 and "not healthy after 0 s: db=starting" in out and "marker" not in out, out


def test_wait_healthy_fails_when_the_vm_service_list_is_unreadable_or_empty(tmp_path: Path) -> None:
    for sub, how in (("broken", lambda f: f.brk("services")), ("empty", lambda f: f.write("services.txt", ""))):
        f = Fake(tmp_path / sub)
        how(f)
        f.set(DEPLOY_WAIT_S="0")
        rc, out = f.run("--wait-healthy")
        assert rc == 1 and "could not read the VM's service list" in out, (sub, out)


LOADER_SERVICES = "\n".join(["db", "collector", "grafana", "backup", "watchdog", "vatsim-atc-stats", "migrate"]) + "\n"


def test_wait_healthy_skips_a_service_the_vm_does_not_define_but_fails_on_one_it_does(tmp_path: Path) -> None:
    f = Fake(tmp_path / "new")  # the loader is not in the VM's compose file yet, and its container is unhealthy: not waited for
    f.write("inspect.txt", _inspect(loader_health="unhealthy"))
    rc, out = f.run("--wait-healthy")
    assert rc == 0 and "vatsim-atc-stats" not in line_for(out, "containers_healthy"), out
    f = Fake(tmp_path / "defined")  # the VM defines the loader and its container is unhealthy: the wait fails
    f.write("services.txt", LOADER_SERVICES)
    f.write("inspect.txt", _inspect(loader_health="unhealthy"))
    f.set(DEPLOY_WAIT_S="0")
    rc, out = f.run("--wait-healthy")
    assert rc == 1 and "vatsim-atc-stats=unhealthy" in out, out
    f = Fake(tmp_path / "missing")  # the VM defines the loader and it has no container at all: missing, so the wait fails
    f.write("services.txt", LOADER_SERVICES)
    f.write("inspect.txt", "\n".join(ln for ln in _inspect().splitlines() if "atc-stats" not in ln) + "\n")
    f.set(DEPLOY_WAIT_S="0")
    rc, out = f.run("--wait-healthy")
    assert rc == 1 and "vatsim-atc-stats=missing" in out, out


def test_make_deploy_runs_the_steps_in_order_with_no_gh_and_one_sha() -> None:
    lines = (REPO / "Makefile").read_text(encoding="utf-8").splitlines()
    body = []
    for ln in lines[lines.index("deploy: check-env") + 1:]:
        if not ln.startswith("\t"):
            break
        body.append(ln[1:])
    text = "\n".join(body)
    keys = ["image_mode.sh", "git fetch -q origin main", 'git merge --ff-only "$$sha"', "docker compose pull", "sh ops/check_pulled_images.sh",
            "docker compose up -d --no-build", "DEPLOY_LOCAL=1 bash ops/deploy.sh --wait-healthy", "sh ops/smoke.sh"]
    idx = [text.index(k) for k in keys]
    assert idx == sorted(idx) and len(set(idx)) == len(idx), text
    assert text.count("rev-parse origin/main") == 1, text  # the tip is read once; the fast-forward is to that sha
    assert not re.search(r"\bgh\b|--verify|--mark|--preflight|--deploy-|dump|git pull", text), text  # no gh, no gate, no dump, no marker, no pull of a moved main
    chain = body[body.index("fi") + 1:]  # after the mode check: one shell, so a failure stops the chain
    assert all(b.endswith("&& \\") for b in chain[:-1]) and chain[-1] == "sh ops/smoke.sh", chain
    assert not any(b.lstrip().startswith("-") for b in body), body  # no recipe line ignores its own failure


def test_deploy_local_runs_wait_healthy_without_ssh(tmp_path: Path) -> None:
    """make deploy sets DEPLOY_LOCAL=1: every VM command runs in this checkout, so the ssh stub must never be called."""
    f = Fake(tmp_path)
    f.set(DEPLOY_LOCAL="1")
    rc, out = f.run("--wait-healthy")
    assert rc == 0 and line_for(out, "containers_healthy").startswith("PASS"), out
    assert f.ssh_log() == "", f.ssh_log()
    assert not (f.dir / "gh.log").exists()


def test_reachability_message_is_correct_for_ssh_and_for_local(tmp_path: Path) -> None:
    for local, want in (("", "ssh fakehost"), ("1", "the local shell")):
        p = subprocess.run([str(BASH), "-c", 'DEPLOY_SOURCE_ONLY=1; . "$0"; reach_what', str(SCRIPT)],
                           capture_output=True, text=True, cwd=REPO, timeout=60, env=dict(os.environ, DEPLOY_LOCAL=local, DEPLOY_SSH="fakehost"))
        assert p.stdout.strip() == want, (local, p.stdout, p.stderr)
    f = Fake(tmp_path)
    assert line_for(f.run("--verify")[1], "vm_reachable").endswith("ssh answers")  # remote wording unchanged


def test_stale_issues_reads_the_marker_over_ssh(tmp_path: Path) -> None:
    f = _guard_fake(tmp_path, COMMITS, [(94, "ninety-four", "2026-10-09T23:00:00Z")])
    f.set(GH_BIN=str(f.bin / "gh"), REPO="o/r", DEPLOY_SSH="fakehost")
    p = subprocess.run([str(BASH), str(STALE)], capture_output=True, text=True, env=f.env, cwd=REPO, timeout=60)
    assert f"since the release marker {PREV[:10]}" in p.stdout + p.stderr and "fakehost" in f.ssh_log(), p.stdout + p.stderr
