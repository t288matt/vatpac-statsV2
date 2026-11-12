"""Issue 353: Grafana shows UK-style dates and 24-hour times everywhere.

Grafana's own defaults are US-style (`MM/DD`) and ISO-style (`YYYY-MM-DD`, `YYYY-MM`); `docker-compose.yml` replaces every one of them.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPOSE = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
KEYS = ("FULL_DATE", "INTERVAL_SECOND", "INTERVAL_MINUTE", "INTERVAL_HOUR", "INTERVAL_DAY", "INTERVAL_MONTH", "INTERVAL_YEAR")


def formats() -> dict[str, str]:
    found = dict(re.findall(r'^\s+GF_DATE_FORMATS_([A-Z_]+):\s*"([^"]+)"\s*$', COMPOSE, re.M))
    return {k: v for k, v in found.items() if k != "DEFAULT_TIMEZONE"}


def test_every_date_format_is_set() -> None:
    assert sorted(formats()) == sorted(KEYS)


def test_no_format_is_us_style_iso_style_or_twelve_hour() -> None:
    for key, value in formats().items():
        assert "/" not in value and not re.search(r"YYYY-MM|[Aa]\b|h{1,2}(?!H)|Do\b|MMM", value), f"{key}: {value}"
        if "MM" in value and "DD" in value:
            assert value.index("DD") < value.index("MM"), f"{key}: day must come before month: {value}"
        if "HH" in value:
            assert "HH:mm" in value, f"{key}: 24-hour time expected: {value}"


def test_the_full_date_is_day_month_year() -> None:
    assert formats()["FULL_DATE"] == "DD-MM-YYYY HH:mm:ss"
