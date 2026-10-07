"""SPEC s7: vatpac.settings.Settings (env names as in .env.example).

Attribute names are assumed to be the lower-cased env names (pydantic-settings convention).
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from vatpac.settings import Settings

ENV_NAMES = [
    "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "GRAFANA_RO_PASSWORD", "VATSIM_FEED_URL",
    "VATSIM_STATUS_URL", "POLL_INTERVAL_SECONDS", "HTTP_TIMEOUT_SECONDS", "POSITION_RETENTION_DAYS",
    "AIRBORNE_GAP_CAP_SECONDS", "HOME_ICAO_PREFIXES", "LOG_LEVEL", "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID", "UPTIME_KUMA_PUSH_URL",
]


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    # the database credentials are the only values .env.example ships with placeholders for
    monkeypatch.setenv("POSTGRES_DB", "vatpac")
    monkeypatch.setenv("POSTGRES_USER", "vatpac")
    monkeypatch.setenv("POSTGRES_PASSWORD", "change-me")
    return monkeypatch


def make() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_defaults_match_env_example(clean_env: pytest.MonkeyPatch) -> None:
    s = make()
    assert s.vatsim_feed_url == "https://data.vatsim.net/v3/vatsim-data.json"
    assert s.vatsim_status_url == "https://status.vatsim.net/status.json"
    assert s.poll_interval_seconds == 15
    assert s.http_timeout_seconds == 10
    assert s.position_retention_days == 90
    assert s.airborne_gap_cap_seconds == 40
    assert str(s.log_level).upper() == "INFO"
    prefixes = s.home_icao_prefixes
    assert (prefixes == "Y") or list(prefixes) == ["Y"]


def test_env_overrides(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("POLL_INTERVAL_SECONDS", "30")
    clean_env.setenv("HTTP_TIMEOUT_SECONDS", "5")
    clean_env.setenv("POSITION_RETENTION_DAYS", "30")
    clean_env.setenv("AIRBORNE_GAP_CAP_SECONDS", "45")
    clean_env.setenv("LOG_LEVEL", "DEBUG")
    s = make()
    assert (s.poll_interval_seconds, s.http_timeout_seconds) == (30, 5)
    assert (s.position_retention_days, s.airborne_gap_cap_seconds) == (30, 45)
    assert str(s.log_level).upper() == "DEBUG"


@pytest.mark.parametrize(
    "name,value",
    [
        ("POLL_INTERVAL_SECONDS", "abc"),
        ("POLL_INTERVAL_SECONDS", "-5"),
        ("HTTP_TIMEOUT_SECONDS", "ten"),
        ("HTTP_TIMEOUT_SECONDS", "-1"),
        ("POSITION_RETENTION_DAYS", "forever"),
        ("POSITION_RETENTION_DAYS", "-90"),
        ("AIRBORNE_GAP_CAP_SECONDS", "x"),
        ("AIRBORNE_GAP_CAP_SECONDS", "-40"),
    ],
)
def test_invalid_numbers_rejected(clean_env: pytest.MonkeyPatch, name: str, value: str) -> None:
    clean_env.setenv(name, value)
    with pytest.raises(ValidationError):
        make()


def test_invalid_log_level_rejected(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("LOG_LEVEL", "LOUD")
    with pytest.raises(ValidationError):
        make()


def test_optional_values_may_be_empty(clean_env: pytest.MonkeyPatch) -> None:
    """Telegram / Uptime Kuma values are blank in .env.example; blanks must not break startup."""
    clean_env.setenv("TELEGRAM_BOT_TOKEN", "")
    clean_env.setenv("TELEGRAM_CHAT_ID", "")
    clean_env.setenv("UPTIME_KUMA_PUSH_URL", "")
    make()
