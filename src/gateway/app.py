"""FastAPI app: OpenAI-compatible POST /v1/chat/completions plus admin, health and metrics endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
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
from .config import (
    GatewayConfig,
    Settings,
    chaos_requested,
    check_runtime_safety,
    failover_capacity_warning,
    fake_llm_requested,
    get_config,
    load_secrets,
)
from .health import HealthTracker
from .logging_setup import JsonFormatter
from .metrics import Metrics
from .migrate import current_version
from .migrations import REQUIRED_VERSION
from .providers import LiteLLMCaller, ProviderCaller
from .redact import RedactingFilter
from .redis_client import make_redis
from .router import Router
from .service import GatewayService, HandleResult, RequestContext, error_body
from .spend import RedisSpendLedger, SpendGuard, SpendLedger
from .store import IdempotencyStore, KillSwitch, TenantStore

TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
FEATURE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")

# Sent on every response (JSON API; nothing is ever rendered as HTML). HSTS only where TLS terminates.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}
HSTS = "max-age=31536000; includeSubDomains"


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


class KillSwitchBody(BaseModel):
    engaged: bool


def configure_logging(path: str | None) -> None:
    lg = logging.getLogger("gateway.requests")
    if getattr(lg, "_gw_configured", False):
        return
    handler: logging.Handler = (
        logging.FileHandler(path, encoding="utf-8") if path else logging.StreamHandler(sys.stdout)
    )
    handler.setFormatter(logging.Formatter("%(message)s") if path else JsonFormatter())
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
    ledger: SpendLedger | SpendGuard | None = None,
    start_background: bool = True,
) -> FastAPI:
    if cfg is None or settings is None:
        load_secrets()
    cfg = cfg or get_config()
    settings = settings or Settings.from_env()
    log = logging.getLogger("gateway")
    for w in check_runtime_safety(cfg, settings):  # raises UnsafeConfiguration (fail closed) before anything
        log.warning(json.dumps({"event": "config_warning", "detail": w}))
    owns_redis = redis is None
    redis = redis or make_redis(settings)
    metrics = Metrics()
    health = HealthTracker(redis, cfg.breaker.window_s)
    breakers = CircuitBreakers(
        redis, health, cfg.breaker, {n: p.latency_budget_ms for n, p in cfg.providers.items()}, metrics
    )
    if ledger is None:
        if settings.spend_backend == "redis":
            ledger = RedisSpendLedger(redis, settings.project, settings.project_cap_usd)
        else:
            ledger = SpendLedger(
                settings.ledger_path, settings.project, settings.project_cap_usd, settings.global_cap_usd
            )
    kill = KillSwitch(redis, settings.kill_switch)
    fake_mode = caller is None and fake_llm_requested()
    if caller is None:
        if fake_mode:
            from .fake_llm import FakeLLMCaller  # refuses to construct in staging/prod (second guard)

            caller = FakeLLMCaller()
            log.warning(json.dumps({"event": "fake_llm_mode", "detail": "FAKE_LLM=1: no real model calls"}))
        else:
            caller = LiteLLMCaller()
    router = Router(cfg, breakers, health, metrics, ledger, caller, kill_switch=kill.engaged)
    service = GatewayService(cfg, redis, router, metrics, kill)
    tenants = TenantStore(redis)
    configure_logging(settings.request_log_path)
    serve_api = settings.role in ("all", "api")
    run_worker = settings.role in ("all", "worker")

    async def ticker(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await refresh_gauges()
            except Exception:
                logging.getLogger("gateway").exception("gauge refresh failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=1.0)

    async def failover_capacity() -> dict[str, Any]:
        """External (host-independent) providers, and which of them are not behind an open breaker."""
        external = cfg.external_providers()
        healthy = [p for p in external if await breakers.state(p) != "open"]
        metrics.external_providers_enabled.set(len(external))
        metrics.external_providers_healthy.set(len(healthy))
        return {
            "external_enabled": external,
            "external_healthy": healthy,
            "warning": failover_capacity_warning(healthy),
        }

    async def refresh_gauges() -> None:
        await failover_capacity()
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
        if settings.tenant_keys_json and serve_api:
            # Secret Manager is the source of truth for tenant keys (hashes only). Seeding Redis here also
            # repairs a Memorystore that lost its data. A malformed spec fails startup (fail closed).
            n = await tenants.sync_from_spec(json.loads(settings.tenant_keys_json))
            log.info(json.dumps({"event": "tenant_keys_synced", "live_keys": n}))
        metrics_server = None
        if settings.metrics_port:
            from prometheus_client import start_http_server

            metrics_server, _ = start_http_server(
                settings.metrics_port, addr=settings.metrics_bind, registry=metrics.registry
            )
        stop = asyncio.Event()
        tasks: list[asyncio.Task[None]] = []
        if start_background:
            tasks.append(asyncio.create_task(ticker(stop)))
            if run_worker:
                tasks.append(asyncio.create_task(service.run_worker(stop)))
        app.state.draining = False
        with contextlib.suppress(Exception):  # Redis may still be coming up; /readyz reports it then
            cap = await failover_capacity()
            if cap["warning"]:
                log.warning(json.dumps({"event": "failover_capacity", "detail": cap["warning"]}))
        yield
        # Graceful shutdown (SIGTERM from Cloud Run): fail readiness, stop claiming jobs, drain in-flight.
        app.state.draining = True
        stop.set()
        for t in tasks:
            with contextlib.suppress(Exception):
                await t
        left = await service.drain(settings.shutdown_grace_s)
        log.info(json.dumps({"event": "shutdown", "jobs_left_on_lease": left}))
        if metrics_server is not None:
            metrics_server.shutdown()
        if owns_redis:
            with contextlib.suppress(Exception):
                await redis.aclose()

    app = FastAPI(
        title="llmfailover",
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
    app.state.kill_switch = kill
    app.state.settings = settings
    app.state.draining = False
    app.state.fake_llm = fake_mode

    deployed = settings.env in ("staging", "prod")

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if deployed:
            response.headers.setdefault("Strict-Transport-Security", HSTS)
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> Response:
        # Never leak a stack trace or exception text to clients; the trace goes to the server log only.
        log.error(json.dumps({"event": "unhandled_exception", "type": type(exc).__name__}), exc_info=exc)
        # This handler runs outside the header middleware, so it sets the security headers itself.
        return _err(500, "internal error", "server_error", "internal_error", dict(SECURITY_HEADERS))

    def is_admin(request: Request) -> bool:
        tok = request.headers.get("x-admin-token", "")
        return bool(settings.admin_token) and hmac.compare_digest(tok.encode(), settings.admin_token.encode())

    async def authenticate(request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return None
        return await tenants.resolve(auth[7:].strip())

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
        """Liveness: the process and its event loop respond. No dependency checks, so a Redis blip does
        not make Cloud Run restart every instance at once."""
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz() -> Response:
        """Readiness/startup: Redis reachable, keyspace migrated, not draining."""
        body: dict[str, Any] = {"role": settings.role, "env": settings.env}
        ok = not app.state.draining
        body["draining"] = app.state.draining
        try:
            await redis.ping()
            body["redis"] = True
            ver = await current_version(redis)
            body["schema"] = {"current": ver, "required": REQUIRED_VERSION}
            ok = ok and ver >= REQUIRED_VERSION
            body["kill_switch"] = await kill.engaged()
        except Exception as exc:
            body["redis"] = False
            body["error"] = type(exc).__name__
            ok = False
        body["providers_enabled"] = cfg.enabled_providers()
        ok = ok and bool(body["providers_enabled"])
        body["fake_llm"] = fake_mode
        proxy_set = bool(os.environ.get("CHAOS_PROXY_URL", "").strip())
        body["chaos_routing"] = chaos_requested() and proxy_set and settings.env != "prod"
        if body.get("redis"):
            try:
                # WARN only: one healthy external provider still serves traffic, it just has no failover.
                body["failover"] = await failover_capacity()
            except Exception as exc:
                body["failover"] = {"error": type(exc).__name__}
        body["status"] = "ready" if ok else "not_ready"
        return JSONResponse(body, status_code=200 if ok else 503)

    if settings.metrics_port is None:

        @app.get("/metrics")
        async def prom_metrics() -> Response:
            return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    # ---- admin (X-Admin-Token) --------------------------------------------------------------------
    async def create_tenant(body: TenantCreate, request: Request) -> Response:
        if not is_admin(request):
            return _err(403, "admin token required", "permission_error", "admin_only")
        if not settings.allow_admin_tenant_create:
            return _err(
                403,
                "tenant keys are managed in Secret Manager (TENANT_KEYS_JSON) in this environment",
                "permission_error",
                "managed_by_secret_manager",
            )
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

    async def providers(request: Request) -> Response:
        if not is_admin(request):
            return _err(403, "admin token required", "permission_error", "admin_only")
        out = []
        for p in cfg.enabled_providers():
            st = await health.stats(p)
            out.append({**st.as_dict(), "breaker": await breakers.state(p)})
        return JSONResponse({"providers": out, "queue_depth": await service.queue_depth()})

    async def breaker_events(request: Request, since: float = 0.0) -> Response:
        if not is_admin(request):
            return _err(403, "admin token required", "permission_error", "admin_only")
        return JSONResponse({"events": await breakers.events(since)})

    async def costs(request: Request) -> Response:
        if not is_admin(request):
            return _err(403, "admin token required", "permission_error", "admin_only")
        return JSONResponse({"usd_by_tenant_feature": await service.costs.summary(redis)})

    async def side_effects(request: Request) -> Response:
        if not is_admin(request):
            return _err(403, "admin token required", "permission_error", "admin_only")
        body = await request.json()
        counts = await service.idem.side_effects(
            str(body["tenant"]), [str(r) for r in body["request_ids"]][:5000]
        )
        return JSONResponse({"counts": counts})

    async def get_kill_switch(request: Request) -> Response:
        if not is_admin(request):
            return _err(403, "admin token required", "permission_error", "admin_only")
        return JSONResponse({"engaged": await kill.engaged(), "env_flag": settings.kill_switch})

    async def put_kill_switch(body: KillSwitchBody, request: Request) -> Response:
        if not is_admin(request):
            return _err(403, "admin token required", "permission_error", "admin_only")
        await kill.set(body.engaged)
        log.warning(json.dumps({"event": "kill_switch_changed", "engaged": body.engaged}))
        return JSONResponse({"engaged": await kill.engaged(), "env_flag": settings.kill_switch})

    if serve_api:
        app.add_api_route("/v1/chat/completions", chat_completions, methods=["POST"])
        app.add_api_route("/v1/requests/{request_id}", get_request, methods=["GET"])
        app.add_api_route("/admin/tenants", create_tenant, methods=["POST"])
        app.add_api_route("/admin/providers", providers, methods=["GET"])
        app.add_api_route("/admin/breaker/events", breaker_events, methods=["GET"])
        app.add_api_route("/admin/costs", costs, methods=["GET"])
        app.add_api_route("/admin/side-effects", side_effects, methods=["POST"])
        app.add_api_route("/admin/kill-switch", get_kill_switch, methods=["GET"])
        app.add_api_route("/admin/kill-switch", put_kill_switch, methods=["PUT"])
    return app


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn

    from .logging_setup import configure_logging as configure_json_logging

    load_secrets()
    configure_json_logging()
    settings = Settings.from_env()
    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    # Cloud Run services inject PORT; local runs and the worker pool set GATEWAY_PORT. Default 8103.
    port = int(os.environ.get("GATEWAY_PORT") or os.environ.get("PORT") or "8103")
    # keep-alive longer than typical client pools: with uvicorn's 5 s default, a client reusing an idle
    # connection can race the server closing it (we saw one client ReadError in the first chaos run).
    # Graceful shutdown stays inside Cloud Run's 10 s SIGTERM -> SIGKILL window.
    uvicorn.run(
        create_app(settings=settings, cfg=get_config()),
        host=host,
        port=port,
        log_level="warning",
        timeout_keep_alive=75,
        timeout_graceful_shutdown=int(settings.shutdown_grace_s),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
