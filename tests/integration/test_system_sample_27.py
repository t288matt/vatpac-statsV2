"""Issue 27: the daily system sample (table system_sample, view health_sample, functions system_probe_flight_count and
record_system_sample, the recording step in vatpac.maintenance). Each test says what fails if the change is reverted.

The role tests skip when the roles have not been created (db/roles.sql not applied); CI applies it.
"""
from __future__ import annotations

import tempfile
import time
from datetime import UTC, datetime, timedelta

import pytest

from vatpac import maintenance as m

psycopg = pytest.importorskip("psycopg")
pytestmark = pytest.mark.integration

PATH = tempfile.gettempdir()  # any path on a mounted disk: the test needs a disk reading, not the archive volume


def _role(conn, name: str) -> bool:
    return conn.execute("SELECT 1 AS x FROM pg_roles WHERE rolname = %s", (name,)).fetchone() is not None


def _today():
    return datetime.now(UTC).date()


def _rows(conn):
    return conn.execute("SELECT * FROM system_sample ORDER BY sampled_on").fetchall()


def _seed(conn, day) -> None:
    conn.execute("INSERT INTO system_sample VALUES (%s, now(), 1000000, 2000, 1000, 1.5)", (day,))


def test_recording_writes_one_row_with_a_positive_size_and_a_fast_probe(conn) -> None:
    """Reverting the recording step leaves the table empty."""
    assert m.record_system_sample(conn, PATH) == 1
    (row,) = _rows(conn)
    assert row["sampled_on"] == _today() and row["db_bytes"] > 0
    assert 0 <= row["probe_ms"] < 5000 and 0 <= row["disk_used_bytes"] <= row["disk_total_bytes"] and row["disk_total_bytes"] > 0


def test_the_daily_job_records_the_sample(conn) -> None:
    from vatpac.settings import Settings

    out = m.run_maintenance(conn, Settings(database_url="postgresql://x/y"), ("system_sampled",))
    assert out["system_sampled"] == 1 and out["failed"] == 0 and len(_rows(conn)) == 1


def test_the_probe_cap_is_local_to_the_probe(conn) -> None:
    """The 15 minute maintenance timeout must come back after the probe (set_config with is_local); a session-wide 5 s would stop
    the flight writer's daily run."""
    conn.execute("SET statement_timeout = '15min'")
    m.record_system_sample(conn, PATH)
    assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "15min"


def test_a_probe_slower_than_the_cap_is_cancelled_and_the_sample_skipped(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap is in force while the timed statements run; without it the 8 s sleep would finish and be recorded."""
    monkeypatch.setattr(m.timeit, "repeat", lambda fn, number, repeat: [conn.execute("SELECT pg_sleep(8)")])
    t = time.monotonic()
    assert m.record_system_sample(conn, PATH) == 0
    assert time.monotonic() - t < 7 and _rows(conn) == []


def test_a_second_reading_on_the_same_day_replaces_the_first(conn) -> None:
    conn.execute("SELECT record_system_sample(2000, 1000, 5.0)")
    conn.execute("SELECT record_system_sample(4000, 3000, 7.5)")
    (row,) = _rows(conn)
    assert (row["disk_total_bytes"], row["disk_used_bytes"], float(row["probe_ms"])) == (4000, 3000, 7.5)


def test_the_trim_deletes_only_rows_older_than_the_setting(conn) -> None:
    keep = int(conn.execute("SELECT value FROM app_setting WHERE key = 'system_sample_retention_days'").fetchone()["value"])
    assert keep == 400  # the seeded owner decision
    today = _today()
    for age in (keep + 1, keep + 30, keep, keep - 1, 1):
        _seed(conn, today - timedelta(days=age))
    conn.execute("SELECT record_system_sample(2000, 1000, 1)")
    assert {(today - r["sampled_on"]).days for r in _rows(conn)} == {keep, keep - 1, 1, 0}
    conn.execute("UPDATE app_setting SET value = '10' WHERE key = 'system_sample_retention_days'")
    conn.execute("SELECT record_system_sample(2000, 1000, 1)")
    assert {(today - r["sampled_on"]).days for r in _rows(conn)} == {1, 0}


@pytest.mark.parametrize("value", ["abc", "1", "-5"])
def test_a_bad_retention_setting_keeps_the_days_row_and_deletes_nothing(conn, value: str) -> None:
    _seed(conn, _today() - timedelta(days=900))
    conn.execute("UPDATE app_setting SET value = %s WHERE key = 'system_sample_retention_days'", (value,))
    conn.execute("SELECT record_system_sample(2000, 1000, 1)")
    assert len(_rows(conn)) == 2


def test_a_missing_retention_setting_keeps_the_days_row(conn) -> None:
    conn.execute("DELETE FROM app_setting WHERE key = 'system_sample_retention_days'")
    conn.execute("SELECT record_system_sample(2000, 1000, 1)")
    assert len(_rows(conn)) == 1


@pytest.mark.parametrize("values", [(0, 2000, 1000, 1), (1, 0, 0, 1), (1, 2000, 3000, 1), (1, 2000, -1, 1), (1, 2000, 1000, -1)],
                         ids=["no-size", "no-disk", "used-over-total", "used-negative", "probe-negative"])
def test_the_table_refuses_impossible_figures(conn, values) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO system_sample VALUES (current_date, now(), %s, %s, %s, %s)", values)


def test_the_view_shows_the_units_the_panels_use(conn) -> None:
    conn.execute("INSERT INTO system_sample VALUES ('2026-11-01', now(), 524288000, 214748364800, 32212254720, 12.34)")
    row = conn.execute("SELECT * FROM health_sample").fetchone()
    assert (float(row["database_mb"]), float(row["disk_used_gb"]), float(row["disk_total_gb"]), float(row["probe_ms"])) == (500.0, 30.0, 200.0, 12.34)
    assert row["day"] == datetime(2026, 11, 1, tzinfo=UTC)


# ---------------------------------------------------------------- roles
def test_the_collector_records_through_the_function_and_has_no_table_rights(conn) -> None:
    """Reverting the db/roles.sql grant makes the first call fail; widening it to the table fails the privilege asserts."""
    if not _role(conn, "collector"):
        pytest.skip("role collector not created (apply db/roles.sql)")
    conn.execute("SET ROLE collector")
    try:
        assert m.record_system_sample(conn, PATH) == 1
        for sql in ("SELECT * FROM system_sample", "INSERT INTO system_sample SELECT * FROM system_sample",
                    "UPDATE system_sample SET probe_ms = 0", "DELETE FROM system_sample", "SELECT * FROM health_sample",
                    "SELECT * FROM flight_summary"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)
    finally:
        conn.execute("RESET ROLE")
    assert len(_rows(conn)) == 1


def test_the_system_sample_functions_are_not_public(conn) -> None:
    for fn in ("system_probe_flight_count()", "record_system_sample(bigint, bigint, double precision)"):
        row = conn.execute("SELECT proacl::text AS a, prosecdef, proconfig FROM pg_proc WHERE oid = %s::regprocedure", (fn,)).fetchone()
        assert row["a"] is not None and "{=X" not in row["a"] and ",=X" not in row["a"], fn
        assert row["prosecdef"] is True and "search_path=public, pg_temp" in row["proconfig"]
        if _role(conn, "collector"):
            assert conn.execute("SELECT has_function_privilege('collector', %s, 'EXECUTE') AS ok", (fn,)).fetchone()["ok"]


def test_grafana_ro_reads_the_view_and_not_the_table(conn) -> None:
    """The view is picked up by the view loop of db/roles.sql; the table must stay out of reach."""
    if not _role(conn, "grafana_ro"):
        pytest.skip("role grafana_ro not created (apply db/roles.sql)")
    conn.execute("SELECT record_system_sample(2000, 1000, 1)")
    conn.execute("SET ROLE grafana_ro")
    try:
        assert len(conn.execute("SELECT * FROM health_sample").fetchall()) == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM system_sample")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT record_system_sample(2000, 1000, 1)")
    finally:
        conn.execute("RESET ROLE")
