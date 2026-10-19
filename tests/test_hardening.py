"""Production hardening: fake-LLM mode is dev/CI only, security headers, no stack traces, failover
capacity WARN + gauge, the valid-completion quality SLI, and the adaptive hedge delay."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import fakeredis
import httpx
import pytest
from pydantic import ValidationError

from gateway import migrate
from gateway.app import SECURITY_HEADERS, create_app
from gateway.config import (
    REPO_ROOT,
    GatewayConfig,
    ProviderConfig,
    RequestClass,
    Settings,
    UnsafeConfiguration,
    check_runtime_safety,
    load_config,
)
from gateway.errors import ProviderError
from gateway.fake_llm import FakeLLMCaller
from gateway.providers import Completion
from gateway.quality import classify_completion
from gateway.spend import SpendLedger

from .conftest import ADMIN, FakeCaller, chat_body, make_config, new_tenant, ok
from .test_router import MSG, build

PROD_CONFIG = REPO_ROOT / "config" / "gateway.prod.yaml"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def dev_settings(tmp_path: Path, **kw: Any) -> Settings:
    return Settings(redis_url="redis://unused", admin_token=ADMIN, ledger_path=str(tmp_path / "l"), **kw)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for k in ("FAKE_LLM", "FAKE_LLM_FAIL", "CHAOS_ENABLED", "CHAOS_PROXY_URL", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ENV", "dev")
    return monkeypatch


# ---------------------------------------------------------------- fake LLM mode: never in staging/prod


@pytest.mark.parametrize("env", ["staging", "prod"])
def test_fake_llm_is_refused_in_deployed_envs(clean_env: pytest.MonkeyPatch, env: str) -> None:
    clean_env.setenv("ENV", env)
    clean_env.setenv("FAKE_LLM", "1")
    clean_env.setenv("GROQ_API_KEY", "test-not-a-real-key")
    settings = Settings(
        env=env, redis_url="rediss://10.0.0.3:6378/0", admin_token="p" * 40, spend_backend="redis"
    )
    with pytest.raises(UnsafeConfiguration, match="FAKE_LLM=1 is refused"):
        check_runtime_safety(load_config(PROD_CONFIG), settings)
    with pytest.raises(UnsafeConfiguration, match="fake LLM mode is refused"):
        FakeLLMCaller()  # second, independent guard if the startup check were ever bypassed


def test_fake_llm_app_refuses_to_start_in_prod(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("ENV", "prod")
    clean_env.setenv("FAKE_LLM", "1")
    clean_env.setenv("GROQ_API_KEY", "test-not-a-real-key")
    settings = Settings(
        env="prod", redis_url="rediss://10.0.0.3:6378/0", admin_token="p" * 40, spend_backend="redis"
    )
    with pytest.raises(UnsafeConfiguration, match="FAKE_LLM"):
        create_app(
            cfg=load_config(PROD_CONFIG),
            settings=settings,
            redis=fakeredis.FakeAsyncRedis(),
            start_background=False,
        )


async def test_fake_llm_is_deterministic_and_can_fail_a_provider(clean_env: pytest.MonkeyPatch) -> None:
    fake = FakeLLMCaller(latency_ms=0, fail={"b"})
    a = ProviderConfig(name="a", litellm_model="x/a")
    c1 = await fake(a, [{"role": "user", "content": "great product"}], 8, None)
    c2 = await fake(a, [{"role": "user", "content": "great product"}], 8, None)
    assert c1.content == c2.content and c1.content.endswith("(fake:a)")
    assert c1.model == "fake/a" and c1.finish_reason == "stop"
    with pytest.raises(ProviderError):
        await fake(ProviderConfig(name="b", litellm_model="x/b"), MSG, 8, None)


async def test_fake_llm_mode_serves_end_to_end_in_dev(
    clean_env: pytest.MonkeyPatch, tmp_path: Path, ledger: SpendLedger
) -> None:
    clean_env.setenv("FAKE_LLM", "1")
    clean_env.setenv("FAKE_LLM_FAIL", "a")  # preferred provider fails -> failover to b
    redis = fakeredis.FakeAsyncRedis()
    await migrate.apply(redis)
    app = create_app(
        cfg=make_config(), settings=dev_settings(tmp_path), redis=redis, ledger=ledger, start_background=False
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        ready = (await c.get("/readyz")).json()
        assert ready["fake_llm"] is True and ready["chaos_routing"] is False
        key = await new_tenant(c)
        r = await c.post("/v1/chat/completions", json=chat_body(rid="fake-req-001"), headers=auth(key))
        assert r.status_code == 200, r.text
        g = r.json()["gateway"]
        assert g["provider"] == "b" and g["failovers"] == 1
        assert r.json()["choices"][0]["message"]["content"].endswith("(fake:b)")


# ---------------------------------------------------------------- headers and error hygiene


@pytest.fixture
async def dev_app(
    clean_env: pytest.MonkeyPatch, tmp_path: Path, caller: FakeCaller, ledger: SpendLedger
) -> AsyncIterator[tuple[Any, httpx.AsyncClient]]:
    redis = fakeredis.FakeAsyncRedis()
    await migrate.apply(redis)
    app = create_app(
        cfg=make_config(),
        settings=dev_settings(tmp_path),
        redis=redis,
        caller=caller,
        ledger=ledger,
        start_background=False,
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
        yield app, c


async def test_security_headers_on_success_error_and_unknown_routes(
    dev_app: tuple[Any, httpx.AsyncClient],
) -> None:
    _, c = dev_app
    for r in (
        await c.get("/healthz"),
        await c.post("/v1/chat/completions", json=chat_body()),  # 401
        await c.get("/does-not-exist"),  # 404
    ):
        for k, v in SECURITY_HEADERS.items():
            assert r.headers.get(k) == v, (r.status_code, k)
        assert "strict-transport-security" not in r.headers  # dev: no TLS locally


async def test_hsts_only_in_deployed_envs(
    clean_env: pytest.MonkeyPatch, tmp_path: Path, caller: FakeCaller
) -> None:
    st = Settings(env="staging", redis_url="redis://unused", admin_token=ADMIN, ledger_path=str(tmp_path))
    app = create_app(
        cfg=make_config(),
        settings=st,
        redis=fakeredis.FakeAsyncRedis(),
        caller=caller,
        start_background=False,
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as c:
        assert (await c.get("/healthz")).headers["strict-transport-security"].startswith("max-age=")


async def test_unhandled_exception_returns_json_500_without_trace(
    dev_app: tuple[Any, httpx.AsyncClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, c = dev_app

    async def boom(_key: str) -> str | None:
        raise RuntimeError("secret-internal-detail /srv/app.py line 42")

    monkeypatch.setattr(app.state.tenants, "resolve", boom)
    r = await c.post("/v1/chat/completions", json=chat_body(), headers=auth("gwk_anything"))
    assert r.status_code == 500
    assert r.json() == {
        "error": {"message": "internal error", "type": "server_error", "code": "internal_error"}
    }
    assert "Traceback" not in r.text and "secret-internal-detail" not in r.text
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_validation_errors_do_not_echo_input_or_traces(dev_app: tuple[Any, httpx.AsyncClient]) -> None:
    _, c = dev_app
    key = await new_tenant(c)
    r = await c.post(
        "/v1/chat/completions",
        content=b'{"messages": "<script>alert(1)</script>"',
        headers={**auth(key), "content-type": "application/json"},
    )
    assert r.status_code == 400
    assert "<script>" not in r.text and "Traceback" not in r.text


# ---------------------------------------------------------------- failover capacity WARN + gauge


async def test_failover_capacity_warns_below_two_healthy_external_providers(
    dev_app: tuple[Any, httpx.AsyncClient],
) -> None:
    app, c = dev_app
    body = (await c.get("/readyz")).json()
    assert body["failover"]["external_healthy"] == ["a", "b", "c"]
    assert body["failover"]["warning"] is None
    await app.state.breakers.force("a", "open")
    await app.state.breakers.force("b", "open")
    r = await c.get("/readyz")
    assert r.status_code == 200 and r.json()["status"] == "ready"  # WARN, not a readiness failure
    fo = r.json()["failover"]
    assert fo["external_healthy"] == ["c"] and "fewer than 2" in fo["warning"]
    await app.state.refresh_gauges()
    m = (await c.get("/metrics")).text
    assert "gateway_external_providers_healthy 1.0" in m
    assert "gateway_external_providers_enabled 3.0" in m


def test_local_runtimes_do_not_count_as_external_failover_capacity(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.delenv("GROQ_API_KEY", raising=False)
    clean_env.delenv("HF_ENABLED", raising=False)
    cfg = load_config(REPO_ROOT / "config" / "gateway.yaml")
    assert set(cfg.enabled_providers()) >= {"ollama", "ollama_cpu"}
    assert cfg.external_providers() == []
    # also when the CPU runtime is reached by its compose service name, as in the CI smoke stack
    clean_env.setenv("OLLAMA_CPU_URL", "http://ollama-cpu:11434")
    assert cfg.external_providers() == []
    warnings = check_runtime_safety(cfg, Settings())
    assert any("no independent failover target" in w for w in warnings)


# ---------------------------------------------------------------- quality SLI


def _completion(content: str = "positive", finish: str = "stop") -> Any:
    async def _b(p: ProviderConfig) -> Completion:
        return Completion(p.name, p.litellm_model, content, finish, 5, 1, 1.0)

    return _b


async def test_quality_sli_counts_valid_and_invalid_completions_once(
    dev_app: tuple[Any, httpx.AsyncClient], caller: FakeCaller
) -> None:
    _, c = dev_app
    key = await new_tenant(c)
    caller.behaviors["a"] = _completion("positive")
    assert (
        await c.post("/v1/chat/completions", json=chat_body(rid="q-valid-0001"), headers=auth(key))
    ).is_success
    # a replay is not a new completion and must not be counted again
    assert (
        await c.post("/v1/chat/completions", json=chat_body(rid="q-valid-0001"), headers=auth(key))
    ).is_success
    caller.behaviors["a"] = _completion("   ")
    await c.post("/v1/chat/completions", json=chat_body(rid="q-empty-0001", content="x"), headers=auth(key))
    caller.behaviors["a"] = _completion("posi", finish="length")
    await c.post("/v1/chat/completions", json=chat_body(rid="q-trunc-0001", content="y"), headers=auth(key))
    m = (await c.get("/metrics")).text

    def series(validity: str, reason: str) -> str:
        return (
            f'gateway_completions_total{{provider="a",reason="{reason}",'
            f'request_class="generate",validity="{validity}"}}'
        )

    assert f"{series('valid', 'ok')} 1.0" in m
    assert f"{series('invalid', 'empty')} 1.0" in m
    assert f"{series('invalid', 'truncated')} 1.0" in m


def test_classify_completion_rejects_schema_violations() -> None:
    assert classify_completion({"object": "chat.completion"}) == (False, "schema")
    good = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        "gateway": {"request_id": "r", "provider": "a", "request_class": "g", "failovers": 0, "attempts": []},
    }
    assert classify_completion(good) == (True, "ok")
    bad_role = {**good, "choices": [{**good["choices"][0], "message": {"role": "user", "content": "hi"}}]}
    assert classify_completion(bad_role) == (False, "schema")


# ---------------------------------------------------------------- adaptive hedge delay


def adaptive_cfg(**cls: Any) -> GatewayConfig:
    classify = {
        "mode": "interactive",
        "preference": ["a", "b"],
        "max_tokens_cap": 16,
        "hedge_after_ms": 1500,
        "hedge_adaptive": True,
        "hedge_quantile": "p95",
        "hedge_min_ms": 50,
        "hedge_max_ms": 2000,
        **cls,
    }
    return make_config(classes={"classify": classify})


async def _seed(
    redis: fakeredis.FakeAsyncRedis, cfg: GatewayConfig, provider: str, ms: float, n: int
) -> None:
    _, b, _ = build(cfg, redis, FakeCaller())
    for _ in range(n):
        await b.health.record(provider, True, ms)


async def test_adaptive_hedge_fires_near_primary_p95(
    redis: fakeredis.FakeAsyncRedis, caller: FakeCaller
) -> None:
    cfg = adaptive_cfg()
    await _seed(redis, cfg, "a", 300.0, 10)  # primary's recent p95 = 300 ms
    caller.behaviors["a"] = ok(delay=2.0)  # this call is slow
    caller.behaviors["b"] = ok(delay=0.01)
    r, _, m = build(cfg, redis, caller)
    t0 = time.perf_counter()
    rr = await r.route(cfg.classes["classify"], MSG, 8, None)
    elapsed = time.perf_counter() - t0
    assert rr.completion.provider == "b" and rr.hedged
    assert rr.hedge_delay_ms == 300.0
    assert elapsed < 1.0  # the 1500 ms fallback (or the old fixed 2.5 s) would still be waiting
    assert m.hedge_delay.labels("classify", "adaptive")._value.get() == pytest.approx(0.3)


async def test_adaptive_hedge_uses_fallback_until_window_has_samples(
    redis: fakeredis.FakeAsyncRedis, caller: FakeCaller
) -> None:
    cfg = adaptive_cfg()
    await _seed(redis, cfg, "a", 50.0, cfg.breaker.min_samples - 1)  # too few samples to trust
    caller.behaviors["a"] = ok(delay=0.4)
    r, _, m = build(cfg, redis, caller)
    rr = await r.route(cfg.classes["classify"], MSG, 8, None)
    assert rr.completion.provider == "a" and not rr.hedged  # 400 ms < 1500 ms fallback: no hedge
    assert rr.hedge_delay_ms == 1500.0
    assert m.hedge_delay.labels("classify", "fallback")._value.get() == pytest.approx(1.5)


@pytest.mark.parametrize(("p95_ms", "expected_ms"), [(5.0, 50.0), (9000.0, 2000.0)])
async def test_adaptive_hedge_delay_is_clamped(
    redis: fakeredis.FakeAsyncRedis, p95_ms: float, expected_ms: float
) -> None:
    cfg = adaptive_cfg()
    await _seed(redis, cfg, "a", p95_ms, 10)
    r, _, _ = build(cfg, redis, FakeCaller())
    d = await r.hedge_delay_s(cfg.classes["classify"], "a")
    assert d == pytest.approx(expected_ms / 1000)


async def test_fixed_hedge_delay_is_configurable_per_class(redis: fakeredis.FakeAsyncRedis) -> None:
    cfg = make_config()
    r, _, _ = build(cfg, redis, FakeCaller())
    assert await r.hedge_delay_s(cfg.classes["classify"], "a") == pytest.approx(0.1)  # conftest: 100 ms
    assert await r.hedge_delay_s(cfg.classes["generate"], "a") is None  # class does not hedge


def test_adaptive_hedge_requires_a_fallback_delay() -> None:
    with pytest.raises(ValidationError, match="hedge_adaptive needs hedge_after_ms"):
        RequestClass(mode="interactive", preference=["a"], hedge_adaptive=True)
    with pytest.raises(ValidationError, match="hedge_max_ms"):
        RequestClass(
            mode="interactive", preference=["a"], hedge_after_ms=100, hedge_min_ms=500, hedge_max_ms=200
        )


def test_shipped_configs_use_adaptive_hedging_for_classify() -> None:
    for name in ("gateway.yaml", "gateway.prod.yaml"):
        c = load_config(REPO_ROOT / "config" / name).classes["classify"]
        assert c.hedge_adaptive and c.hedge_quantile == "p95"
        assert c.hedge_after_ms is not None and c.hedge_after_ms < 2500


async def test_hedge_delay_does_not_slow_the_fast_path(
    redis: fakeredis.FakeAsyncRedis, caller: FakeCaller
) -> None:
    """Guard: a primary that answers before the delay is never hedged."""
    cfg = adaptive_cfg()
    await _seed(redis, cfg, "a", 300.0, 10)
    caller.behaviors["a"] = ok(delay=0.05)
    r, _, _ = build(cfg, redis, caller)
    rr = await r.route(cfg.classes["classify"], MSG, 8, None)
    assert rr.completion.provider == "a" and not rr.hedged
    await asyncio.sleep(0)


def test_prod_refuses_a_provider_on_a_single_label_service_host(clean_env: pytest.MonkeyPatch) -> None:
    """A bare service name (docker/k8s DNS) is a private-network peer, not an external provider."""
    clean_env.setenv("ENV", "prod")
    clean_env.setenv("GROQ_API_KEY", "test-not-a-real-key")
    cfg = load_config(PROD_CONFIG)
    cfg.providers["groq"].api_base_default = "http://ollama-cpu:11434"
    settings = Settings(
        env="prod", redis_url="rediss://10.0.0.3:6378/0", admin_token="p" * 40, spend_backend="redis"
    )
    with pytest.raises(UnsafeConfiguration, match="local/private-network providers"):
        check_runtime_safety(cfg, settings)


def test_explicit_external_flag_overrides_url_heuristics() -> None:
    """A same-host runtime behind a public-looking name must still not count as failover capacity."""
    box = {"name": "x", "litellm_model": "m", "api_base_default": "https://gpu-box.lab.example.com"}
    assert ProviderConfig(**box).is_external() is True
    assert ProviderConfig(**box, external=False).is_external() is False
    assert ProviderConfig(**{**box, "api_base_default": "http://127.0.0.1:1"}, external=True).is_external()
