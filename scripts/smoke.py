"""End-to-end smoke suite for a running llmfailover gateway (local, CI compose stack, staging, prod canary).

Standard library only, so it runs on a bare CI runner or deploy job: ``python3 scripts/smoke.py``.

    python scripts/smoke.py --base-url http://127.0.0.1:8103 --json-out results/smoke_local.json

Credentials come from the environment, never from flags (nothing secret lands in shell history or logs):

* ``SMOKE_API_KEY`` + ``SMOKE_TENANT``: a tenant key for the functional checks (deploy jobs read it from
  Secret Manager). Without it, and with an admin token in dev, a throwaway tenant is created instead.
* ``SMOKE_ADMIN_TOKEN``: enables admin checks (side-effect count, kill switch read, rate-limit tenant).
* ``SMOKE_ID_TOKEN``: Cloud Run identity token, sent as ``X-Serverless-Authorization`` so the
  ``Authorization`` header stays free for the tenant key.

``--allow-disruptive`` toggles the GLOBAL kill switch (all instances share it through Redis): use it only on
an isolated stack (CI, local), never against prod. Prints one PASS/FAIL/SKIP/WARN line per check and exits 1
on any FAIL. ``--require-functional`` turns a skipped functional check into a failure (deploy jobs use it).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

FUNCTIONAL = {
    "auth_accepted",
    "input_validation",
    "oversize_rejected",
    "missing_metadata_rejected",
    "chat_completion",
    "idempotent_replay",
}
SEC_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "cache-control": "no-store",
}
TRACE_MARKERS = ("Traceback (most recent call last)", 'File "/', 'File "C:', '.py", line ')
PROMPT = "Classify the sentiment of this review as positive or negative: 'a delightful, well-made film'."


class Skip(Exception):
    pass


class Warn(Exception):
    pass


@dataclass
class Resp:
    status: int
    headers: dict[str, str]
    text: str

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError:
            return None


@dataclass
class Ctx:
    base: str
    timeout: float
    id_token: str | None
    admin: str | None
    metrics_url: str | None
    allow_disruptive: bool
    request_class: str
    max_tokens: int
    key: str | None = None
    tenant: str | None = None
    ready: dict[str, Any] = field(default_factory=dict)
    first: dict[str, Any] = field(default_factory=dict)  # request_id, body, response of the core request

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
        url: str | None = None,
    ) -> Resp:
        h = {"Accept": "application/json", "User-Agent": "llmfailover-smoke/1"}
        if self.id_token:
            h["X-Serverless-Authorization"] = f"Bearer {self.id_token}"
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
        if data is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        req = urllib.request.Request(url or self.base + path, data=data, method=method, headers=h)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310  # nosec B310
                return Resp(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode())
        except urllib.error.HTTPError as e:
            return Resp(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode())

    def auth(self) -> dict[str, str]:
        if not self.key:
            raise Skip("no tenant key (set SMOKE_API_KEY, or SMOKE_ADMIN_TOKEN in dev to create one)")
        return {"Authorization": f"Bearer {self.key}"}

    def chat_body(self, rid: str, content: str = PROMPT, **meta: Any) -> dict[str, Any]:
        md = {
            "tenant": self.tenant,
            "feature": "smoke-test",
            "request_id": rid,
            "request_class": self.request_class,
        }
        md.update(meta)
        return {
            "model": "auto",
            "messages": [{"role": "user", "content": content}],
            "max_tokens": self.max_tokens,
            "metadata": {k: v for k, v in md.items() if v is not None},
        }


def new_rid() -> str:
    return f"smoke-{uuid.uuid4().hex[:20]}"


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def no_trace(r: Resp) -> None:
    for m in TRACE_MARKERS:
        expect(m not in r.text, f"response leaks a stack trace marker {m!r}")


def err_code(r: Resp) -> str | None:
    j = r.json()
    return (j or {}).get("error", {}).get("code") if isinstance(j, dict) else None


# ---------------------------------------------------------------- independent response-schema oracle
def validate_completion(j: Any, rid: str) -> list[str]:
    """OpenAI chat.completion shape plus the gateway block. Written separately from the server's model."""
    errs: list[str] = []

    def need(path: str, ok: bool) -> None:
        if not ok:
            errs.append(path)

    need("body is object", isinstance(j, dict))
    if errs:
        return errs
    need("id: non-empty str", isinstance(j.get("id"), str) and bool(j["id"]))
    need("object == chat.completion", j.get("object") == "chat.completion")
    need("created: int", isinstance(j.get("created"), int))
    need("model: str", isinstance(j.get("model"), str))
    ch = j.get("choices")
    need("choices: non-empty list", isinstance(ch, list) and len(ch) >= 1)
    if isinstance(ch, list) and ch:
        c0 = ch[0] if isinstance(ch[0], dict) else {}
        msg = c0.get("message") if isinstance(c0.get("message"), dict) else {}
        need("choices[0].message.role == assistant", msg.get("role") == "assistant")
        need(
            "choices[0].message.content: non-empty str",
            isinstance(msg.get("content"), str) and bool(msg["content"].strip()),
        )
        need("choices[0].finish_reason: str", isinstance(c0.get("finish_reason"), str))
    u = j.get("usage") if isinstance(j.get("usage"), dict) else {}
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        need(f"usage.{k}: int >= 0", isinstance(u.get(k), int) and u[k] >= 0)
    g = j.get("gateway") if isinstance(j.get("gateway"), dict) else {}
    need("gateway.request_id echoes the request", g.get("request_id") == rid)
    need("gateway.provider: str", isinstance(g.get("provider"), str) and bool(g.get("provider")))
    need("gateway.attempts: list", isinstance(g.get("attempts"), list))
    need("gateway.failovers: int", isinstance(g.get("failovers"), int))
    return errs


# ---------------------------------------------------------------- checks
def c_liveness(c: Ctx) -> str:
    r = c.call("GET", "/healthz")
    expect(r.status == 200, f"/healthz -> {r.status}")
    expect((r.json() or {}).get("status") == "ok", f"/healthz body {r.text[:120]}")
    return "200 ok"


def c_readiness(c: Ctx) -> str:
    r = c.call("GET", "/readyz")
    j = r.json() or {}
    c.ready = j
    expect(r.status == 200 and j.get("status") == "ready", f"/readyz -> {r.status} {r.text[:300]}")
    expect(j.get("redis") is True, "redis dependency not reported reachable")
    sch = j.get("schema") or {}
    expect(sch.get("current", -1) >= sch.get("required", 1), f"keyspace not migrated: {sch}")
    expect(bool(j.get("providers_enabled")), "no provider enabled")
    return f"env={j.get('env')} redis=ok schema={sch.get('current')}/{sch.get('required')} " + (
        f"providers={j.get('providers_enabled')} kill_switch={j.get('kill_switch')}"
    )


def c_failover_capacity(c: Ctx) -> str:
    fo = c.ready.get("failover")
    if not isinstance(fo, dict) or "external_healthy" not in fo:
        raise AssertionError(f"/readyz has no failover capacity block: {fo}")
    if fo.get("warning"):
        raise Warn(fo["warning"])
    return f"healthy external providers: {fo['external_healthy']}"


def c_deploy_guards(c: Ctx) -> str:
    env = c.ready.get("env")
    fake, chaos = c.ready.get("fake_llm"), c.ready.get("chaos_routing")
    expect(fake is not None and chaos is not None, "/readyz does not report fake_llm/chaos_routing")
    if env in ("staging", "prod"):
        expect(fake is False, f"fake LLM mode is ON in {env}")
        expect(chaos is False, f"chaos routing is ON in {env}")
        return f"{env}: fake_llm=false chaos_routing=false"
    return f"{env}: fake_llm={fake} chaos_routing={chaos} (allowed outside staging/prod)"


def c_security_headers(c: Ctx) -> str:
    for path in ("/healthz", "/v1/chat/completions"):
        r = c.call("GET" if path == "/healthz" else "POST", path, body=None if path == "/healthz" else {})
        for k, v in SEC_HEADERS.items():
            expect(r.headers.get(k) == v, f"{path}: header {k}={r.headers.get(k)!r}, want {v!r}")
        expect("content-security-policy" in r.headers, f"{path}: no Content-Security-Policy")
        if c.base.startswith("https://"):
            expect("strict-transport-security" in r.headers, "HTTPS without Strict-Transport-Security")
    return "nosniff, DENY, no-store, CSP" + (", HSTS" if c.base.startswith("https://") else "")


def c_no_stack_traces(c: Ctx) -> str:
    r404 = c.call("GET", f"/no-such-route-{uuid.uuid4().hex[:6]}")
    expect(r404.status == 404, f"unknown route -> {r404.status}")
    no_trace(r404)
    bad = c.call("POST", "/v1/chat/completions", raw=b'{"messages": [', headers=c.auth() if c.key else {})
    expect(400 <= bad.status < 500, f"malformed JSON -> {bad.status}")
    no_trace(bad)
    expect(isinstance(bad.json(), dict), "error body is not JSON")
    return f"404 and malformed-JSON ({bad.status}) bodies are JSON without traces"


def c_auth_rejected(c: Ctx) -> str:
    body = c.chat_body(new_rid())
    r1 = c.call("POST", "/v1/chat/completions", body=body)
    expect(r1.status == 401 and err_code(r1) == "invalid_api_key", f"no key -> {r1.status} {r1.text[:120]}")
    r2 = c.call("POST", "/v1/chat/completions", body=body, headers={"Authorization": "Bearer gwk_not-a-key"})
    expect(r2.status == 401, f"bad key -> {r2.status}")
    r3 = c.call("GET", "/admin/providers")
    expect(r3.status == 403, f"admin endpoint without token -> {r3.status}")
    return "no key 401, bad key 401, admin without token 403"


def c_auth_accepted(c: Ctx) -> str:
    # validated (and rejected) after authentication, so 400 here proves the key was accepted without an
    # LLM call; the real accepted path is chat_completion below.
    r = c.call("POST", "/v1/chat/completions", body={"messages": []}, headers=c.auth())
    expect(r.status == 400, f"authenticated request -> {r.status} (401 means the key was refused)")
    return "key accepted (request reached validation)"


def c_input_validation(c: Ctx) -> str:
    h = c.auth()
    cases = [
        ("empty messages", {"messages": [], "metadata": c.chat_body(new_rid())["metadata"]}, "invalid_body"),
        (
            "bad role",
            {**c.chat_body(new_rid()), "messages": [{"role": "tool", "content": "x"}]},
            "invalid_body",
        ),
        ("temperature 9", {**c.chat_body(new_rid()), "temperature": 9}, "invalid_body"),
        ("unknown class", c.chat_body(new_rid(), request_class="no-such-class"), "bad_request_class"),
        ("stream=true", {**c.chat_body(new_rid()), "stream": True}, "stream_unsupported"),
    ]
    for name, body, code in cases:
        r = c.call("POST", "/v1/chat/completions", body=body, headers=h)
        expect(r.status in (400, 422) and err_code(r) == code, f"{name} -> {r.status} {r.text[:160]}")
        no_trace(r)
    return f"{len(cases)} invalid inputs rejected with 400 and a typed error code"


def c_oversize(c: Ctx) -> str:
    h = c.auth()
    big = c.call("POST", "/v1/chat/completions", body=c.chat_body(new_rid(), content="x" * 70_000), headers=h)
    expect(big.status == 413 and err_code(big) == "too_large", f"70 KB body -> {big.status}")
    long = c.call("POST", "/v1/chat/completions", body=c.chat_body(new_rid(), content="y" * 9000), headers=h)
    expect(long.status == 400 and err_code(long) == "prompt_too_large", f"9000-char prompt -> {long.status}")
    return "70 KB body 413, 9000-char prompt 400 prompt_too_large"


def c_missing_metadata(c: Ctx) -> str:
    body = c.chat_body(new_rid())
    body.pop("metadata")
    r = c.call("POST", "/v1/chat/completions", body=body, headers=c.auth())
    expect(r.status == 400 and err_code(r) == "missing_metadata", f"no metadata -> {r.status} {r.text[:160]}")
    spoof = c.chat_body(new_rid(), tenant="someone-else")
    r2 = c.call("POST", "/v1/chat/completions", body=spoof, headers=c.auth())
    expect(r2.status == 403 and err_code(r2) == "tenant_mismatch", f"spoofed tenant -> {r2.status}")
    return "missing metadata 400; spoofed tenant 403"


def c_chat_completion(c: Ctx) -> str:
    rid = new_rid()
    body = c.chat_body(rid)
    t0 = time.perf_counter()
    r = c.call("POST", "/v1/chat/completions", body=body, headers=c.auth())
    ms = (time.perf_counter() - t0) * 1000
    expect(r.status == 200, f"chat -> {r.status} {r.text[:300]}")
    j = r.json()
    errs = validate_completion(j, rid)
    expect(not errs, f"schema violations: {errs}")
    g = j["gateway"]
    expect(r.headers.get("x-gateway-provider") == g["provider"], "X-Gateway-Provider != gateway.provider")
    expect(r.headers.get("x-idempotent-replay") == "false", "first request marked as a replay")
    c.first = {"rid": rid, "body": body, "resp": j}
    content = j["choices"][0]["message"]["content"].strip().replace("\n", " ")
    return (
        f"200 in {ms:.0f} ms via {g['provider']} (failovers={g['failovers']}, hedged={g.get('hedged')}): "
        f"{content[:60]!r}"
    )


def c_idempotent_replay(c: Ctx) -> str:
    if not c.first:
        raise Skip("no successful core request to replay")
    rid, body, first = c.first["rid"], c.first["body"], c.first["resp"]
    r = c.call("POST", "/v1/chat/completions", body=body, headers=c.auth())
    expect(r.status == 200, f"replay -> {r.status}")
    expect(r.headers.get("x-idempotent-replay") == "true", "replay lacks X-Idempotent-Replay: true")
    j = r.json()
    expect(j["id"] == first["id"], "replay returned a different completion id")
    expect(j["choices"] == first["choices"], "replay returned different content")
    changed = {**body, "messages": [{"role": "user", "content": PROMPT + " (changed)"}]}
    r2 = c.call("POST", "/v1/chat/completions", body=changed, headers=c.auth())
    expect(r2.status == 422 and err_code(r2) == "idempotency_key_mismatch", f"reused id -> {r2.status}")
    detail = "replayed with X-Idempotent-Replay: true; reused id with a new body 422"
    if c.admin:
        sx = c.call(
            "POST",
            "/admin/side-effects",
            body={"tenant": c.tenant, "request_ids": [rid]},
            headers={"X-Admin-Token": c.admin},
        )
        expect(sx.status == 200, f"/admin/side-effects -> {sx.status}")
        n = (sx.json() or {}).get("counts", {}).get(rid)
        expect(n == 1, f"commits for {rid} = {n}, want exactly 1 (no second commit)")
        detail += "; commits=1"
    return detail


def _metrics_text(c: Ctx) -> Resp:
    url = c.metrics_url or (c.base + "/metrics")
    return c.call("GET", "", url=url, headers={"Accept": "text/plain"})


def c_metrics_breaker(c: Ctx) -> str:
    r = _metrics_text(c)
    env = c.ready.get("env")
    if r.status == 404 and env in ("staging", "prod") and not c.metrics_url:
        # In deployed envs /metrics is served on 127.0.0.1:METRICS_PORT for the GMP sidecar only.
        fo = c.ready.get("failover") or {}
        expect("external_healthy" in fo, "breaker-derived state missing from /readyz")
        return "/metrics is not on the public port (sidecar-only); breaker state seen via /readyz"
    expect(r.status == 200, f"/metrics -> {r.status}")
    states = re.findall(r'^gateway_circuit_state\{provider="([^"]+)"\} ([0-9.]+)$', r.text, re.M)
    expect(bool(states), "no gateway_circuit_state series")
    expect(all(v in ("0.0", "1.0", "2.0") for _, v in states), f"bad breaker values {states}")
    names = {"0.0": "closed", "1.0": "half_open", "2.0": "open"}
    shown = ", ".join(f"{p}={names[v]}" for p, v in states)
    for metric in ("gateway_completions_total", "gateway_external_providers_healthy"):
        expect(metric in r.text, f"{metric} not exported")
    return f"gateway_circuit_state for {len(states)} provider(s) [{shown}]; quality + capacity gauges present"


def c_metrics_not_public(c: Ctx) -> str:
    env = c.ready.get("env")
    if env not in ("staging", "prod"):
        raise Skip(f"env={env}: /metrics is intentionally on the API port outside staging/prod")
    r = c.call("GET", "/metrics")
    expect(r.status == 404, f"/metrics is publicly reachable in {env} ({r.status})")
    return "404 on the public port"


def c_rate_limit(c: Ctx) -> str:
    key = os.environ.get("SMOKE_RL_API_KEY")
    tenant = os.environ.get("SMOKE_RL_TENANT")
    if not key:
        if not c.admin:
            raise Skip("needs SMOKE_ADMIN_TOKEN (dev) or SMOKE_RL_API_KEY/SMOKE_RL_TENANT (rpm <= 2)")
        tenant = f"smoke-rl-{uuid.uuid4().hex[:8]}"
        r = c.call(
            "POST", "/admin/tenants", body={"tenant": tenant, "rpm": 2}, headers={"X-Admin-Token": c.admin}
        )
        if r.status == 403:
            raise Skip("tenant creation is disabled here (Secret Manager tenants); set SMOKE_RL_API_KEY")
        expect(r.status == 200, f"create rate-limit tenant -> {r.status}")
        key = r.json()["api_key"]
    h = {"Authorization": f"Bearer {key}"}
    rid = new_rid()
    body = c.chat_body(rid)
    body["metadata"]["tenant"] = tenant
    statuses: list[int] = []
    r = Resp(0, {}, "")
    # one real call, then replays of the same request_id (no LLM call) until the 60 s window is full
    for _ in range(4):
        r = c.call("POST", "/v1/chat/completions", body=body, headers=h)
        statuses.append(r.status)
        if r.status == 429:
            break
    expect(r.status == 429, f"no 429 after {len(statuses)} requests: {statuses}")
    expect(err_code(r) == "tenant_rate_limited", f"429 code {err_code(r)}")
    ra = r.headers.get("retry-after", "")
    expect(ra.isdigit() and int(ra) >= 1, f"Retry-After={ra!r}")
    return f"statuses {statuses}; Retry-After: {ra}"


def c_kill_switch(c: Ctx) -> str:
    if not c.admin:
        raise Skip("needs SMOKE_ADMIN_TOKEN")
    a = {"X-Admin-Token": c.admin}
    g = c.call("GET", "/admin/kill-switch", headers=a)
    expect(g.status == 200, f"GET /admin/kill-switch -> {g.status}")
    expect((g.json() or {}).get("engaged") is False, "kill switch is ENGAGED on this deployment")
    if not c.allow_disruptive:
        return "read-only: not engaged (pass --allow-disruptive on an isolated stack to toggle it)"
    on = c.call("PUT", "/admin/kill-switch", body={"engaged": True}, headers=a)
    expect(on.status == 200 and (on.json() or {}).get("engaged") is True, f"engage -> {on.status}")
    try:
        r = c.call("POST", "/v1/chat/completions", body=c.chat_body(new_rid()), headers=c.auth())
        expect(
            r.status == 503 and err_code(r) == "kill_switch", f"engaged: chat -> {r.status} {r.text[:120]}"
        )
        expect(r.headers.get("retry-after", "").isdigit(), "kill-switch 503 without Retry-After")
        rz = c.call("GET", "/readyz").json() or {}
        expect(rz.get("kill_switch") is True, "/readyz does not show the kill switch")
    finally:
        off = c.call("PUT", "/admin/kill-switch", body={"engaged": False}, headers=a)
    expect(off.status == 200 and (off.json() or {}).get("engaged") is False, "kill switch did not release")
    return "engaged -> chat 503 kill_switch + Retry-After, /readyz shows it; released"


# ---------------------------------------------------------------- runner
CHECKS: list[tuple[str, Callable[[Ctx], str]]] = [
    ("liveness", c_liveness),
    ("readiness", c_readiness),
    ("failover_capacity", c_failover_capacity),
    ("deploy_guards", c_deploy_guards),
    ("security_headers", c_security_headers),
    ("no_stack_traces", c_no_stack_traces),
    ("auth_rejected", c_auth_rejected),
    ("auth_accepted", c_auth_accepted),
    ("input_validation", c_input_validation),
    ("oversize_rejected", c_oversize),
    ("missing_metadata_rejected", c_missing_metadata),
    ("chat_completion", c_chat_completion),
    ("idempotent_replay", c_idempotent_replay),
    ("metrics_breaker_gauges", c_metrics_breaker),
    ("metrics_not_public", c_metrics_not_public),
    ("rate_limit", c_rate_limit),
    ("kill_switch", c_kill_switch),
]


def wait_ready(c: Ctx, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            if c.call("GET", "/readyz").status == 200:
                return
        except OSError:
            pass
        if time.monotonic() > deadline:
            return  # the readiness check reports the failure
        time.sleep(2)


def provision_tenant(c: Ctx) -> str:
    c.key = os.environ.get("SMOKE_API_KEY") or None
    if c.key:
        c.tenant = os.environ.get("SMOKE_TENANT", "smoke")
        return f"tenant key from SMOKE_API_KEY (tenant {c.tenant})"
    if c.admin:
        tenant = f"smoke-{uuid.uuid4().hex[:8]}"
        r = c.call(
            "POST", "/admin/tenants", body={"tenant": tenant, "rpm": 600}, headers={"X-Admin-Token": c.admin}
        )
        if r.status == 200:
            c.key, c.tenant = r.json()["api_key"], tenant
            return f"created throwaway tenant {tenant}"
        return f"could not create a tenant ({r.status}); functional checks will skip"
    return "no tenant key; functional checks will skip"


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="End-to-end smoke suite for a running llmfailover gateway")
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--metrics-url", help="scrape URL if /metrics is not on the API port")
    ap.add_argument("--json-out", help="write the JSON summary here")
    ap.add_argument("--timeout", type=float, default=30.0, help="per-request timeout (s)")
    ap.add_argument("--wait-ready", type=float, default=0.0, help="poll /readyz up to N s first (cold start)")
    ap.add_argument("--request-class", default="generate")
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--allow-disruptive", action="store_true", help="toggle the global kill switch")
    ap.add_argument("--require-functional", action="store_true", help="a skipped functional check fails")
    ap.add_argument("--expect-env", help="fail unless /readyz reports this env")
    a = ap.parse_args(argv)
    c = Ctx(
        base=a.base_url.rstrip("/"),
        timeout=a.timeout,
        id_token=os.environ.get("SMOKE_ID_TOKEN") or None,
        admin=os.environ.get("SMOKE_ADMIN_TOKEN") or None,
        metrics_url=a.metrics_url,
        allow_disruptive=a.allow_disruptive,
        request_class=a.request_class,
        max_tokens=a.max_tokens,
    )
    t_start = time.time()
    if a.wait_ready:
        wait_ready(c, a.wait_ready)
    print(f"smoke: {c.base}")
    results: list[dict[str, Any]] = []
    setup: str | None = None
    for name, fn in CHECKS:
        if name == "failover_capacity":  # after readiness: get a tenant key for the functional checks
            setup = provision_tenant(c)
            print(f"      setup: {setup}")
        t0 = time.perf_counter()
        try:
            detail, status = fn(c), "PASS"
        except Skip as e:
            detail, status = str(e), "SKIP"
            if a.require_functional and name in FUNCTIONAL:
                status, detail = "FAIL", f"skipped but --require-functional: {e}"
        except Warn as e:
            detail, status = str(e), "WARN"
        except AssertionError as e:
            detail, status = str(e), "FAIL"
        except Exception as e:
            detail, status = f"{type(e).__name__}: {e}", "FAIL"
        ms = round((time.perf_counter() - t0) * 1000, 1)
        results.append({"name": name, "status": status, "detail": detail, "ms": ms})
        print(f"{status:<5} {name:<26} {detail}")
        if name == "readiness" and a.expect_env and c.ready.get("env") != a.expect_env:
            results.append(
                {"name": "expected_env", "status": "FAIL", "detail": f"env={c.ready.get('env')}", "ms": 0}
            )
            print(f"FAIL  expected_env               env={c.ready.get('env')}, want {a.expect_env}")
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("PASS", "FAIL", "SKIP", "WARN")}
    summary = {
        "base_url": c.base,
        "env": c.ready.get("env"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_start)),
        "duration_s": round(time.time() - t_start, 2),
        "total": len(results),
        "passed": counts["PASS"],
        "failed": counts["FAIL"],
        "skipped": counts["SKIP"],
        "warned": counts["WARN"],
        "ok": counts["FAIL"] == 0,
        "setup": setup,
        "allow_disruptive": c.allow_disruptive,
        "request_class": c.request_class,
        "checks": results,
    }
    print(
        f"smoke {'PASSED' if summary['ok'] else 'FAILED'}: {counts['PASS']} passed, {counts['FAIL']} failed, "
        f"{counts['SKIP']} skipped, {counts['WARN']} warned of {len(results)} in {summary['duration_s']} s"
    )
    if a.json_out:
        os.makedirs(os.path.dirname(os.path.abspath(a.json_out)), exist_ok=True)
        with open(a.json_out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(summary, f, indent=2)
            f.write("\n")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(run())
