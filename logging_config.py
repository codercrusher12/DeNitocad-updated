"""
Structured logging setup.

The original codebase used bare `print()` calls for everything -
"Parsing description: ...", "Detected part type: ...", validation
warnings, and full tracebacks on failure. That's fine on a laptop with
one terminal open; on Railway (or anywhere with a real log aggregator)
it means no log level, no timestamps, no request correlation, and no
way to filter "just errors" or ship logs to something queryable.

This module configures the stdlib `logging` package once at startup
(`configure_logging()`, called from web_app.py's lifespan) with either:
  - JSON lines (LOG_FORMAT=json) - one object per line, machine-parseable,
    the format you actually want once this runs somewhere with a log
    aggregator (Railway, Datadog, CloudWatch, etc).
  - Human-readable console output (LOG_FORMAT=console, the default) -
    for local dev, close to what `print()` looked like but with a level
    and timestamp prefix.

Every module gets its own logger via `get_logger(__name__)` instead of
importing `print`, which also means log level filtering (LOG_LEVEL env
var) actually works - "only show me warnings and above" wasn't possible
before this.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

# Populated per-request by the correlation-id middleware in web_app.py so
# every log line emitted while handling a request - across modules -
# carries the same request_id without threading it through every function
# signature.
_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


def get_request_id() -> str:
    return _request_id_ctx.get()


def set_request_id(request_id: str | None = None) -> str:
    rid = request_id or uuid.uuid4().hex[:12]
    _request_id_ctx.set(rid)
    return rid


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Allow callers to pass structured fields via `extra={"field": ...}`
        # without them getting silently dropped.
        reserved = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
            "message",
            "asctime",
            "request_id",
        }
        for key, value in record.__dict__.items():
            if key not in reserved:
                payload[key] = value
        return json.dumps(payload, default=str)


_CONSOLE_FORMAT = "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"

_configured = False


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Idempotent - safe to call multiple times (e.g. once at import time
    for CLI usage, again from the FastAPI lifespan)."""
    global _configured
    root = logging.getLogger()
    root.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RequestIdFilter())
    if fmt == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        formatter = logging.Formatter(_CONSOLE_FORMAT, datefmt="%H:%M:%S")
        handler.setFormatter(formatter)

    if _configured:
        root.handlers.clear()
    root.addHandler(handler)

    # Quiet down noisy third-party loggers unless we're at DEBUG.
    if level != "DEBUG":
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("botocore").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class log_duration:
    """Context manager / decorator-free helper for timing a block and
    logging it - used around the parse/validate/generate/export stages in
    cad_generator.py so slow steps are visible without manual
    time.time() bookkeeping at every call site.

    Usage:
        with log_duration(logger, "cadquery_export"):
            cq.exporters.export(workplane, path)
    """

    def __init__(self, logger: logging.Logger, step: str, **extra: Any):
        self.logger = logger
        self.step = step
        self.extra = extra
        self._start = 0.0

    def __enter__(self) -> "log_duration":
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        elapsed_ms = (time.perf_counter() - self._start) * 1000
        if exc_type is None:
            self.logger.info(
                "%s completed in %.1fms", self.step, elapsed_ms,
                extra={"step": self.step, "duration_ms": round(elapsed_ms, 1), **self.extra},
            )
        else:
            self.logger.warning(
                "%s failed after %.1fms: %s", self.step, elapsed_ms, exc,
                extra={"step": self.step, "duration_ms": round(elapsed_ms, 1), **self.extra},
            )
