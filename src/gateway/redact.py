"""PII and secret redaction for request logs.

Applied to every request-log record (prompt previews, metadata) and, as defence in depth, by a logging
filter on the gateway logger. Patterns are deliberately conservative toward over-redaction.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SECRET = re.compile(
    r"(?:\b(?:sk|gsk|hf|gwk|xox[abpr]|ghp|gho|github_pat)[-_][A-Za-z0-9_\-]{10,}\b"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bBearer\s+[A-Za-z0-9._\-~+/]{12,}=*)"
)
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_PHONE = re.compile(r"(?<![\w])(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?![\w])")
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _card_sub(m: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    return "[REDACTED_CARD]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)


def redact(text: str) -> str:
    """Return ``text`` with emails, secrets, SSNs, card numbers, phone numbers and IPv4s replaced."""
    if not text:
        return text
    out = _SECRET.sub("[REDACTED_SECRET]", text)
    out = _EMAIL.sub("[REDACTED_EMAIL]", out)
    out = _SSN.sub("[REDACTED_SSN]", out)
    out = _CARD_CANDIDATE.sub(_card_sub, out)
    out = _PHONE.sub("[REDACTED_PHONE]", out)
    out = _IPV4.sub("[REDACTED_IP]", out)
    return out


def redact_obj(obj: Any) -> Any:
    """Recursively redact strings inside dicts/lists (for structured log records)."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact_obj(v) for v in obj]
    return obj


def _redact_message(msg: str) -> str:
    """JSON log lines: redact string values only, so numeric fields (``ts``) are not mangled into
    "[REDACTED_PHONE]" and the line stays valid JSON for Cloud Logging. Anything else: redact the text."""
    if msg.startswith("{"):
        try:
            obj = json.loads(msg)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            return json.dumps(redact_obj(obj))
    return redact(msg)


class RedactingFilter(logging.Filter):
    """Logging filter that redacts the fully formatted message before any handler sees it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except (TypeError, ValueError):
            msg = str(record.msg)
        record.msg = _redact_message(msg)
        record.args = None
        return True
