"""Structured logging configuration for MNM Controller.

Provides JSON or human-readable log output, configurable via environment variables:
  MNM_LOG_FORMAT: json | text (default: json)
  MNM_LOG_LEVEL: DEBUG | INFO | WARNING | ERROR (default: INFO)

Every log entry includes: timestamp, level, module, event, message, context.
An in-memory ring buffer stores recent entries for the /api/logs endpoint.
"""

import collections
import json
import logging
import os
import time
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Ring buffer for in-memory log access via API
# ---------------------------------------------------------------------------

MAX_LOG_BUFFER = 10000
_log_buffer: collections.deque = collections.deque(maxlen=MAX_LOG_BUFFER)


def get_recent_logs(
    level: str | None = None,
    module: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return recent log entries, optionally filtered."""
    results = []
    for entry in reversed(_log_buffer):
        if level and entry.get("level", "").upper() != level.upper():
            continue
        if module and entry.get("module", "") != module:
            continue
        results.append(entry)
        if len(results) >= limit:
            break
    return results


# ---------------------------------------------------------------------------
# Structured log formatter
# ---------------------------------------------------------------------------

# Secrets patterns to mask in log context
_SECRET_KEYS = frozenset({
    "password", "secret", "token", "api_key", "apikey", "auth_key",
    "priv_key", "community", "snmp_community", "credential",
})


def _mask_secrets(data: dict) -> dict:
    """Recursively mask secret values in a dict."""
    if not isinstance(data, dict):
        return data
    masked = {}
    for k, v in data.items():
        if any(s in k.lower() for s in _SECRET_KEYS):
            masked[k] = "***"
        elif isinstance(v, dict):
            masked[k] = _mask_secrets(v)
        else:
            masked[k] = v
    return masked


class StructuredFormatter(logging.Formatter):
    """Formats log records as JSON or human-readable text with structured fields."""

    def __init__(self, fmt_type: str = "json"):
        super().__init__()
        self.fmt_type = fmt_type

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "module": getattr(record, "mnm_module", record.name.split(".")[-1]),
            "event": getattr(record, "mnm_event", ""),
            "message": record.getMessage(),
            "context": _mask_secrets(getattr(record, "mnm_context", {})),
        }

        # Add exception info if present
        if record.exc_info and record.exc_info[1]:
            entry["context"]["exception"] = str(record.exc_info[1])
            if record.levelno >= logging.DEBUG:
                import traceback
                entry["context"]["traceback"] = traceback.format_exception(*record.exc_info)

        # Store in ring buffer
        _log_buffer.append(entry)

        if self.fmt_type == "json":
            return json.dumps(entry, default=str)
        else:
            # Human-readable text format
            ctx = entry["context"]
            ctx_str = " ".join(f"{k}={v}" for k, v in ctx.items()) if ctx else ""
            event = entry["event"]
            event_str = f"[{event}] " if event else ""
            return (
                f"{entry['timestamp'][:19]} {entry['level']:7s} "
                f"{entry['module']:20s} {event_str}{entry['message']}"
                f"{' | ' + ctx_str if ctx_str else ''}"
            )


# ---------------------------------------------------------------------------
# Structured logger helper
# ---------------------------------------------------------------------------

class StructuredLogger:
    """Wrapper around stdlib logger that adds structured fields."""

    def __init__(self, name: str, module: str | None = None):
        self._logger = logging.getLogger(name)
        self._module = module or name.split(".")[-1]

    def _log(self, level: int, event: str, message: str, context: dict | None = None, **kwargs):
        extra = {
            "mnm_module": self._module,
            "mnm_event": event,
            "mnm_context": context or {},
        }
        self._logger.log(level, message, extra=extra, **kwargs)

    def debug(self, event: str, message: str, context: dict | None = None):
        self._log(logging.DEBUG, event, message, context)

    def info(self, event: str, message: str, context: dict | None = None):
        self._log(logging.INFO, event, message, context)

    def warning(self, event: str, message: str, context: dict | None = None):
        self._log(logging.WARNING, event, message, context)

    def error(self, event: str, message: str, context: dict | None = None, exc_info=None):
        self._log(logging.ERROR, event, message, context, exc_info=exc_info)


# ---------------------------------------------------------------------------
# Setup — call once at startup
# ---------------------------------------------------------------------------

_configured = False


def setup_logging():
    """Configure the root logger with structured formatting. Call once at startup."""
    global _configured
    if _configured:
        return
    _configured = True

    log_format = os.environ.get("MNM_LOG_FORMAT", "json").lower()
    log_level = os.environ.get("MNM_LOG_LEVEL", "INFO").upper()

    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter(fmt_type=log_format))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, log_level, logging.INFO))

    # Quiet noisy third-party loggers
    for noisy in ("httpx", "httpcore", "urllib3", "docker", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
