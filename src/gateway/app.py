"""FastAPI app: OpenAI-compatible POST /v1/chat/completions plus admin, health and metrics endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.asyncio import Redis

from .breaker import CircuitBreakers
from .config import GatewayConfig, Settings, get_config, load_secrets
from .health import HealthTracker
from .metrics import Metrics
from .providers import LiteLLMCaller, ProviderCaller
from .redact import RedactingFilter
from .router import Router
from .service import GatewayService, HandleResult, RequestContext, error_body
from .spend import SpendLedger
from .store import IdempotencyStore, TenantStore

TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
FEATURE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str | None = None
    messages: list[Message] = Field(min_length=1)
    max_tokens: int | None = Field(default=None, ge=1, le=4096)
    temperature: float | None = Field(default=None, ge=0, le=2)
    stream: bool = False
    metadata: dict[str, Any] | None = None


class TenantCreate(BaseModel):
    tenant: str
    rpm: int = Field(default=600, ge=1, le=100_000)


def configure_logging(path: str | None) -> None:
    lg = logging.getLogger("gateway.requests")
    if getattr(lg, "_gw_configured", False):
        return
    handler: logging.Handler = (
        logging.FileHandler(path, encoding="utf-8") if path else logging.StreamHandler(sys.stdout)
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    lg.addHandler(handler)
    lg.addFilter(RedactingFilter())
    lg.setLevel(logging.INFO)
    lg.propagate = False
    lg._gw_configured = True  # type: ignore[attr-defined]


def _json(res: HandleResult) -> JSONResponse:
    return JSONResponse(res.body, status_code=res.status, headers=res.headers)


def _err(status: int, msg: str, etype: str, code: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(error_body(msg, etype, code), status_code=status, headers=headers)


def create_app(
    cfg: GatewayConfig | None = None,
    settings: Settings | None = None,
    redis: Redis | None = None,
    caller: ProviderCaller | None = None,
    ledger: SpendLedger | None = None,
    start_background: bool = True,
) -> FastAPI:
    if cfg is None or settings is None:
        load_secrets()
    cfg = cfg or get_config()
    settings = settings or Settings.from_env()
    redis = redis or Redis.from_url(settings.redis_url)
    metrics = Metrics()
    health = HealthTracker(redis, cfg.breaker.window_s)
    breakers = CircuitBreakers(
        redis, health, cfg.breaker, {n: p.latency_budget_ms for n, p in cfg.providers.items()}, metrics
    )
    if ledger is None:
        ledger = SpendLedger(
            settings.ledger_path, settings.project, settings.project_cap_usd, settings.global_cap_usd
        )
    router = Router(cfg, breakers, health, metrics, ledger, caller or LiteLLMCaller())
    service = GatewayService(cfg, redis, router, metrics)
    tenants = TenantStore(redis)
    configure_logging(settings.request_log_path)

    async def ticker(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await refresh_gauges()
            except Exception:
                logging.getLogger("gateway").exception("gauge refresh failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=1.0)

    async def refresh_gauges() -> None:
        for p in cfg.enabled_providers():
            await breakers.state(p)  # also drives open -> half_open when the cooldown elapses
            st = await health.stats(p)
            metrics.health_samples.labels(p).set(st.samples)
            if st.success_rate is not None:
                metrics.health_success_rate.labels(p).set(st.success_rate)
            for q, v in (("0.5", st.p50_ms), ("0.95", st.p95_ms), ("0.99", st.p99_ms)):
                if v is not None:
                    metrics.health_latency.labels(p, q).set(v / 1000)
        metrics.queue_depth.set(await service.queue_depth())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        tasks: list[asyncio.Task[None]] = []
        if start_background:
            tasks = [asyncio.create_task(service.run_worker(stop)), asyncio.create_task(ticker(stop))]
        yield
        stop.set()
        for t in tasks:
            with contextlib.suppress(Exception):
                await t

    app = FastAPI(
        title="Self-Healing LLM Gateway",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.service = service
    app.state.breakers = breakers
    app.state.health = health
    app.state.metrics = metrics
    app.state.tenants = tenants
    app.state.redis = redis
    app.state.refresh_gauges = refresh_gauges

    def is_admin(request: Request) -> bool:
        tok = request.headers.get("x-admin-token", "")
        return bool(settings.admin_token) and hmac.compare_digest(tok.encode(), settings.admin_token.encode())

    async def authenticate(request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        return await tenants.resolve(auth[7:].strip())

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        tenant = await authenticate(request)
        if tenant is None:
            return _err(401, "missing or invalid API key", "authentication_error", "invalid_api_key")
        try:
            raw = await request.body()
            if len(raw) > 64_000:
                return _err(413, "request body too large", "invalid_request_error", "too_large")
            req = ChatRequest.model_validate(json.loads(raw or b"{}"))
        except (ValueError, ValidationError) as exc:
            msg = (
                "invalid request body"
                if not isinstance(exc, ValidationError)
                else f"invalid request body: {exc.error_count()} validation error(s)"
            )
            return _err(400, msg, "invalid_request_error", "invalid_body")
        meta = req.metadata or {}

        def pick(key: str, header: str) -> str | None:
            v = meta.get(key, request.headers.get(header))
            return str(v) if v is not None else None

        m_tenant = pick("tenant", "x-tenant-id")
        feature = pick("feature", "x-feature")
        rid = pick("request_id", "x-request-id")
        rclass = pick("request_class", "x-request-class") or "generate"
        if not m_tenant or not feature or not rid:
            pairs = (("tenant", m_tenant), ("feature", feature), ("request_id", rid))
            missing = [k for k, v in pairs if not v]
            return _err(
                400,
                f"missing required metadata: {', '.join(missing)}",
                "invalid_request_error",
                "missing_metadata",
            )
        if not TENANT_RE.match(m_tenant) or not FEATURE_RE.match(feature) or not REQUEST_ID_RE.match(rid):
            return _err(
                400,
                "malformed metadata (tenant/feature/request_id format)",
                "invalid_request_error",
                "bad_metadata",
            )
        if not hmac.compare_digest(m_tenant, tenant):
            return _err(403, "metadata tenant does not match API key", "permission_error", "tenant_mismatch")
        if rclass not in cfg.classes:
            return _err(
                400,
                f"unknown request_class; valid: {sorted(cfg.classes)}",
                "invalid_request_error",
                "bad_request_class",
            )
        if req.stream:
            return _err(
                400,
                "stream=true is not supported by this gateway",
                "invalid_request_error",
                "stream_unsupported",
            )
        lim = cfg.limits
        messages = [{"role": m.role, "content": m.content} for m in req.messages]
        if (
            len(messages) > lim.max_messages
            or sum(len(m["content"]) for m in messages) > lim.max_prompt_chars
        ):
            return _err(400, "prompt too large", "invalid_request_error", "prompt_too_large")
        allowed, retry_after = await tenants.allow_request(tenant)
        if not allowed:
            app.state.metrics.rate_limited.labels(tenant).inc()
            return _err(
                429,
                "tenant rate limit exceeded",
                "rate_limit_error",
                "tenant_rate_limited",
                {"Retry-After": f"{retry_after:.0f}"},
            )
        cls = cfg.classes[rclass]
        max_tokens = min(req.max_tokens or cls.max_tokens_cap, cls.max_tokens_cap)
        label = await tenants.feature_label(tenant, feature, lim.max_features_per_tenant)
        ctx = RequestContext(
            tenant=tenant, feature=feature, request_id=rid, request_class=rclass, feature_label=label
        )
        return _json(await service.handle(ctx, messages, max_tokens, req.temperature))

    @app.get("/v1/requests/{request_id}")
    async def get_request(request_id: str, request: Request) -> Response:
        tenant = await authenticate(request)
        if tenant is None:
            return _err(401, "missing or invalid API key", "authentication_error", "invalid_api_key")
        if not REQUEST_ID_RE.match(request_id):
            return _err(400, "malformed request_id", "invalid_request_error", "bad_metadata")
        rec = await IdempotencyStore(redis).get(tenant, request_id)
        if rec is None:
            return _err(404, "unknown request_id", "not_found", "not_found")
        if rec.status == "done" and rec.response is not None:
            return JSONResponse(rec.response)
        return JSONResponse(
            {
                "object": "gateway.deferred",
                "status": rec.status,
                "request_id": request_id,
                "error": rec.error,
            },
            status_code=202 if rec.status in ("queued", "in_flight") else 503,
        )

    @app.get("/healthz")
    async def healthz() -> Response:
        try:
            await redis.ping()
        except Exception:
            return JSONResponse({"status": "degraded", "redis": False}, status_code=503)
        return JSONResponse({"status": "ok", "redis": True})

    @app.get("/metrics")
    async def prom_metrics() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    # ---- admin (X-Admin-Token) --------------------------------------------------------------------
    @app.post("/admin/tenants")
    async def create_tenant(body: TenantCreate, request: Request) -> Response:
        if not is_admin(request):
            return _err(403, "admin token required", "permission_error", "admin_only")
        if not TENANT_RE.match(body.tenant):
            return _err(400, "malformed tenant", "invalid_request_error", "bad_metadata")
        key = await tenants.create(body.tenant, body.rpm)
        return JSONResponse(
            {
                "tenant": body.tenant,
                "rpm": body.rpm,
                "api_key": key,
                "note": "store this key now; only its SHA-256 is kept",
            }
        )

    @app.get("/admin/providers")
    async def providers(request: Request) -> Response:
        if not is_admin(request):
            return _err(403, "admin token required", "permission_error", "admin_only")
        out = []
        for p in cfg.enabled_providers():
            st = await health.stats(p)
            out.append({**st.as_dict(), "breaker": await breakers.state(p)})
        return JSONResponse({"providers": out, "queue_depth": await service.queue_depth()})

    @app.get("/admin/breaker/events")
    async def breaker_events(request: Request, since: float = 0.0) -> Response:
        if not is_admin(request):
            return _err(403, "admin token required", "permission_error", "admin_only")
        return JSONResponse({"events": await breakers.events(since)})

    @app.get("/admin/costs")
    async def costs(request: Request) -> Response:
        if not is_admin(request):
            return _err(403, "admin token required", "permission_error", "admin_only")
        return JSONResponse({"usd_by_tenant_feature": await service.costs.summary(redis)})

    @app.post("/admin/side-effects")
    async def side_effects(request: Request) -> Response:
        if not is_admin(request):
            return _err(403, "admin token required", "permission_error", "admin_only")
        body = await request.json()
        counts = await service.idem.side_effects(
            str(body["tenant"]), [str(r) for r in body["request_ids"]][:5000]
        )
        return JSONResponse({"counts": counts})

    return app


def main() -> None:  # pragma: no cover - process entry point
    import os

    import uvicorn

    load_secrets()
    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("GATEWAY_PORT", "8103"))
    # keep-alive longer than typical client pools: with uvicorn's 5 s default, a client reusing an idle
    # connection can race the server closing it (we saw one client ReadError in the first chaos run).
    uvicorn.run(create_app(), host=host, port=port, log_level="warning", timeout_keep_alive=75)


if __name__ == "__main__":  # pragma: no cover
    main()
