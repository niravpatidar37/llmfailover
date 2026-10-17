"""Per-provider circuit breaker (closed / open / half-open) with state persisted in Redis.

* closed -> open: error rate >= threshold, or p95 latency > the provider's budget, over the sliding window
  (counting only samples since the last state change, so a recovered provider is judged on fresh data).
* open -> half_open: after a cooldown that doubles after every failed probe (capped).
* half_open: only ``half_open_probe_ratio`` of eligible requests are let through as probes, at most
  ``MAX_PROBES_IN_FLIGHT`` at a time. ``half_open_successes`` consecutive successes close the breaker;
  any probe failure (or a probe slower than the latency budget) reopens it immediately.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from .config import BreakerPolicy
from .errors import NOT_PROVIDER_HEALTH, ErrorType
from .health import HealthTracker
from .metrics import BREAKER_STATE_VALUE, Metrics

KEY = "gw:breaker:"
EVENTS_KEY = "gw:breaker:events"
MAX_PROBES_IN_FLIGHT = 2

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


def _m(d: dict[str, str]) -> dict[Any, Any]:
    return dict(d)


@dataclass
class Admission:
    allowed: bool
    probe: bool = False
    state: str = CLOSED


class CircuitBreakers:
    def __init__(
        self,
        redis: Redis,
        health: HealthTracker,
        policy: BreakerPolicy,
        latency_budget_ms: dict[str, float],
        metrics: Metrics,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.redis = redis
        self.health = health
        self.policy = policy
        self.budget = latency_budget_ms
        self.metrics = metrics
        self.rng = rng
        self._locks: dict[str, asyncio.Lock] = {}
        self._probes: dict[str, int] = {}

    def _lock(self, p: str) -> asyncio.Lock:
        return self._locks.setdefault(p, asyncio.Lock())

    async def _load(self, p: str) -> dict[str, str]:
        raw = await self.redis.hgetall(KEY + p)
        d = {
            (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
            for k, v in raw.items()
        }
        if not d:
            d = {
                "state": CLOSED,
                "changed_at": "0",
                "opened_at": "0",
                "cooldown_s": str(self.policy.open_cooldown_s),
                "probe_successes": "0",
            }
        return d

    async def _transition(
        self, p: str, d: dict[str, str], to: str, reason: str, now: float, extra: dict[str, Any] | None = None
    ) -> None:
        frm = d["state"]
        d["state"] = to
        d["changed_at"] = str(now)
        d["probe_successes"] = "0"
        if to == OPEN:
            d["opened_at"] = str(now)
        await self.redis.hset(KEY + p, mapping=_m(d))
        event = {"ts": now, "provider": p, "from": frm, "to": to, "reason": reason, **(extra or {})}
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.rpush(EVENTS_KEY, json.dumps(event))
            pipe.ltrim(EVENTS_KEY, -5000, -1)
            await pipe.execute()
        self.metrics.breaker_transitions.labels(p, frm, to).inc()
        self.metrics.breaker_state.labels(p).set(BREAKER_STATE_VALUE[to])

    async def state(self, p: str, now: float | None = None) -> str:
        t = now if now is not None else time.time()
        async with self._lock(p):
            d = await self._load(p)
            if d["state"] == OPEN and t - float(d["opened_at"]) >= float(d["cooldown_s"]):
                await self._transition(p, d, HALF_OPEN, "cooldown_elapsed", t)
            self.metrics.breaker_state.labels(p).set(BREAKER_STATE_VALUE[d["state"]])
            return d["state"]

    async def admit(self, p: str, now: float | None = None) -> Admission:
        st = await self.state(p, now)
        if st == CLOSED:
            return Admission(True, False, st)
        if st == OPEN:
            return Admission(False, False, st)
        if self._probes.get(p, 0) >= MAX_PROBES_IN_FLIGHT or self.rng() >= self.policy.half_open_probe_ratio:
            return Admission(False, False, st)
        self._probes[p] = self._probes.get(p, 0) + 1
        return Admission(True, True, st)

    def release_probe(self, p: str) -> None:
        self._probes[p] = max(0, self._probes.get(p, 0) - 1)

    async def on_result(
        self,
        p: str,
        ok: bool,
        latency_ms: float,
        error_type: ErrorType | None,
        probe: bool,
        now: float | None = None,
    ) -> None:
        t = now if now is not None else time.time()
        if not ok and error_type in NOT_PROVIDER_HEALTH:
            if probe:
                self.release_probe(p)
            return
        async with self._lock(p):
            d = await self._load(p)
            budget = self.budget.get(p, float("inf"))
            if d["state"] == HALF_OPEN and probe:
                if ok and latency_ms <= budget:
                    d["probe_successes"] = str(int(d["probe_successes"]) + 1)
                    if int(d["probe_successes"]) >= self.policy.half_open_successes:
                        d["cooldown_s"] = str(self.policy.open_cooldown_s)
                        await self._transition(p, d, CLOSED, "probes_succeeded", t)
                    else:
                        await self.redis.hset(KEY + p, mapping=_m(d))
                else:
                    d["cooldown_s"] = str(min(self.policy.max_cooldown_s, float(d["cooldown_s"]) * 2))
                    reason = "probe_failed" if not ok else "probe_too_slow"
                    await self._transition(
                        p, d, OPEN, reason, t, {"error_type": error_type.value if error_type else None}
                    )
            elif d["state"] == CLOSED:
                st = await self.health.stats(p, since=float(d["changed_at"]), now=t)
                judged = st.successes + st.health_failures
                er = st.error_rate
                if (
                    judged >= self.policy.min_samples
                    and er is not None
                    and er >= self.policy.error_rate_threshold
                ):
                    await self._transition(
                        p,
                        d,
                        OPEN,
                        "error_rate",
                        t,
                        {"error_rate": round(er, 3), "samples": judged, "errors": st.errors},
                    )
                elif st.successes >= self.policy.min_samples and st.p95_ms is not None and st.p95_ms > budget:
                    await self._transition(
                        p, d, OPEN, "p95_over_budget", t, {"p95_ms": st.p95_ms, "budget_ms": budget}
                    )
        if probe:
            self.release_probe(p)

    async def force(self, p: str, to: str, reason: str = "admin") -> None:
        async with self._lock(p):
            d = await self._load(p)
            await self._transition(p, d, to, reason, time.time())

    async def events(self, since: float = 0.0) -> list[dict[str, Any]]:
        raw = await self.redis.lrange(EVENTS_KEY, 0, -1)
        out = [json.loads(r) for r in raw]
        return [e for e in out if e["ts"] >= since]
