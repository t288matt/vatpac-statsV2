"""Issue 343: ops/check_skips.py fails when the number of skipped tests is not the expected one."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ops"))
import check_skips as cs  # noqa: E402


def junit(passed: int, skipped: int) -> str:
    cases = "".join('<testcase classname="t" name="p%d"/>' % i for i in range(passed))
    cases += "".join('<testcase classname="t" name="s%d"><skipped type="pytest.skip" message="x"/></testcase>' % i for i in range(skipped))
    return f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{cases}</testsuite></testsuites>'


def run(tmp_path: Path, xml: str, *extra: str) -> subprocess.CompletedProcess[str]:
    f = tmp_path / "unit.xml"
    f.write_text(xml, encoding="utf-8")
    return subprocess.run([sys.executable, str(REPO / "ops" / "check_skips.py"), str(f), *extra], capture_output=True, text=True)


def test_the_expected_number_is_ok(tmp_path: Path) -> None:
    assert cs.count_skipped(junit(5, cs.EXPECTED_UNIT_SKIPS)) == cs.EXPECTED_UNIT_SKIPS
    assert run(tmp_path, junit(5, cs.EXPECTED_UNIT_SKIPS)).returncode == 0


def test_one_extra_skip_and_one_fewer_both_fail(tmp_path: Path) -> None:
    assert run(tmp_path, junit(5, cs.EXPECTED_UNIT_SKIPS + 1)).returncode == 1
    assert run(tmp_path, junit(5, cs.EXPECTED_UNIT_SKIPS - 1)).returncode == 1


def test_an_unreadable_or_missing_file_fails_and_the_number_can_be_overridden(tmp_path: Path) -> None:
    assert run(tmp_path, "not xml").returncode == 1
    assert subprocess.run([sys.executable, str(REPO / "ops" / "check_skips.py"), str(tmp_path / "none.xml")],
                          capture_output=True).returncode == 1
    assert run(tmp_path, junit(5, 24), "--expected", "24").returncode == 0
    assert run(tmp_path, junit(5, 24), "--expected", "x").returncode == 2
