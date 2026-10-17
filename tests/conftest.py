from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fakeredis
import httpx
import pytest

from gateway.app import create_app
from gateway.config import GatewayConfig, ProviderConfig, Settings
from gateway.errors import ErrorType, ProviderError
from gateway.providers import Completion
from gateway.spend import SpendLedger

ADMIN = "test-admin-token-0123456789"


def make_config(**overrides: Any) -> GatewayConfig:
    raw: dict[str, Any] = {
        "providers": {
            "a": {"litellm_model": "fake/a", "latency_budget_ms": 1000},
            "b": {"litellm_model": "fake/b", "latency_budget_ms": 1000},
            "c": {
                "litellm_model": "fake/c",
                "paid": True,
                "price_in_per_m": 1.0,
                "price_out_per_m": 2.0,
                "latency_budget_ms": 1000,
            },
        },
        "classes": {
            "classify": {
                "mode": "interactive",
                "preference": ["a", "b", "c"],
                "max_tokens_cap": 16,
                "hedge_after_ms": 100,
            },
            "generate": {"mode": "interactive", "preference": ["a", "b", "c"], "max_tokens_cap": 64},
            "batch": {"mode": "deferrable", "preference": ["a", "b"], "max_tokens_cap": 64},
        },
        "breaker": {
            "window_s": 30,
            "min_samples": 4,
            "error_rate_threshold": 0.5,
            "open_cooldown_s": 5,
            "max_cooldown_s": 40,
            "half_open_probe_ratio": 1.0,
            "half_open_successes": 2,
        },
        "queue": {"base_delay_s": 0.01, "max_delay_s": 0.05, "max_attempts": 4, "poll_interval_s": 0.01},
    }
    raw.update(overrides)
    return GatewayConfig.model_validate(raw)


Behavior = Callable[[ProviderConfig], Awaitable[Completion]]


@dataclass
class FakeCaller:
    behaviors: dict[str, Behavior] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)

    async def __call__(
        self,
        provider: ProviderConfig,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float | None,
    ) -> Completion:
        self.calls.append(provider.name)
        beh = self.behaviors.get(provider.name, ok())
        try:
            return await beh(provider)
        except asyncio.CancelledError:
            self.cancelled.append(provider.name)
            raise


def ok(delay: float = 0.0, content: str = "positive") -> Behavior:
    async def _b(p: ProviderConfig) -> Completion:
        if delay:
            await asyncio.sleep(delay)
        return Completion(
            p.name, p.litellm_model, f"{content} from {p.name}", "stop", 20, 3, delay * 1000 + 1
        )

    return _b


def fail(et: ErrorType = ErrorType.SERVER_ERROR, delay: float = 0.0) -> Behavior:
    async def _b(p: ProviderConfig) -> Completion:
        if delay:
            await asyncio.sleep(delay)
        raise ProviderError(p.name, et, "injected", 503)

    return _b


@pytest.fixture
def redis() -> fakeredis.FakeAsyncRedis:
    return fakeredis.FakeAsyncRedis()


@pytest.fixture
def ledger(tmp_path: Path) -> SpendLedger:
    return SpendLedger(tmp_path / "ledger.jsonl", "p3-test", project_cap_usd=1.0, global_cap_usd=25.0)


@pytest.fixture
def caller() -> FakeCaller:
    return FakeCaller()


@pytest.fixture
def cfg() -> GatewayConfig:
    return make_config()


@pytest.fixture
async def client(
    cfg: GatewayConfig,
    redis: fakeredis.FakeAsyncRedis,
    caller: FakeCaller,
    ledger: SpendLedger,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(redis_url="redis://unused", admin_token=ADMIN, ledger_path=str(tmp_path / "l.jsonl"))
    app = create_app(
        cfg=cfg, settings=settings, redis=redis, caller=caller, ledger=ledger, start_background=False
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
        c.app = app  # type: ignore[attr-defined]
        yield c


async def new_tenant(client: httpx.AsyncClient, tenant: str = "acme", rpm: int = 1000) -> str:
    r = await client.post(
        "/admin/tenants", json={"tenant": tenant, "rpm": rpm}, headers={"X-Admin-Token": ADMIN}
    )
    assert r.status_code == 200, r.text
    return str(r.json()["api_key"])


def chat_body(
    rid: str = "req-00000001",
    tenant: str = "acme",
    feature: str = "support-triage",
    request_class: str = "generate",
    content: str = "Classify: great product",
) -> dict[str, Any]:
    return {
        "model": "auto",
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 8,
        "metadata": {"tenant": tenant, "feature": feature, "request_id": rid, "request_class": request_class},
    }
