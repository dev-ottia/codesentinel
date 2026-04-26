"""
Structured logging with structlog.

JSON in production (LOG_FORMAT=json), coloured console in dev (LOG_FORMAT=console).
The root cause of the previous crash: structlog.PrintLoggerFactory creates a
PrintLogger which has no .name attribute. add_logger_name calls logger.name and
crashes. Fix: only use add_logger_name with the stdlib LoggerFactory (console mode).
"""
from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

from app.core.config import settings

_REDACT_KEYS = frozenset({
    "password", "secret", "token", "api_key", "authorization",
    "x-hub-signature-256", "client_secret", "access_token",
})


def _redact_processor(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict.keys()):
        if key.lower() in _REDACT_KEYS:
            event_dict[key] = "***REDACTED***"
    return event_dict


def _otel_processor(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    try:
        from opentelemetry import trace
        span = trace.get_current_span()
        if span and span.is_recording():
            ctx = span.get_span_context()
            event_dict["trace_id"] = format(ctx.trace_id, "032x")
            event_dict["span_id"]  = format(ctx.span_id, "016x")
    except Exception:
        pass
    return event_dict


def setup_logging() -> None:
    """Configure structlog. Must be called once before any logger is used."""
    log_level  = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    use_json   = getattr(settings, "LOG_FORMAT", "console").lower() == "json"

    # Base processors — safe for both PrintLogger and stdlib Logger
    base: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _redact_processor,
        _otel_processor,
    ]

    if use_json:
        # ── Production JSON ───────────────────────────────────────────────────
        # PrintLoggerFactory → PrintLogger (no .name) — do NOT use add_logger_name
        structlog.configure(
            processors=base + [
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(log_level),
            context_class=dict,
            logger_factory=structlog.PrintLoggerFactory(sys.stdout),
            cache_logger_on_first_use=True,
        )
        # Silence stdlib noise in JSON mode
        logging.basicConfig(level=logging.WARNING, format="%(message)s", stream=sys.stdout)

    else:
        # ── Development coloured console ──────────────────────────────────────
        # stdlib LoggerFactory → stdlib Logger (has .name) — add_logger_name is safe
        structlog.configure(
            processors=[structlog.stdlib.add_logger_name] + base + [
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            wrapper_class=structlog.make_filtering_bound_logger(log_level),
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )
        fmt = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(colors=True),
            ],
        )
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(fmt)
        root = logging.getLogger()
        root.handlers = [handler]
        root.setLevel(log_level)

    # Suppress noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "arq", "httpx", "fastapi", "watchfiles"):
        logging.getLogger(name).setLevel(log_level)


def get_logger(name: str) -> structlog.BoundLogger:
    return structlog.get_logger(name)
