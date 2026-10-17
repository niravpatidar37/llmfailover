"""Request handling: idempotency, routing, deferrable queueing, cost attribution and request logging."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis

from .config import GatewayConfig, RequestClass
from .metrics import Metrics
from .redact import redact
from .router import AllProvidersFailed, Router, RouteResult
from .store import CostStore, IdempotencyStore, IdemRecord

QUEUE_KEY = "gw:queue"
PROCESSING_KEY = "gw:queue:processing"
JOB_PREFIX = "gw:job:"
LEASE_S = 120
IN_FLIGHT_WAIT_S = 15.0

req_log = logging.getLogger("gateway.requests")


@dataclass
class RequestContext:
    tenant: str
    feature: str
    request_id: str
    request_class: str
    feature_label: str = ""


@dataclass
class HandleResult:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


def fingerprint(messages: list[dict[str, str]], max_tokens: int, temperature: float | None, cls: str) -> str:
    canon = json.dumps({"m": messages, "t": max_tokens, "temp": temperature, "c": cls}, sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()


def error_body(message: str, etype: str, code: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"message": message, "type": etype, "code": code, **extra}}


class GatewayService:
    def __init__(self, cfg: GatewayConfig, redis: Redis, router: Router, metrics: Metrics) -> None:
        self.cfg = cfg
        self.redis = redis
        self.router = router
        self.metrics = metrics
        self.idem = IdempotencyStore(redis)
        self.costs = CostStore()
        self._worker_sem = asyncio.Semaphore(4)
        self._tasks: set[asyncio.Task[None]] = set()

    # ---- response shaping -----------------------------------------------------------------------
    def _response(self, ctx: RequestContext, rr: RouteResult) -> dict[str, Any]:
        c = rr.completion
        return {
            "id": "chatcmpl-gw-" + hashlib.sha256(f"{ctx.tenant}:{ctx.request_id}".encode()).hexdigest()[:24],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": c.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": c.content},
                    "finish_reason": c.finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": c.input_tokens,
                "completion_tokens": c.output_tokens,
                "total_tokens": c.input_tokens + c.output_tokens,
            },
            "gateway": {
                "request_id": ctx.request_id,
                "provider": c.provider,
                "request_class": ctx.request_class,
                "failovers": rr.failovers,
                "hedged": rr.hedged,
                "cost_usd": round(rr.cost_usd, 10),
                "attempts": [a.as_dict() for a in rr.attempts],
            },
        }

    def _log(
        self,
        ctx: RequestContext,
        messages: list[dict[str, str]],
        status: int,
        latency_ms: float,
        provider: str | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        preview = " | ".join(m["content"] for m in messages)[:200]
        rec = {
            "ts": time.time(),
            "tenant": ctx.tenant,
            "feature": ctx.feature,
            "request_id": ctx.request_id,
            "class": ctx.request_class,
            "status": status,
            "latency_ms": round(latency_ms, 1),
            "provider": provider,
            "prompt_preview": redact(preview),
            **(extra or {}),
        }
        req_log.info(json.dumps(rec))

    async def _commit(self, ctx: RequestContext, rr: RouteResult, fp: str) -> dict[str, Any]:
        body = self._response(ctx, rr)
        await self.idem.commit(ctx.tenant, ctx.request_id, IdemRecord("done", fp, response=body))
        c = rr.completion
        label = ctx.feature_label or ctx.feature
        await self.costs.add(
            self.redis, ctx.tenant, label, c.provider, rr.cost_usd, c.input_tokens, c.output_tokens
        )
        self.metrics.cost.labels(ctx.tenant, label, c.provider).inc(rr.cost_usd)
        self.metrics.tokens.labels(ctx.tenant, label, c.provider, "in").inc(c.input_tokens)
        self.metrics.tokens.labels(ctx.tenant, label, c.provider, "out").inc(c.output_tokens)
        return body

    # ---- main entry -----------------------------------------------------------------------------
    async def handle(
        self, ctx: RequestContext, messages: list[dict[str, str]], max_tokens: int, temperature: float | None
    ) -> HandleResult:
        t0 = time.perf_counter()
        cls = self.cfg.classes[ctx.request_class]
        fp = fingerprint(messages, max_tokens, temperature, ctx.request_class)
        res = await self._handle(ctx, cls, messages, max_tokens, temperature, fp)
        latency = (time.perf_counter() - t0) * 1000
        served = res.body.get("gateway", {}).get("provider") if res.status == 200 else None
        self.metrics.client_requests.labels(ctx.request_class, str(res.status), served or "none").inc()
        self.metrics.client_latency.labels(ctx.request_class).observe(latency / 1000)
        self._log(
            ctx,
            messages,
            res.status,
            latency,
            served,
            {"replay": res.headers.get("X-Idempotent-Replay") == "true"},
        )
        return res

    async def _handle(
        self,
        ctx: RequestContext,
        cls: RequestClass,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float | None,
        fp: str,
    ) -> HandleResult:
        claimed, rec = await self.idem.claim(ctx.tenant, ctx.request_id, fp)
        if not claimed:
            return await self._duplicate(ctx, rec, fp)
        try:
            rr = await self.router.route(cls, messages, max_tokens, temperature)
        except AllProvidersFailed as exc:
            attempts = [a.as_dict() for a in exc.attempts]
            if cls.mode == "deferrable":
                await self._enqueue(ctx, messages, max_tokens, temperature, fp, attempt=0)
                return HandleResult(202, self._queued_body(ctx), {"Retry-After": "2"})
            await self.idem.release(ctx.tenant, ctx.request_id)
            return HandleResult(
                503,
                error_body(
                    "all providers unavailable for this request class",
                    "service_unavailable",
                    "providers_degraded",
                    attempts=attempts,
                ),
                {"Retry-After": "5"},
            )
        except BaseException:
            await self.idem.release(ctx.tenant, ctx.request_id)
            raise
        body = await self._commit(ctx, rr, fp)
        headers = {
            "X-Gateway-Provider": rr.completion.provider,
            "X-Gateway-Failovers": str(rr.failovers),
            "X-Idempotent-Replay": "false",
        }
        return HandleResult(200, body, headers)

    def _queued_body(self, ctx: RequestContext) -> dict[str, Any]:
        return {
            "object": "gateway.deferred",
            "status": "queued",
            "request_id": ctx.request_id,
            "poll": f"/v1/requests/{ctx.request_id}",
        }

    async def _duplicate(self, ctx: RequestContext, rec: IdemRecord | None, fp: str) -> HandleResult:
        deadline = time.monotonic() + IN_FLIGHT_WAIT_S
        while rec is not None and rec.status == "in_flight" and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            rec = await self.idem.get(ctx.tenant, ctx.request_id)
        if rec is None:  # the original failed and released its claim; the client may retry
            return HandleResult(
                409, error_body("original request failed; retry", "conflict", "retry"), {"Retry-After": "1"}
            )
        if rec.fingerprint != fp:
            return HandleResult(
                422,
                error_body(
                    "request_id reused with a different request body",
                    "invalid_request_error",
                    "idempotency_key_mismatch",
                ),
            )
        self.metrics.idempotent_replays.inc()
        if rec.status == "done" and rec.response is not None:
            return HandleResult(
                200,
                rec.response,
                {"X-Idempotent-Replay": "true", "X-Gateway-Provider": rec.response["gateway"]["provider"]},
            )
        if rec.status == "queued":
            return HandleResult(202, self._queued_body(ctx), {"X-Idempotent-Replay": "true"})
        if rec.status == "failed":
            return HandleResult(
                503,
                error_body(rec.error or "deferred request failed", "service_unavailable", "deferred_failed"),
                {"X-Idempotent-Replay": "true"},
            )
        return HandleResult(
            409,
            error_body("request with this request_id is still in flight", "conflict", "in_flight"),
            {"Retry-After": "2"},
        )

    # ---- deferrable queue ---------------------------------------------------------------------
    def backoff_s(self, attempt: int) -> float:
        """Exponential backoff with 'equal jitter': half fixed, half random, capped."""
        q = self.cfg.queue
        d = float(min(q.max_delay_s, q.base_delay_s * (2.0**attempt)))
        return d / 2 + random.uniform(0, d / 2)  # nosec B311 - backoff jitter, not security

    async def _enqueue(
        self,
        ctx: RequestContext,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float | None,
        fp: str,
        attempt: int,
    ) -> None:
        member = f"{ctx.tenant}:{ctx.request_id}"
        job = {
            "ctx": ctx.__dict__,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "fp": fp,
            "attempt": attempt,
        }
        when = time.time() + self.backoff_s(attempt)
        await self.idem.set(ctx.tenant, ctx.request_id, IdemRecord("queued", fp))
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.set(JOB_PREFIX + member, json.dumps(job), ex=24 * 3600)
            pipe.zadd(QUEUE_KEY, {member: when})
            pipe.zrem(PROCESSING_KEY, member)
            await pipe.execute()
        self.metrics.queue_events.labels("enqueued" if attempt == 0 else "retry_scheduled").inc()

    async def queue_depth(self) -> int:
        return int(await self.redis.zcard(QUEUE_KEY)) + int(await self.redis.zcard(PROCESSING_KEY))

    async def worker_tick(self) -> int:
        """Claim due jobs (and expired leases) and process them. Returns the number of jobs started."""
        now = time.time()
        for raw in await self.redis.zrangebyscore(PROCESSING_KEY, 0, now):  # lease expired: crashed worker
            m = raw.decode() if isinstance(raw, bytes) else str(raw)
            if await self.redis.zrem(PROCESSING_KEY, m):
                await self.redis.zadd(QUEUE_KEY, {m: now})
        started = 0
        for raw in await self.redis.zrangebyscore(QUEUE_KEY, 0, now, start=0, num=8):
            member = raw.decode() if isinstance(raw, bytes) else str(raw)
            if not await self.redis.zrem(QUEUE_KEY, member):
                continue  # another worker claimed it
            await self.redis.zadd(PROCESSING_KEY, {member: now + LEASE_S})
            task = asyncio.create_task(self._process(member))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            started += 1
        return started

    async def _process(self, member: str) -> None:
        async with self._worker_sem:
            raw = await self.redis.get(JOB_PREFIX + member)
            if raw is None:
                await self.redis.zrem(PROCESSING_KEY, member)
                return
            job = json.loads(raw)
            ctx = RequestContext(**job["ctx"])
            rec = await self.idem.get(ctx.tenant, ctx.request_id)
            if rec is not None and rec.status == "done":  # already committed: never execute twice
                self.metrics.queue_events.labels("dedup_skipped").inc()
                await self.redis.zrem(PROCESSING_KEY, member)
                return
            cls = self.cfg.classes[ctx.request_class]
            t0 = time.perf_counter()
            try:
                rr = await self.router.route(cls, job["messages"], job["max_tokens"], job["temperature"])
            except AllProvidersFailed:
                attempt = int(job["attempt"]) + 1
                if attempt >= self.cfg.queue.max_attempts:
                    await self.idem.set(
                        ctx.tenant,
                        ctx.request_id,
                        IdemRecord("failed", job["fp"], error="max attempts exhausted"),
                    )
                    await self.redis.zrem(PROCESSING_KEY, member)
                    self.metrics.queue_events.labels("dead_lettered").inc()
                else:
                    await self._enqueue(
                        ctx, job["messages"], job["max_tokens"], job["temperature"], job["fp"], attempt
                    )
                return
            await self._commit(ctx, rr, job["fp"])
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.zrem(PROCESSING_KEY, member)
                pipe.delete(JOB_PREFIX + member)
                await pipe.execute()
            self.metrics.queue_events.labels("completed").inc()
            self._log(
                ctx,
                job["messages"],
                200,
                (time.perf_counter() - t0) * 1000,
                rr.completion.provider,
                {"deferred": True, "attempt": job["attempt"]},
            )

    async def run_worker(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.worker_tick()
            except Exception:  # keep the worker alive; errors are logged
                logging.getLogger("gateway").exception("queue worker tick failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.cfg.queue.poll_interval_s)
