"""QA for issue 387: the migration, the generated columns, the indexes and the role rights, on a throwaway Postgres.

Written from the design (sections 3, 4 and 8), not from the builders' tests. Run explicitly:
    python -m pytest tests/qa_stats_loader_db.py   (container: see qa_stats_loader_support.py)
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qa_stats_loader_support as sup  # noqa: E402

pytestmark = pytest.mark.integration
DB = "qa_schema_test"
TABLES = {"atc_stats_session", "atc_stats_run_log"}


@pytest.fixture(scope="module")
def su() -> Iterator[psycopg.Connection]:
    sup.need_db()
    sup.build_db(DB)
    with psycopg.connect(sup.dsn(DB), autocommit=True) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean(su: psycopg.Connection) -> None:
    su.execute("TRUNCATE atc_stats_session, atc_stats_run_log RESTART IDENTITY")


def cols(su: psycopg.Connection, table: str) -> dict[str, tuple[str, str, str]]:
    rows = su.execute("SELECT column_name, data_type, is_nullable, coalesce(is_generated, '') FROM information_schema.columns "
                      "WHERE table_name = %s", (table,)).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


# ---- migration ---------------------------------------------------------------------------------------------------
def test_the_migration_applies_on_a_blank_database_and_creates_exactly_the_two_tables(su: psycopg.Connection) -> None:
    names = {r[0] for r in su.execute("SELECT relname FROM pg_class WHERE relname LIKE 'atc_stats%' AND relkind = 'r'")}
    assert names == TABLES


def test_the_migration_applies_on_a_database_at_mains_schema_and_changes_nothing_else() -> None:
    sup.need_db()
    sup.build_db("qa_main_test", stop_before_new=True)
    with psycopg.connect(sup.dsn("qa_main_test"), autocommit=True) as c:
        before = {r[0] for r in c.execute("SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace")}
        assert not {r[0] for r in c.execute("SELECT relname FROM pg_class WHERE relname LIKE 'atc_stats%'")}
    sup.psql("qa_main_test", sup.up_part(sup.REPO / "db" / "migrations" / sup.NEW_MIGRATION))
    sup.apply_roles("qa_main_test")
    sup.apply_roles("qa_main_test")  # roles.sql is re-run on every deploy: it must be safe to repeat
    with psycopg.connect(sup.dsn("qa_main_test"), autocommit=True) as c:
        after = {r[0] for r in c.execute("SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace")}
    added = after - before
    assert {"atc_stats_session", "atc_stats_run_log"} <= added
    assert all(n.startswith("atc_stats") for n in added), added


def test_no_mode_column_and_no_run_types(su: psycopg.Connection) -> None:
    run = cols(su, "atc_stats_run_log")
    assert not [c for c in run if c in ("mode", "run_type", "kind", "type")]
    assert set(run) >= {"started_at", "finished_at", "requests", "sessions_inserted", "sessions_updated", "sessions_deleted",
                        "days_loaded", "last_day_at", "failed_days", "hour_windows_from", "empty_dates", "status", "error"}
    assert "on_whitelist" not in cols(su, "atc_stats_session")
    assert not su.execute("SELECT 1 FROM pg_class WHERE relname LIKE 'atc_stats%coverage%'").fetchone()


def test_run_status_is_one_of_the_five_states(su: psycopg.Connection) -> None:
    for s in ("running", "success", "failed", "stopped", "interrupted"):
        su.execute("INSERT INTO atc_stats_run_log (status) VALUES (%s)", (s,))
    with pytest.raises(psycopg.errors.CheckViolation):
        su.execute("INSERT INTO atc_stats_run_log (status) VALUES ('daily')")
    assert su.execute("INSERT INTO atc_stats_run_log DEFAULT VALUES RETURNING status").fetchone() == ("running",)


# ---- columns, generated columns -------------------------------------------------------------------------------
def test_session_columns_and_types(su: psycopg.Connection) -> None:
    c = cols(su, "atc_stats_session")
    assert c["connection_id"][0] == "bigint" and c["cid"][0] == "bigint" and c["callsign"][0] == "text"
    assert c["session_start"][0] == "timestamp with time zone" and c["session_end"][0] == "timestamp with time zone"
    assert c["session_start"][1] == "NO" and c["session_end"][1] == "YES" and c["cid"][1] == "NO" and c["callsign"][1] == "NO"
    assert c["position_type"][2] == "ALWAYS" and c["duration_seconds"][2] == "ALWAYS"
    counts = ["aircrafttracked", "aircraftseen", "flightsamended", "handoffsinitiated", "handoffsreceived", "handoffsrefused",
              "squawksassigned", "cruisealtsmodified", "tempaltsmodified", "scratchpadmods"]
    for n in counts:
        assert c[n][0] == "integer" and c[n][1] == "NO"
    su.execute("INSERT INTO atc_stats_session (connection_id, cid, callsign, session_start) VALUES (1, 1, 'X', now())")
    assert su.execute("SELECT " + ",".join(counts) + " FROM atc_stats_session").fetchone() == (0,) * 10


@pytest.mark.parametrize(("callsign", "expect"), [("ML_APP", "APP"), ("ml_twr", "TWR"), ("YSSY_CTR", "CTR"), ("AB", "AB"),
                                                  ("AU-ATC_FMP", "FMP"), ("SY_ATIS", "TIS"), ("BN_A_DEP", "DEP"), ("TMU", "TMU")])
def test_position_type_is_upper_of_the_last_three_letters(su: psycopg.Connection, callsign: str, expect: str) -> None:
    su.execute("INSERT INTO atc_stats_session (connection_id, cid, callsign, session_start) VALUES (1, 1, %s, now())", (callsign,))
    assert su.execute("SELECT position_type FROM atc_stats_session").fetchone() == (expect,)


def test_position_type_cannot_be_written_and_has_no_fixed_list(su: psycopg.Connection) -> None:
    with pytest.raises(psycopg.errors.GeneratedAlways):
        su.execute("INSERT INTO atc_stats_session (connection_id, cid, callsign, session_start, position_type) "
                   "VALUES (1, 1, 'X_APP', now(), 'ZZZ')")
    su.execute("INSERT INTO atc_stats_session (connection_id, cid, callsign, session_start) VALUES (2, 1, 'X_QQQ', now())")
    assert su.execute("SELECT position_type FROM atc_stats_session WHERE connection_id = 2").fetchone() == ("QQQ",)


@pytest.mark.parametrize("zone", ["UTC", "Pacific/Auckland", "America/Los_Angeles"])
def test_duration_is_the_same_in_every_time_zone_and_null_without_an_end(su: psycopg.Connection, zone: str) -> None:
    su.execute(f"SET timezone = '{zone}'")
    su.execute("INSERT INTO atc_stats_session (connection_id, cid, callsign, session_start, session_end) VALUES "
               "(1, 1, 'X_APP', '2026-03-29 00:30:00+00', '2026-03-29 03:30:01+00'), (2, 1, 'X_APP', now(), NULL)")
    assert su.execute("SELECT duration_seconds FROM atc_stats_session ORDER BY connection_id").fetchall() == [(3 * 3600 + 1,), (None,)]
    su.execute("RESET timezone")


def test_connection_id_is_unique(su: psycopg.Connection) -> None:
    su.execute("INSERT INTO atc_stats_session (connection_id, cid, callsign, session_start) VALUES (1, 1, 'X_APP', now())")
    with pytest.raises(psycopg.errors.UniqueViolation):
        su.execute("INSERT INTO atc_stats_session (connection_id, cid, callsign, session_start) VALUES (1, 2, 'Y_APP', now())")


# ---- indexes ----------------------------------------------------------------------------------------------------
def test_the_four_indexes_exist_with_the_designed_shape(su: psycopg.Connection) -> None:
    idx = {r[0]: r[1] for r in su.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'atc_stats_session'")}
    assert set(idx) == {"atc_stats_session_pkey", "atc_stats_session_cid_idx", "atc_stats_session_callsign_idx",
                        "atc_stats_session_start_brin"}
    assert "(connection_id)" in idx["atc_stats_session_pkey"]
    assert "(cid, session_start DESC)" in idx["atc_stats_session_cid_idx"]
    assert "(callsign, session_start DESC)" in idx["atc_stats_session_callsign_idx"]
    assert "USING brin (session_start)" in idx["atc_stats_session_start_brin"]
    assert not [n for n, d in idx.items() if "position_type" in d]  # none on position_type at first


# ---- roles ------------------------------------------------------------------------------------------------------
def as_role(role: str) -> psycopg.Connection:
    return psycopg.connect(sup.dsn(DB, role), autocommit=True)


def denied(c: psycopg.Connection, sql: str) -> bool:
    try:
        c.execute(sql)
    except psycopg.errors.InsufficientPrivilege:
        return True
    return False


def test_stats_loader_rights_are_exactly_the_two_tables(su: psycopg.Connection) -> None:
    with as_role("stats_loader") as c:
        c.execute("INSERT INTO atc_stats_session (connection_id, cid, callsign, session_start) VALUES (1, 1, 'X_APP', now())")
        c.execute("UPDATE atc_stats_session SET server = 'S'")
        assert c.execute("SELECT count(*) FROM atc_stats_session").fetchone() == (1,)
        c.execute("DELETE FROM atc_stats_session")
        c.execute("INSERT INTO atc_stats_run_log DEFAULT VALUES")
        c.execute("UPDATE atc_stats_run_log SET status = 'success'")
        assert c.execute("SELECT count(*) FROM atc_stats_run_log").fetchone() == (1,)
        assert denied(c, "DELETE FROM atc_stats_run_log")          # the DELETE exception is for the session table only
        assert denied(c, "TRUNCATE atc_stats_session")
        assert denied(c, "CREATE TABLE qa_x (a int)")
        assert denied(c, "DROP TABLE atc_stats_session")
        assert denied(c, "ALTER TABLE atc_stats_session ADD COLUMN z int")
        for other in ("poll_log", "pilot_session", "controller_session", "network_snapshot", "app_setting"):
            assert denied(c, f"SELECT 1 FROM {other} LIMIT 1"), other
            assert denied(c, f"INSERT INTO {other} DEFAULT VALUES"), other


def test_stats_loader_has_no_other_table_rights_at_all(su: psycopg.Connection) -> None:
    rows = su.execute(
        "SELECT c.relname, has_table_privilege('stats_loader', c.oid, 'SELECT'), has_table_privilege('stats_loader', c.oid, 'INSERT'), "
        "has_table_privilege('stats_loader', c.oid, 'UPDATE'), has_table_privilege('stats_loader', c.oid, 'DELETE') "
        "FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'v', 'm', 'p')").fetchall()
    for name, sel, ins, upd, dele in rows:
        if name == "atc_stats_session":
            assert (sel, ins, upd, dele) == (True, True, True, True)
        elif name == "atc_stats_run_log":
            assert (sel, ins, upd, dele) == (True, True, True, False)
        else:
            assert not (sel or ins or upd or dele), name


def test_stats_loader_role_is_not_privileged_and_has_utc(su: psycopg.Connection) -> None:
    r = su.execute("SELECT rolsuper, rolcreatedb, rolcreaterole, rolcanlogin, rolconfig FROM pg_roles WHERE rolname = 'stats_loader'").fetchone()
    assert r[:4] == (False, False, False, True)  # type: ignore[index]
    with as_role("stats_loader") as c:
        assert c.execute("SHOW timezone").fetchone() == ("UTC",)
    assert su.execute("SHOW timezone").fetchone() == ("Pacific/Auckland",)  # the database default is not UTC


def test_grafana_ro_cannot_read_the_new_tables(su: psycopg.Connection) -> None:
    with as_role("grafana_ro") as c:
        assert denied(c, "SELECT 1 FROM atc_stats_session")
        assert denied(c, "SELECT 1 FROM atc_stats_run_log")


def test_watchdog_ro_reads_the_run_log_only_and_cannot_write(su: psycopg.Connection) -> None:
    su.execute("INSERT INTO atc_stats_run_log (status) VALUES ('success')")
    with as_role("watchdog_ro") as c:
        assert c.execute("SELECT count(*) FROM atc_stats_run_log").fetchone() == (1,)
        assert denied(c, "SELECT 1 FROM atc_stats_session")
        with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction)):
            c.execute("INSERT INTO atc_stats_run_log (status) VALUES ('failed')")


def test_the_collector_role_has_no_rights_on_the_new_tables(su: psycopg.Connection) -> None:
    for role in ("collector", "backup_ro"):
        sel = su.execute("SELECT has_table_privilege(%s, 'atc_stats_session', 'INSERT')", (role,)).fetchone()
        assert sel == (False,), role


def test_the_run_log_sequence_is_usable_by_stats_loader(su: psycopg.Connection) -> None:
    with as_role("stats_loader") as c:
        for _ in range(2):
            c.execute("INSERT INTO atc_stats_run_log DEFAULT VALUES")
        assert c.execute("SELECT count(*) FROM atc_stats_run_log").fetchone() == (2,)
