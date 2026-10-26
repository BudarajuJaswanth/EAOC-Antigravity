"""
EAOC Observability Log Formatters
Provides machine-readable StructuredJsonFormatter and development console formatter.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict
from eaoc.config import get_settings
from eaoc.observability.context import get_current_context
from eaoc.observability.redaction import redact_sensitive_data


class StructuredJsonFormatter(logging.Formatter):
    """
    JSON Log Formatter for structured production & ingestion-friendly logging.
    Incorporate contextvars tracing identifiers and sensitive data redaction.
    """

    def format(self, record: logging.LogRecord) -> str:
        settings = get_settings()
        now_utc = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()

        log_data: Dict[str, Any] = {
            "timestamp": now_utc,
            "log_level": record.levelname,
            "logger": record.name,
            "message": redact_sensitive_data(record.getMessage()),
            "environment": settings.ENVIRONMENT,
            "service": "eaoc-core",
        }

        # Inject contextvars tracing fields if set
        ctx = get_current_context()
        for k, v in ctx.items():
            if v is not None:
                log_data[k] = v

        # Inject custom extra attributes passed to logger
        custom_attrs = [
            "event_name",
            "duration_ms",
            "error_type",
            "error_code",
            "workflow_id",
            "agent_id",
            "tool_id",
            "payload",
        ]
        for attr in custom_attrs:
            if hasattr(record, attr):
                val = getattr(record, attr)
                if val is not None:
                    log_data[attr] = redact_sensitive_data(val)

        # Include sanitized exception info if present
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                log_data["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else "Exception"
                log_data["exception"] = redact_sensitive_data(record.exc_text)

        return json.dumps(log_data)


class ConsoleDevelopmentFormatter(logging.Formatter):
    """
    Console Log Formatter for readable local development logging.
    """

    def format(self, record: logging.LogRecord) -> str:
        settings = get_settings()
        now_str = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        ctx = get_current_context()
        req_str = f" [{ctx['request_id']}]" if "request_id" in ctx else ""

        event_str = f" event={record.event_name}" if hasattr(record, "event_name") else ""
        dur_str = f" duration={record.duration_ms}ms" if hasattr(record, "duration_ms") else ""

        msg = redact_sensitive_data(record.getMessage())
        base_log = f"{now_str} [{record.levelname:<5}] [{record.name}]{req_str}: {msg}{event_str}{dur_str}"

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                base_log += f"\n{redact_sensitive_data(record.exc_text)}"

        return base_log
