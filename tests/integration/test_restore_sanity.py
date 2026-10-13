"""Issue 90: restore-lib.sh sanity_check must reject an empty or stale restore (ops/backup/restore-lib.sh).

The real function runs under bash against a small scratch database. A tiny `psql` shim written in Python (psycopg) is put on
PATH so the test needs no psql binary: it understands exactly `psql -X -tA -d DB -v ON_ERROR_STOP=1 -c SQL`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration

LIB = Path(__file__).resolve().parents[2] / "ops" / "backup" / "restore-lib.sh"
SCRATCH = "vatpac_sanity_scratch_test"
SHIM = '''#!{python}
import os, sys, psycopg
args = sys.argv[1:]
db = args[args.index("-d") + 1]
sql = args[args.index("-c") + 1]
with psycopg.connect(os.environ["SHIM_BASE_DSN"] + db, autocommit=True) as c:
    cur = c.execute(sql)
    for row in (cur.fetchall() if cur.description else []):
        print("|".join("t" if v is True else "f" if v is False else str(v) for v in row))
'''


def _dsn_for(dsn: str, db: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit((parts.scheme, parts.netloc, "/" + db, "", ""))


@pytest.fixture()
def scratch(dsn: str, tmp_path: Path):
    if shutil.which("bash") is None:
        pytest.skip("bash not available")
    admin = psycopg.connect(_dsn_for(dsn, "postgres"), autocommit=True)
    admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH}" WITH (FORCE)')
    admin.execute(f'CREATE DATABASE "{SCRATCH}"')
    with psycopg.connect(_dsn_for(dsn, SCRATCH), autocommit=True) as c:
        c.execute("CREATE TABLE schema_migrations (version text PRIMARY KEY)")
        c.execute("INSERT INTO schema_migrations VALUES ('1')")
        c.execute("CREATE TABLE pilot_session (id bigserial PRIMARY KEY)")
        c.execute("CREATE TABLE poll_log (id bigserial PRIMARY KEY, polled_at timestamptz NOT NULL)")
    shim = tmp_path / "bin" / "psql"
    shim.parent.mkdir()
    shim.write_text(SHIM.format(python=sys.executable), newline="\n")
    shim.chmod(0o755)
    base = _dsn_for(dsn, "")  # ".../" : the shim appends the database name
    env = dict(os.environ, PATH=f"{shim.parent}{os.pathsep}{os.environ['PATH']}", SHIM_BASE_DSN=base)
    try:
        yield env
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{SCRATCH}" WITH (FORCE)')
        admin.close()


def _fill(dsn: str, sessions: int, poll_times: list[str]) -> None:
    with psycopg.connect(_dsn_for(dsn, SCRATCH), autocommit=True) as c:
        for _ in range(sessions):
            c.execute("INSERT INTO pilot_session DEFAULT VALUES")
        for t in poll_times:
            c.execute("INSERT INTO poll_log (polled_at) VALUES (%s)", (t,))


def _sanity(env: dict[str, str], dump_name: str | None, **extra: str) -> subprocess.CompletedProcess[str]:
    arg = f'"/backups/{dump_name}"' if dump_name else ""
    e = dict(env, **extra)
    return subprocess.run(["bash", "-c", f'. "{LIB}"; sanity_check "{SCRATCH}" {arg}'], env=e, capture_output=True, text=True, timeout=60)


def test_a_schema_only_dump_fails(dsn: str, scratch: dict[str, str]) -> None:
    r = _sanity(scratch, "vatpac_20261007T020000Z.dump")
    assert r.returncode == 1 and "poll_log is empty" in r.stderr and r.stdout == ""


def test_rows_in_poll_log_but_no_pilot_session_fails(dsn: str, scratch: dict[str, str]) -> None:
    _fill(dsn, 0, ["2026-10-07 01:59:00+00"])
    r = _sanity(scratch, "vatpac_20261007T020000Z.dump")
    assert r.returncode == 1 and "pilot_session is empty" in r.stderr


def test_a_good_dump_passes_with_the_same_output_format_as_before(dsn: str, scratch: dict[str, str]) -> None:
    _fill(dsn, 3, ["2026-10-07 01:59:00+00", "2026-10-06 12:00:00+00"])
    r = _sanity(scratch, "vatpac_20261007T020000Z.dump")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "poll_log=2 pilot_session=3"


def test_a_dump_whose_newest_poll_is_older_than_48_hours_before_the_dump_fails(dsn: str, scratch: dict[str, str]) -> None:
    _fill(dsn, 1, ["2026-10-04 01:00:00+00"])               # 73 h before the dump
    r = _sanity(scratch, "vatpac_20261007T020000Z.dump")
    # return code 2 = stale only (everything else fine): the weekly test warns, restore.sh refuses and names the override
    assert r.returncode == 2 and r.stdout.startswith("STALE poll_log=1 pilot_session=1") and "more than 48 h older" in r.stdout


def test_an_old_dump_is_judged_against_its_own_time_not_todays(dsn: str, scratch: dict[str, str]) -> None:
    _fill(dsn, 1, ["2025-01-01 00:00:00+00"])
    assert _sanity(scratch, "vatpac_20250101T020000Z.dump").returncode == 0


def test_the_age_limit_is_configurable_and_zero_turns_it_off(dsn: str, scratch: dict[str, str]) -> None:
    _fill(dsn, 1, ["2026-10-04 01:00:00+00"])
    assert _sanity(scratch, "vatpac_20261007T020000Z.dump", RESTORE_MAX_POLL_AGE_HOURS="100").returncode == 0
    assert _sanity(scratch, "vatpac_20261007T020000Z.dump", RESTORE_MAX_POLL_AGE_HOURS="0").returncode == 0


def test_a_file_name_without_a_stamp_still_needs_rows_but_skips_the_age_check(dsn: str, scratch: dict[str, str]) -> None:
    assert _sanity(scratch, "copy.dump").returncode == 1
    _fill(dsn, 1, ["2020-01-01 00:00:00+00"])
    assert _sanity(scratch, "copy.dump").returncode == 0
    assert _sanity(scratch, None).returncode == 0


def test_a_new_empty_install_can_be_allowed_explicitly(dsn: str, scratch: dict[str, str]) -> None:
    r = _sanity(scratch, "vatpac_20261007T020000Z.dump", RESTORE_SANITY_ALLOW_EMPTY="1")
    assert r.returncode == 0 and r.stdout.strip() == "poll_log=0 pilot_session=0"


def test_decimal_hours_are_accepted(dsn: str, scratch: dict[str, str]) -> None:
    _fill(dsn, 1, ["2026-10-07 00:00:00+00"])               # 2 h before the dump
    assert _sanity(scratch, "vatpac_20261007T020000Z.dump", RESTORE_MAX_POLL_AGE_HOURS="2.5").returncode == 0
    assert _sanity(scratch, "vatpac_20261007T020000Z.dump", RESTORE_MAX_POLL_AGE_HOURS="1.5").returncode == 2
    assert _sanity(scratch, "vatpac_20261007T020000Z.dump", RESTORE_MAX_POLL_AGE_HOURS="0.0").returncode == 0


@pytest.mark.parametrize("bad", ["abc", "1,5", "-3", "1.2.3", ".", "5.", "1 h"])
def test_a_non_numeric_age_limit_gets_a_clear_message_not_a_sql_error(dsn: str, scratch: dict[str, str], bad: str) -> None:
    _fill(dsn, 1, ["2026-10-07 01:00:00+00"])
    r = _sanity(scratch, "vatpac_20261007T020000Z.dump", RESTORE_MAX_POLL_AGE_HOURS=bad)
    assert r.returncode == 1 and "must be a number of hours" in r.stderr and "ERROR" not in r.stderr


def test_allow_empty_waives_only_the_row_counts_not_the_age_limit(dsn: str, scratch: dict[str, str]) -> None:
    _fill(dsn, 0, ["2026-10-01 00:00:00+00"])               # no sessions (waived), but poll data far older than the dump
    r = _sanity(scratch, "vatpac_20261007T020000Z.dump", RESTORE_SANITY_ALLOW_EMPTY="1")
    assert r.returncode == 2 and r.stdout.startswith("STALE")
    assert _sanity(scratch, "vatpac_20261007T020000Z.dump", RESTORE_SANITY_ALLOW_EMPTY="1", RESTORE_MAX_POLL_AGE_HOURS="0").returncode == 0


def _dump_with(dsn: str, *statements: str) -> None:
    _fill(dsn, 3, ["2026-10-07 01:59:00+00"])
    with psycopg.connect(_dsn_for(dsn, SCRATCH), autocommit=True) as c:
        for st in statements:
            c.execute(st)


def test_a_dump_after_phase_5_counts_flight_summary(dsn: str, scratch: dict[str, str]) -> None:
    """Issue 89 phase 5: flight_summary holds the finished flights (it has session_ids) and is read and counted."""
    _dump_with(dsn, "CREATE TABLE flight_summary (session_id bigint, session_ids bigint[])",
               "INSERT INTO flight_summary VALUES (1, '{1}'), (2, '{2}')")
    r = _sanity(scratch, "vatpac_20261007T020000Z.dump")
    assert r.returncode == 0 and r.stdout.strip() == "poll_log=1 pilot_session=3 flight_summary=2", (r.stdout, r.stderr)


def test_a_dump_from_before_phase_5_counts_the_finished_flights_under_their_old_name(dsn: str, scratch: dict[str, str]) -> None:
    """Dumps are kept for 14 days. A pre-phase-5 dump has the finished flights as flight_record AND the old pieces as flight_summary
    (no session_ids column): the count must be the flights (2), never the pieces (5)."""
    _dump_with(dsn, "CREATE TABLE flight_record (session_id bigint)", "INSERT INTO flight_record VALUES (1), (2)",
               "CREATE TABLE flight_summary (session_id bigint, gap_segment int, span_part int)",
               "INSERT INTO flight_summary SELECT g, 1, 0 FROM generate_series(1, 5) g")
    r = _sanity(scratch, "vatpac_20261007T020000Z.dump")
    assert r.returncode == 0 and r.stdout.strip() == "poll_log=1 pilot_session=3 flight_summary=2", (r.stdout, r.stderr)


def test_a_dump_from_before_phase_1_has_only_the_pieces_and_still_passes(dsn: str, scratch: dict[str, str]) -> None:
    _dump_with(dsn, "CREATE TABLE flight_summary (session_id bigint, gap_segment int, span_part int)",
               "INSERT INTO flight_summary VALUES (1, 1, 0)")
    r = _sanity(scratch, "vatpac_20261007T020000Z.dump")
    assert r.returncode == 0 and r.stdout.strip() == "poll_log=1 pilot_session=3", (r.stdout, r.stderr)


def test_a_dump_without_any_flight_table_still_passes(dsn: str, scratch: dict[str, str]) -> None:
    _fill(dsn, 3, ["2026-10-07 01:59:00+00"])
    r = _sanity(scratch, "vatpac_20261007T020000Z.dump")
    assert r.returncode == 0 and r.stdout.strip() == "poll_log=1 pilot_session=3", (r.stdout, r.stderr)


def test_an_unreadable_flight_summary_fails_the_restore_check(dsn: str, scratch: dict[str, str]) -> None:
    _fill(dsn, 1, ["2026-10-07 01:59:00+00"])
    with psycopg.connect(_dsn_for(dsn, SCRATCH), autocommit=True) as c:
        c.execute("CREATE VIEW flight_summary AS SELECT 1 AS session_id, ARRAY[1::bigint] AS session_ids WHERE 1 / (SELECT count(*) FROM poll_log WHERE false) = 1")
    r = _sanity(scratch, "vatpac_20261007T020000Z.dump")
    assert r.returncode == 1 and "flight_summary unreadable" in r.stderr
