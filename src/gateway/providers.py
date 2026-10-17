"""Provider calls through LiteLLM, normalised into ``Completion`` or a taxonomy-tagged ``ProviderError``."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

from .config import ProviderConfig
from .errors import ProviderError, classify_exception

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # no network fetch of the cost map at import


@dataclass
class Completion:
    provider: str
    model: str
    content: str
    finish_reason: str
    input_tokens: int
    output_tokens: int
    latency_ms: float


class ProviderCaller(Protocol):
    async def __call__(
        self,
        provider: ProviderConfig,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float | None,
    ) -> Completion: ...


class LiteLLMCaller:
    """Real provider calls. ``num_retries=0``: retries/failover are the gateway's job, not LiteLLM's."""

    def __init__(self) -> None:
        import litellm

        litellm.telemetry = False
        litellm.suppress_debug_info = True
        litellm.drop_params = True
        self._litellm = litellm

    async def __call__(
        self,
        provider: ProviderConfig,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float | None,
    ) -> Completion:
        kwargs: dict[str, Any] = {
            "model": provider.litellm_model,
            "messages": messages,
            "max_tokens": max(max_tokens, provider.min_max_tokens),
            "timeout": provider.timeout_s,
            "num_retries": 0,
            **provider.extra,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        base = provider.api_base()
        if base:
            kwargs["api_base"] = base
        key = provider.api_key()
        if key:
            kwargs["api_key"] = key
        t0 = time.perf_counter()
        try:
            resp = await self._litellm.acompletion(**kwargs)
        except Exception as exc:
            et, status = classify_exception(exc)
            raise ProviderError(provider.name, et, type(exc).__name__, status) from exc
        latency_ms = (time.perf_counter() - t0) * 1000
        choice = resp.choices[0]
        usage = getattr(resp, "usage", None)
        return Completion(
            provider=provider.name,
            model=str(getattr(resp, "model", provider.litellm_model)),
            content=str(choice.message.content or ""),
            finish_reason=str(choice.finish_reason or "stop"),
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            latency_ms=latency_ms,
        )
