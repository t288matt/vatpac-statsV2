"""On-demand mutation harness for reviewers (issues 273 and 345). It is NOT run by CI and is not part of the collector size budget.

  python tests/tools/mutation/mutate.py LANES REPO_DIR OUT.json [DB_NAME]

LANES is a string of lane letters: A ingest, B throttle, C watchdog, D drop gate, F flight writer, W workflow/compose/backup/deploy.
Each mutant changes ONE place in a COPY of the repository (REPO_DIR; use a throwaway `git worktree`, never your own checkout), runs
the test stages, and records KILLED (a test failed) or SURVIVED (all green), or INVALID (the pattern is not found exactly once).
The database stages (INT) need a throwaway PostgreSQL 16 built like CI (docs/RUNBOOKS/run-tests.md) with a database whose name ends
_test; the SQL mutants are applied to that database's functions and put back afterwards. Settings come from the environment:
VATPAC_MUT_PORT (55999), VATPAC_MUT_HOST (127.0.0.1), VATPAC_MUT_USER (postgres), VATPAC_MUT_PASSWORD (empty),
VATPAC_MUT_GRAFANA_RO_PASSWORD (ci-grafana-ro-pw), VATPAC_MUT_PSQL (psql on PATH). SURV=previous.json re-runs only the survivors.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import pathlib
lane, repo, out = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
DB = sys.argv[4] if len(sys.argv) > 4 else "vatpac_test"
E = os.environ.get
PSQL = E("VATPAC_MUT_PSQL") or shutil.which("psql") or "psql"
HOST, PORT, USER, PW = E("VATPAC_MUT_HOST", "127.0.0.1"), E("VATPAC_MUT_PORT", "55999"), E("VATPAC_MUT_USER", "postgres"), E("VATPAC_MUT_PASSWORD", "")
env = dict(os.environ, PYTHONPATH="collector/src", VATPAC_TEST_STRICT="1", PGPASSWORD=PW,
           VATPAC_TEST_DSN=f"postgresql://{USER}:{PW}@{HOST}:{PORT}/{DB}",
           VATPAC_TEST_GRAFANA_RO_DSN=f"postgresql://grafana_ro:{E('VATPAC_MUT_GRAFANA_RO_PASSWORD', 'ci-grafana-ro-pw')}@{HOST}:{PORT}/{DB}")
PSQL_ARGS = [PSQL, "-h", HOST, "-p", PORT, "-U", USER, "-d", DB]
UNIT = ["-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-m", "not integration", "collector/tests", "tests",
        "--ignore=tests/test_deploy_script.py", "--ignore=tests/test_backup_script.py"]
INT = ["-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-m", "integration", "tests/integration",
       "--ignore=tests/integration/test_restore_sanity.py", "--ignore=tests/integration/test_logon_whole_seconds_migration.py"]
WDINT = ["-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-m", "integration",
         "tests/integration/test_watchdog_flap.py", "tests/integration/test_integrity_checks.py"]


def run(args, timeout=1800):
    t = time.time()
    p = subprocess.run([sys.executable, *args], cwd=repo, env=env, capture_output=True, text=True, timeout=timeout)
    tail = (p.stdout + p.stderr).strip().splitlines()[-3:]
    return p.returncode, round(time.time() - t, 1), " | ".join(tail)[-300:]


M = []


def py(i, area, f, old, new, stages):
    M.append((i, area, "py", f, old, new, stages))


def sql(i, area, fn, old, new):
    M.append((i, area, "sql", fn, old, new, ["INT"]))


S = "collector/src/vatpac/store.py"
MD = "collector/src/vatpac/model.py"
MN = "collector/src/vatpac/main.py"
FD = "collector/src/vatpac/feed.py"
WD = "ops/watchdog/watchdog.py"
# lane 1: identity and ingest, drop gate, flight writer (need the database)
py("A01", "ingest", S, '_NEWER = "EXCLUDED.last_seen >= ps.last_seen"', '_NEWER = "EXCLUDED.last_seen > ps.last_seen"', ["UNIT", "INT"])
py("A02", "ingest", S, "if not inside_polygon(polygon, p.latitude, p.longitude):", "if inside_polygon(polygon, p.latitude, p.longitude):", ["UNIT", "INT"])
py("A03", "ingest", S, "if c.callsign in whitelist:", "if c.callsign in whitelist or is_near_miss(c.callsign, whitelist, prefixes):", ["UNIT", "INT"])
py("A04", "ingest", S, "    last_seen = GREATEST(ps.last_seen, EXCLUDED.last_seen),\n    plan_changed_at", "    last_seen = LEAST(ps.last_seen, EXCLUDED.last_seen),\n    plan_changed_at", ["UNIT", "INT"])
py("A05", "ingest", S, "    max_gs_kt = GREATEST(ps.max_gs_kt, EXCLUDED.max_gs_kt),\n    max_alt_ft", "    max_gs_kt = LEAST(ps.max_gs_kt, EXCLUDED.max_gs_kt),\n    max_alt_ft", ["UNIT", "INT"])
py("A06", "ingest", S, "psycopg.IntegrityError, psycopg.errors.ProgramLimitExceeded)", "psycopg.IntegrityError)", ["UNIT", "INT"])
py("A07", "ingest", S, "if isinstance(exc, psycopg.errors.CheckViolation) and exc.diag", "if False and exc.diag", ["UNIT", "INT"])
py("A08", "ingest", MD, "except (ArithmeticError, ValueError):  # InvalidOperation", "except ValueError:  # InvalidOperation", ["UNIT", "INT"])
py("A09", "ingest", S, "res.snapshot_inserted = cur.rowcount == 1", "res.snapshot_inserted = cur.rowcount >= 0", ["UNIT", "INT"])
py("A10", "ingest", S, "res.new_near_misses = [r[0] for r in cur.fetchall() if r[1]]", "res.new_near_misses = [r[0] for r in cur.fetchall() if not r[1]]", ["UNIT", "INT"])
py("A11", "ingest", S, 'if (p.departure or "").strip() and (p.arrival or "").strip():', 'if (p.departure or "").strip() or (p.arrival or "").strip():', ["UNIT", "INT"])
py("A12", "ingest", S, "res.pilots_kept -= len(bad)\n            res.rejected_values += len(bad)", "res.pilots_kept -= len(bad)", ["UNIT", "INT"])
py("A13", "ingest", S, "WHERE p.sid IS NULL OR p.val IS DISTINCT FROM v.val", "WHERE p.sid IS NULL", ["UNIT", "INT"])
py("A14", "ingest", MD, "not GS_RANGE[0] <= gs <= GS_RANGE[1]", "not GS_RANGE[0] <= gs < GS_RANGE[1]", ["UNIT", "INT"])
py("A15", "ingest", S, "last_seen = EXCLUDED.last_seen,\n  polls_seen = u.polls_seen + 1", "last_seen = EXCLUDED.last_seen,\n  polls_seen = u.polls_seen", ["UNIT", "INT"])
# drop gate (the live definitions are rewritten by migration 300; patched on the live catalogue text)
DG = "drop_old_pilot_position_partitions(integer,date)"
UR = "unreconciled_partition_sessions(timestamptz,timestamptz)"
sql("D01", "drop gate", DG, "CONTINUE WHEN p_end > cutoff;", "CONTINUE WHEN p_end > cutoff - interval '24 hours';")
sql("D02", "drop gate", DG, "CONTINUE WHEN p_end > cutoff;", "CONTINUE WHEN p_end > cutoff + interval '24 hours';")
sql("D03", "drop gate", DG, "AND s.last_seen >= cutoff;", "AND s.last_seen > cutoff;")  # near-equivalent: the re-check before the drop repeats it
sql("D04", "drop gate", DG, "IF retention_days IS NULL OR retention_days < 1 THEN", "IF retention_days IS NULL OR retention_days < 0 THEN")
sql("D05", "drop gate", UR, "WHERE s.id IS NULL\n        OR s.flown_upto IS NULL OR", "WHERE s.id IS NULL\n        OR")
sql("D06", "drop gate", UR, "COALESCE(cov.n, 0) < pos.n)\n    UNION ALL", "cov.n < pos.n)\n    UNION ALL")
sql("D07", "drop gate", UR, "f.sample_count <> (SELECT", "f.sample_count > (SELECT")
sql("D08", "drop gate", UR, "AND p.observed_at BETWEEN fl.start_time AND fl.end_time)", "AND p.observed_at BETWEEN fl.start_time AND fl.start_time)")
sql("D09", "drop gate", UR, "OR s.flown_upto IS NULL OR s.flown_upto < pos.last_at\n        OR (NULLIF", "OR s.flown_upto IS NULL OR s.flown_upto < pos.last_at - interval '1 hour'\n        OR (NULLIF")
# flight writer
WF = "write_finished_flights(integer)"
sql("F01", "flight writer", WF, "p.gs_kt >= 60", "p.gs_kt > 60")
sql("F02", "flight writer", WF, "OVER w - p.observed_at <= interval '120 minutes'", "OVER w - p.observed_at < interval '120 minutes'")
sql("F03", "flight writer", WF, "o.logon_time - o.prev_end > interval '120 minutes'", "o.logon_time - o.prev_end >= interval '120 minutes'")
sql("F04", "flight writer", WF, "THEN LEAST(extract(epoch FROM lead(p.observed_at) OVER w - p.observed_at), gap_cap)", "THEN extract(epoch FROM lead(p.observed_at) OVER w - p.observed_at)")
sql("F05", "flight writer", WF, "ref - f.newest_seen >= quiet", "ref - f.newest_seen > quiet")
sql("F06", "flight writer", WF, "HAVING ref - min(ps.last_seen) >= quiet", "HAVING ref - min(ps.last_seen) > quiet")
sql("F07", "flight writer", WF, "UPDATE pilot_session ps SET flown_upto = ps.last_seen", "UPDATE pilot_session ps SET flown_upto = ps.first_seen")
sql("F08", "flight writer", WF, "gap_cap := setting_num('airborne_gap_cap_seconds');", "gap_cap := setting_num('airborne_gap_cap_seconds') * 10;")
sql("F09", "flight writer", WF, "(sum(c.air_s) / 60.0)::numeric, 2)", "(sum(c.air_s) / 60.0)::numeric, 0)")
sql("F10", "flight writer", WF, "ref := LEAST(ref, now() + interval '5 minutes');", "ref := GREATEST(ref, now() + interval '5 minutes');")
# lane 2: throttle, pacing, watchdog (unit tests only; survivors are re-run later against the database)
py("B01", "throttle", MN, "UNCHANGED_RETRY_S = 30.0", "UNCHANGED_RETRY_S = 5.0", ["UNIT"])
py("B02", "throttle", FD, "MIN_REQUEST_GAP_S = 30.0", "MIN_REQUEST_GAP_S = 15.0", ["UNIT"])
py("B03", "throttle", MN, "next_at = max(next_at + interval, (self.feed.last_request_at or 0.0) + interval)", "next_at = next_at + interval", ["UNIT"])
py("B04", "throttle", MN, "next_at += interval * math.ceil(", "next_at += interval * math.floor(", ["UNIT"])
py("B05", "throttle", MN, "MAX_MALFORMED_FRACTION, MIN_WHITELIST_FRACTION = 0.2, 0.7", "MAX_MALFORMED_FRACTION, MIN_WHITELIST_FRACTION = 0.2, 0.0", ["UNIT"])
py("B06", "throttle", MN, "err, http, retry_in = str(exc), 429, exc.retry_after", "err, http, retry_in = str(exc), 429, None", ["UNIT"])
py("B07", "throttle", MN, "snap_ts <= self.last_ts", "snap_ts < self.last_ts", ["UNIT"])
py("B08", "throttle", FD, "wait = self.last_request_at + self._min_gap_s - time.monotonic()", "wait = self.last_request_at - time.monotonic()", ["UNIT"])
py("B09", "throttle", FD, "RETRY_AFTER_CAP_S = 120.0", "RETRY_AFTER_CAP_S = 1200.0", ["UNIT"])
py("B10", "throttle", MN, "if ts - datetime.now(UTC) > timedelta(seconds=bound):", "if datetime.now(UTC) - ts > timedelta(seconds=bound):", ["UNIT"])
py("B11", "throttle", MN, "MAX_MALFORMED_FRACTION, MIN_WHITELIST_FRACTION = 0.2, 0.7", "MAX_MALFORMED_FRACTION, MIN_WHITELIST_FRACTION = 1.0, 0.7", ["UNIT"])
py("C01", "watchdog", WD, "if age >= cfg.page_s:", "if age > cfg.page_s:", ["UNIT"])
py("C02", "watchdog", WD, "if age >= cfg.warn_s:", "if age > cfg.warn_s:", ["UNIT"])
py("C03", "watchdog", WD, 'return WARN, f"no {what} yet"', 'return OK, f"no {what} yet"', ["UNIT"])
py("C04", "watchdog", WD, "if not (pilot_pairs or controller_pairs):", "if not (pilot_pairs and controller_pairs):", ["UNIT"])
py("C05", "watchdog", WD, '2 * float(arc.get("compact_every_s") or 300)', '1 * float(arc.get("compact_every_s") or 300)', ["UNIT"])
py("C06", "watchdog", WD, "OUTBOX_WARN_AGE_S = 900.0", "OUTBOX_WARN_AGE_S = 9000.0", ["UNIT"])
py("C07", "watchdog", WD, 'elif health == "unhealthy":', "elif False:", ["UNIT"])
py("C08", "watchdog", WD, "if prev is not None and rc > prev:", "if prev is not None and rc >= prev:", ["UNIT"])
py("C09", "watchdog", WD, "recent[svc] = time.time() + 600", "recent[svc] = time.time() - 600", ["UNIT"])
py("C10", "watchdog", WD, 'ok = status == "exited" and code == 0', 'ok = status == "exited"', ["UNIT"])
py("C11", "watchdog", WD, "OUTBOX_MAX_REJECTIONS = 5", "OUTBOX_MAX_REJECTIONS = 500", ["UNIT"])
py("C12", "watchdog", WD, "OUTBOX_POISON_STATUSES = (400, 413)", "OUTBOX_POISON_STATUSES = (400,)", ["UNIT"])
def fast(files):
    return ["-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-m", "integration"] + ["tests/integration/" + f for f in files]


FAST = {
    "ingest": fast(["test_ingest.py", "test_upserts_and_events.py", "test_hardening_ingest.py", "test_logon_precision_ingest.py", "test_fuzz_ingest.py", "test_review_extra_db.py", "test_qa_gap_decisions.py", "test_replay_real_feed.py", "test_audit_t30.py"]),
    "drop gate": fast(["test_drop_gate.py", "test_partition_drop_locks.py", "test_partitions.py", "test_phase5_remove_old.py", "test_audit_t30.py"]),
    "flight writer": fast(["test_flight_writer.py", "test_flight_writer_qa.py", "test_flight_quiet_per_flight.py", "test_flight_view.py", "test_flight_reconnect_merge.py", "test_qa_gap_decisions.py", "test_audit_t30.py"]),
}

exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mutants_workflow.py"), encoding="utf-8").read())
BAK = ["-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests/test_backup_script.py"]
DEPF = ["-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests/test_deploy_script.py", "-k", "mark or stale or refuse"]
DEP = ["-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "tests/test_deploy_script.py"]
STAGES = {"UNIT": UNIT, "INT": INT, "WDINT": WDINT, "BAK": BAK, "DEPF": DEPF, "DEP": DEP}
todo = [m for m in M if m[0][0] in lane]
if os.environ.get("SURV"):
    surv = {k for k, v in json.load(open(os.environ["SURV"])).items() if v["verdict"] == "SURVIVED"}
    todo = [(i, a, k, f, o, n, ["INT"] if "INT" not in st else st) for (i, a, k, f, o, n, st) in todo if i in surv]
res = json.loads(out.read_text()) if out.exists() else {}


def save():
    out.write_text(json.dumps(res, indent=1))


def psql(text):
    p = subprocess.run([*PSQL_ARGS, "-X", "-q", "-v", "ON_ERROR_STOP=1"], input=text + "\n;\n", capture_output=True, text=True, env=env)
    return p.returncode, p.stderr.strip()[-200:]


for (i, area, kind, f, old, new, stages) in todo:
    if i in res:
        continue
    r = {"area": area, "old": str(old)[:90], "new": str(new)[:90], "stages": []}
    orig = ""
    path = repo / f
    try:
        if kind == "py":
            orig = path.read_bytes().decode("utf-8")
            if isinstance(old, list):  # alternatives for different layouts of one file: the first one found exactly once is used
                old, new = next(((o, n) for o, n in old if orig.count(o) == 1), old[0])
            if orig.count(old) != 1:
                r["verdict"] = f"INVALID (pattern found {orig.count(old)} times)"
                res[i] = r
                save()
                continue
            path.write_bytes(orig.replace(old, new).encode("utf-8"))
        else:
            p = subprocess.run([*PSQL_ARGS, "-XAtc", f"select pg_get_functiondef('{f}'::regprocedure)"], capture_output=True, text=True, env=env)
            orig = p.stdout
            if orig.count(old) != 1:
                r["verdict"] = f"INVALID (pattern found {orig.count(old)} times)"
                res[i] = r
                save()
                continue
            rc, err = psql(orig.replace(old, new))
            if rc:
                r["verdict"] = "INVALID (" + err + ")"
                res[i] = r
                save()
                continue
        verdict = "SURVIVED"
        plan = []
        for st in stages:
            if st == "INT" and area in FAST:
                plan.append(("INT-fast", FAST[area]))
            plan.append((st, STAGES[st]))
        for st, cmd in plan:
            rc, secs, tail = run(cmd)
            r["stages"].append([st, rc, secs, tail])
            if rc != 0:
                verdict = ("KILLED by " + st) if rc == 1 else f"ERROR rc={rc} in {st}"
                break
        r["verdict"] = verdict
    finally:
        if kind == "py":
            path.write_bytes(orig.encode("utf-8"))
        else:
            psql(orig)
    res[i] = r
    save()
    print(i, r["verdict"], flush=True)
