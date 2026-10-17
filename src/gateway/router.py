"""Provider selection: preference lists per request class, breaker admission, failover and hedging."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .breaker import CircuitBreakers
from .config import GatewayConfig, RequestClass
from .errors import ErrorType, ProviderError
from .health import HealthTracker
from .metrics import Metrics
from .providers import Completion, ProviderCaller
from .spend import BudgetExceeded, SpendLedger, estimate_tokens


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


class AllProvidersFailed(Exception):
    def __init__(self, attempts: list[Attempt]):
        super().__init__("no provider could serve the request")
        self.attempts = attempts


@dataclass
class _Running:
    provider: str
    probe: bool
    estimate_usd: float
    started: float
    hedge: bool = False


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
        ledger: SpendLedger | None,
        caller: ProviderCaller,
    ) -> None:
        self.cfg = cfg
        self.breakers = breakers
        self.health = health
        self.metrics = metrics
        self.ledger = ledger
        self.caller = caller
        self._hedge_times: deque[float] = deque()

    # ---- helpers -------------------------------------------------------------------------------
    def _hedge_budget_ok(self, now: float) -> bool:
        while self._hedge_times and now - self._hedge_times[0] > 60:
            self._hedge_times.popleft()
        return len(self._hedge_times) < self.cfg.hedging.max_hedges_per_minute

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
            if est > 0 and self.ledger is not None:
                try:
                    self.ledger.reserve(est)
                except BudgetExceeded:
                    if adm.probe:
                        self.breakers.release_probe(p)
                    ctx.attempts.append(Attempt(p, "skipped_budget"))
                    self.metrics.provider_errors.labels(p, ErrorType.BUDGET.value).inc()
                    continue
            return _Running(p, adm.probe, est, time.perf_counter())
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
        if self.ledger is not None and run.estimate_usd:
            self.ledger.release(run.estimate_usd)
        await self.health.record(run.provider, False, latency, err.error_type)
        await self.breakers.on_result(run.provider, False, latency, err.error_type, run.probe)

    async def _record_success(self, run: _Running, comp: Completion, ctx: _Ctx) -> float:
        latency = comp.latency_ms
        ctx.attempts.append(Attempt(run.provider, "ok", round(latency, 1), run.probe, run.hedge))
        self.metrics.provider_attempts.labels(run.provider, "ok").inc()
        self.metrics.provider_latency.labels(run.provider).observe(latency / 1000)
        cost = self.cfg.providers[run.provider].cost_usd(comp.input_tokens, comp.output_tokens)
        if self.ledger is not None and run.estimate_usd:
            self.ledger.release(run.estimate_usd)
            self.ledger.record(run.provider, comp.model, comp.input_tokens, comp.output_tokens, cost)
        await self.health.record(run.provider, True, latency)
        await self.breakers.on_result(run.provider, True, latency, None, run.probe)
        return cost

    async def _record_cancel(self, run: _Running, ctx: _Ctx) -> float:
        """A cancelled hedge loser. Providers may still bill it, so paid ones are charged worst-case."""
        latency = (time.perf_counter() - run.started) * 1000
        ctx.attempts.append(Attempt(run.provider, "cancelled", round(latency, 1), run.probe, run.hedge))
        self.metrics.provider_attempts.labels(run.provider, "cancelled").inc()
        if run.probe:
            self.breakers.release_probe(run.provider)
        if self.ledger is not None and run.estimate_usd:
            self.ledger.release(run.estimate_usd)
            self.ledger.record(
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

    # ---- main entry -----------------------------------------------------------------------------
    async def route(
        self, cls: RequestClass, messages: list[dict[str, str]], max_tokens: int, temperature: float | None
    ) -> RouteResult:
        candidates = [p for p in cls.preference if self.cfg.providers[p].enabled()]
        ctx = _Ctx(cls=cls, candidates=candidates)
        running: dict[asyncio.Task[Completion], _Running] = {}
        hedge_used = False
        extra_cost = 0.0

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
                    timeout = max(0.0, cls.hedge_after_ms / 1000 - (time.perf_counter() - lone.started))
                done, _ = await asyncio.wait(
                    running.keys(), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    hedge_used = True
                    if not self._hedge_budget_ok(time.time()):
                        self.metrics.hedges.labels("budget_exhausted").inc()
                        continue
                    nxt = await self._next_admitted(ctx, messages, max_tokens)
                    if nxt is not None:
                        nxt.hedge = True
                        self._hedge_times.append(time.time())
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
                    return RouteResult(
                        comp,
                        ctx.attempts,
                        ctx.failovers,
                        hedge_used and any(a.hedge for a in ctx.attempts),
                        cost + extra_cost,
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
