"""Collector settings from environment variables (names as in .env.example; defaults in brackets).

POSTGRES_HOST [db] POSTGRES_PORT [5432] POSTGRES_DB [vatpac] POSTGRES_USER [vatpac] POSTGRES_PASSWORD: database
connection, assembled with make_conninfo (DATABASE_URL overrides all five). VATSIM_FEED_URL: fallback feed URL when
discovery through VATSIM_STATUS_URL (data.v3[0]) fails. POLL_INTERVAL_SECONDS [15] (5..300). HTTP_TIMEOUT_SECONDS [10].
TEXT_MAX_LENGTH [2000]: free-text fields are cut to this many characters at ingest (each cut counts in
poll_log.rejected_values). FEED_MAX_FUTURE_SECONDS [120]: a snapshot dated further ahead of the clock is a feed error.
POSITION_RETENTION_DAYS [90]. AIRBORNE_GAP_CAP_SECONDS [40] and HOME_ICAO_PREFIXES [Y, comma separated] are published
to app_setting for the SQL views. LOG_LEVEL [INFO]. POLYGON_PATH / WHITELIST_PATH: reference files [/app/config/...].
HEALTH_PORT [8080]. CIRCUIT_FAILURE_THRESHOLD [5] consecutive failures open the breaker; CIRCUIT_SLOW_INTERVAL_SECONDS
[60] is the cadence while open (polling never stops).
ARCHIVE_ENABLED [true] ARCHIVE_DIR [/archive] ARCHIVE_COMPACT_EVERY_S [300] ARCHIVE_FULL_EVERY_S [3600]
ARCHIVE_COMPACT_KEEP_DAYS [30] ARCHIVE_FULL_KEEP_DAYS [7] ARCHIVE_ANOMALY_KEEP_DAYS [90] ARCHIVE_MAX_MB [1024]
ARCHIVE_GAP_S [35]: the rolling raw-feed archive (archive.py, docs/RUNBOOKS/feed-archive.md).
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
    text_max_length: int = Field(2000, ge=100, le=100_000)
    feed_max_future_seconds: int = Field(120, ge=15, le=3600)
    position_retention_days: int = Field(90, ge=2, le=3650)
    airborne_gap_cap_seconds: int = Field(40, ge=15, le=600)
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
    archive_gap_s: float = Field(35.0, ge=16, le=3600)

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
