"""The loader against a throwaway Postgres 16 (the real migrations and db/roles.sql) and the fake API. The loader's own
connection acts as the stats_loader role, so these pass only with the rights that role really has."""

from __future__ import annotations

from datetime import date, time, timedelta
from typing import Any

import psycopg
import pytest

from fakes import FakeApi, FakeClock, at
from vatsim_stats import store
from vatsim_stats.api import Api, parse_session
from vatsim_stats.app import run_once, serve
from vatsim_stats.settings import Settings

pytestmark = pytest.mark.integration


def cfg(**over: Any) -> Settings:
    base: dict[str, Any] = {"database_url": "unused", "run_at": time(6), "min_interval_seconds": 5.0, "page_size": 100,
                            "overlap_days": 2, "retention_years": 3}
    return Settings(**{**base, **over})


def api_for(fake: FakeApi, clock: FakeClock) -> Api:
    return Api(fake.client(), clock, 5.0, 100)


def populate(fake: FakeApi, first: date, last: date, per_day: int = 2) -> int:
    n, day = 0, first
    while day <= last:
        for i in range(per_day):
            fake.add(7000 + n, "ML_TWR", at(day.year, day.month, day.day, 3, i))
            n += 1
        day += timedelta(days=1)
    return n


def run(conn: Any, fake: FakeApi, clock: FakeClock, **over: Any) -> str:
    return run_once(conn, api_for(fake, clock), clock, cfg(**over))


def one(conn: Any, sql: str, *args: Any) -> Any:
    return conn.execute(sql, args).fetchone()[0]


def runs(conn: Any) -> list[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM atc_stats_run_log ORDER BY id")
    names = [d.name for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


def on_the_eighth(conn: Any) -> int:
    return int(one(conn, "SELECT count(*) FROM atc_stats_session "
                         "WHERE (session_start AT TIME ZONE 'UTC')::date = '2026-11-08'"))


def test_the_migration_made_the_tables_and_the_named_indexes(conn: Any) -> None:
    got = {r[0]: r[1] for r in conn.execute(
        "SELECT i.relname, a.amname FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
        "JOIN pg_am a ON a.oid = i.relam "
        "WHERE x.indrelid = 'atc_stats_session'::regclass").fetchall()}
    assert got == {"atc_stats_session_pkey": "btree", "atc_stats_session_cid_idx": "btree",
                   "atc_stats_session_callsign_idx": "btree", "atc_stats_session_start_brin": "brin"}
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO atc_stats_run_log (status) VALUES ('finished')")


def test_first_run_loads_three_recent_days_then_backfills_to_the_start_of_history(loader_conn: Any, fake: FakeApi,
                                                                                   clock: FakeClock) -> None:
    expected = populate(fake, date(2026, 11, 1), date(2026, 11, 14))
    assert run(loader_conn, fake, clock) == "success"
    [first] = runs(loader_conn)
    assert first["days_loaded"] == 14 and first["sessions_inserted"] == expected
    assert first["empty_dates"] == [date(2026, 10, 31)]  # all 24 hours empty: the start of history, so it stopped there
    assert first["requests"] == len(fake.calls) == 14 + 25
    assert [c.start.date() for c in fake.calls[:3]] == [date(2026, 11, 14), date(2026, 11, 13), date(2026, 11, 12)]
    # the next run does not ask for the empty date, or anything older, again
    fake.calls.clear()
    assert run(loader_conn, fake, clock) == "success"
    assert len(fake.calls) == 3
    assert one(loader_conn, "SELECT count(*) FROM atc_stats_session") == expected


def test_idempotent_reload_position_type_duration_atis_and_a_later_close(loader_conn: Any, fake: FakeApi,
                                                                           clock: FakeClock) -> None:
    fake.add(1, "ml_app", at(2026, 11, 13, 1), at(2026, 11, 13, 2, 30))
    fake.add(2, "AB_CTR", at(2026, 11, 13, 2))
    fake.items[-1]["connection_id"]["end"] = None  # not closed yet
    fake.add(3, "ML_ATIS", at(2026, 11, 13, 3))
    run(loader_conn, fake, clock)
    assert [tuple(r) for r in loader_conn.execute(
        "SELECT cid, position_type, duration_seconds FROM atc_stats_session ORDER BY cid").fetchall()] == [
        (1, "APP", 5400), (2, "CTR", None)]
    again = run(loader_conn, fake, clock)
    assert again == "success" and one(loader_conn, "SELECT count(*) FROM atc_stats_session") == 2
    assert runs(loader_conn)[1]["sessions_inserted"] == 0 and runs(loader_conn)[1]["sessions_updated"] == 0
    loaded_at = one(loader_conn, "SELECT loaded_at FROM atc_stats_session WHERE cid = 2")
    fake.items[1]["connection_id"]["end"] = "2026-11-13T04:00:00Z"  # it closed later
    run(loader_conn, fake, clock)
    row = loader_conn.execute("SELECT duration_seconds, loaded_at < updated_at, loaded_at FROM atc_stats_session "
                              "WHERE cid = 2").fetchone()
    assert row[0] == 7200 and row[1] and row[2] == loaded_at
    assert runs(loader_conn)[2]["sessions_updated"] == 1


def test_a_day_is_written_in_one_transaction_or_not_at_all(loader_conn: Any, fake: FakeApi, clock: FakeClock) -> None:
    populate(fake, date(2026, 11, 14), date(2026, 11, 14), per_day=3)
    rows = [parse_session(i) for i in fake.items]
    rows[2]["cid"] = None  # breaks NOT NULL on the last row
    with pytest.raises(psycopg.errors.NotNullViolation):
        store.write_day(loader_conn, rows)
    assert one(loader_conn, "SELECT count(*) FROM atc_stats_session") == 0


def test_a_failed_day_writes_nothing_and_the_next_run_loads_it(
        loader_conn: Any, fake: FakeApi, clock: FakeClock) -> None:
    populate(fake, date(2026, 11, 5), date(2026, 11, 14))
    for i in range(5):  # five in one hour, so a page of 2 needs three requests while the count keeps growing
        fake.add(500 + i, "ML_TWR", at(2026, 11, 8, 22, 0, i))
    broken = [True]
    fake.on_call = lambda n: fake.add(2000 + n, "ML_TWR", at(2026, 11, 8, 22, 30)) if broken[0] else None
    api = Api(fake.client(), clock, 5.0, 2)
    assert run_once(loader_conn, api, clock, cfg(page_size=2)) == "failed"
    [log] = runs(loader_conn)
    assert log["failed_days"] == [date(2026, 11, 8)] and "2026-11-08" in log["error"]
    assert on_the_eighth(loader_conn) == 0
    broken[0] = False  # the day is older than the overlap: the missing-date search finds it
    assert run_once(loader_conn, api, clock, cfg(page_size=2)) == "success"
    assert on_the_eighth(loader_conn) > 5


def test_a_timeout_makes_hour_windows_and_a_restart_does_not_pay_for_it_again(loader_conn: Any, fake: FakeApi,
                                                                               clock: FakeClock) -> None:
    populate(fake, date(2026, 11, 1), date(2026, 11, 14))
    fake.slow_before = at(2026, 11, 10)

    def timeouts() -> int:
        return sum(c.hours > 1 and c.start < fake.slow_before for c in fake.calls)  # type: ignore[operator]

    assert run(loader_conn, fake, clock) == "success"
    assert timeouts() == 1  # one timeout, on 9 November; every older day then went straight to hour windows
    assert runs(loader_conn)[0]["hour_windows_from"] == date(2026, 11, 9)
    assert one(loader_conn, "SELECT count(*) FROM atc_stats_session") == 28
    loader_conn.execute("DELETE FROM atc_stats_session")  # as after a restart that finds old days missing again
    assert run(loader_conn, fake, clock) == "success"
    assert timeouts() == 1 and one(loader_conn, "SELECT count(*) FROM atc_stats_session") == 28


def test_runs_start_at_the_run_time_and_a_restart_after_a_success_starts_none(loader_conn: Any, fake: FakeApi) -> None:
    populate(fake, date(2026, 11, 1), date(2026, 11, 14))
    clock = FakeClock(at(2026, 11, 15, 6), stop_at=at(2026, 11, 17, 12))
    fake.clock = clock
    serve(loader_conn, api_for(fake, clock), clock, cfg())
    log = runs(loader_conn)
    # a first start runs at once, then one run at each STATS_RUN_AT (06:00 UTC); the stop signal arrives while it waits
    # for 18 November, so no run is cut short
    assert [r["status"] for r in log] == ["success"] * 3
    assert [r["started_at"] for r in log][1:] == [at(2026, 11, 16, 6), at(2026, 11, 17, 6)]
    # no run starts before the one before it has finished
    assert all(b["started_at"] >= a["finished_at"] for a, b in zip(log, log[1:], strict=False))
    before = len(log)
    restart = FakeClock(at(2026, 11, 17, 12), stop_at=at(2026, 11, 17, 23))
    fake.clock = restart
    serve(loader_conn, api_for(fake, restart), restart, cfg())
    assert len(runs(loader_conn)) == before  # the daily run of 17 November is done, so a restart waits for tomorrow


def test_a_crash_leaves_a_running_row_and_the_next_start_marks_it_interrupted(loader_conn: Any, fake: FakeApi,
                                                                              clock: FakeClock) -> None:
    populate(fake, date(2026, 11, 10), date(2026, 11, 14))
    crashed = store.start_run(loader_conn, clock.now())  # the process died here: the row is still `running`
    clock.stop_at = clock.now() + timedelta(seconds=30)  # stop during the 60 second wait after the start
    serve(loader_conn, api_for(fake, clock), clock, cfg())
    log = runs(loader_conn)
    assert log[0]["id"] == crashed and log[0]["status"] == "interrupted" and log[0]["finished_at"] is not None
    assert "crash" in log[0]["error"]
    assert fake.calls == []  # nothing was asked of the API during the 60 second wait
    assert log[1]["status"] == "stopped"  # the unfinished work resumed at once, then the clean stop ended it


def test_a_clean_stop_abandons_the_day_in_progress_and_marks_stopped(loader_conn: Any, fake: FakeApi,
                                                                                     clock: FakeClock) -> None:
    populate(fake, date(2026, 11, 1), date(2026, 11, 14), per_day=3)
    fake.on_call = lambda n: clock.stop.set() if n == 6 else None
    assert run(loader_conn, fake, clock) == "stopped"
    [log] = runs(loader_conn)
    assert log["status"] == "stopped" and log["finished_at"] is not None and log["days_loaded"] == 6
    assert one(loader_conn, "SELECT count(*) FROM atc_stats_session") == 6 * 3  # whole days only
    assert len(fake.calls) == 6  # no request after the signal


def test_retention_lower_deletes_older_sessions_and_raising_it_reloads_them(loader_conn: Any, fake: FakeApi,
                                                                             clock: FakeClock) -> None:
    # Scaled down: 3 years and 2 years stand for 15 and 10, one session a day, a fake clock, the fake API.
    total = populate(fake, date(2023, 11, 15), date(2026, 11, 14), per_day=1)
    assert run(loader_conn, fake, clock) == "success"
    assert one(loader_conn, "SELECT count(*) FROM atc_stats_session") == total
    # a failed run deletes nothing, even with a lower setting
    fake.script = [429]
    assert run(loader_conn, fake, clock, retention_years=2) == "failed"
    assert one(loader_conn, "SELECT count(*) FROM atc_stats_session") == total
    # a stopped run deletes nothing either
    first = len(fake.calls)
    fake.on_call = lambda n: clock.stop.set() if n == first + 2 else None
    assert run(loader_conn, fake, clock, retention_years=2) == "stopped"
    fake.on_call = None
    clock.stop.clear()
    assert one(loader_conn, "SELECT count(*) FROM atc_stats_session") == total
    # (a) lower: older than 15 November 2024 goes, nothing newer is touched, no flag needed
    older = one(loader_conn, "SELECT count(*) FROM atc_stats_session WHERE session_start < '2024-11-15 00:00+00'")
    assert 0 < older < total
    assert run(loader_conn, fake, clock, retention_years=2) == "success"
    assert one(loader_conn, "SELECT count(*) FROM atc_stats_session") == total - older
    assert one(loader_conn, "SELECT min(session_start) FROM atc_stats_session") == at(2024, 11, 15, 3)
    assert runs(loader_conn)[-1]["sessions_deleted"] == older
    # (b) raise again: nothing is deleted, the dropped dates are missing, and a large run loads them newest first
    fake.calls.clear()
    assert run(loader_conn, fake, clock, retention_years=3) == "success"
    last = runs(loader_conn)[-1]
    assert last["sessions_deleted"] == 0 and last["days_loaded"] == 3 + older
    assert one(loader_conn, "SELECT count(*) FROM atc_stats_session") == total
    starts = [c.start for c in fake.calls]
    assert starts == sorted(starts, reverse=True)
    dates = one(loader_conn, "SELECT count(DISTINCT (session_start AT TIME ZONE 'UTC')::date) FROM atc_stats_session")
    assert dates == total


def test_a_restart_after_a_failed_or_stopped_run_today_runs_at_once(loader_conn: Any, fake: FakeApi) -> None:
    populate(fake, date(2026, 11, 12), date(2026, 11, 14))
    clock = FakeClock(at(2026, 11, 15, 8), stop_at=at(2026, 11, 15, 12))
    fake.clock = clock
    for status in ("failed", "stopped"):
        run_id = store.start_run(loader_conn, clock.now())
        store.finish_run(loader_conn, run_id, status, "x", clock.now())
    serve(loader_conn, api_for(fake, clock), clock, cfg())
    log = runs(loader_conn)
    assert [r["status"] for r in log] == ["failed", "stopped", "success"]  # it did not wait for 06:00 tomorrow


def test_three_failed_days_in_a_row_stop_the_run_as_failed(loader_conn: Any, fake: FakeApi, clock: FakeClock) -> None:
    populate(fake, date(2026, 11, 1), date(2026, 11, 14), per_day=3)
    fake.on_call = lambda n: fake.add(3000 + n, "ML_TWR", fake.calls[-1].start + timedelta(seconds=5))  # never settles
    api = Api(fake.client(), clock, 5.0, 2)
    assert run_once(loader_conn, api, clock, cfg(page_size=2)) == "failed"
    [log] = runs(loader_conn)
    assert len(log["failed_days"]) == 3 and "3 failed days in a row" in log["error"]
    assert len({c.start.date() for c in fake.calls}) == 3  # no fourth day was tried
    assert one(loader_conn, "SELECT count(*) FROM atc_stats_session") == 0


def test_the_healthcheck_reads_the_run_log(loader_conn: Any, clock: FakeClock) -> None:
    now = clock.now()
    assert not store.healthy(loader_conn, now, 36)  # no run yet
    run_id = store.start_run(loader_conn, now - timedelta(days=3))
    assert not store.healthy(loader_conn, now, 36)  # running, started long ago, written no day
    store.update_run(loader_conn, run_id, requests=0)
    loader_conn.execute("UPDATE atc_stats_run_log SET started_at = %s", (now - timedelta(hours=2),))
    assert store.healthy(loader_conn, now, 36)  # just started: counts by its start time
    loader_conn.execute("UPDATE atc_stats_run_log SET started_at = %s", (now - timedelta(days=3),))
    store.update_run(loader_conn, run_id, last_day_at=now - timedelta(hours=1))
    assert store.healthy(loader_conn, now, 36)  # running and writing
    store.update_run(loader_conn, run_id, last_day_at=now - timedelta(hours=40))
    assert not store.healthy(loader_conn, now, 36)  # running but stuck
    store.finish_run(loader_conn, run_id, "success", None, now - timedelta(hours=2))
    assert store.healthy(loader_conn, now, 36)
    assert not store.healthy(loader_conn, now + timedelta(hours=40), 36)


def role_error(conn: Any, role: str, sql: str) -> bool:
    conn.execute(f"SET ROLE {role}")
    try:
        conn.execute(sql)
        return False
    except psycopg.errors.InsufficientPrivilege:
        return True
    finally:
        conn.execute("RESET ROLE")


def test_role_rights(conn: Any) -> None:
    insert = ("INSERT INTO atc_stats_session (connection_id, cid, callsign, session_start) "
              "VALUES (9, 9, 'AA_TWR', now())")
    for sql in (insert, "UPDATE atc_stats_session SET cid = 8", "DELETE FROM atc_stats_session",
                "SELECT * FROM atc_stats_session", "INSERT INTO atc_stats_run_log DEFAULT VALUES",
                "UPDATE atc_stats_run_log SET error = 'x'",
                "SELECT * FROM atc_stats_run_log"):
        assert not role_error(conn, "stats_loader", sql), sql
    # nothing else: no delete on the run log, no other table, no DDL
    for sql in ("DELETE FROM atc_stats_run_log", "TRUNCATE atc_stats_session", "SELECT * FROM controller_session",
                "SELECT * FROM poll_log", "CREATE TABLE stats_loader_x (a int)",
                "SELECT * FROM app_setting"):
        assert role_error(conn, "stats_loader", sql), sql
    for sql in ("SELECT * FROM atc_stats_session", "SELECT * FROM atc_stats_run_log"):
        assert role_error(conn, "grafana_ro", sql), sql  # grafana_ro reads views only
    assert not role_error(conn, "watchdog_ro", "SELECT * FROM atc_stats_run_log")
    assert role_error(conn, "watchdog_ro", "SELECT * FROM atc_stats_session")
    assert role_error(conn, "collector", "SELECT * FROM atc_stats_session")
    cfgs = one(conn, "SELECT rolconfig FROM pg_roles WHERE rolname = 'stats_loader'")
    assert "timezone=utc" in [c.lower() for c in cfgs]
