"""Issue 279 / ADR-035: the one-off loader ops/v1_import/load.sh (v1 flight_summaries and controller_summaries into flight_summary and
controller_session), run for real against the throwaway `_test` database with SYNTHETIC files (tests/tools/v1_import_synth.py).

The mapping test compares every loaded column of every row with a reference written from the owner's field map and the collector's own
text helpers, so it fails if a mapping rule (the airborne cap, the start time, the home ICAO, the frequency, the whole-second logon, the
text caps) or the re-run rule (a second run inserts nothing) is broken. Needs `psql` on PATH and a bash (Git Bash on Windows)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "tools"))
import v1_import_synth as synth  # noqa: E402
from drop_gate_helpers import process_flights, unreconciled  # noqa: E402
from test_integrity_checks import World, failing, sweep  # noqa: E402
from test_restore_sanity import _find_bash  # noqa: E402

BASH = _find_bash()
LOAD = REPO / "ops" / "v1_import" / "load.sh"
TABLES = (("flight_summary", "session_id"), ("controller_session", "id"), ("pilot_session", "id"))
FLIGHT_VIEWS = ("flight", "flight_ext", "movements", "cohorts", "controller_session_merged", "controller_session_ranked",
                "controller_summaries", "monday_attendance")


@pytest.fixture(autouse=True)
def _tools() -> None:
    if BASH is None:
        pytest.skip("bash not available (on Windows this needs Git for Windows)")
    if shutil.which("psql") is None:
        pytest.skip("psql binary not available")


@pytest.fixture(scope="module")
def files(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, list[dict[str, Any]]]]:
    d = tmp_path_factory.mktemp("v1files")
    synth.generate(d, synth.SMALL_FLIGHTS, synth.SMALL_CONTROLLERS)
    return d, synth.read_rows(d)  # read back from the files, like COPY does


LIVE_ROWS_SQL = (
    "INSERT INTO flight_summary (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, logon_time, "
    "sample_count) SELECT 900000000 + n, now(), now(), 9, 'LIVE9', 'YSSY', 'YMML', ARRAY[900000000 + n], now(), 5 "
    "FROM (SELECT nextval('controller_session_id_seq') AS n) s;\n"
    # the key (cid, callsign, logon_time) must be unique per injection: two injections in one second collided on CI (issue 279)
    "INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen) "
    "SELECT nextval('controller_session_id_seq') + 3000000000, 'ML_APP', date_trunc('second', now()), now(), now();\n"
)


def load(dsn: str, conn: Any, data_dir: Path | None, *flags: str, db: str | None = None,
         inject_before: str | None = None, tmp: Path | None = None, inject_sql: str = LIVE_ROWS_SQL) -> subprocess.CompletedProcess[str]:
    """Run load.sh. inject_before: a line prefix in the SQL stream before which ANOTHER session runs inject_sql (by default it commits
    a live flight and a live controller session, what the writer and the collector do while the load transaction is open). The
    injection appends one line to tmp/inject.log: "ok", or "FAILED" if its SQL failed (the test then fails, nothing is silent)."""
    name = db or conn.execute("SELECT current_database() AS d").fetchone()["d"]
    psql = f"psql -X -q -v ON_ERROR_STOP=1 '{dsn}'"
    if inject_before and tmp:
        (tmp / "inject.sql").write_text(inject_sql, encoding="utf-8")
        log = (tmp / "inject.log").as_posix()
        run = f"psql -X -q -v ON_ERROR_STOP=1 -f '{(tmp / 'inject.sql').as_posix()}' '{dsn}'"
        script = f"{run} && echo ok >> '{log}' || echo FAILED >> '{log}'\n"
        (tmp / "inject.sh").write_text(script, encoding="utf-8", newline="\n")
        bang = "\\\\! sh " + (tmp / "inject.sh").as_posix()  # sed turns two backslashes into one: psql sees a single one
        psql = f"sed 's|^{inject_before}|{bang}\\n&|' | {psql}"
    env = {**os.environ, "V1_IMPORT_PSQL": psql}
    cmd = [str(BASH), LOAD.as_posix(), "--expect-db", name, *(["--data-dir", data_dir.as_posix()] if data_dir else []), *flags]
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120, cwd=REPO)


def after_row(stdout: str) -> dict[str, str]:
    """The one-row table printed under '== AFTER ==' as {column: value}."""
    lines = stdout.split("== AFTER ==", 1)[1].strip().splitlines()
    return dict(zip([c.strip() for c in lines[0].split("|")], [v.strip() for v in lines[2].split("|")], strict=True))


def counts(conn: Any) -> dict[str, int]:
    return {k: conn.execute(q).fetchone()["n"] for k, q in {
        "flights_live": "SELECT count(*) AS n FROM flight_summary WHERE session_id >= 0",
        "flights_imported": "SELECT count(*) AS n FROM flight_summary WHERE session_id < 0",
        "controllers_live": "SELECT count(*) AS n FROM controller_session WHERE id > 0",
        "controllers_imported": "SELECT count(*) AS n FROM controller_session WHERE id < 0",
        "pilot_session": "SELECT count(*) AS n FROM pilot_session", "pilot_position": "SELECT count(*) AS n FROM pilot_position",
        "rating_events": "SELECT count(*) AS n FROM controller_rating_event",
        "frequency_events": "SELECT count(*) AS n FROM controller_frequency_event"}.items()}


def dump(conn: Any, table: str, order: str) -> list[dict[str, Any]]:
    return conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()  # noqa: S608


def test_the_mapping_matches_the_reference_for_every_row_and_the_month_counts(conn: Any, dsn: str, files: Any) -> None:
    d, rows = files
    seq_before = conn.execute("SELECT last_value, is_called FROM controller_session_id_seq").fetchone()
    p = load(dsn, conn, d, "--apply")
    assert p.returncode == 0, p.stdout + p.stderr
    assert synth.compare(conn, rows) == []  # every column of every row (320 flights, 240 controller sessions)
    c = counts(conn)
    assert (c["flights_imported"], c["controllers_imported"]) == (len(rows["flights"]), len(rows["controllers"]))
    assert c["pilot_session"] == c["pilot_position"] == c["rating_events"] == c["frequency_events"] == 0  # nothing else is written
    assert conn.execute("SELECT last_value, is_called FROM controller_session_id_seq").fetchone() == seq_before  # the serial is untouched
    by_month = {r["m"]: r["n"] for r in conn.execute(
        "SELECT to_char(end_time AT TIME ZONE 'UTC', 'YYYY-MM') AS m, count(*) AS n FROM flight_summary GROUP BY 1").fetchall()}
    assert by_month == synth.SMALL_FLIGHTS
    by_month = {r["m"]: r["n"] for r in conn.execute(
        "SELECT to_char(logon_time AT TIME ZONE 'UTC', 'YYYY-MM') AS m, count(*) AS n FROM controller_session GROUP BY 1").fetchall()}
    assert by_month == synth.SMALL_CONTROLLERS
    # the constant columns of the convention
    assert conn.execute("SELECT count(*) AS n FROM flight_summary WHERE sample_count <> 0 OR max_gs_kt IS NOT NULL "
                        "OR max_alt_ft IS NOT NULL "
                        "OR remarks IS NOT NULL OR session_ids <> ARRAY[session_id]").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM controller_session "
                        "WHERE visual_range IS NOT NULL OR first_seen <> logon_time").fetchone()["n"] == 0


def test_a_second_run_inserts_nothing_and_touches_nothing(conn: Any, dsn: str, files: Any) -> None:
    d, _ = files
    assert load(dsn, conn, d, "--apply").returncode == 0
    f1, c1 = dump(conn, "flight_summary", "session_id"), dump(conn, "controller_session", "id")
    p = load(dsn, conn, d, "--apply")
    assert p.returncode == 0, p.stdout + p.stderr
    assert dump(conn, "flight_summary", "session_id") == f1  # including written_at: not one row was rewritten
    assert dump(conn, "controller_session", "id") == c1
    after = after_row(p.stdout)
    assert (after["flights_inserted"], after["controllers_inserted"]) == ("0", "0"), p.stdout


def test_a_dry_run_writes_nothing(conn: Any, dsn: str, files: Any) -> None:
    d, rows = files
    before = counts(conn)
    p = load(dsn, conn, d)  # no --apply
    assert p.returncode == 0, p.stdout + p.stderr
    assert "DRY RUN" in p.stdout and "Nothing was written" in p.stdout
    assert counts(conn) == before
    assert f"{len(rows['flights'])} |" in p.stdout  # it still reports the counts it would load


def test_rollback_restores_the_starting_rows_exactly(conn: Any, dsn: str, files: Any) -> None:
    d, _ = files
    World(conn)  # live-style rows: pilot sessions, positions, three live flights, a live controller session
    now = datetime.now(UTC).replace(microsecond=0)
    conn.execute("INSERT INTO flight_summary (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, "
                 "logon_time, "
                 "sample_count) VALUES (7, %s, %s, 9, 'LIVE1', 'YSSY', 'YMML', ARRAY[7], %s, 5)", (now, now, now))
    start = {t: dump(conn, t, o) for t, o in TABLES}
    start_counts = counts(conn)
    assert load(dsn, conn, d, "--apply").returncode == 0
    assert counts(conn)["flights_imported"] > 0
    p = load(dsn, conn, None, "--rollback", "--apply")
    assert p.returncode == 0, p.stdout + p.stderr
    assert counts(conn) == start_counts
    assert {t: dump(conn, t, o) for t, o in TABLES} == start
    assert load(dsn, conn, None, "--rollback", "--apply").returncode == 0  # a second rollback deletes nothing and does not fail
    assert counts(conn) == start_counts


def test_imported_rows_leave_live_rows_the_views_the_gate_and_the_checks_alone(conn: Any, dsn: str, files: Any) -> None:
    d, rows = files
    World(conn)
    live_views = {v: conn.execute(f"SELECT count(*) AS n FROM {v}").fetchone()["n"] for v in FLIGHT_VIEWS}  # noqa: S608
    live_flights = conn.execute("SELECT * FROM flight ORDER BY session_id").fetchall()
    assert failing(sweep(dsn)) == set()
    assert load(dsn, conn, d, "--apply").returncode == 0
    # the imported rows are visible to the views (that is the point) and nothing positive changed
    assert conn.execute("SELECT * FROM flight WHERE session_id > 0 ORDER BY session_id").fetchall() == live_flights
    for view in ("flight", "flight_ext"):
        assert conn.execute(f"SELECT count(*) AS n FROM {view} WHERE session_id < 0").fetchone()["n"] == len(rows["flights"])  # noqa: S608
    assert conn.execute("SELECT count(*) AS n FROM controller_session_merged").fetchone()["n"] > live_views["controller_session_merged"]
    # the integrity pack, the drop gate and the writer ignore them
    assert failing(sweep(dsn)) == set()
    assert unreconciled(conn, datetime(2025, 9, 15, tzinfo=UTC), datetime(2025, 9, 16, tzinfo=UTC)) == []
    assert process_flights(conn) == 0  # nothing new to write, and no error
    assert conn.execute("SELECT count(*) AS n FROM flight_summary WHERE session_id < 0").fetchone()["n"] == len(rows["flights"])


def test_the_collector_still_inserts_after_the_load(conn: Any, dsn: str, files: Any, payload: dict, refdata: Any) -> None:
    from helpers import apply, partition_for
    d, _ = files
    assert load(dsn, conn, d, "--apply").returncode == 0
    partition_for(conn, datetime(2026, 10, 7, 16, 11, 19, tzinfo=UTC))
    apply(conn, payload, refdata)
    assert conn.execute("SELECT count(*) AS n FROM pilot_session").fetchone()["n"] > 0
    assert conn.execute("SELECT min(id) AS m FROM controller_session WHERE id > 0").fetchone()["m"] == 1  # the serial still starts at 1


def test_the_wrong_database_name_is_refused_before_anything_is_written(conn: Any, dsn: str, files: Any) -> None:
    d, _ = files
    p = load(dsn, conn, d, "--apply", db="some_other_db")
    assert p.returncode != 0 and "REFUSING" in p.stdout
    p = load(dsn, conn, None, "--rollback", "--apply", db="some_other_db")
    assert p.returncode != 0 and "REFUSING" in p.stdout
    assert counts(conn)["flights_imported"] == counts(conn)["controllers_imported"] == 0
    assert load(dsn, conn, d, "--apply", db="bad name;").returncode == 2  # only letters, digits and _


def test_a_changed_or_truncated_file_is_refused(conn: Any, dsn: str, files: Any, tmp_path: Path) -> None:
    d, rows = files
    bad = tmp_path / "changed"
    shutil.copytree(d, bad)
    csv = bad / "v1_flights.csv"
    csv.write_bytes(csv.read_bytes().replace(b"AUSTRALIA", b"AUSTRALIB", 1))
    p = load(dsn, conn, bad, "--apply")
    assert p.returncode == 2 and "checksum" in p.stderr  # the frozen file changed after extraction
    short = tmp_path / "short"  # a consistent checksum but fewer rows than v1 reported (a cut-off extract)
    shutil.copytree(d, short)
    lines = (short / "v1_flights.csv").read_text(encoding="utf-8").splitlines(keepends=True)
    (short / "v1_flights.csv").write_text("".join(lines[:-5]), encoding="utf-8", newline="")
    synth.write_sums(short)
    p = load(dsn, conn, short, "--apply")
    assert p.returncode != 0 and "EXPECTED_COUNTS" in p.stdout + p.stderr
    assert counts(conn)["flights_imported"] == 0


def test_a_header_that_does_not_match_is_refused(conn: Any, dsn: str, files: Any, tmp_path: Path) -> None:
    d, _ = files
    odd = tmp_path / "odd"
    shutil.copytree(d, odd)
    f = odd / "v1_controllers.csv"
    f.write_text(f.read_text(encoding="utf-8").replace("session_end_time", "session_finish_time", 1), encoding="utf-8", newline="")
    synth.write_sums(odd)
    p = load(dsn, conn, odd, "--apply")
    assert p.returncode != 0 and counts(conn)["flights_imported"] == 0


def test_rejected_rows_are_listed_and_stop_the_run_unless_allowed(conn: Any, dsn: str, files: Any, tmp_path: Path) -> None:
    _, rows = files
    f_rows, c_rows = list(rows["flights"]), list(rows["controllers"])
    f_rows.append({**f_rows[0], "id": 800_000_001, "departure": "  "})  # blank departure
    f_rows.append({**f_rows[0], "id": 800_000_002, "completion_time": datetime(2026, 4, 2, tzinfo=UTC)})  # outside the range
    c_rows.append({**c_rows[0], "id": 800_000_003, "callsign": "NOT_ON_LIST"})  # not on the whitelist
    c_rows.append({**c_rows[0], "id": 800_000_004})  # same cid, callsign and second as c_rows[0]
    d = tmp_path / "rej"
    synth.write_files(d, f_rows, c_rows)
    p = load(dsn, conn, d, "--apply")
    assert p.returncode != 0 and "STOP: rejected rows exist" in p.stdout
    for reason in ("departure is blank", "outside the range", "not on the v3 whitelist", "same cid, callsign and whole-second logon"):
        assert reason in p.stdout, reason
    assert counts(conn)["flights_imported"] == counts(conn)["controllers_imported"] == 0  # nothing at all, not even the good rows
    dry = load(dsn, conn, d)  # the dry run reports the same rejects (and also fails, so a script notices)
    assert dry.returncode != 0 and "departure is blank" in dry.stdout and counts(conn)["flights_imported"] == 0
    p = load(dsn, conn, d, "--apply", "--allow-rejects")
    assert p.returncode == 0, p.stdout + p.stderr
    c = counts(conn)
    assert (c["flights_imported"], c["controllers_imported"]) == (len(rows["flights"]), len(rows["controllers"]))


def test_a_live_session_with_the_same_key_is_a_reject_not_a_silent_skip(conn: Any, dsn: str, files: Any) -> None:
    d, rows = files
    r = rows["controllers"][0]
    conn.execute("INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen) VALUES (%s, %s, %s, %s, %s)",
                 (r["cid"], r["callsign"], r["session_start_time"].replace(microsecond=0), r["session_start_time"], r["session_end_time"]))
    p = load(dsn, conn, d, "--apply")
    assert p.returncode != 0 and "held by another session" in p.stdout
    assert counts(conn)["controllers_imported"] == 0


def injections(tmp: Path) -> list[str]:
    return (tmp / "inject.log").read_text(encoding="utf-8").split()


def test_a_live_row_committed_during_the_load_does_not_abort_it(conn: Any, dsn: str, files: Any, tmp_path: Path) -> None:
    d, rows = files
    p = load(dsn, conn, d, "--apply", inject_before="-- Checks:", tmp=tmp_path)
    assert p.returncode == 0, p.stdout + p.stderr
    assert injections(tmp_path) == ["ok"]  # the second session really committed its rows
    c = counts(conn)
    assert (c["flights_imported"], c["flights_live"], c["controllers_live"]) == (len(rows["flights"]), 1, 1)  # the injected rows are there
    p = load(dsn, conn, None, "--rollback", "--apply", inject_before="WITH d AS (DELETE FROM controller_session", tmp=tmp_path)
    assert p.returncode == 0, p.stdout + p.stderr
    assert injections(tmp_path) == ["ok", "ok"]
    c = counts(conn)
    assert (c["flights_imported"], c["controllers_imported"], c["flights_live"], c["controllers_live"]) == (0, 0, 2, 2)


SWAP_SQL = ("DELETE FROM flight_summary WHERE session_id = 7;\nDELETE FROM controller_session WHERE cid = 4242;\n" + LIVE_ROWS_SQL)


def test_a_live_row_deleted_while_another_is_added_is_caught(conn: Any, dsn: str, files: Any, tmp_path: Path) -> None:
    """The live COUNT is the same at the end (one removed, one added); the check compares the ids that existed at the start."""
    d, _ = files
    now = datetime.now(UTC).replace(microsecond=0)
    conn.execute("INSERT INTO flight_summary (session_id, start_time, end_time, cid, callsign, departure, arrival, session_ids, "
                 "logon_time, sample_count) VALUES (7, %s, %s, 9, 'LIVE1', 'YSSY', 'YMML', ARRAY[7], %s, 5)", (now, now, now))
    conn.execute("INSERT INTO controller_session (cid, callsign, logon_time, first_seen, last_seen) VALUES (4242, 'ML_APP', %s, %s, %s)",
                 (now, now, now))
    sql = SWAP_SQL.replace("DELETE FROM controller_session WHERE cid = 4242;" + "\n", "")
    p = load(dsn, conn, d, "--apply", inject_before="-- Checks:", tmp=tmp_path, inject_sql=sql)
    assert injections(tmp_path) == ["ok"]
    assert p.returncode != 0 and "post-load checks failed" in p.stdout + p.stderr
    c = counts(conn)
    assert (c["flights_imported"], c["controllers_imported"]) == (0, 0)  # nothing was committed; the live deletion is the other session's
    sql = "DELETE FROM controller_session WHERE cid = 4242;" + "\n" + LIVE_ROWS_SQL
    marker = "WITH d AS (DELETE FROM controller_session"
    p = load(dsn, conn, None, "--rollback", "--apply", inject_before=marker, tmp=tmp_path, inject_sql=sql)
    assert p.returncode != 0 and "rollback checks failed" in p.stdout + p.stderr


def test_already_loaded_rows_that_differ_from_the_file_stop_the_run(conn: Any, dsn: str, files: Any, tmp_path: Path) -> None:
    d, rows = files
    assert load(dsn, conn, d, "--apply").returncode == 0
    before = dump(conn, "flight_summary", "session_id")
    edited = tmp_path / "edited"
    shutil.copytree(d, edited)
    f = edited / "v1_flights.csv"
    f.write_text(f.read_text(encoding="utf-8").replace(",90,", ",91,", 1), encoding="utf-8", newline="")  # one flight's time online
    synth.write_sums(edited)
    p = load(dsn, conn, edited, "--apply")
    assert p.returncode != 0 and "already_loaded_but_different" in p.stdout and "STOP" in p.stdout
    assert dump(conn, "flight_summary", "session_id") == before
    p = load(dsn, conn, edited, "--apply", "--allow-rejects")  # explicit: the loaded rows stay as they are
    assert p.returncode == 0 and dump(conn, "flight_summary", "session_id") == before
