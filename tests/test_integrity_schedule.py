"""Issue 20 (T16): the scheduled integrity checks in ops/watchdog/integrity.py, without a database.

Covers the pure parts: reading the invariant pack, judging its rows, the persisted schedule, the alert behaviour on top
of the watchdog's Alerter, the cross-check comparison, and the wiring (settings, compose, Dockerfile, grants). The checks
firing on deliberately broken data are in tests/integration/test_integrity_checks.py."""
from __future__ import annotations

import gzip
import importlib.util
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "ops" / "watchdog"))
sys.path.insert(0, str(REPO / "ops" / "audit"))
import integrity  # noqa: E402

spec = importlib.util.spec_from_file_location("watchdog_under_test_integrity", REPO / "ops" / "watchdog" / "watchdog.py")
assert spec and spec.loader
wd = importlib.util.module_from_spec(spec)
sys.modules["watchdog_under_test_integrity"] = wd
spec.loader.exec_module(wd)

SQL = (REPO / "ops" / "audit" / "invariants.sql").read_text(encoding="utf-8")
NAMES = integrity.invariant_names(SQL)
HOUR = 3600.0


# ------------------------------------------------------------------ the pack itself
def test_every_select_of_the_pack_is_recognised_as_an_invariant() -> None:
    """If someone adds an invariant in a shape the parser cannot read, the watchdog would never expect it: the number of
    UNION ALL branches plus one must equal the number of names found."""
    body = integrity.executable_sql(SQL)
    branches = len(re.findall(r"^\s*UNION ALL\s*$", body, re.M | re.I)) + 1
    assert len(NAMES) == branches
    assert len(set(NAMES)) == len(NAMES)  # names are the key: a duplicate would hide one


def test_the_pack_has_the_known_invariants() -> None:
    assert len(NAMES) == 20
    assert "identity duplicates (cid, callsign, logon_time)" in NAMES


def test_the_plausibility_invariant_stays_removed() -> None:
    """Issue 243: the pilot_position CHECK constraints (altitude, groundspeed, heading) make it impossible to fire, so it is gone
    from the pack and from the plain wording. ops/audit/independent.py keeps its own limits on purpose."""
    name = "positions outside plausibility caps"
    assert name not in NAMES and name not in SQL
    assert name not in (REPO / "ops" / "watchdog" / "messages.py").read_text(encoding="utf-8")


def test_executable_sql_drops_only_the_transaction_lines() -> None:
    body = integrity.executable_sql(SQL)
    assert "BEGIN ISOLATION" not in body and "COMMIT;" not in body
    assert body.count("count(*)") >= len(NAMES)  # nothing else was cut


def test_the_pack_is_select_only() -> None:
    body = integrity.executable_sql(SQL)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|GRANT)\b", re.sub(r"--.*", "", body), re.I)


# ------------------------------------------------------------------ judging rows
def rows(**over: int) -> list[tuple[str, int, str | None]]:
    return [(n, over.get(n, 0), None) for n in NAMES]


def test_all_zero_is_ok() -> None:
    findings, problems = integrity.evaluate(rows(), NAMES)
    assert findings == [] and problems == []
    assert integrity.summarise_invariants(findings, problems, len(NAMES)) == (integrity.OK, "all 20 invariants hold")


def test_one_violation_is_a_warning_naming_it() -> None:
    findings, problems = integrity.evaluate(rows(**{NAMES[9]: 3}), NAMES)
    level, text = integrity.summarise_invariants(findings, problems, len(NAMES))
    assert level == integrity.WARN and NAMES[9] in text and "(3)" in text and "1 of 20" in text


def test_a_missing_invariant_is_a_failure_not_a_pass() -> None:
    """A pack that lost a row (or a check deleted from the file) must not read as 'all good'."""
    findings, problems = integrity.evaluate(rows()[:-1], NAMES)
    level, text = integrity.summarise_invariants(findings, problems, len(NAMES))
    assert level == integrity.WARN and NAMES[-1] in text and "did not report" in text


def test_an_unknown_or_repeated_row_is_a_failure() -> None:
    _, problems = integrity.evaluate([*rows(), ("something new", 0, None)], NAMES)
    assert any("unexpected" in p for p in problems)
    _, problems = integrity.evaluate([*rows(), (NAMES[0], 0, None)], NAMES)
    assert any("twice" in p for p in problems)


def test_an_empty_pack_is_a_failure() -> None:
    assert integrity.evaluate([], [])[1]


def test_long_lists_are_cut_and_still_short_enough_for_telegram() -> None:
    findings, problems = integrity.evaluate([(n, 9999, "x" * 500) for n in NAMES], NAMES)
    level, text = integrity.summarise_invariants(findings, problems, len(NAMES))
    assert level == integrity.WARN and "and 14 more" in text and len(text) < 1200
    assert "x" * 50 not in text  # details go to the log, not the message


# ------------------------------------------------------------------ the schedule
def test_due_when_never_run_then_not_until_the_interval_has_passed() -> None:
    e: dict[str, Any] = {}
    assert integrity.is_due(e, 72 * HOUR, 1000.0)
    e["last_run"] = 1000.0
    assert not integrity.is_due(e, 72 * HOUR, 1000.0 + 72 * HOUR - 1)
    assert integrity.is_due(e, 72 * HOUR, 1000.0 + 72 * HOUR)


def test_interval_zero_disables() -> None:
    assert not integrity.is_due({}, 0, 1e9)


def test_retry_time_wins_over_the_interval() -> None:
    e = {"last_run": 1000.0, "retry_at": 1000.0 + HOUR}
    assert not integrity.is_due(e, 72 * HOUR, 1000.0 + HOUR - 1)
    assert integrity.is_due(e, 72 * HOUR, 1000.0 + HOUR)


def test_shortening_the_interval_takes_effect_without_waiting_for_the_old_one() -> None:
    assert integrity.is_due({"last_run": 0.0}, 24 * HOUR, 25 * HOUR)


def test_a_clock_that_moved_far_backwards_does_not_block_for_days() -> None:
    assert integrity.is_due({"last_run": 10_000_000.0}, 72 * HOUR, 1000.0)


def plan(**kw: Any) -> integrity.Plan:
    return integrity.Plan(dsn="", sql_path=Path("x"), archive_dir=Path("x"), config_dir=Path("x"), **kw)


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[float]]:
    seen: dict[str, list[float]] = {"sweep": [], "cross": []}
    box: dict[str, Any] = {"sweep": ((integrity.OK, "all 18 invariants hold"), [], None),
                           "cross": ((integrity.OK, "48 archived snapshots match"), [], None)}

    def sweep(p: integrity.Plan) -> Any:
        seen["sweep"].append(1)
        v = box["sweep"]
        if isinstance(v, Exception):
            raise v
        return v

    def cross(p: integrity.Plan, now: float) -> Any:
        seen["cross"].append(now)
        v = box["cross"]
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(integrity, "sweep", sweep)
    monkeypatch.setattr(integrity, "crosscheck", cross)
    seen["box"] = box  # type: ignore[assignment]
    return seen


def test_runs_once_then_waits_the_interval_and_survives_a_restart(calls: dict[str, Any]) -> None:
    state: dict[str, Any] = {}
    p = plan()
    res, _ = integrity.run_due(p, state, now=1000.0)
    assert len(calls["sweep"]) == 1 and len(calls["cross"]) == 1
    assert res["data_integrity"][0] == integrity.OK and "(checked 01-01-1970 00:16 UTC)" in res["data_integrity"][1]  # DD-MM-YYYY, not ISO (issue 102)
    for t in (1060.0, 1000.0 + 71 * HOUR):  # later cycles: nothing runs, the stored result is still reported
        res, _ = integrity.run_due(p, state, now=t)
        assert len(calls["sweep"]) == 1 and "data_integrity" in res
    restarted = json.loads(json.dumps(state))  # the status file is JSON: a restart reloads exactly this
    integrity.run_due(p, restarted, now=1000.0 + 71 * HOUR + 60)
    assert len(calls["sweep"]) == 1  # a restart neither re-runs at once ...
    integrity.run_due(p, restarted, now=1000.0 + 72 * HOUR)
    assert len(calls["sweep"]) == 2 and len(calls["cross"]) == 1  # ... nor skips the due run; cross-check waits 14 days
    integrity.run_due(p, restarted, now=1000.0 + 14 * 86400)
    assert len(calls["cross"]) == 2


def test_each_check_can_be_switched_off(calls: dict[str, Any]) -> None:
    res, _ = integrity.run_due(plan(invariants_interval_s=0), {}, now=1.0)
    assert set(res) == {"feed_crosscheck"} and not calls["sweep"]
    calls["cross"].clear()
    res, _ = integrity.run_due(plan(crosscheck_interval_s=0), {}, now=1.0)
    assert set(res) == {"data_integrity"} and not calls["cross"]


def test_a_run_that_crashes_is_a_warning_and_is_retried_in_an_hour(calls: dict[str, Any]) -> None:
    calls["box"]["sweep"] = RuntimeError("password=hunter2")
    state: dict[str, Any] = {}
    res, _ = integrity.run_due(plan(), state, now=1000.0)
    level, text = res["data_integrity"]
    assert level == integrity.WARN and "RuntimeError" in text and "hunter2" not in text
    integrity.run_due(plan(), state, now=1000.0 + HOUR - 1)
    assert len(calls["sweep"]) == 1
    calls["box"]["sweep"] = ((integrity.OK, "all 18 invariants hold"), [], None)
    res, _ = integrity.run_due(plan(), state, now=1000.0 + HOUR)
    assert len(calls["sweep"]) == 2 and res["data_integrity"][0] == integrity.OK


def test_new_violations_while_already_failing_produce_a_follow_up_notice(calls: dict[str, Any]) -> None:
    calls["box"]["sweep"] = ((integrity.WARN, "1 of 18"), ["a"], integrity.RECHECK_AFTER_FINDINGS_S)
    state: dict[str, Any] = {}
    _, notices = integrity.run_due(plan(), state, now=0.0)
    assert notices == []  # the first alert is the normal state change
    calls["box"]["sweep"] = ((integrity.WARN, "2 of 18"), ["a", "b"], integrity.RECHECK_AFTER_FINDINGS_S)
    _, notices = integrity.run_due(plan(), state, now=6 * HOUR)
    assert len(notices) == 1 and notices[0].startswith("WARN data_integrity:") and "b" in notices[0] and "a," not in notices[0]
    calls["box"]["sweep"] = ((integrity.WARN, "1 of 18"), ["a"], integrity.RECHECK_AFTER_FINDINGS_S)
    _, notices = integrity.run_due(plan(), state, now=12 * HOUR)
    assert notices == []  # a violation ageing out is not news


def test_a_sweep_with_violations_is_repeated_after_six_hours_not_three_days(calls: dict[str, Any]) -> None:
    calls["box"]["sweep"] = ((integrity.WARN, "1 of 18"), ["a"], integrity.RECHECK_AFTER_FINDINGS_S)
    state: dict[str, Any] = {}
    integrity.run_due(plan(), state, now=0.0)
    integrity.run_due(plan(), state, now=6 * HOUR - 1)
    assert len(calls["sweep"]) == 1
    integrity.run_due(plan(), state, now=6 * HOUR)
    assert len(calls["sweep"]) == 2


class Recorder(wd.Alerter):
    def __init__(self, sent: list[str]) -> None:
        self.confirmed, self.pending, self.sent = {}, {}, sent
        self.cfg = type("C", (), {"confirm": 2})()

    def send(self, text: str) -> None:
        self.sent.append(text)


def test_alert_text_dedup_and_recovery_through_the_watchdogs_alerter(calls: dict[str, Any]) -> None:
    """The persisted result is fed to Alerter.update every cycle (60 s): one WARN when it confirms, nothing while it
    stays bad, one RECOVERED when the next sweep is clean."""
    sent: list[str] = []
    a, state = Recorder(sent), {}
    calls["box"]["sweep"] = ((integrity.WARN, "1 of 18 invariants report violations: x (2)"), ["x"], integrity.RECHECK_AFTER_FINDINGS_S)
    now = 0.0
    for _ in range(200):  # 200 cycles, a bit over three hours
        res, _n = integrity.run_due(plan(), state, now=now)
        for k, (lv, d) in res.items():
            a.update(k, lv, d)
        now += 60
    assert [s for s in sent if "data_integrity" in s] == [sent[0]] and len(sent) == 1
    assert sent[0].startswith("WARN data_integrity: 1 of 18 invariants report violations: x (2) (checked ")
    calls["box"]["sweep"] = ((integrity.OK, "all 18 invariants hold"), [], None)
    now = 6 * HOUR
    for _ in range(5):
        res, _n = integrity.run_due(plan(), state, now=now)
        for k, (lv, d) in res.items():
            a.update(k, lv, d)
        now += 60
    assert len(sent) == 2 and sent[1].startswith("RECOVERED data_integrity (was warn): all 18 invariants hold")


# ------------------------------------------------------------------ the cross-check
class Cur:
    def __init__(self, snapshot: tuple[Any, ...] | None, pilots: list[tuple[int, str]]) -> None:
        self.snapshot, self.pilots, self.n = snapshot, pilots, 0

    def execute(self, sql: str, params: Any = None) -> None:
        self.n += 1

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.snapshot

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.pilots)


MINE = {"update_timestamp": "2026-10-07T16:11:19.123456789Z", "pilots_global": 100, "pilots_inside": 3,
        "controllers_global": 10, "controllers_kept": 2,
        "kept_pilots": [{"cid": 1, "callsign": "QFA1"}, {"cid": 2, "callsign": "VOZ2"}, {"cid": 3, "callsign": "JST3"}]}
STORED = (100, 3, 10, 2)
KEYS = [(1, "QFA1"), (2, "VOZ2"), (3, "JST3")]


def test_cross_check_matches_when_everything_agrees() -> None:
    assert integrity.compare_sample(Cur(STORED, KEYS), MINE) == []


@pytest.mark.parametrize("idx,word", [(0, "pilots_global"), (1, "pilots_in_region"), (2, "controllers_global"),
                                      (3, "controllers_in_region")])
def test_each_stored_count_is_compared(idx: int, word: str) -> None:
    bad = list(STORED)
    bad[idx] += 1
    out = integrity.compare_sample(Cur(tuple(bad), KEYS), MINE)
    assert len(out) == 1 and word in out[0] and "07-10-2026 16:11:19 UTC" in out[0]


def test_a_missing_snapshot_row_is_reported() -> None:
    assert "no network_snapshot row" in integrity.compare_sample(Cur(None, []), MINE)[0]


def test_same_count_but_different_pilots_is_reported() -> None:
    out = integrity.compare_sample(Cur(STORED, [(1, "QFA1"), (2, "VOZ2"), (4, "XXX4")]), MINE)
    assert len(out) == 1 and "pilot sets differ" in out[0] and "JST3" in out[0] and "XXX4" in out[0]


def test_timestamp_nanoseconds_are_trimmed_like_the_audit_did() -> None:
    assert integrity.parse_ts("2026-10-07T16:11:19.123456789Z") == datetime(2026, 10, 7, 16, 11, 19, 123456, tzinfo=UTC)
    assert integrity.parse_ts("2026-10-07T16:11:19Z").tzinfo is not None


def touch(root: Path, when: datetime) -> Path:
    p = root / "compact" / f"{when:%Y%m%d}" / f"{when:%H%M%S}.json.gz"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(gzip.compress(b"{}"))
    return p


def test_samples_are_spread_over_the_window_and_limited(tmp_path: Path) -> None:
    t0 = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
    made = [touch(tmp_path, t0 + timedelta(minutes=5 * i)) for i in range(1000)]
    (tmp_path / "compact" / "20261001" / "notes.txt").write_text("x")
    (tmp_path / "full" / "20261001").mkdir(parents=True)
    touch(tmp_path / "full", t0)  # other folders are never sampled
    got = integrity.pick_samples(tmp_path, t0, t0 + timedelta(days=30), 10)
    assert len(got) == 10 and got[0] == made[0] and got[-1] == made[-1] and got == sorted(got)
    assert len(integrity.pick_samples(tmp_path, t0, t0 + timedelta(days=30), 5000)) == 1000
    window = integrity.pick_samples(tmp_path, t0 + timedelta(minutes=50), t0 + timedelta(minutes=70), 99)
    assert [integrity.file_time(p) for p in window] == [t0 + timedelta(minutes=m) for m in range(50, 71, 5)]
    assert integrity.pick_samples(tmp_path / "nowhere", t0, t0, 3) == []


def test_cross_check_summaries() -> None:
    ok = integrity.summarise_crosscheck(integrity.CrossResult(compared=48, skipped=2), 50)
    assert ok[0] == integrity.OK and "48 archived snapshots match" in ok[1]
    bad = integrity.summarise_crosscheck(integrity.CrossResult(compared=48, mismatches=["a", "b", "c", "d", "e"]), 48)
    assert bad[0] == integrity.WARN and "5 difference(s) in 48" in bad[1] and "and 2 more" in bad[1] and "a; b; c" in bad[1]
    for res in (integrity.CrossResult(), integrity.CrossResult(skipped=4)):  # nothing to compare is never a silent pass
        assert integrity.summarise_crosscheck(res, 0)[0] == integrity.WARN


# ------------------------------------------------------------------ settings and wiring
def test_default_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("WATCHDOG_INTEGRITY_INTERVAL_HOURS", "WATCHDOG_CROSSCHECK_INTERVAL_DAYS", "WATCHDOG_CROSSCHECK_SAMPLES"):
        monkeypatch.delenv(k, raising=False)
    p = wd.Config().integrity
    assert p.invariants_interval_s == 72 * 3600 and p.crosscheck_interval_s == 14 * 86400 and p.samples == 24
    assert p.sql_path.name == "invariants.sql" and integrity.find_pack(p.sql_path).is_file()  # falls back to ops/audit in a checkout


def test_settings_can_be_changed_and_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_INTEGRITY_INTERVAL_HOURS", "24")
    monkeypatch.setenv("WATCHDOG_CROSSCHECK_INTERVAL_DAYS", "0")
    monkeypatch.setenv("WATCHDOG_CROSSCHECK_SAMPLES", "7")
    p = wd.Config().integrity
    assert (p.invariants_interval_s, p.crosscheck_interval_s, p.samples) == (24 * 3600, 0, 7)


def test_compose_dockerfile_and_env_example_agree_with_the_code() -> None:
    import yaml

    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["watchdog"]
    env = compose["environment"]
    assert tuple(str(env[k]) for k in ("WATCHDOG_INTEGRITY_INTERVAL_HOURS", "WATCHDOG_CROSSCHECK_INTERVAL_DAYS",
                                       "WATCHDOG_CROSSCHECK_SAMPLES")) == ("72", "14", "24")
    assert "archive:/archive:ro" in compose["volumes"] and "./config:/config:ro" in compose["volumes"]
    assert env["ARCHIVE_DIR"] == "/archive" and env["REFERENCE_CONFIG_DIR"] == "/config"
    assert compose["build"] == {"context": "./ops", "dockerfile": "watchdog/Dockerfile"}
    docker = (REPO / "ops" / "watchdog" / "Dockerfile").read_text(encoding="utf-8")
    for f in ("watchdog/watchdog.py", "watchdog/integrity.py", "audit/independent.py", "audit/invariants.sql", "audit/identity_flap.sql"):
        assert f in docker
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    for k in ("WATCHDOG_INTEGRITY_INTERVAL_HOURS", "WATCHDOG_CROSSCHECK_INTERVAL_DAYS", "WATCHDOG_CROSSCHECK_SAMPLES"):
        assert not re.search(rf"^{k}=", example, re.M)  # policy values live in docker-compose.yml (ADR-034)
    release = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert re.search(r"name: watchdog\s+context: ops\b", release)


def test_the_image_files_are_the_repository_files_and_independent_imports_nothing_from_the_collector() -> None:
    src = (REPO / "ops" / "audit" / "independent.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+(vatpac|collector|shapely)", src, re.M)


def test_migrate_grants_the_watchdog_role_what_the_checks_read() -> None:
    sh = (REPO / "ops" / "migrate" / "migrate.sh").read_text(encoding="utf-8")
    block = re.search(r"GRANT SELECT ON %I TO watchdog_ro.*?ARRAY\[(.*?)\]", sh, re.S)
    assert block
    names = set(re.findall(r"'(\w+)'", block.group(1)))
    tables_read = set(re.findall(r"\b(?:FROM|JOIN)\s+([a-z_]+)", re.sub(r"--.*", "", SQL)))
    needed = {t for t in tables_read if t in {"network_snapshot", "poll_log", "pilot_position", "pilot_session", "controller_session",
                                              "flight_summary", "reference_data_change"}}
    assert needed <= names
    assert {"pilot_position", "flight_summary", "reference_data_change"} <= names
    assert not names & {"app_setting", "controller_rating_event"}  # nothing wider than the checks need
