from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import litellm
import pytest

from gateway.chaos import ChaosDisabled, create_chaos_app
from gateway.errors import ErrorType, classify_exception
from gateway.redact import redact
from gateway.spend import BudgetExceeded, SpendLedger

# ---------------------------------------------------------------- error taxonomy (real LiteLLM exceptions)


def _req() -> httpx.Request:
    return httpx.Request("POST", "https://example.invalid/v1/chat/completions")


def test_taxonomy_from_litellm_exceptions() -> None:
    resp429 = httpx.Response(429, request=_req())
    cases = [
        (litellm.RateLimitError("slow down", "groq", "m", response=resp429), ErrorType.RATE_LIMIT),
        (litellm.Timeout("timed out", "m", "groq"), ErrorType.TIMEOUT),
        (litellm.AuthenticationError("bad key", "groq", "m"), ErrorType.AUTH),
        (litellm.ContentPolicyViolationError("blocked", "m", "groq"), ErrorType.CONTENT_FILTER),
        (litellm.ServiceUnavailableError("down", "groq", "m"), ErrorType.SERVER_ERROR),
        (litellm.InternalServerError("boom", "groq", "m"), ErrorType.SERVER_ERROR),
        (litellm.APIConnectionError("refused", "ollama", "m"), ErrorType.CONNECTION),
        (TimeoutError(), ErrorType.TIMEOUT),
    ]
    for exc, expected in cases:
        assert classify_exception(exc)[0] is expected, type(exc).__name__


def test_status_code_beats_exception_class() -> None:
    """Observed with LiteLLM 1.105.1: a bad Groq key raises BadRequestError carrying status_code=401."""
    exc = litellm.BadRequestError("invalid api key", "m", "groq")
    exc.status_code = 401
    assert classify_exception(exc)[0] is ErrorType.AUTH
    paid = litellm.APIError(402, "no remaining credits", "openai", "m")
    assert classify_exception(paid)[0] is ErrorType.AUTH
    assert classify_exception(litellm.BadRequestError("bad", "m", "groq"))[0] is ErrorType.BAD_REQUEST


# ---------------------------------------------------------------- redaction


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("mail me at a.b-c@corp.example.org now", "a.b-c@corp.example.org"),
        ("call 415-555-0199 please", "415-555-0199"),
        ("call (415) 555 0199", "555 0199"),
        ("card 5500-0000-0000-0004 ok", "5500-0000-0000-0004"),
        ("ssn 078-05-1120", "078-05-1120"),
        ("groq key gsk_ABCDEFGHIJKLMNOPQRSTUV", "gsk_ABCDEFGHIJKLMNOPQRSTUV"),
        ("hf token hf_abcdefghijklmnopqrstu", "hf_abcdefghijklmnopqrstu"),
        ("Authorization: Bearer abcdef0123456789xyz", "abcdef0123456789xyz"),
        ("server at 10.20.30.40", "10.20.30.40"),
    ],
)
def test_redact_removes_pii(text: str, leak: str) -> None:
    assert leak not in redact(text)


def test_redact_keeps_ordinary_text_and_non_luhn_numbers() -> None:
    s = "Order 1234 shipped on 2026-10-16; classify sentiment: great!"
    assert redact(s) == s


# ---------------------------------------------------------------- spend ledger


def test_ledger_caps_and_concurrent_appends(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps({"project": "other", "usd": 24.5}) + "\n")
    led = SpendLedger(path, "p3-test", project_cap_usd=1.0, global_cap_usd=25.0)
    led.reserve(0.4)
    with pytest.raises(BudgetExceeded):
        led.reserve(0.2)  # 24.5 + 0.4 reserved + 0.2 > 25 global
    led.release(0.4)

    def writer() -> None:
        w = SpendLedger(path, "p3-test", 1.0, 25.0)
        for _ in range(50):
            w.record("groq", "m", 10, 1, 0.001)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = path.read_text().splitlines()
    assert len(lines) == 201 and all(json.loads(x) for x in lines)
    _, p = led.totals()
    assert p == pytest.approx(0.2)
    with pytest.raises(BudgetExceeded):
        led.reserve(0.85)  # project 0.2 + 0.85 > 1.0


# ---------------------------------------------------------------- chaos proxy


def test_chaos_proxy_refuses_to_start_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHAOS_ENABLED", raising=False)
    with pytest.raises(ChaosDisabled):
        create_chaos_app(admin_token="x" * 32)
    monkeypatch.setenv("CHAOS_ENABLED", "1")
    with pytest.raises(ChaosDisabled):
        create_chaos_app(admin_token="")  # admin token is mandatory


async def test_chaos_proxy_injects_and_clears_faults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAOS_ENABLED", "1")
    seen: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    tok = "chaos-token-0123456789abcdef"
    app = create_chaos_app(
        {"ollama": "http://upstream.local"}, admin_token=tok, transport=httpx.MockTransport(upstream)
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://c") as c,
    ):
        r = await c.post("/ollama/api/chat", json={})
        assert r.status_code == 200 and seen == ["http://upstream.local/api/chat"]
        r = await c.put("/admin/faults/ollama", json={"error_rate": 1.0}, headers={"X-Chaos-Token": "wrong"})
        assert r.status_code == 403
        r = await c.put(
            "/admin/faults/ollama",
            json={"error_rate": 1.0, "status_code": 503},
            headers={"X-Chaos-Token": tok},
        )
        assert r.status_code == 200
        r = await c.post("/ollama/api/chat", json={})
        assert r.status_code == 503 and len(seen) == 1
        m = (await c.get("/metrics")).text
        assert 'chaos_injected_errors_total{provider="ollama"} 1.0' in m
        await c.delete("/admin/faults/ollama", headers={"X-Chaos-Token": tok})
        assert (await c.post("/ollama/api/chat", json={})).status_code == 200
        # only fixed upstreams: unknown provider names are not forwarded anywhere
        assert (await c.post("/evil.example.com/x", json={})).status_code == 404
