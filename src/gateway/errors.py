"""Error taxonomy for provider failures.

Classification is by HTTP status first, then by exception type. We checked LiteLLM 1.105.1 directly:
an invalid Groq key surfaces as ``litellm.BadRequestError`` carrying ``status_code=401``, and an HF router
402 surfaces as a plain ``litellm.APIError``, so exception class alone is not a reliable signal.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum


class ErrorType(StrEnum):
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    CONTENT_FILTER = "content_filter"
    AUTH = "auth"
    CONNECTION = "connection"
    BAD_REQUEST = "bad_request"
    BUDGET = "budget"
    OTHER = "other"


# Errors caused by the request itself or by our own local budget refusal say nothing about provider
# health, so they must not trip a breaker.
NOT_PROVIDER_HEALTH = frozenset({ErrorType.CONTENT_FILTER, ErrorType.BAD_REQUEST, ErrorType.BUDGET})


class ProviderError(Exception):
    def __init__(self, provider: str, error_type: ErrorType, message: str, status: int | None = None):
        super().__init__(f"{provider}: {error_type}: {message[:200]}")
        self.provider = provider
        self.error_type = error_type
        self.status = status


def _from_status(status: int) -> ErrorType | None:
    if status == 429:
        return ErrorType.RATE_LIMIT
    if status in (401, 402, 403):
        return ErrorType.AUTH  # 402 = billing/entitlement: the credential cannot be used
    if status in (408, 504):
        return ErrorType.TIMEOUT
    if status >= 500:
        return ErrorType.SERVER_ERROR
    if status in (400, 404, 413, 422):
        return ErrorType.BAD_REQUEST
    return None


def classify_exception(exc: BaseException) -> tuple[ErrorType, int | None]:
    """Map any exception raised by a provider call onto the taxonomy."""
    if isinstance(exc, ProviderError):
        return exc.error_type, exc.status
    if isinstance(exc, asyncio.TimeoutError | TimeoutError):
        return ErrorType.TIMEOUT, None
    name = type(exc).__name__
    mro_names = {c.__name__ for c in type(exc).__mro__}
    raw_status = getattr(exc, "status_code", None)
    status = raw_status if isinstance(raw_status, int) else None
    if "ContentPolicyViolationError" in mro_names or "content_filter" in str(exc).lower()[:500]:
        return ErrorType.CONTENT_FILTER, status
    if "Timeout" in mro_names or "APITimeoutError" in mro_names:
        return ErrorType.TIMEOUT, status
    if status is not None:
        mapped = _from_status(status)
        if mapped is not None:
            # LiteLLM reports connection failures as APIConnectionError with a synthetic 500.
            if mapped is ErrorType.SERVER_ERROR and "APIConnectionError" in mro_names:
                return ErrorType.CONNECTION, status
            return mapped, status
    if "RateLimitError" in mro_names:
        return ErrorType.RATE_LIMIT, status
    if "AuthenticationError" in mro_names or "PermissionDeniedError" in mro_names:
        return ErrorType.AUTH, status
    if "APIConnectionError" in mro_names or name in {"ConnectError", "ConnectionError"}:
        return ErrorType.CONNECTION, status
    if "ServiceUnavailableError" in mro_names or "InternalServerError" in mro_names:
        return ErrorType.SERVER_ERROR, status
    if "BadRequestError" in mro_names:
        return ErrorType.BAD_REQUEST, status
    return ErrorType.OTHER, status
