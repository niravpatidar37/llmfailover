"""Chaos proxy on 127.0.0.1:8113: sits between the gateway and providers and injects faults on demand.

* Refuses to start unless CHAOS_ENABLED=1, and the gateway only routes through it when CHAOS_ENABLED=1.
* Refuses to start when ENV=prod, whatever CHAOS_ENABLED says; the gateway itself also refuses to start
  with ENV=prod and CHAOS_ENABLED=1, and never routes to the proxy in prod (``ProviderConfig.api_base``).
* ``/{provider}/{path}`` forwards to a FIXED upstream per provider (no caller-controlled URLs, so no SSRF).
* ``/admin/faults/{provider}`` (PUT/DELETE) and ``/admin/faults`` (GET) require ``X-Chaos-Token``.
* A fault is ``{error_rate, latency_ms, status_code, duration_s}``: each forwarded request first sleeps
  ``latency_ms`` and then, with probability ``error_rate``, gets ``status_code``
  instead of the upstream answer.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, generate_latest
from pydantic import BaseModel, Field

from .config import chaos_requested, runtime_env

HOP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
    "accept-encoding",
}


def default_upstreams() -> dict[str, str]:
    return {
        "ollama": os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"),
        "ollama_cpu": os.environ.get("OLLAMA_CPU_URL", "http://127.0.0.1:11435"),
        "groq": "https://api.groq.com/openai/v1",
        "hf": "https://router.huggingface.co/v1",
    }


class Fault(BaseModel):
    error_rate: float = Field(default=1.0, ge=0, le=1)
    latency_ms: int = Field(default=0, ge=0, le=120_000)
    status_code: int = Field(default=503, ge=400, le=599)
    duration_s: float | None = Field(default=None, gt=0, le=3600)


class ChaosDisabled(RuntimeError):
    pass


def create_chaos_app(
    upstreams: dict[str, str] | None = None,
    admin_token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    if runtime_env() == "prod":
        raise ChaosDisabled("chaos proxy never runs when ENV=prod")
    if not chaos_requested():
        raise ChaosDisabled("chaos proxy is disabled; set CHAOS_ENABLED=1 to start it")
    token = admin_token if admin_token is not None else os.environ.get("CHAOS_ADMIN_TOKEN", "")
    if len(token) < 16:
        raise ChaosDisabled("CHAOS_ADMIN_TOKEN must be set (>= 16 chars)")
    ups = upstreams or default_upstreams()
    faults: dict[str, tuple[Fault, float | None]] = {}
    reg = CollectorRegistry()
    injected = Counter("chaos_injected_errors_total", "Errors injected", ["provider"], registry=reg)
    forwarded = Counter("chaos_forwarded_total", "Requests forwarded upstream", ["provider"], registry=reg)
    active = Gauge("chaos_fault_error_rate", "Active injected error rate", ["provider"], registry=reg)
    lat_g = Gauge("chaos_fault_latency_ms", "Active injected latency", ["provider"], registry=reg)
    for p in ups:
        active.labels(p).set(0)
        lat_g.labels(p).set(0)
    client_holder: dict[str, httpx.AsyncClient] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client_holder["c"] = httpx.AsyncClient(timeout=httpx.Timeout(120.0), transport=transport)
        yield
        await client_holder["c"].aclose()

    app = FastAPI(title="chaos-proxy", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorized(request: Request) -> bool:
        return hmac.compare_digest(request.headers.get("x-chaos-token", "").encode(), token.encode())

    def current(provider: str) -> Fault | None:
        entry = faults.get(provider)
        if entry is None:
            return None
        fault, until = entry
        if until is not None and time.time() > until:
            faults.pop(provider, None)
            active.labels(provider).set(0)
            lat_g.labels(provider).set(0)
            return None
        return fault

    @app.get("/admin/faults")
    async def list_faults(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "chaos admin token required"}, status_code=403)
        return JSONResponse({p: f.model_dump() for p in list(faults) if (f := current(p)) is not None})

    @app.put("/admin/faults/{provider}")
    async def set_fault(provider: str, fault: Fault, request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "chaos admin token required"}, status_code=403)
        if provider not in ups:
            return JSONResponse({"error": "unknown provider"}, status_code=404)
        faults[provider] = (fault, time.time() + fault.duration_s if fault.duration_s else None)
        active.labels(provider).set(fault.error_rate)
        lat_g.labels(provider).set(fault.latency_ms)
        return JSONResponse({"provider": provider, "fault": fault.model_dump(), "ts": time.time()})

    @app.delete("/admin/faults/{provider}")
    async def clear_fault(provider: str, request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "chaos admin token required"}, status_code=403)
        faults.pop(provider, None)
        if provider in ups:
            active.labels(provider).set(0)
            lat_g.labels(provider).set(0)
        return JSONResponse({"provider": provider, "cleared": True, "ts": time.time()})

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(reg), media_type=CONTENT_TYPE_LATEST)

    @app.api_route("/{provider}/{path:path}", methods=["GET", "POST"])
    async def proxy(provider: str, path: str, request: Request) -> Response:
        base = ups.get(provider)
        if base is None:
            return JSONResponse({"error": "unknown provider"}, status_code=404)
        if ".." in path or "://" in path:
            return JSONResponse({"error": "bad path"}, status_code=400)
        fault = current(provider)
        if fault is not None:
            if fault.latency_ms:
                await asyncio.sleep(fault.latency_ms / 1000)
            if random.random() < fault.error_rate:  # nosec B311 - fault sampling, not security
                injected.labels(provider).inc()
                return JSONResponse(
                    {"error": {"message": "chaos: injected failure", "type": "server_error"}},
                    status_code=fault.status_code,
                )
        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
        url = f"{base.rstrip('/')}/{path}"
        body = await request.body()
        resp = await client_holder["c"].request(
            request.method, url, content=body, headers=headers, params=dict(request.query_params)
        )
        forwarded.labels(provider).inc()
        out_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in HOP_HEADERS and k.lower() != "content-encoding"
        }
        return Response(resp.content, status_code=resp.status_code, headers=out_headers)

    return app


def main() -> None:  # pragma: no cover
    import uvicorn
    from dotenv import load_dotenv

    from .config import REPO_ROOT

    load_dotenv(REPO_ROOT / ".env", override=False)  # chaos tokens only; provider secrets not needed here
    app = create_chaos_app()
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("CHAOS_PORT", "8113")), log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    main()
