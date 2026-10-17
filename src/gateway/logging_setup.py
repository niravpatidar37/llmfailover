"""Structured JSON logs (one object per line, Cloud Logging compatible) with PII redaction on every record."""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from .redact import RedactingFilter, redact_obj

_LEVEL_TO_SEVERITY = {"WARNING": "WARNING", "ERROR": "ERROR", "CRITICAL": "CRITICAL", "INFO": "INFO"}


class JsonFormatter(logging.Formatter):
    """``severity`` and ``message`` are the fields Cloud Logging maps; JSON messages are merged in."""

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        out: dict[str, Any] = {
            "severity": _LEVEL_TO_SEVERITY.get(record.levelname, "DEBUG"),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "logger": record.name,
        }
        parsed: Any = None
        if msg.startswith("{"):
            try:
                parsed = json.loads(msg)
            except ValueError:
                parsed = None
        if isinstance(parsed, dict):
            out.update(redact_obj(parsed))
            out.setdefault("message", parsed.get("event", record.name))
        else:
            out["message"] = msg
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Root logger -> stdout as JSON, redacted. Idempotent."""
    root = logging.getLogger()
    if getattr(root, "_gw_json", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    root.handlers = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "LiteLLM", "litellm"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root._gw_json = True  # type: ignore[attr-defined]
