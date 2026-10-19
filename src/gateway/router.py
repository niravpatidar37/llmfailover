"""Provider selection: preference lists per request class, breaker admission, failover and hedging."""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .breaker import CircuitBreakers
from .config import GatewayConfig, RequestClass
from .errors import ErrorType, ProviderError
from .health import HealthTracker
from .metrics import Metrics
from .providers import Completion, ProviderCaller
from .spend import BudgetExceeded, FileSpendGuard, SpendGuard, SpendLedger, estimate_tokens
from .store import SlidingWindow

HEDGE_KEY = "gw:hedges"


@dataclass
class Attempt:
    provider: str
    outcome: (
        str  # "ok", an ErrorType value, "skipped_open", "skipped_half_open", "skipped_budget", "cancelled"
    )
    latency_ms: float | None = None
    probe: bool = False
    hedge: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "outcome": self.outcome,
            "latency_ms": self.latency_ms,
            "probe": self.probe,
            "hedge": self.hedge,
        }


@dataclass
class RouteResult:
    completion: Completion
    attempts: list[Attempt]
    failovers: int
    hedged: bool
    cost_usd: float
    hedge_delay_ms: float | None = None


class AllProvidersFailed(Exception):
    def __init__(self, attempts: list[Attempt]):
        super().__init__("no provider could serve the request")
        self.attempts = attempts


class KillSwitchEngaged(Exception):
    """All provider calls are disabled by the operator kill switch (env flag or Redis key)."""


@dataclass
class _Running:
    provider: str
    probe: bool
    estimate_usd: float
    started: float
    hedge: bool = False
    probe_token: str | None = None
    reservation: str | None = None


@dataclass
class _Ctx:
    cls: RequestClass
    candidates: list[str]
    attempts: list[Attempt] = field(default_factory=list)
    idx: int = 0
    failovers: int = 0


class Router:
    def __init__(
        self,
        cfg: GatewayConfig,
        breakers: CircuitBreakers,
        health: HealthTracker,
        metrics: Metrics,
        ledger: SpendLedger | SpendGuard | None,
        caller: ProviderCaller,
        kill_switch: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self.cfg = cfg
        self.breakers = breakers
        self.health = health
        self.metrics = metrics
        self.ledger: SpendGuard | None = FileSpendGuard(ledger) if isinstance(ledger, SpendLedger) else ledger
        self.caller = caller
        self.kill_switch = kill_switch
        # Hedge budget lives in Redis, so it caps hedging across all gateway instances, not per process.
        self._hedges = SlidingWindow(breakers.redis)

    # ---- helpers -------------------------------------------------------------------------------
    async def _hedge_budget_ok(self, now: float) -> bool:
        allowed, _ = await self._hedges.allow(
            HEDGE_KEY, self.cfg.hedging.max_hedges_per_minute, 60.0, now, secrets.token_hex(8)
        )
        return allowed

    def _estimate(self, provider: str, messages: list[dict[str, str]], max_tokens: int) -> float:
        p = self.cfg.providers[provider]
        if not p.paid:
            return 0.0
        chars = sum(len(m["content"]) for m in messages)
        return p.cost_usd(estimate_tokens(chars), max(max_tokens, p.min_max_tokens))

    async def _next_admitted(
        self, ctx: _Ctx, messages: list[dict[str, str]], max_tokens: int
    ) -> _Running | None:
        while ctx.idx < len(ctx.candidates):
            p = ctx.candidates[ctx.idx]
            ctx.idx += 1
            adm = await self.breakers.admit(p)
            if not adm.allowed:
                ctx.attempts.append(Attempt(p, f"skipped_{adm.state}"))
                continue
            est = self._estimate(p, messages, max_tokens)
            reservation: str | None = None
            if est > 0 and self.ledger is not None:
                try:
                    reservation = await self.ledger.reserve(est)
                except BudgetExceeded:
                    await self.breakers.release_probe(p, adm.token)
                    ctx.attempts.append(Attempt(p, "skipped_budget"))
                    self.metrics.provider_errors.labels(p, ErrorType.BUDGET.value).inc()
                    continue
            return _Running(
                p, adm.probe, est, time.perf_counter(), probe_token=adm.token, reservation=reservation
            )
        return None

    async def _call(
        self, run: _Running, messages: list[dict[str, str]], max_tokens: int, temperature: float | None
    ) -> Completion:
        cfg = self.cfg.providers[run.provider]
        try:
            return await self.caller(cfg, messages, max_tokens, temperature)
        except ProviderError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # defensive: anything unexpected is an "other" provider error
            raise ProviderError(run.provider, ErrorType.OTHER, type(exc).__name__) from exc

    async def _record_failure(self, run: _Running, err: ProviderError, ctx: _Ctx) -> None:
        latency = (time.perf_counter() - run.started) * 1000
        ctx.attempts.append(
            Attempt(run.provider, err.error_type.value, round(latency, 1), run.probe, run.hedge)
        )
        self.metrics.provider_attempts.labels(run.provider, "error").inc()
        self.metrics.provider_errors.labels(run.provider, err.error_type.value).inc()
        if self.ledger is not None and run.reservation:
            await self.ledger.release(run.reservation)
        await self.health.record(run.provider, False, latency, err.error_type)
        await self.breakers.on_result(run.provider, False, latency, err.error_type, run.probe_token)

    async def _record_success(self, run: _Running, comp: Completion, ctx: _Ctx) -> float:
        latency = comp.latency_ms
        ctx.attempts.append(Attempt(run.provider, "ok", round(latency, 1), run.probe, run.hedge))
        self.metrics.provider_attempts.labels(run.provider, "ok").inc()
        self.metrics.provider_latency.labels(run.provider).observe(latency / 1000)
        cost = self.cfg.providers[run.provider].cost_usd(comp.input_tokens, comp.output_tokens)
        if self.ledger is not None and run.reservation:
            await self.ledger.settle(
                run.reservation, run.provider, comp.model, comp.input_tokens, comp.output_tokens, cost
            )
        await self.health.record(run.provider, True, latency)
        await self.breakers.on_result(run.provider, True, latency, None, run.probe_token)
        return cost

    async def _record_cancel(self, run: _Running, ctx: _Ctx) -> float:
        """A cancelled hedge loser. Providers may still bill it, so paid ones are charged worst-case."""
        latency = (time.perf_counter() - run.started) * 1000
        ctx.attempts.append(Attempt(run.provider, "cancelled", round(latency, 1), run.probe, run.hedge))
        self.metrics.provider_attempts.labels(run.provider, "cancelled").inc()
        await self.breakers.release_probe(run.provider, run.probe_token)
        if self.ledger is not None and run.reservation:
            await self.ledger.settle(
                run.reservation,
                run.provider,
                self.cfg.providers[run.provider].litellm_model,
                0,
                0,
                run.estimate_usd,
                note="cancelled hedge loser, worst-case estimate",
            )
        return run.estimate_usd

    def _count_failover(self, ctx: _Ctx, to: str, reason: str) -> None:
        frm = next((a.provider for a in reversed(ctx.attempts) if a.outcome != "ok"), "none")
        ctx.failovers += 1
        self.metrics.failovers.labels(ctx.cls.name, frm, to, reason).inc()

    async def hedge_delay_s(self, cls: RequestClass, provider: str) -> float | None:
        """Delay before hedging a lone in-flight call to ``provider``; None if the class does not hedge.

        Fixed: ``hedge_after_ms``. Adaptive: the provider's recent latency quantile from the shared health
        window ("hedge after the p95", Dean & Barroso, The Tail at Scale, CACM 2013), clamped to
        [hedge_min_ms, hedge_max_ms or hedge_after_ms]; it falls back to ``hedge_after_ms`` until the window
        holds ``breaker.min_samples`` successes, so a cold or idle provider is not hedged aggressively.
        """
        if not cls.hedge_after_ms:
            return None
        delay_ms, source = float(cls.hedge_after_ms), "fixed"
        if cls.hedge_adaptive:
            source = "fallback"
            st = await self.health.stats(provider)
            q = {"p50": st.p50_ms, "p95": st.p95_ms, "p99": st.p99_ms}[cls.hedge_quantile]
            if q is not None and st.successes >= self.cfg.breaker.min_samples:
                hi = float(cls.hedge_max_ms or cls.hedge_after_ms)
                delay_ms, source = min(hi, max(float(cls.hedge_min_ms), q)), "adaptive"
        self.metrics.hedge_delay.labels(cls.name, source).set(delay_ms / 1000)
        return delay_ms / 1000

    # ---- main entry -----------------------------------------------------------------------------
    async def route(
        self, cls: RequestClass, messages: list[dict[str, str]], max_tokens: int, temperature: float | None
    ) -> RouteResult:
        if self.kill_switch is not None and await self.kill_switch():
            raise KillSwitchEngaged("LLM calls are disabled by the kill switch")
        candidates = [p for p in cls.preference if self.cfg.providers[p].enabled()]
        ctx = _Ctx(cls=cls, candidates=candidates)
        running: dict[asyncio.Task[Completion], _Running] = {}
        hedge_used = False
        extra_cost = 0.0
        delays: dict[str, float | None] = {}  # per-provider hedge delay, computed once per request

        def start(run: _Running) -> None:
            task = asyncio.create_task(self._call(run, messages, max_tokens, temperature))
            running[task] = run

        first = await self._next_admitted(ctx, messages, max_tokens)
        if first is None:
            raise AllProvidersFailed(ctx.attempts)
        if ctx.attempts:  # preferred provider(s) skipped because their breaker is open
            self._count_failover(ctx, first.provider, "breaker_open")
        start(first)
        try:
            while running:
                timeout: float | None = None
                if (
                    cls.hedge_after_ms
                    and not hedge_used
                    and len(running) == 1
                    and ctx.idx < len(ctx.candidates)
                ):
                    lone = next(iter(running.values()))
                    if lone.provider not in delays:
                        delays[lone.provider] = await self.hedge_delay_s(cls, lone.provider)
                    d = delays[lone.provider]
                    if d is not None:
                        timeout = max(0.0, d - (time.perf_counter() - lone.started))
                done, _ = await asyncio.wait(
                    running.keys(), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    hedge_used = True
                    if not await self._hedge_budget_ok(time.time()):
                        self.metrics.hedges.labels("budget_exhausted").inc()
                        continue
                    nxt = await self._next_admitted(ctx, messages, max_tokens)
                    if nxt is not None:
                        nxt.hedge = True
                        self.metrics.hedges.labels("fired").inc()
                        start(nxt)
                    continue
                for task in done:
                    run = running.pop(task)
                    try:
                        comp = task.result()
                    except ProviderError as err:
                        await self._record_failure(run, err, ctx)
                        continue
                    cost = await self._record_success(run, comp, ctx)
                    for other_task, other in list(running.items()):
                        other_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, ProviderError):
                            await other_task
                        extra_cost += await self._record_cancel(other, ctx)
                        running.pop(other_task, None)
                    if hedge_used and any(a.hedge for a in ctx.attempts):
                        self.metrics.hedges.labels("hedge_won" if run.hedge else "primary_won").inc()
                    first_delay = next(iter(delays.values()), None)
                    return RouteResult(
                        comp,
                        ctx.attempts,
                        ctx.failovers,
                        hedge_used and any(a.hedge for a in ctx.attempts),
                        cost + extra_cost,
                        round(first_delay * 1000, 1) if first_delay is not None else None,
                    )
                if not running:
                    nxt = await self._next_admitted(ctx, messages, max_tokens)
                    if nxt is None:
                        break
                    self._count_failover(ctx, nxt.provider, "error")
                    start(nxt)
        finally:
            for task, run in list(running.items()):  # caller cancelled us: do not leak provider calls
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, ProviderError):
                    await task
                await self._record_cancel(run, ctx)
        raise AllProvidersFailed(ctx.attempts)
