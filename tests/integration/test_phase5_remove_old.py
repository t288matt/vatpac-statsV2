"""Issue #89 phase 5 (#141, NOT YET DEPLOYED): the old flight machinery is removed and flight_record becomes flight_summary.

Migration under test: 20261013000300_remove_pieces_rename_flight_summary.sql.

Like test_logon_whole_seconds_migration.py, the database tests build their own databases: a template from every migration BEFORE the phase 5
one, then one copy per test, in which a fixture is loaded, the old state is captured, the migration is applied (up half only, as dbmate does)
and the new state is captured. They need the superuser DSN of the throwaway server (VATPAC_TEST_DSN); the databases they create end in _test.

  * Old against new, across the rename: every view that reads the flight table (flight, flight_ext, movements, cohorts, monday_attendance and
    anything else that depends on them) returns the same rows before and after (count and a hash of the rows).
  * The re-created function bodies are the old ones with ONLY the table name changed (writer, reconciliation helper, drop function).
  * Nothing old is left: no relation, function, grant, comment or file names it (the file scan has a short list of intended exceptions).
"""
from __future__ import annotations

import re
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

# Every test in this file is selected by the integration job (-m integration): the static checks below need no database, but they sat
# unmarked under tests/integration, where the unit job skips them (no database) and the integration job deselects them, so they never ran in CI.
pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
MIG_DIR = REPO / "db" / "migrations"
NEW = "20261013000300_remove_pieces_rename_flight_summary.sql"
TMPL = "vatpac_p5_tmpl_test"
OLD_WORDS = re.compile(r"\b(flight_record|flight_part|flight_legacy|refresh_flight_summary(?:_sessions)?)\b")


def up_half(path: Path) -> str:
    return re.split(r"^-- migrate:down", path.read_text(encoding="utf-8"), maxsplit=1, flags=re.M)[0]


# =============================================================================== no database: what the files say
def test_the_migration_drops_without_cascade_in_the_safe_order_and_is_forward_only() -> None:
    text = (MIG_DIR / NEW).read_text(encoding="utf-8")
    up = up_half(MIG_DIR / NEW)
    code = "\n".join(line for line in up.splitlines() if not line.lstrip().startswith("--"))
    assert not re.search(r"(?i)\bcascade\b", code)                                  # requirement 2: never CASCADE
    order = [code.index(s) for s in ("DROP VIEW IF EXISTS flight_legacy", "DROP VIEW flight_part",
                                      "DROP FUNCTION IF EXISTS refresh_flight_summary(interval)",
                                      "DROP FUNCTION IF EXISTS refresh_flight_summary_sessions(bigint[], boolean)",
                                      "ALTER TABLE flight_summary RENAME TO flight_summary_old_p5",
                                      "ALTER TABLE flight_record RENAME TO flight_summary", "DROP TABLE flight_summary_old_p5;")]
    assert order == sorted(order)         # legacy -> part -> functions -> pieces renamed aside -> finished-flights rename -> the pieces dropped
    # lock order (Opus review 13-10-2026): the one statement that locks pilot_session (the DROP of the pieces table, which owns the foreign
    # key) is the LAST statement, after every statement that can wait for a reader of the flight view
    statements = [x.strip() for x in code.split(";") if x.strip()]
    assert statements[-1] == "DROP TABLE flight_summary_old_p5"
    assert [x for x in statements if re.match(r"(?i)DROP\s+TABLE", x)] == ["DROP TABLE flight_summary_old_p5"]
    assert "SET LOCAL lock_timeout = '5s'" in up
    assert "Forward-only" in text.split("-- migrate:down")[1]
    names = sorted(p.name for p in MIG_DIR.glob("*.sql"))
    assert names[-1] == NEW or names.index(NEW) > names.index("20261013000200_switch_flight_views.sql")
    assert names.index(NEW) > names.index("20261013000210_drop_gate_reconciles_flights.sql")


def test_the_migration_renames_the_key_the_check_and_the_four_indexes() -> None:
    up = up_half(MIG_DIR / NEW)
    for old, new in (("cid", "cid"), ("start_time", "start_time"), ("end_time", "end_time"), ("logon_time", "logon_time")):
        assert f"ALTER INDEX IF EXISTS flight_record_{old}_idx" in up and f"RENAME TO flight_summary_{new}_idx" in up
    assert "RENAME CONSTRAINT" in up and "flight_summary_pkey" in up and "flight_summary_check" in up


def test_the_migration_regrants_the_writer_to_the_collector_and_refuses_to_finish_with_old_names() -> None:
    up = up_half(MIG_DIR / NEW)
    assert "GRANT EXECUTE ON FUNCTION write_finished_flights(int) TO collector" in up       # requirement 1
    assert "functions still name an old object" in up and "views still name an old object" in up


def test_roles_sql_grants_the_writer_and_no_dropped_function() -> None:
    roles = (REPO / "db" / "roles.sql").read_text(encoding="utf-8")
    code = "\n".join(line for line in roles.splitlines() if not line.lstrip().startswith("--"))
    assert "write_finished_flights(int)" in code                                    # requirement 3: a stale grant aborts the migrate job
    assert not OLD_WORDS.search(roles), OLD_WORDS.findall(roles)


def test_the_collector_calls_the_writer_with_a_small_batch_and_contains_a_cancel() -> None:
    src = (REPO / "collector" / "src" / "vatpac" / "maintenance.py").read_text(encoding="utf-8")
    assert "SELECT write_finished_flights(%s)" in src and "FLIGHT_WRITE_BATCH = 50" in src
    assert "QueryCanceled" in src and "FLIGHT_REFRESH_WINDOW" not in src and not OLD_WORDS.search(src)
    assert "refresh_flight_summary" not in (REPO / "collector" / "src" / "vatpac" / "main.py").read_text(encoding="utf-8")


def test_the_watchdog_has_no_check_for_the_pieces_and_counts_21() -> None:
    sql = (REPO / "ops" / "audit" / "invariants.sql").read_text(encoding="utf-8")
    assert len(re.findall(r"SELECT +'[^']+'( +AS +check_name)? *, *count\(", sql)) == 21        # requirement 4
    assert "rows missing for sessions older than 12 h" not in sql and "final or settled" not in sql
    msg = (REPO / "ops" / "watchdog" / "messages.py").read_text(encoding="utf-8")
    assert "rows missing for sessions older than 12 h" not in msg and "_MAINTENANCE" not in msg


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout
        return [f for f in out.splitlines() if f]
    except (OSError, subprocess.CalledProcessError):
        return [str(p.relative_to(REPO)).replace("\\", "/") for p in REPO.rglob("*")
                if p.is_file() and ".git" not in p.parts and ".worktrees" not in p.parts]


# Files that intentionally keep an old name, and why. Migrations are history (replayed in order on a fresh install).
ALLOWED_OLD_NAMES = {
    "ops/backup/restore-lib.sh": "reads a pre-phase-5 dump, where the finished flights are still called flight_record (requirement 6)",
    "tests/test_phase3_switch.py": "pins the phase 3 migration and the validator's refusal list",
    "tests/integration/test_phase5_remove_old.py": "this file",
    "tests/integration/test_restore_sanity.py": "builds a pre-phase-5 dump",
    "tests/integration/test_logon_whole_seconds_migration.py": "pinned to migration 100, before the pieces were removed",
    "grafana/validate_dashboards.py": "the refusal list: no panel may read a name that history still creates",
    "tests/test_data_dictionary_tests.py": "asserts the old names are gone",
    "docs/DATA_DICTIONARY.md": "documents what migration 20261013000300 removes and the production names until it deploys",
    "docs/ARCHITECTURE.md": "says which name production still uses until the phase 5 deploy",
    "docs/CHART_DEFINITIONS.md": "says which name production still uses until the phase 5 deploy",
    "docs/DEFINITIONS.md": "says which name production still uses until the phase 5 deploy",
    "docs/PENDING_DOC_CHANGES.md": "lists the doc changes to make when phases 4 and 5 deploy",
    "docs/RUNBOOKS/drop-gate-rehearsal.md": "rehearses on a restore of a live dump, which has the old names until the phase 5 deploy",
    "docs/RUNBOOKS/integrity-checks.md": "says which name production still uses until the phase 5 deploy",
    "SPEC.md": "says which name production still uses until the phase 5 deploy",
    "docs/CONFIG_MAP.md": "says which name production still uses until the phase 5 deploy",
    "tests/integration/test_drop_gate.py": "asserts the old names are gone",
    "tests/integration/test_roles.py": "asserts the old functions are gone",
}
HISTORY_DOCS = {  # dated records of earlier phases, audits and decisions: they describe the state at that time (each carries a note)
    "CHANGELOG.md", "docs/DESIGN_89_OPTIONS.md", "docs/TEST_AUDIT.md", "docs/DOC_AUDIT.md", "docs/DATA_INTEGRITY_AUDIT.md",
    "docs/CONFORMANCE_89_PHASE1.md", "docs/CONFORMANCE_89_PHASE3.md", "docs/CONFORMANCE_89_PHASE4.md",
    "docs/CONFORMANCE_89_PHASE5.md", "docs/RESTORE_DRILL.md", "docs/DECISIONS.md", "docs/HANDOVER.md", "docs/TRACKER.md",
    "docs/FLIGHT_VIEW_SCALING.md", "docs/RUNBOOKS/deploy-to-vm.md", "docs/reference/v1_metabase_cards.json",
    "docs/t14_notes/pilots.md",
}


def test_no_code_test_grant_or_watchdog_file_names_an_old_object_outside_the_listed_exceptions() -> None:
    """Requirement 7. Migrations are history and skipped. The word flight_summary is NOT an old name any more (it is the new table)."""
    hits: dict[str, list[str]] = {}
    for f in _tracked_files():
        if f.startswith("db/migrations/") or f.startswith("docs/reviews/") or f.startswith("docs/qa_89/") or f in ALLOWED_OLD_NAMES or f in HISTORY_DOCS:   # docs/qa_89: QA tools and dated records that compare the old model with the new one
            continue
        if not f.endswith((".py", ".sql", ".sh", ".json", ".md", ".yml", ".yaml", ".toml", ".txt", ".ini", ".cfg", ".env.example")):
            continue
        try:
            text = (REPO / f).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found = sorted(set(OLD_WORDS.findall(text)))
        if found:
            hits[f] = found
    assert hits == {}, hits


def test_the_old_pieces_columns_are_named_nowhere_in_the_collector_or_the_watchdog() -> None:
    for sub in ("collector/src", "ops/watchdog"):
        for p in (REPO / sub).rglob("*.py"):
            t = p.read_text(encoding="utf-8")
            assert not re.search(r"\b(gap_segment|span_part)\b", t), p


# =============================================================================== database: old against new across the rename
@pytest.fixture(scope="module")
def template(dsn: str):
    with psycopg.connect(make_conninfo(dsn, dbname="postgres"), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TMPL} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TMPL}")
        with psycopg.connect(make_conninfo(dsn, dbname=TMPL), autocommit=True) as c:
            for f in sorted(MIG_DIR.glob("*.sql")):
                if f.name < NEW:
                    c.execute(up_half(f))
        yield TMPL
        admin.execute(f"DROP DATABASE IF EXISTS {TMPL} WITH (FORCE)")


@pytest.fixture()
def db(dsn: str, template: str):
    name = f"vatpac_p5_{uuid.uuid4().hex[:8]}_test"
    with psycopg.connect(make_conninfo(dsn, dbname="postgres"), autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name} TEMPLATE {template}")
        try:
            with psycopg.connect(make_conninfo(dsn, dbname=name), autocommit=True, row_factory=dict_row) as c:
                yield c
        finally:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


MON = datetime(2026, 10, 5, tzinfo=UTC)          # a Monday: the attendance view has a row


def _session(c, cid, callsign, first, n, *, dep="YSSY", arr="YMML", logon=None, gs=250, **kw) -> int:
    last = first + timedelta(seconds=15 * (n - 1))
    cols = {"cid": cid, "callsign": callsign, "logon_time": (logon or first).replace(microsecond=0), "first_seen": first,
            "last_seen": last, "departure": dep, "arrival": arr, "name": f"Pilot {cid}", "home_icao": "YSSY", "server": "SYD1",
            "aircraft_icao": "B738", **kw}
    sid = c.execute(f"INSERT INTO pilot_session ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
                    list(cols.values())).fetchone()["id"]
    with c.cursor() as cur:
        cur.executemany("INSERT INTO pilot_position (observed_at, session_id, lat, lon, alt_ft, gs_kt, hdg) "
                        "VALUES (%s, %s, -33.9, 151.2, 5000, %s, 90)",
                        [(first + timedelta(seconds=15 * i), sid, gs) for i in range(n)])
    return sid


def _load(c) -> None:
    for d in (0, 1, 2):
        c.execute("SELECT ensure_pilot_position_partitions(0, %s)", ((MON + timedelta(days=d)).date(),))
    c.execute("INSERT INTO network_snapshot VALUES (%s, 1, 1, 1, 1)", (MON,))
    t = MON + timedelta(hours=8)
    _session(c, 4101, "QFA101", t, 40)
    _session(c, 4102, "VOZ102", t + timedelta(hours=1), 60, dep="YBBN", arr="YPPH", aircraft_icao="A320")
    _session(c, 4103, "JST103", t + timedelta(hours=3), 20, dep="YMML", arr="YSCB")                 # a reconnect 20 minutes later: one flight
    _session(c, 4103, "JST103", t + timedelta(hours=3, minutes=25), 20, dep="YMML", arr="YSCB")
    _session(c, 4104, "ZZZ104", t + timedelta(hours=4), 30, arr="ZZZZ")
    _session(c, 4105, "CIR105", t + timedelta(hours=5), 30, arr="YSSY")
    _session(c, 4106, "BAW106", t + timedelta(hours=6), 30, dep="EGLL", arr="KJFK", home_icao="EGLL")
    _session(c, 4107, "NOP107", t + timedelta(hours=7), 30, dep=None, arr=None)                     # no plan: no flight
    _session(c, 4108, "RAAF08", t + timedelta(days=1), 40, dep="YSRI", arr="YSRI", aircraft_icao="F16", remarks="OPR/RAAFVIRTUAL.ORG")
    _session(c, 1104109, "XYZ109", t + timedelta(days=1, hours=2), 40)                              # another joining cohort
    c.execute("INSERT INTO network_snapshot VALUES (%s, 1, 1, 1, 1)", (MON + timedelta(days=5),))   # the data ends days later: all quiet
    while c.execute("SELECT write_finished_flights(500) AS n").fetchone()["n"]:
        pass
    c.execute("SELECT refresh_flight_summary(NULL)")                                                # fill the old pieces table too, so the drop has rows


def _views(c) -> list[str]:
    """Every public view that reads the flight table, directly or through another view."""
    return [r["relname"] for r in c.execute(
        """
        WITH RECURSIVE dep(oid) AS (
            SELECT c.oid FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relname = 'flight'
            UNION
            SELECT rw.ev_class
              FROM dep
              JOIN pg_depend d ON d.refobjid = dep.oid AND d.classid = 'pg_rewrite'::regclass
              JOIN pg_rewrite rw ON rw.oid = d.objid AND rw.ev_class <> dep.oid)
        SELECT c.relname FROM dep JOIN pg_class c ON c.oid = dep.oid WHERE c.relkind = 'v' ORDER BY 1
        """).fetchall()]


def _capture(c, views: list[str]) -> dict[str, tuple[int, str]]:
    out = {}
    for v in views:
        r = c.execute(f"SELECT count(*) AS n, md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t::text), '')) AS h FROM {v} t").fetchone()  # noqa: S608
        out[v] = (r["n"], r["h"])
    return out


FUNCS = ("write_finished_flights(int)", "unreconciled_partition_sessions(timestamptz, timestamptz)",
         "drop_old_pilot_position_partitions(int, date)")


def _defs(c) -> dict[str, tuple[str, str | None]]:
    return {f: (c.execute("SELECT pg_get_functiondef(%s::regprocedure) AS d", (f,)).fetchone()["d"],
                c.execute("SELECT proacl::text AS a FROM pg_proc WHERE oid = %s::regprocedure", (f,)).fetchone()["a"]) for f in FUNCS}


def test_every_flight_view_returns_the_same_rows_before_and_after_the_rename(db) -> None:
    _load(db)
    views = _views(db)
    assert {"flight", "flight_ext", "movements", "cohorts", "monday_attendance"} <= set(views), views
    before = _capture(db, views)
    assert before["flight"][0] == 8 and before["monday_attendance"][0] >= 1          # 10 sessions: 8 flights (one has no plan, one reconnect joined)
    db.execute(up_half(MIG_DIR / NEW))
    assert _views(db) == views                                                       # the dependants survived (no CASCADE, none dropped)
    assert _capture(db, views) == before


def test_the_old_objects_are_gone_and_the_new_names_are_in_place(db) -> None:
    _load(db)
    db.execute(up_half(MIG_DIR / NEW))
    for rel in ("flight_record", "flight_part", "flight_legacy", "flight_record_pkey", "flight_record_cid_idx"):
        assert db.execute("SELECT to_regclass(%s) AS r", (rel,)).fetchone()["r"] is None, rel
    assert db.execute("SELECT count(*) AS n FROM pg_proc WHERE proname LIKE 'refresh_flight_summary%'").fetchone()["n"] == 0
    cols = {r["column_name"] for r in db.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'flight_summary'").fetchall()}
    assert {"session_ids", "written_at"} <= cols and not {"final", "gap_segment", "span_part", "computed_at"} & cols
    assert {r["indexname"] for r in db.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'flight_summary'").fetchall()} == {
        "flight_summary_pkey", "flight_summary_cid_idx", "flight_summary_start_time_idx", "flight_summary_end_time_idx",
        "flight_summary_logon_time_idx"}
    assert {r["conname"] for r in db.execute("SELECT conname FROM pg_constraint WHERE conrelid = 'flight_summary'::regclass").fetchall()} == {
        "flight_summary_pkey", "flight_summary_check"}
    assert db.execute("SELECT count(*) AS n FROM pg_constraint WHERE confrelid = 'flight_summary'::regclass").fetchone()["n"] == 0
    # no comment, function body or view definition names an old object (the migration checks this itself; here it is checked again)
    assert [r["d"] for r in db.execute("SELECT description AS d FROM pg_description WHERE description ~ '\\mflight_record\\M'").fetchall()] == []
    assert db.execute("SELECT count(*) AS n FROM pg_proc WHERE pronamespace = 'public'::regnamespace AND prosrc ~ '\\mflight_record\\M'").fetchone()["n"] == 0


def test_the_recreated_functions_are_the_old_ones_with_only_the_table_name_changed(db) -> None:
    """Requirement 5: the phase 4 gate logic is byte for byte the same text apart from the table name; owner, ACL and search_path stay."""
    _load(db)
    before = _defs(db)
    db.execute(up_half(MIG_DIR / NEW))
    after = _defs(db)
    for f in FUNCS:
        expected = re.sub(r"\bflight_record\b", "flight_summary", before[f][0])
        assert after[f][0] == expected, f
        # grants unchanged, except that the migration adds EXECUTE on the writer for the collector (requirement 1)
        def acl(a: str | None) -> set[str]:
            return {x for x in (a or "").strip("{}").split(",") if x}
        extra = {x for x in acl(after[f][1]) - acl(before[f][1])}
        assert acl(before[f][1]) <= acl(after[f][1]), f
        assert extra == set() or (f == "write_finished_flights(int)" and all(x.startswith("collector=X/") for x in extra)), (f, extra)
        assert "SET search_path TO 'public', 'pg_temp'" in after[f][0] or "search_path=public, pg_temp" in after[f][0]
        assert "SECURITY DEFINER" in after[f][0]
    assert before["write_finished_flights(int)"][0] != after["write_finished_flights(int)"][0]
    assert before["unreconciled_partition_sessions(timestamptz, timestamptz)"][0] != after["unreconciled_partition_sessions(timestamptz, timestamptz)"][0]


def test_grants_on_the_table_follow_the_rename(db) -> None:
    _load(db)
    role = f"p5_probe_{uuid.uuid4().hex[:8]}"
    db.execute(f"CREATE ROLE {role} NOLOGIN")
    try:
        db.execute(f"GRANT SELECT ON flight_record TO {role}")
        db.execute(up_half(MIG_DIR / NEW))
        assert db.execute("SELECT has_table_privilege(%s, 'flight_summary', 'SELECT') AS ok", (role,)).fetchone()["ok"]
    finally:
        db.execute(f"DROP OWNED BY {role}")
        db.execute(f"DROP ROLE {role}")


def test_the_writer_and_the_drop_function_work_after_the_rename(db) -> None:
    _load(db)
    n_before = db.execute("SELECT count(*) AS n FROM flight_record").fetchone()["n"]
    db.execute(up_half(MIG_DIR / NEW))
    assert db.execute("SELECT count(*) AS n FROM flight_summary").fetchone()["n"] == n_before == 8
    assert db.execute("SELECT write_finished_flights(500) AS n").fetchone()["n"] == 0            # nothing left to write: idempotent
    t = MON + timedelta(days=6)                                                                  # a new flight after the rename is written
    db.execute("SELECT ensure_pilot_position_partitions(0, %s)", (t.date(),))
    _session(db, 4120, "NEW120", t, 30)
    db.execute("INSERT INTO network_snapshot VALUES (%s, 1, 1, 1, 1)", (t + timedelta(days=1),))
    assert db.execute("SELECT write_finished_flights(500) AS n").fetchone()["n"] == 1
    assert db.execute("SELECT count(*) AS n FROM flight WHERE cid = 4120").fetchone()["n"] == 1
    # the drop gate (phase 4 logic) still reconciles against the renamed table: the processed, covered days are dropped, flights stay
    dropped = 0
    for _ in range(20):                                                                          # phase 4 drops ONE day per call
        n = db.execute("SELECT drop_old_pilot_position_partitions(90, %s) AS n", ((MON + timedelta(days=400)).date(),)).fetchone()["n"]
        dropped += n
        if n == 0:
            break
    assert dropped >= 3
    assert db.execute("SELECT count(*) AS n FROM flight_summary").fetchone()["n"] == 9


def test_the_migration_refuses_to_run_before_phase_3_and_changes_nothing(dsn: str, template: str) -> None:
    """If the readers still use the pieces columns (phase 3 not applied), the pre-check raises before anything is dropped."""
    name = f"vatpac_p5_{uuid.uuid4().hex[:8]}_test"
    with psycopg.connect(make_conninfo(dsn, dbname="postgres"), autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name} TEMPLATE {template}")
        try:
            with psycopg.connect(make_conninfo(dsn, dbname=name), autocommit=True, row_factory=dict_row) as c:
                c.execute("DROP VIEW flight CASCADE")                                                   # and its four dependants
                c.execute("CREATE VIEW flight AS SELECT 1 AS session_id, 1 AS gap_segment")                # the old shape
                with pytest.raises(psycopg.errors.RaiseException):
                    c.execute(up_half(MIG_DIR / NEW))
                assert c.execute("SELECT to_regclass('flight_part') IS NOT NULL AS ok").fetchone()["ok"]
        finally:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def test_a_reader_of_the_flight_view_does_not_make_a_poll_write_wait(db, dsn: str) -> None:
    """Opus review of 13-10-2026 (lock order). While a Grafana-style reader holds the flight view, the migration waits at the rename of the
    finished-flights table. It must hold NO exclusive lock on pilot_session during that wait, so the collector's writes are not delayed
    (the DROP of the pieces table, the only statement that locks pilot_session, comes last)."""
    import threading
    import time

    _load(db)
    sid = db.execute("SELECT min(id) AS i FROM pilot_session").fetchone()["i"]
    dsn_db = make_conninfo(dsn, dbname=db.info.dbname)
    reader = psycopg.connect(dsn_db, autocommit=False)
    reader.execute("SELECT count(*) FROM flight")                                       # holds ACCESS SHARE on the flight view and its table
    result: dict[str, object] = {}

    def migrate() -> None:
        try:
            with psycopg.connect(dsn_db, autocommit=True) as c:
                result["pid"] = c.info.backend_pid
                c.execute(up_half(MIG_DIR / NEW))
            result["ok"] = True
        except Exception as exc:  # noqa: BLE001
            result["ok"] = repr(exc)

    t = threading.Thread(target=migrate)
    t.start()
    try:
        deadline = time.time() + 3
        waiting = False
        while time.time() < deadline and not waiting:                                   # the migration is queued behind the reader
            waiting = bool(db.execute("SELECT 1 FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND pid <> pg_backend_pid() "
                                      "AND pid <> %s", (reader.info.backend_pid,)).fetchall())
            time.sleep(0.05)
        assert waiting
        # evidence: no ACCESS EXCLUSIVE lock on pilot_session is held while the migration waits
        held = db.execute("SELECT count(*) AS n FROM pg_locks l JOIN pg_class c ON c.oid = l.relation WHERE c.relname = 'pilot_session' "
                          "AND l.mode = 'AccessExclusiveLock'").fetchone()["n"]
        assert held == 0
        worst = 0.0
        with psycopg.connect(dsn_db, autocommit=True) as writer:
            writer.execute("SET lock_timeout = '2s'")
            for _ in range(10):                                                         # what the collector does: update a session
                t0 = time.time()
                writer.execute("UPDATE pilot_session SET last_seen = last_seen WHERE id = %s", (sid,))
                worst = max(worst, time.time() - t0)
                time.sleep(0.05)
        assert worst < 0.5, worst
    finally:
        reader.rollback()
        reader.close()
        t.join(timeout=30)
    assert result.get("ok") is True, result
