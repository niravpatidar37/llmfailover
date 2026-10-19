"""Deterministic fake LLM for CI smoke tests and local demos without model runtimes or keys.

Enabled with ``FAKE_LLM=1``. It is refused when ENV is staging or prod: ``config.check_runtime_safety``
raises at startup, and this class refuses to be constructed there as a second, independent guard.

Behaviour (all deterministic, no network):

* the answer is ``"<label> (fake:<provider>)"`` where ``label`` is ``positive``/``negative`` chosen from a
  SHA-256 of the last user message, so the same prompt always gets the same answer;
* token counts are a chars/4 estimate, latency is ``FAKE_LLM_LATENCY_MS`` (default 20 ms);
* providers listed in ``FAKE_LLM_FAIL`` (comma-separated) always fail with ``server_error``, which lets a
  smoke test exercise failover and breaker trips without a chaos proxy.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time

from .config import DEPLOYED_ENVS, ProviderConfig, UnsafeConfiguration, runtime_env
from .errors import ErrorType, ProviderError
from .providers import Completion


class FakeLLMCaller:
    def __init__(self, latency_ms: float | None = None, fail: set[str] | None = None) -> None:
        env = runtime_env()
        if env in DEPLOYED_ENVS:
            raise UnsafeConfiguration(f"fake LLM mode is refused when ENV={env}")
        if latency_ms is None:
            latency_ms = float(os.environ.get("FAKE_LLM_LATENCY_MS", "20"))
        self.latency_s = max(0.0, latency_ms) / 1000
        if fail is None:
            fail = {p.strip() for p in os.environ.get("FAKE_LLM_FAIL", "").split(",") if p.strip()}
        self.fail = fail

    async def __call__(
        self,
        provider: ProviderConfig,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float | None,
    ) -> Completion:
        t0 = time.perf_counter()
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if provider.name in self.fail:
            raise ProviderError(provider.name, ErrorType.SERVER_ERROR, "fake injected failure", 503)
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        digest = hashlib.sha256(last_user.encode("utf-8")).digest()
        label = "positive" if digest[0] % 2 == 0 else "negative"
        content = f"{label} (fake:{provider.name})"
        prompt_chars = sum(len(m["content"]) for m in messages)
        return Completion(
            provider=provider.name,
            model=f"fake/{provider.name}",
            content=content,
            finish_reason="stop",
            input_tokens=max(1, prompt_chars // 4),
            output_tokens=min(max_tokens, max(1, len(content) // 4)),
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
