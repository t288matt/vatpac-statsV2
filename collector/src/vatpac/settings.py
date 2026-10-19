"""Collector settings from environment variables: names, defaults and ranges are the fields below; each variable is
described in docs/CONFIG_MAP.md. DATABASE_URL overrides the five POSTGRES_* values. Derived from POLL_INTERVAL_SECONDS
(ADR-024) unless set: AIRBORNE_GAP_CAP_SECONDS 2.5x, ARCHIVE_GAP_S 2x + 5, and the /health ages (health.py).
Published to app_setting for the SQL views: POLL_INTERVAL_SECONDS, AIRBORNE_GAP_CAP_SECONDS, HOME_ICAO_PREFIXES."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False, env_ignore_empty=True)

    database_url: str | None = None
    postgres_host: str = "db"
    postgres_port: int = Field(5432, ge=1, le=65535)
    postgres_db: str = "vatpac"
    postgres_user: str = "vatpac"
    postgres_password: str = ""
    vatsim_feed_url: str = "https://data.vatsim.net/v3/vatsim-data.json"
    vatsim_status_url: str = "https://status.vatsim.net/status.json"
    poll_interval_seconds: float = Field(30.0, ge=5, le=300)
    http_timeout_seconds: float = Field(10.0, ge=1, le=60)
    text_max_length: int = Field(2000, ge=100, le=100_000)
    feed_max_future_seconds: int = Field(120, ge=15, le=3600)
    position_retention_days: int = Field(180, ge=2, le=3650)
    airborne_gap_cap_seconds: int | None = Field(None, ge=15, le=600)  # None: 2.5 x poll interval
    home_icao_prefixes: str = "Y"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    polygon_path: str = "/app/config/australian_airspace_polygon.json"
    whitelist_path: str = "/app/config/controller_callsigns_list.txt"
    health_port: int = Field(8080, ge=1, le=65535)
    circuit_failure_threshold: int = Field(5, ge=1, le=1000)
    circuit_slow_interval_seconds: float = Field(60.0, ge=5, le=3600)
    archive_enabled: bool = True
    archive_dir: str = "/archive"
    archive_compact_every_s: int = Field(300, ge=1, le=86400)
    archive_full_every_s: int = Field(3600, ge=1, le=604800)
    archive_compact_keep_days: float = Field(30.0, gt=0, le=3650)
    archive_full_keep_days: float = Field(7.0, gt=0, le=3650)
    archive_anomaly_keep_days: float = Field(90.0, gt=0, le=3650)
    archive_max_mb: int = Field(1024, ge=1, le=1_000_000)
    archive_gap_s: float | None = Field(None, ge=16, le=3600)  # None: 2 x poll interval + 5

    @model_validator(mode="after")
    def _derive_from_poll_interval(self) -> Self:
        """Everything that scales with the cadence follows POLL_INTERVAL_SECONDS unless set explicitly."""
        if self.airborne_gap_cap_seconds is None:
            self.airborne_gap_cap_seconds = max(15, int(round(2.5 * self.poll_interval_seconds)))
        if self.archive_gap_s is None:
            self.archive_gap_s = max(16.0, 2 * self.poll_interval_seconds + 5)
        return self

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        return v.upper() if isinstance(v, str) else v

    @field_validator("home_icao_prefixes")
    @classmethod
    def _prefixes(cls, v: str) -> str:
        parts = [p.strip().upper() for p in v.split(",") if p.strip()]
        if not parts or not all(p.isalpha() for p in parts):
            raise ValueError("HOME_ICAO_PREFIXES must be comma separated letters, e.g. 'Y' or 'Y,NZ'")
        return ",".join(parts)

    @field_validator("vatsim_feed_url", "vatsim_status_url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("must be an http(s) URL")
        return v

    @property
    def dsn(self) -> str:
        from psycopg.conninfo import make_conninfo

        return self.database_url or make_conninfo(
            host=self.postgres_host, port=self.postgres_port, dbname=self.postgres_db,
            user=self.postgres_user, password=self.postgres_password)
