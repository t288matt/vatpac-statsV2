"""Collector settings from environment variables (names as in .env.example; defaults in brackets).

POSTGRES_HOST [db] POSTGRES_PORT [5432] POSTGRES_DB [vatpac] POSTGRES_USER [vatpac] POSTGRES_PASSWORD:
database connection (DATABASE_URL overrides all five). VATSIM_FEED_URL: fallback feed URL when discovery
through VATSIM_STATUS_URL (data.v3[0]) fails. POLL_INTERVAL_SECONDS [15] (5..300). HTTP_TIMEOUT_SECONDS [10].
POSITION_RETENTION_DAYS [90]. AIRBORNE_GAP_CAP_SECONDS [40] and HOME_ICAO_PREFIXES [Y, comma separated] are
published to app_setting for the SQL views. LOG_LEVEL [INFO]. POLYGON_PATH / WHITELIST_PATH: reference files
[/app/config/...]. HEALTH_PORT [8080]. CIRCUIT_FAILURE_THRESHOLD [5] consecutive failures open the breaker;
CIRCUIT_SLOW_INTERVAL_SECONDS [60] is the cadence while open (polling never stops).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    database_url: str | None = None
    postgres_host: str = "db"
    postgres_port: int = Field(5432, ge=1, le=65535)
    postgres_db: str = "vatpac"
    postgres_user: str = "vatpac"
    postgres_password: str = ""
    vatsim_feed_url: str = "https://data.vatsim.net/v3/vatsim-data.json"
    vatsim_status_url: str = "https://status.vatsim.net/status.json"
    poll_interval_seconds: float = Field(15.0, ge=5, le=300)
    http_timeout_seconds: float = Field(10.0, ge=1, le=60)
    position_retention_days: int = Field(90, ge=2, le=3650)
    airborne_gap_cap_seconds: int = Field(40, ge=15, le=600)
    home_icao_prefixes: str = "Y"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    polygon_path: str = "/app/config/australian_airspace_polygon.json"
    whitelist_path: str = "/app/config/controller_callsigns_list.txt"
    health_port: int = Field(8080, ge=1, le=65535)
    circuit_failure_threshold: int = Field(5, ge=1, le=1000)
    circuit_slow_interval_seconds: float = Field(60.0, ge=5, le=3600)

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
