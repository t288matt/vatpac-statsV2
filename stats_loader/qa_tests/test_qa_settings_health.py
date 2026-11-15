# ruff: noqa: E501
"""QA: settings (whole numbers, the 5-second floor, refusing to start) and the healthcheck rule."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import psycopg
import pytest

from vatsim_stats import app, settings

ENV = {"DATABASE_URL": "postgresql://x", "STATS_RUN_AT": "06:00", "STATS_MIN_INTERVAL_SECONDS": "5", "STATS_PAGE_SIZE": "5000",
       "STATS_OVERLAP_DAYS": "2", "STATS_RETENTION_YEARS": "15"}


def load(**over: str) -> settings.Settings:
    return settings.load({**ENV, **over})


def test_defaults_parse() -> None:
    c = load()
    assert (c.run_at, c.min_interval_seconds, c.page_size, c.overlap_days, c.retention_years) == (time(6, 0), 5.0, 5000, 2, 15)


@pytest.mark.parametrize("bad", ["0", "-1", "1.5", "abc", "", " ", "1e1", "0.9", "٣", "15 years", "+3"])
def test_retention_must_be_a_whole_number_of_1_or_more(bad: str) -> None:
    with pytest.raises(settings.SettingsError):
        load(STATS_RETENTION_YEARS=bad)


@pytest.mark.parametrize("good", ["1", "3", "15", "20"])
def test_retention_whole_numbers_accepted(good: str) -> None:
    assert load(STATS_RETENTION_YEARS=good).retention_years == int(good)


@pytest.mark.parametrize(("given", "used"), [("1", 5.0), ("0", 5.0), ("4.99", 5.0), ("5", 5.0), ("7.5", 7.5), ("30", 30.0)])
def test_interval_never_goes_below_five_seconds(given: str, used: float) -> None:
    assert load(STATS_MIN_INTERVAL_SECONDS=given).min_interval_seconds == used


@pytest.mark.parametrize("name", ["STATS_PAGE_SIZE", "STATS_OVERLAP_DAYS"])
def test_other_whole_number_settings_reject_junk(name: str) -> None:
    for bad in ("abc", "-1", "2.5", ""):
        with pytest.raises(settings.SettingsError):
            load(**{name: bad})


def test_missing_database_url_and_bad_run_time_are_refused() -> None:
    with pytest.raises(settings.SettingsError):
        settings.load({k: v for k, v in ENV.items() if k != "DATABASE_URL"})
    for bad in ("6", "25:00", "06:61", "noon", ""):
        with pytest.raises(settings.SettingsError):
            load(STATS_RUN_AT=bad)


def test_the_loader_refuses_to_start_on_a_bad_retention(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    for k, v in {**ENV, "STATS_RETENTION_YEARS": "0"}.items():
        monkeypatch.setenv(k, v)
    assert app.main(["x"]) != 0
    assert "STATS_RETENTION_YEARS" in caplog.text


# ---- healthcheck: healthy if a success within 36 h, or a running run with coalesce(last_day_at, started_at) within 36 h ----



def _row(su: psycopg.Connection, status: str, started_h: float, finished_h: float | None = None, last_day_h: float | None = None) -> None:
    su.execute("INSERT INTO atc_stats_run_log (started_at, finished_at, last_day_at, status) VALUES "
               "(now() - make_interval(hours => %s), now() - make_interval(hours => %s), now() - make_interval(hours => %s), %s)",
               (started_h, finished_h, last_day_h, status))


@pytest.mark.parametrize(("rows", "healthy"), [
    ([("success", 40, 35, None)], True),
    ([("success", 40, 37, None)], False),
    ([("running", 10, None, None)], True),            # no day written yet: started 10 h ago counts
    ([("running", 40, None, None)], False),
    ([("running", 100, None, 10)], True),             # long run that wrote a day 10 h ago
    ([("running", 1, None, 40)], False),              # last_day_at (40 h) wins over started_at (1 h): coalesce
    ([("failed", 1, 1, None)], False),
    ([("stopped", 1, 1, None)], False),
    ([("interrupted", 1, 1, None)], False),
    ([("failed", 1, 1, None), ("success", 30, 29, None)], True),
    ([], False),
])
def test_healthcheck_rule(conn: psycopg.Connection, su: psycopg.Connection, rows: list, healthy: bool) -> None:
    for r in rows:
        _row(su, *r)
    now = datetime.now(UTC)
    from vatsim_stats import store

    assert store.healthy(conn, now, 36) is healthy
    assert timedelta(hours=36) == timedelta(hours=app.HEALTH_HOURS)


def test_health_command_exit_codes(monkeypatch: pytest.MonkeyPatch, su: psycopg.Connection) -> None:
    import qa_stats_loader_support as sup

    for k, v in {**ENV, "DATABASE_URL": sup.dsn("qa_loader_test", "stats_loader")}.items():
        monkeypatch.setenv(k, v)
    assert app.main(["x", "health"]) == 1  # nothing has run
    _row(su, "success", 3, 2)
    assert app.main(["x", "health"]) == 0
