"""Run scripts/smoke.py for real against an in-process gateway (fake LLM mode, fakeredis) over HTTP.

This keeps the smoke suite itself honest: every check must PASS against a correct gateway, and a gateway
with a broken control must make the matching check FAIL.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import fakeredis
import pytest
import uvicorn

from gateway import migrate
from gateway.app import create_app
from gateway.config import Settings
from gateway.fake_llm import FakeLLMCaller

from .conftest import ADMIN, make_config

ROOT = Path(__file__).resolve().parents[1]


def _load_smoke() -> Any:
    spec = importlib.util.spec_from_file_location("smoke", ROOT / "scripts" / "smoke.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["smoke"] = mod  # dataclasses resolve string annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve(app: Any) -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        th.join(timeout=10)


def _app(tmp_path: Path, mutate: str | None = None) -> Any:
    redis = fakeredis.FakeAsyncRedis()
    asyncio.run(migrate.apply(redis))
    settings = Settings(redis_url="redis://unused", admin_token=ADMIN, ledger_path=str(tmp_path / "l"))
    app = create_app(
        cfg=make_config(),
        settings=settings,
        redis=redis,
        caller=FakeLLMCaller(latency_ms=1),
        start_background=False,
    )
    if mutate == "no_security_headers":
        app.user_middleware.clear()
        app.middleware_stack = None
    return app


@pytest.fixture
def smoke_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for k in ("SMOKE_API_KEY", "SMOKE_TENANT", "SMOKE_ID_TOKEN", "SMOKE_RL_API_KEY", "SMOKE_RL_TENANT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("SMOKE_ADMIN_TOKEN", ADMIN)
    return monkeypatch


def test_smoke_suite_passes_end_to_end_against_a_real_http_server(
    smoke_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    smoke = _load_smoke()
    out = tmp_path / "smoke.json"
    for base in _serve(_app(tmp_path)):
        code = smoke.run(
            ["--base-url", base, "--allow-disruptive", "--require-functional", "--json-out", str(out)]
        )
    summary = json.loads(out.read_text("utf-8"))
    by = {c["name"]: c["status"] for c in summary["checks"]}
    assert code == 0, summary
    assert summary["failed"] == 0 and summary["duration_s"] < 60
    for name in (
        "liveness",
        "readiness",
        "security_headers",
        "no_stack_traces",
        "auth_rejected",
        "auth_accepted",
        "input_validation",
        "oversize_rejected",
        "missing_metadata_rejected",
        "chat_completion",
        "idempotent_replay",
        "metrics_breaker_gauges",
        "rate_limit",
        "kill_switch",
    ):
        assert by[name] == "PASS", (name, summary)
    assert by["metrics_not_public"] == "SKIP"  # dev serves /metrics on the API port by design


def test_smoke_suite_fails_when_a_control_is_removed(smoke_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Teeth: the same gateway without the security-header middleware must fail the smoke run."""
    smoke = _load_smoke()
    out = tmp_path / "smoke.json"
    for base in _serve(_app(tmp_path, mutate="no_security_headers")):
        code = smoke.run(["--base-url", base, "--json-out", str(out)])
    by = {c["name"]: c["status"] for c in json.loads(out.read_text("utf-8"))["checks"]}
    assert code == 1
    assert by["security_headers"] == "FAIL"


def test_require_functional_turns_missing_credentials_into_failure(
    smoke_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    smoke_env.delenv("SMOKE_ADMIN_TOKEN")
    smoke = _load_smoke()
    out = tmp_path / "smoke.json"
    for base in _serve(_app(tmp_path)):
        code = smoke.run(["--base-url", base, "--require-functional", "--json-out", str(out)])
    by = {c["name"]: c["status"] for c in json.loads(out.read_text("utf-8"))["checks"]}
    assert code == 1 and by["chat_completion"] == "FAIL"
    assert by["kill_switch"] == "SKIP"  # admin-only, not a functional check
