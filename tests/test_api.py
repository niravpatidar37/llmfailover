from __future__ import annotations

import asyncio
import logging

import fakeredis
import httpx
import pytest

from gateway.errors import ErrorType
from gateway.store import hash_key

from .conftest import ADMIN, FakeCaller, chat_body, fail, new_tenant, ok


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------- metadata, auth, tenancy
async def test_happy_path_openai_shape(client: httpx.AsyncClient) -> None:
    key = await new_tenant(client)
    r = await client.post("/v1/chat/completions", json=chat_body(), headers=auth(key))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["object"] == "chat.completion"
    assert j["choices"][0]["message"]["role"] == "assistant"
    assert j["usage"]["total_tokens"] == 23
    assert j["gateway"]["provider"] == "a"
    assert r.headers["X-Gateway-Provider"] == "a"


@pytest.mark.parametrize("drop", ["tenant", "feature", "request_id"])
async def test_missing_metadata_rejected(client: httpx.AsyncClient, caller: FakeCaller, drop: str) -> None:
    key = await new_tenant(client)
    body = chat_body()
    del body["metadata"][drop]
    r = await client.post("/v1/chat/completions", json=body, headers=auth(key))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "missing_metadata"
    assert drop in r.json()["error"]["message"]
    assert caller.calls == []  # never reaches a provider


async def test_metadata_accepted_from_headers(client: httpx.AsyncClient) -> None:
    key = await new_tenant(client)
    body = chat_body()
    body.pop("metadata")
    hdrs = {**auth(key), "X-Tenant-Id": "acme", "X-Feature": "f1", "X-Request-Id": "hdr-req-0001"}
    r = await client.post("/v1/chat/completions", json=body, headers=hdrs)
    assert r.status_code == 200, r.text


async def test_no_key_or_bad_key_is_401(client: httpx.AsyncClient, caller: FakeCaller) -> None:
    await new_tenant(client)
    assert (await client.post("/v1/chat/completions", json=chat_body())).status_code == 401
    r = await client.post("/v1/chat/completions", json=chat_body(), headers=auth("gwk_not-a-real-key"))
    assert r.status_code == 401
    assert caller.calls == []


async def test_tenant_spoofing_is_403(client: httpx.AsyncClient, caller: FakeCaller) -> None:
    key_acme = await new_tenant(client, "acme")
    await new_tenant(client, "globex")
    r = await client.post("/v1/chat/completions", json=chat_body(tenant="globex"), headers=auth(key_acme))
    assert r.status_code == 403
    assert caller.calls == []


async def test_api_keys_stored_only_as_hashes(
    client: httpx.AsyncClient, redis: fakeredis.FakeAsyncRedis
) -> None:
    key = await new_tenant(client)
    dump = b""
    for k in await redis.keys("*"):
        dump += k
        t = await redis.type(k)
        if t == b"string":
            dump += await redis.get(k) or b""
        elif t == b"hash":
            for f, v in (await redis.hgetall(k)).items():
                dump += f + v
    assert key.encode() not in dump
    assert hash_key(key).encode() in dump


async def test_admin_endpoints_require_token(client: httpx.AsyncClient) -> None:
    for method, path in [
        ("post", "/admin/tenants"),
        ("get", "/admin/providers"),
        ("get", "/admin/costs"),
        ("get", "/admin/breaker/events"),
    ]:
        r = (
            await getattr(client, method)(
                path, json={"tenant": "x"} if method == "post" else None, headers={"X-Admin-Token": "wrong"}
            )
            if method == "post"
            else await client.get(path, headers={"X-Admin-Token": "wrong"})
        )
        assert r.status_code == 403, path


async def test_tenant_rate_limit(client: httpx.AsyncClient, caller: FakeCaller) -> None:
    key = await new_tenant(client, rpm=3)
    codes = []
    for i in range(5):
        r = await client.post(
            "/v1/chat/completions", json=chat_body(rid=f"rl-req-{i:04d}"), headers=auth(key)
        )
        codes.append(r.status_code)
    assert codes == [200, 200, 200, 429, 429]
    assert len(caller.calls) == 3
    # another tenant is unaffected
    key2 = await new_tenant(client, "globex", rpm=3)
    r = await client.post("/v1/chat/completions", json=chat_body(tenant="globex"), headers=auth(key2))
    assert r.status_code == 200


async def test_unknown_class_and_stream_rejected(client: httpx.AsyncClient) -> None:
    key = await new_tenant(client)
    r = await client.post("/v1/chat/completions", json=chat_body(request_class="nope"), headers=auth(key))
    assert r.status_code == 400
    body = chat_body(rid="stream-req-01")
    body["stream"] = True
    r = await client.post("/v1/chat/completions", json=body, headers=auth(key))
    assert r.status_code == 400


# ---------------------------------------------------------------- idempotency
async def test_duplicate_request_id_returns_cached_result(
    client: httpx.AsyncClient, caller: FakeCaller
) -> None:
    key = await new_tenant(client)
    r1 = await client.post("/v1/chat/completions", json=chat_body(rid="dup-req-0001"), headers=auth(key))
    r2 = await client.post("/v1/chat/completions", json=chat_body(rid="dup-req-0001"), headers=auth(key))
    assert r1.status_code == r2.status_code == 200
    assert r2.headers["X-Idempotent-Replay"] == "true"
    assert r1.json()["choices"] == r2.json()["choices"]
    assert caller.calls == ["a"]  # executed exactly once


async def test_concurrent_duplicates_execute_once(client: httpx.AsyncClient, caller: FakeCaller) -> None:
    caller.behaviors["a"] = ok(delay=0.3)
    key = await new_tenant(client)
    rs = await asyncio.gather(
        *[
            client.post("/v1/chat/completions", json=chat_body(rid="conc-req-0001"), headers=auth(key))
            for _ in range(5)
        ]
    )
    assert all(r.status_code == 200 for r in rs)
    assert caller.calls == ["a"]
    r = await client.post(
        "/admin/side-effects",
        json={"tenant": "acme", "request_ids": ["conc-req-0001"]},
        headers={"X-Admin-Token": ADMIN},
    )
    assert r.json()["counts"]["conc-req-0001"] == 1


async def test_request_id_reuse_with_different_body_is_rejected(
    client: httpx.AsyncClient, caller: FakeCaller
) -> None:
    key = await new_tenant(client)
    await client.post("/v1/chat/completions", json=chat_body(rid="mis-req-0001"), headers=auth(key))
    r = await client.post(
        "/v1/chat/completions", json=chat_body(rid="mis-req-0001", content="other"), headers=auth(key)
    )
    assert r.status_code == 422
    assert caller.calls == ["a"]


async def test_idempotency_is_scoped_per_tenant(client: httpx.AsyncClient, caller: FakeCaller) -> None:
    k1 = await new_tenant(client, "acme")
    k2 = await new_tenant(client, "globex")
    await client.post("/v1/chat/completions", json=chat_body(rid="shared-rid-01"), headers=auth(k1))
    r = await client.post(
        "/v1/chat/completions", json=chat_body(rid="shared-rid-01", tenant="globex"), headers=auth(k2)
    )
    assert r.headers["X-Idempotent-Replay"] == "false"  # globex cannot read acme's cached result
    assert len(caller.calls) == 2


async def test_failed_interactive_request_can_be_retried(
    client: httpx.AsyncClient, caller: FakeCaller
) -> None:
    for p in ("a", "b", "c"):
        caller.behaviors[p] = fail(ErrorType.SERVER_ERROR)
    key = await new_tenant(client)
    r = await client.post("/v1/chat/completions", json=chat_body(rid="retry-req-01"), headers=auth(key))
    assert r.status_code == 503
    for p in ("a", "b", "c"):
        caller.behaviors[p] = ok()
    r = await client.post("/v1/chat/completions", json=chat_body(rid="retry-req-01"), headers=auth(key))
    assert r.status_code == 200 and r.headers["X-Idempotent-Replay"] == "false"


# ---------------------------------------------------------------- deferrable queue
async def test_deferrable_is_queued_and_completed_when_providers_recover(
    client: httpx.AsyncClient, caller: FakeCaller
) -> None:
    caller.behaviors["a"] = fail(ErrorType.RATE_LIMIT)
    caller.behaviors["b"] = fail(ErrorType.TIMEOUT)
    key = await new_tenant(client)
    r = await client.post(
        "/v1/chat/completions", json=chat_body(rid="batch-req-01", request_class="batch"), headers=auth(key)
    )
    assert r.status_code == 202 and r.json()["status"] == "queued"
    poll = await client.get("/v1/requests/batch-req-01", headers=auth(key))
    assert poll.status_code == 202
    svc = client.app.state.service  # type: ignore[attr-defined]
    caller.behaviors["b"] = ok()
    for _ in range(100):
        await svc.worker_tick()
        await asyncio.sleep(0.02)
        poll = await client.get("/v1/requests/batch-req-01", headers=auth(key))
        if poll.status_code == 200:
            break
    assert poll.status_code == 200 and poll.json()["gateway"]["provider"] == "b"
    # duplicate submission after completion is a replay, not a re-execution
    n = len(caller.calls)
    r = await client.post(
        "/v1/chat/completions", json=chat_body(rid="batch-req-01", request_class="batch"), headers=auth(key)
    )
    assert r.status_code == 200 and r.headers["X-Idempotent-Replay"] == "true"
    assert len(caller.calls) == n


async def test_interactive_is_not_queued(client: httpx.AsyncClient, caller: FakeCaller) -> None:
    for p in ("a", "b", "c"):
        caller.behaviors[p] = fail()
    key = await new_tenant(client)
    r = await client.post("/v1/chat/completions", json=chat_body(rid="int-req-001"), headers=auth(key))
    assert r.status_code == 503
    assert await client.app.state.service.queue_depth() == 0  # type: ignore[attr-defined]


async def test_backoff_is_exponential_with_jitter(client: httpx.AsyncClient) -> None:
    svc = client.app.state.service  # type: ignore[attr-defined]
    svc.cfg.queue.base_delay_s, svc.cfg.queue.max_delay_s = 1.0, 30.0
    samples = {a: [svc.backoff_s(a) for _ in range(200)] for a in range(6)}
    for a, xs in samples.items():
        d = min(30.0, 2**a)
        assert all(d / 2 <= x <= d for x in xs)
        assert len({round(x, 6) for x in xs}) > 1  # jittered
    assert max(samples[5]) <= 30.0


async def test_worker_skips_job_already_committed(
    client: httpx.AsyncClient, caller: FakeCaller, redis: fakeredis.FakeAsyncRedis
) -> None:
    """A crashed worker's lease expires and the job is re-run: it must not execute twice."""
    caller.behaviors["a"] = fail()
    caller.behaviors["b"] = fail()
    key = await new_tenant(client)
    await client.post(
        "/v1/chat/completions", json=chat_body(rid="lease-req-01", request_class="batch"), headers=auth(key)
    )
    caller.behaviors["a"] = ok()
    svc = client.app.state.service  # type: ignore[attr-defined]
    for _ in range(100):
        await svc.worker_tick()
        await asyncio.sleep(0.02)
        if (await client.get("/v1/requests/lease-req-01", headers=auth(key))).status_code == 200:
            break
    # simulate the job reappearing (lease expiry after a crash between commit and cleanup)
    job = {
        "ctx": {
            "tenant": "acme",
            "feature": "support-triage",
            "request_id": "lease-req-01",
            "request_class": "batch",
            "feature_label": "support-triage",
        },
        "messages": [{"role": "user", "content": "x"}],
        "max_tokens": 8,
        "temperature": None,
        "fp": "x",
        "attempt": 1,
    }
    import json as _json

    await redis.set("gw:job:acme:lease-req-01", _json.dumps(job))
    await redis.zadd("gw:queue", {"acme:lease-req-01": 0})
    n = caller.calls.count("a")
    await svc.worker_tick()
    await asyncio.sleep(0.05)
    assert caller.calls.count("a") == n
    r = await client.post(
        "/admin/side-effects",
        json={"tenant": "acme", "request_ids": ["lease-req-01"]},
        headers={"X-Admin-Token": ADMIN},
    )
    assert r.json()["counts"]["lease-req-01"] == 1


# ---------------------------------------------------------------- cost attribution + logs
async def test_cost_attributed_per_tenant_and_feature(client: httpx.AsyncClient, caller: FakeCaller) -> None:
    caller.behaviors["a"] = fail()
    caller.behaviors["b"] = fail()  # forces paid provider c (1$/M in, 2$/M out; 20 in, 3 out)
    k1 = await new_tenant(client, "acme")
    k2 = await new_tenant(client, "globex")
    await client.post(
        "/v1/chat/completions", json=chat_body(rid="cost-req-001", feature="search"), headers=auth(k1)
    )
    await client.post(
        "/v1/chat/completions", json=chat_body(rid="cost-req-002", feature="search"), headers=auth(k1)
    )
    await client.post(
        "/v1/chat/completions",
        json=chat_body(rid="cost-req-003", tenant="globex", feature="chat"),
        headers=auth(k2),
    )
    r = await client.get("/admin/costs", headers={"X-Admin-Token": ADMIN})
    costs = r.json()["usd_by_tenant_feature"]
    assert costs["acme"]["search"] == pytest.approx(2 * 26e-6)
    assert costs["globex"]["chat"] == pytest.approx(26e-6)
    m = (await client.get("/metrics")).text
    assert 'gateway_cost_usd_total{feature="search",provider="c",tenant="acme"}' in m


async def test_feature_label_cardinality_is_bounded(client: httpx.AsyncClient) -> None:
    client.app.state.service.cfg.limits.max_features_per_tenant = 2  # type: ignore[attr-defined]
    key = await new_tenant(client)
    for i in range(4):
        await client.post(
            "/v1/chat/completions",
            json=chat_body(rid=f"feat-req-{i:03d}", feature=f"f{i}"),
            headers=auth(key),
        )
    m = (await client.get("/metrics")).text
    assert 'feature="other"' in m and 'feature="f3"' not in m


async def test_request_log_redacts_pii(client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture) -> None:
    key = await new_tenant(client)
    pii = (
        "Contact jane.doe@example.com or +1 (415) 555-0132, card 4111 1111 1111 1111, "
        "ssn 123-45-6789, key sk-abcdefghijklmnop1234"
    )
    lg = logging.getLogger("gateway.requests")
    lg.propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="gateway.requests"):
            r = await client.post(
                "/v1/chat/completions", json=chat_body(rid="pii-req-0001", content=pii), headers=auth(key)
            )
    finally:
        lg.propagate = False
    assert r.status_code == 200
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "pii-req-0001" in text  # the log line was emitted
    for secret in (
        "jane.doe@example.com",
        "555-0132",
        "4111 1111 1111 1111",
        "123-45-6789",
        "sk-abcdefghijklmnop1234",
    ):
        assert secret not in text
    assert "[REDACTED_EMAIL]" in text


async def test_metrics_endpoint_exposes_provider_series(
    client: httpx.AsyncClient, caller: FakeCaller
) -> None:
    caller.behaviors["a"] = fail(ErrorType.RATE_LIMIT)
    key = await new_tenant(client)
    await client.post("/v1/chat/completions", json=chat_body(rid="met-req-0001"), headers=auth(key))
    await client.app.state.refresh_gauges()  # type: ignore[attr-defined]
    m = (await client.get("/metrics")).text
    assert 'gateway_provider_errors_total{error_type="rate_limit",provider="a"} 1.0' in m
    assert (
        'gateway_failovers_total{from_provider="a",reason="error",'
        'request_class="generate",to_provider="b"} 1.0' in m
    )
    assert 'gateway_circuit_state{provider="a"}' in m
    assert "gateway_health_success_rate" in m
