from __future__ import annotations

import asyncio
import json
from pathlib import Path

import fakeredis
import pytest

from gateway.breaker import CircuitBreakers
from gateway.config import GatewayConfig
from gateway.errors import ErrorType
from gateway.health import HealthTracker
from gateway.metrics import Metrics
from gateway.router import AllProvidersFailed, Router
from gateway.spend import SpendLedger

from .conftest import FakeCaller, fail, make_config, ok

MSG = [{"role": "user", "content": "hello"}]


def build(
    cfg: GatewayConfig, redis: fakeredis.FakeAsyncRedis, caller: FakeCaller, ledger: SpendLedger | None = None
) -> tuple[Router, CircuitBreakers, Metrics]:
    m = Metrics()
    h = HealthTracker(redis, cfg.breaker.window_s)
    b = CircuitBreakers(redis, h, cfg.breaker, {n: p.latency_budget_ms for n, p in cfg.providers.items()}, m)
    return Router(cfg, b, h, m, ledger, caller), b, m


async def test_failover_to_next_provider_on_error(
    cfg: GatewayConfig, redis: fakeredis.FakeAsyncRedis, caller: FakeCaller
) -> None:
    caller.behaviors["a"] = fail(ErrorType.SERVER_ERROR)
    r, _, m = build(cfg, redis, caller)
    rr = await r.route(cfg.classes["generate"], MSG, 8, None)
    assert rr.completion.provider == "b"
    assert rr.failovers == 1
    assert [a.outcome for a in rr.attempts] == ["server_error", "ok"]
    assert m.failovers.labels("generate", "a", "b", "error")._value.get() == 1


async def test_open_breaker_is_skipped_without_calling_provider(
    cfg: GatewayConfig, redis: fakeredis.FakeAsyncRedis, caller: FakeCaller
) -> None:
    r, b, _ = build(cfg, redis, caller)
    await b.force("a", "open")
    rr = await r.route(cfg.classes["generate"], MSG, 8, None)
    assert rr.completion.provider == "b"
    assert "a" not in caller.calls
    assert rr.attempts[0].outcome == "skipped_open"


async def test_interactive_fails_fast_when_all_open(
    cfg: GatewayConfig, redis: fakeredis.FakeAsyncRedis, caller: FakeCaller
) -> None:
    r, b, _ = build(cfg, redis, caller)
    for p in ("a", "b", "c"):
        await b.force(p, "open")
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    with pytest.raises(AllProvidersFailed):
        await r.route(cfg.classes["generate"], MSG, 8, None)
    assert loop.time() - t0 < 0.1
    assert caller.calls == []


async def test_repeated_failures_trip_breaker_then_route_around(
    cfg: GatewayConfig, redis: fakeredis.FakeAsyncRedis, caller: FakeCaller
) -> None:
    caller.behaviors["a"] = fail(ErrorType.TIMEOUT)
    r, b, _ = build(cfg, redis, caller)
    for _ in range(4):
        await r.route(cfg.classes["generate"], MSG, 8, None)
    assert await b.state("a") == "open"
    calls_before = caller.calls.count("a")
    await r.route(cfg.classes["generate"], MSG, 8, None)
    assert caller.calls.count("a") == calls_before  # no longer tried


async def test_hedge_fires_and_cancels_loser(
    cfg: GatewayConfig, redis: fakeredis.FakeAsyncRedis, caller: FakeCaller
) -> None:
    caller.behaviors["a"] = ok(delay=1.0)  # slow primary; classify hedges after 100 ms
    caller.behaviors["b"] = ok(delay=0.01)
    r, _, m = build(cfg, redis, caller)
    rr = await r.route(cfg.classes["classify"], MSG, 8, None)
    assert rr.completion.provider == "b" and rr.hedged
    assert caller.cancelled == ["a"]
    outcomes = {a.provider: a.outcome for a in rr.attempts}
    assert outcomes == {"a": "cancelled", "b": "ok"}
    assert m.hedges.labels("fired")._value.get() == 1
    assert m.hedges.labels("hedge_won")._value.get() == 1


async def test_no_hedge_for_non_hedged_class(
    cfg: GatewayConfig, redis: fakeredis.FakeAsyncRedis, caller: FakeCaller
) -> None:
    caller.behaviors["a"] = ok(delay=0.3)
    r, _, _ = build(cfg, redis, caller)
    rr = await r.route(cfg.classes["generate"], MSG, 8, None)
    assert rr.completion.provider == "a" and not rr.hedged
    assert caller.calls == ["a"]


async def test_hedge_budget_caps_hedging(redis: fakeredis.FakeAsyncRedis, caller: FakeCaller) -> None:
    cfg = make_config(hedging={"max_hedges_per_minute": 1})
    caller.behaviors["a"] = ok(delay=0.2)
    r, _, m = build(cfg, redis, caller)
    await r.route(cfg.classes["classify"], MSG, 8, None)
    await r.route(cfg.classes["classify"], MSG, 8, None)
    assert m.hedges.labels("fired")._value.get() == 1
    assert m.hedges.labels("budget_exhausted")._value.get() == 1


async def test_paid_provider_cost_recorded_in_ledger(
    redis: fakeredis.FakeAsyncRedis, caller: FakeCaller, ledger: SpendLedger
) -> None:
    cfg = make_config()
    caller.behaviors["a"] = fail()
    caller.behaviors["b"] = fail()
    r, _, _ = build(cfg, redis, caller, ledger)
    rr = await r.route(cfg.classes["generate"], MSG, 8, None)
    assert rr.completion.provider == "c"
    rows = [json.loads(x) for x in Path(ledger.path).read_text().splitlines()]  # noqa: ASYNC240
    assert len(rows) == 1 and rows[0]["provider"] == "c" and rows[0]["project"] == "p3-test"
    assert rows[0]["usd"] == pytest.approx((20 * 1.0 + 3 * 2.0) / 1e6)


async def test_budget_exceeded_skips_paid_provider(
    tmp_path: Path, redis: fakeredis.FakeAsyncRedis, caller: FakeCaller
) -> None:
    cfg = make_config()
    tiny = SpendLedger(tmp_path / "l.jsonl", "p3-test", project_cap_usd=1e-9, global_cap_usd=25)
    caller.behaviors["a"] = fail()
    caller.behaviors["b"] = fail()
    r, _, _ = build(cfg, redis, caller, tiny)
    with pytest.raises(AllProvidersFailed) as ei:
        await r.route(cfg.classes["generate"], MSG, 8, None)
    assert ei.value.attempts[-1].outcome == "skipped_budget"
    assert "c" not in caller.calls
