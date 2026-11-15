"""Settings: read from the environment, refused when unusable, with no defaults of their own."""

from __future__ import annotations

from datetime import time

import pytest

from vatsim_stats.settings import SettingsError, load

OK = {"DATABASE_URL": "postgresql://u:p@h/d", "STATS_RUN_AT": "06:00", "STATS_MIN_INTERVAL_SECONDS": "5",
      "STATS_PAGE_SIZE": "5000", "STATS_OVERLAP_DAYS": "2", "STATS_RETENTION_YEARS": "3"}


def test_a_full_environment_loads() -> None:
    cfg = load(OK)
    assert (cfg.run_at, cfg.page_size, cfg.overlap_days, cfg.retention_years) == (time(6, 0), 5000, 2, 3)


@pytest.mark.parametrize("bad", ["0", "-1", "2.5", "abc", "", " ", "3 years", "１５"])
def test_retention_must_be_a_whole_number_of_one_or_more(bad: str) -> None:
    with pytest.raises(SettingsError, match="STATS_RETENTION_YEARS"):
        load({**OK, "STATS_RETENTION_YEARS": bad})


def test_the_interval_never_goes_below_five_seconds() -> None:
    assert load({**OK, "STATS_MIN_INTERVAL_SECONDS": "1"}).min_interval_seconds == 5.0
    assert load({**OK, "STATS_MIN_INTERVAL_SECONDS": "7.5"}).min_interval_seconds == 7.5


@pytest.mark.parametrize("name", list(OK))
def test_every_setting_is_required_there_is_no_hidden_default(name: str) -> None:
    env = dict(OK)
    del env[name]
    with pytest.raises(SettingsError):
        load(env)


def test_a_bad_run_time_is_refused() -> None:
    with pytest.raises(SettingsError):
        load({**OK, "STATS_RUN_AT": "25:00"})
