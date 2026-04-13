"""
shared.logging_config
---------------------
Structured JSON logging for all LibraryAI services.

Usage::

    from shared.logging_config import setup_logging

    setup_logging(service_name="eep", log_level="INFO")

    # After setup, all logger calls emit newline-delimited JSON to stdout.
    import logging
    logger = logging.getLogger(__name__)
    logger.info("job created", extra={"job_id": "abc"})
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any


class _JSONFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": os.environ.get("SERVICE_NAME", "unknown"),
        }

        # Include any extra fields passed via the `extra` kwarg
        for key, value in record.__dict__.items():
            if key not in _STDLIB_LOG_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


# Standard LogRecord attributes to exclude from the `extra` passthrough
_STDLIB_LOG_ATTRS = frozenset(
    {
        "args",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


def setup_logging(
    service_name: str = "unknown",
    log_level: str = "INFO",
) -> None:
    """
    Configure the root logger to emit structured JSON to stdout.

    Call once at service startup, before ``app = FastAPI()``.

    Args:
        service_name: Embedded in every log line as ``"service"``.
        log_level:    Standard Python level name (INFO, DEBUG, WARNING, …).
    """
    os.environ["SERVICE_NAME"] = service_name

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JSONFormatter())
    root.addHandler(handler)

    # Reduce noise from high-volume third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
