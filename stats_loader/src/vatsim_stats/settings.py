"""Settings, read from the environment. Docker Compose declares them with their defaults; none is hard-coded here.

The only value that is not a STATS_* setting is DATABASE_URL (the connection, with the stats_loader password)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time

from .windows import oldest_allowed

MIN_INTERVAL_FLOOR = 5.0  # seconds between requests: the setting can raise this, never lower it


class SettingsError(ValueError):
    """A setting is missing or not usable; the loader refuses to start."""


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    run_at: time  # UTC
    min_interval_seconds: float
    page_size: int
    overlap_days: int
    retention_years: int


def _whole(env: Mapping[str, str], name: str, minimum: int) -> int:
    raw = env.get(name, "").strip()
    if not (raw.isascii() and raw.isdecimal()) or int(raw) < minimum:
        raise SettingsError(f"{name} must be a whole number of {minimum} or more, got {raw!r}")
    return int(raw)


def load(env: Mapping[str, str]) -> Settings:
    url = env.get("DATABASE_URL", "").strip()
    if not url:
        raise SettingsError("DATABASE_URL is not set")
    try:
        hh, mm = env.get("STATS_RUN_AT", "").strip().split(":")
        run_at = time(int(hh), int(mm))
    except ValueError:
        raise SettingsError(f"STATS_RUN_AT must be HH:MM in UTC, got {env.get('STATS_RUN_AT')!r}") from None
    try:
        interval = float(env.get("STATS_MIN_INTERVAL_SECONDS", ""))
    except ValueError:
        raise SettingsError("STATS_MIN_INTERVAL_SECONDS must be a number") from None
    years = _whole(env, "STATS_RETENTION_YEARS", 1)
    try:
        oldest_allowed(datetime.now(UTC), years)
    except ValueError:
        raise SettingsError(f"STATS_RETENTION_YEARS is too large: {years} years goes back before the year 1") from None
    return Settings(
        database_url=url,
        run_at=run_at,
        min_interval_seconds=max(MIN_INTERVAL_FLOOR, interval),
        page_size=_whole(env, "STATS_PAGE_SIZE", 1),
        overlap_days=_whole(env, "STATS_OVERLAP_DAYS", 0),
        retention_years=years,
    )
