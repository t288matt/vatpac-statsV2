"""JSON-lines logging to stdout (one object per line, extra= fields included)."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from typing import Any

_STD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
            **{k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS},
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
