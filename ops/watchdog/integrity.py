"""Scheduled data-integrity checks for the watchdog (GitHub issue 20, tracker T16, slim version approved 09-10-2026).

Two read-only checks that run on a slow, persisted schedule instead of every cycle:

1. The invariant sweep: ops/audit/invariants.sql (one row per invariant: name, violations, detail), run in one
   repeatable-read READ ONLY transaction as the watchdog's own role. Default every 72 hours.
2. The archive cross-check: sampled compact files of the collector's raw-feed archive (docs/RUNBOOKS/feed-archive.md)
   are recomputed with the independent rules (ops/audit/independent.py, which imports nothing from collector/) and
   compared with what the collector stored for the same update_timestamp. No network call and no secret are needed.
   Default every 14 days.

Both report one level only (warn). The watchdog's Alerter does the de-duplication (one alert when a check turns bad,
one RECOVERED when it is clean again). This module never writes to the database and never imports watchdog.py.
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql as pgsql

OK, WARN = "ok", "warn"
Result = tuple[str, str]

log = logging.getLogger("watchdog.integrity")

INVARIANTS_KEY, CROSSCHECK_KEY = "data_integrity", "feed_crosscheck"
RETRY_AFTER_ERROR_S = 3600.0  # a run that could not complete is tried again after an hour
RECHECK_AFTER_FINDINGS_S = 6 * 3600.0  # a sweep with violations is repeated sooner, so RECOVERED does not wait 3 days
MAX_NAMES_IN_ALERT = 6
SETTLE_S = 120.0  # archive files newer than this are not sampled (the stored snapshot is written first anyway)


@dataclass
class Plan:
    """What the watchdog hands over (built from its Config)."""
    dsn: str
    sql_path: Path
    archive_dir: Path
    config_dir: Path
    invariants_interval_s: float = 72 * 3600.0  # 0 disables
    crosscheck_interval_s: float = 14 * 86400.0  # 0 disables
    samples: int = 24


@dataclass
class Finding:
    name: str
    count: int
    detail: str | None = None


# ---------------------------------------------------------------- the invariant sweep

_NAME_RE = re.compile(r"SELECT\s+'([^']+)'(?:\s+AS\s+check_name)?\s*,\s*count\(\*\)")


def invariant_names(sql: str) -> list[str]:
    """The names of the invariants in the SQL pack, in file order (each is `SELECT '<name>', <count>, <detail>`)."""
    return _NAME_RE.findall(sql)


def executable_sql(sql: str) -> str:
    """The pack without its own BEGIN and COMMIT lines: the caller owns the transaction."""
    return re.sub(r"(?im)^\s*(BEGIN|COMMIT)\b[^;]*;\s*$", "", sql)


def run_invariants(conn: Any, sql: str, timeout_s: int = 120, role: str | None = None) -> list[tuple[str, int, str | None]]:
    """Run the pack on an open, non-autocommit connection. Read only, repeatable read, one statement timeout for the pack."""
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    conn.read_only = True
    with conn.cursor() as cur:
        if role:
            cur.execute(pgsql.SQL("SET LOCAL ROLE {}").format(pgsql.Identifier(role)))
        cur.execute(f"SET LOCAL statement_timeout = '{int(timeout_s)}s'")
        cur.execute(executable_sql(sql))
        rows = [(str(r[0]), int(r[1] or 0), None if r[2] is None else str(r[2])) for r in cur.fetchall()]
    conn.rollback()
    return rows


def evaluate(rows: list[tuple[str, int, str | None]], expected: list[str]) -> tuple[list[Finding], list[str]]:
    """Violations (count above 0) and structural problems: an expected invariant that did not report, an unknown one,
    or one that reported twice. A pack that silently lost an invariant is a failure, not a pass."""
    problems: list[str] = []
    got = [r[0] for r in rows]
    missing = [n for n in expected if n not in got]
    unknown = [n for n in got if n not in expected]
    dup = sorted({n for n in got if got.count(n) > 1})
    if missing:
        problems.append(f"{len(missing)} invariant(s) did not report: {', '.join(missing)}")
    if unknown:
        problems.append(f"{len(unknown)} unexpected row(s): {', '.join(unknown)}")
    if dup:
        problems.append(f"reported twice: {', '.join(dup)}")
    if not expected:
        problems.append("no invariants found in the SQL pack")
    return [Finding(n, c, d) for n, c, d in rows if c > 0], problems


def summarise_invariants(findings: list[Finding], problems: list[str], total: int) -> Result:
    if not findings and not problems:
        return OK, f"all {total} invariants hold"
    parts = [f"{f.name} ({f.count})" for f in findings[:MAX_NAMES_IN_ALERT]]
    if len(findings) > MAX_NAMES_IN_ALERT:
        parts.append(f"and {len(findings) - MAX_NAMES_IN_ALERT} more")
    head = f"{len(findings)} of {total} invariants report violations: " + "; ".join(parts) if findings else ""
    tail = ("; " if findings else "") + "; ".join(problems) if problems else ""
    return WARN, head + tail + ". Details are in the watchdog log; run ops/audit/invariants.sql (docs/RUNBOOKS/alerts.md)"


# ---------------------------------------------------------------- the archive cross-check

def parse_ts(text: str) -> datetime:
    """Feed timestamps carry nanoseconds; Python keeps microseconds (the same trim the audit's compare_samples.py used)."""
    t = re.sub(r"(\.\d{6})\d+", r"\1", text.replace("Z", "+00:00"))
    d = datetime.fromisoformat(t)
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def file_time(path: Path) -> datetime | None:
    """compact/YYYYMMDD/HHMMSS.json.gz -> UTC time, or None for any other name."""
    try:
        return datetime.strptime(path.parent.name + path.name.split(".")[0], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def pick_samples(archive_dir: Path, since: datetime, until: datetime, n: int) -> list[Path]:
    """Up to n compact files in [since, until], evenly spread over the window (oldest first)."""
    found = sorted((t, p) for p in (archive_dir / "compact").glob("*/*.json.gz") if (t := file_time(p)) and since <= t <= until)
    files = [p for _, p in found]
    if n <= 0 or len(files) <= n:
        return files
    if n == 1:
        return [files[len(files) // 2]]
    return [files[round(i * (len(files) - 1) / (n - 1))] for i in range(n)]


@dataclass
class CrossResult:
    compared: int = 0
    skipped: int = 0  # samples older than the last reference-file change (the polygon or whitelist differed then)
    mismatches: list[str] = field(default_factory=list)


def _independent() -> Any:
    try:
        import independent
    except ImportError:  # running from a checkout: ops/audit sits beside ops/watchdog
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "audit"))
        import independent
    return independent


def compare_sample(cur: Any, mine: dict[str, Any]) -> list[str]:
    """Differences between the independent analysis of one archived payload and the stored rows for the same timestamp."""
    ts = parse_ts(mine["update_timestamp"])
    label = ts.strftime("%d-%m-%Y %H:%M:%S UTC")
    cur.execute("SELECT pilots_global, pilots_in_region, controllers_global, controllers_in_region "
                "FROM network_snapshot WHERE observed_at = %s", (ts,))
    row = cur.fetchone()
    if row is None:
        return [f"{label}: no network_snapshot row stored"]
    out = []
    for (what, independent_n), stored in zip((("pilots_global", mine["pilots_global"]), ("pilots_in_region", mine["pilots_inside"]),
                                              ("controllers_global", mine["controllers_global"]),
                                              ("controllers_in_region", mine["controllers_kept"])), row, strict=True):
        if independent_n != stored:
            out.append(f"{label}: {what} independent {independent_n}, stored {stored}")
    cur.execute("SELECT s.cid, s.callsign FROM pilot_position p JOIN pilot_session s ON s.id = p.session_id "
                "WHERE p.observed_at = %s", (ts,))
    stored_keys = {(int(r[0]), str(r[1])) for r in cur.fetchall()}
    mine_keys = {(int(k["cid"]), str(k["callsign"])) for k in mine["kept_pilots"]}
    if stored_keys != mine_keys:
        extra, lack = sorted(stored_keys - mine_keys), sorted(mine_keys - stored_keys)
        out.append(f"{label}: in-region pilot sets differ ({len(lack)} should be stored but are not, e.g. "
                   f"{[c for _, c in lack[:3]]}; {len(extra)} stored but outside the independent rule, e.g. {[c for _, c in extra[:3]]})")
    return out


def run_crosscheck(cur: Any, files: list[Path], config_dir: Path) -> CrossResult:
    ind = _independent()
    ring = ind.load_ring(config_dir / "australian_airspace_polygon.json")
    wl = ind.load_whitelist(config_dir / "controller_callsigns_list.txt")
    cur.execute("SELECT max(changed_at) FROM reference_data_change")
    row = cur.fetchone()
    changed = row[0] if row else None
    res = CrossResult()
    for f in files:
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                mine = ind.analyse(json.load(fh), ring, wl)
            ts = parse_ts(mine["update_timestamp"])
        except Exception as exc:  # a damaged archive file is itself a finding
            res.mismatches.append(f"{f.parent.name}/{f.name}: could not be analysed ({type(exc).__name__})")
            continue
        if changed is not None and ts <= changed:
            res.skipped += 1
            continue
        res.compared += 1
        res.mismatches.extend(compare_sample(cur, mine))
    return res


def summarise_crosscheck(res: CrossResult, requested: int) -> Result:
    if res.compared == 0:
        why = f"{res.skipped} older than the last reference-file change" if res.skipped else "no archive samples found"
        return WARN, (f"cross-check had nothing to compare ({why}); is the feed archive enabled and writing "
                      "(docs/RUNBOOKS/feed-archive.md)?")
    if not res.mismatches:
        return OK, f"{res.compared} archived snapshots match the stored rows ({res.skipped} skipped, before a reference change)"
    shown = "; ".join(res.mismatches[:3])
    more = f" (and {len(res.mismatches) - 3} more)" if len(res.mismatches) > 3 else ""
    return WARN, (f"{len(res.mismatches)} difference(s) in {res.compared} archived snapshots: {shown}{more}. "
                  "Details are in the watchdog log (docs/RUNBOOKS/alerts.md)")


# ---------------------------------------------------------------- schedule (persisted in the watchdog state)

def is_due(entry: dict[str, Any], interval_s: float, now: float) -> bool:
    """Due when never run, when the retry time has passed, or when the last run is older than the interval.
    The entry lives in the status file, so a restart neither re-runs at once nor waits a whole interval again."""
    if interval_s <= 0:
        return False
    last = entry.get("last_run")
    if last is None:
        return True
    retry = entry.get("retry_at")
    if retry is not None:
        return bool(now >= float(retry))
    if float(last) > now + 300:  # the clock moved backwards a long way: do not wait for it
        return True
    return bool(now >= float(last) + interval_s)


def _failing_names(result: dict[str, Any]) -> set[str]:
    return set(result.get("failing", []))


def _record(entry: dict[str, Any], now: float, res: Result, *, failing: list[str], retry_after: float | None) -> None:
    entry.update(last_run=now, level=res[0], detail=res[1], failing=sorted(failing),
                 last_run_utc=datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 retry_at=None if retry_after is None else now + retry_after)


def _guard(entry: dict[str, Any], now: float, what: str, fn: Any) -> list[str]:
    """Run fn() -> (Result, failing names, retry_after) and store the outcome; a crash becomes a warning and a retry."""
    try:
        res, failing, retry_after = fn()
    except Exception as exc:  # never log exception text: it can hold connection details
        log.warning("%s could not run (%s)", what, type(exc).__name__)
        res, failing, retry_after = (WARN, f"{what} could not run ({type(exc).__name__}); retrying in 1 h"), None, RETRY_AFTER_ERROR_S
    before = _failing_names(entry)
    if failing is None:  # keep what the last good run found, so the next good run can tell what is new
        failing = sorted(before)
    _record(entry, now, res, failing=failing, retry_after=retry_after)
    return sorted(set(failing) - before) if before else []


def run_due(plan: Plan, state: dict[str, Any], now: float | None = None) -> tuple[dict[str, Result], list[str]]:
    """Run whichever checks are due and return (current result of each enabled check, notices).
    A notice is a follow-up alert text: violations that are new since the previous sweep while it was already failing."""
    now = time.time() if now is None else now
    sched: dict[str, Any] = state.setdefault("integrity", {})
    notices: list[str] = []
    if plan.invariants_interval_s > 0:
        entry = sched.setdefault(INVARIANTS_KEY, {})
        if is_due(entry, plan.invariants_interval_s, now):
            new = _guard(entry, now, "invariant sweep", lambda: sweep(plan))
            if new:
                notices.append(f"WARN {INVARIANTS_KEY}: new violations since the last sweep: {', '.join(new[:MAX_NAMES_IN_ALERT])}")
    if plan.crosscheck_interval_s > 0:
        entry = sched.setdefault(CROSSCHECK_KEY, {})
        if is_due(entry, plan.crosscheck_interval_s, now):
            _guard(entry, now, "archive cross-check", lambda: crosscheck(plan, now))
    results: dict[str, Result] = {}
    for key, on in ((INVARIANTS_KEY, plan.invariants_interval_s > 0), (CROSSCHECK_KEY, plan.crosscheck_interval_s > 0)):
        e = sched.get(key, {})
        if on and e.get("level"):
            results[key] = (e["level"], f"{e['detail']} (checked {_checked_text(e)})")
    return results, notices


def _checked_text(entry: dict[str, Any]) -> str:
    """When the check last ran, as DD-MM-YYYY HH:MM UTC for people (the status file keeps last_run_utc in ISO form)."""
    try:
        return datetime.fromtimestamp(float(entry["last_run"]), UTC).strftime("%d-%m-%Y %H:%M UTC")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return "at an unknown time"


def find_pack(path: Path) -> Path:
    """The pack beside the watchdog in the image (/app), else ops/audit/invariants.sql when run from a checkout."""
    return path if path.is_file() else Path(__file__).resolve().parent.parent / "audit" / "invariants.sql"


def sweep(plan: Plan) -> tuple[Result, list[str], float | None]:
    sql = find_pack(plan.sql_path).read_text(encoding="utf-8")
    expected = invariant_names(sql)
    with psycopg.connect(plan.dsn) as conn:
        rows = run_invariants(conn, sql)
    findings, problems = evaluate(rows, expected)
    for f in findings:
        log.warning("integrity violation: %s = %s%s", f.name, f.count, f" ({f.detail})" if f.detail else "")
    for p in problems:
        log.warning("integrity pack problem: %s", p)
    res = summarise_invariants(findings, problems, len(expected))
    log.info("invariant sweep: %s", res[1])
    failing = [f.name for f in findings] + (["invariant pack incomplete"] if problems else [])
    return res, failing, RECHECK_AFTER_FINDINGS_S if res[0] != OK else None


def crosscheck(plan: Plan, now: float) -> tuple[Result, list[str], float | None]:
    until = datetime.fromtimestamp(now, UTC) - timedelta(seconds=SETTLE_S)
    since = until - timedelta(seconds=max(plan.crosscheck_interval_s, 86400.0))
    files = pick_samples(plan.archive_dir, since, until, plan.samples)
    with psycopg.connect(plan.dsn) as conn, conn.cursor() as cur:
        conn.read_only = True
        res = run_crosscheck(cur, files, plan.config_dir)
    for m in res.mismatches:
        log.warning("cross-check difference: %s", m)
    out = summarise_crosscheck(res, len(files))
    log.info("archive cross-check: %s", out[1])
    unavailable = res.compared == 0
    return out, (["mismatch"] if res.mismatches else []), RETRY_AFTER_ERROR_S if unavailable else None
